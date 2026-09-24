"""PTYs for ``screen``: one pseudo-terminal per (board, console), bridged to the console broker.

Why
---
david attaches to a board console with ``screen <path>`` from any terminal (VS
Code's, say) while the GUI shows the same console at the same time. So each
console gets a PTY in the process that holds the board (the daemon), with a
stable path:

    /tmp/harness-manager-$USER/<board-slug>/<console>  ->  /dev/pts/N

The PTY is one more subscriber of the broker's single board connection, so the
GUI, the TCP export and the PTY all see the same bytes, and keystrokes typed in
``screen`` go through the broker's write path (paced for the DUT UARTs).

Behaviour
---------
- **On demand, one per (board, console).** ``open`` is idempotent; the PTY
  closes when the board closes (``ConsoleBroker.close_all``) or on ``close``.
- **The daemon keeps a slave fd open**, so the master never sees EIO while no
  client is attached. The slave is raw by default (no echo, no CR/LF
  translation), so ``cat <path>`` shows the bytes as the board sent them.
- **One terminal per PTY.** A PTY is one terminal line: two programs reading it
  at once split its bytes between them (a tty property). ``screen`` opens it
  exclusively (TIOCEXCL), which keeps a second opener out. The GUI and the TCP
  export are separate broker subscribers and always see everything.
- **Re-attach.** ``screen`` leaves TIOCEXCL set and its own line modes behind
  when it exits (seen with screen 4.06.02, because the daemon still holds the
  slave). When the attached-client count drops to 0 and the client left
  something behind (``needs_reset``), the daemon clears TIOCEXCL and puts the
  line back to raw at its preset speed, so the next ``screen <path>`` opens it.
- **What a new client sees: the recent output, replayed once it is ready.**
  GNU screen (4.06.02, measured) applies its line modes about 200 ms after it
  opens the PTY, with TCSAFLUSH: anything queued on the line before that is
  thrown away, so output printed before ``screen`` attached (the boot banner, a
  prompt) never showed. So while no client is attached (counted by inotify),
  board output is NOT written to the line; the PTY keeps the last
  ``replay_bytes`` of it. When a client attaches, the PTY waits until the
  client has configured the line (its modes change, plus ``settle_grace_s``)
  or ``settle_s`` has passed (a client that configures nothing, such as
  ``cat``), writes that recent output, and then goes live. When the last client
  leaves, whatever it left unread is discarded from the line (the replay covers
  it next time). Without inotify (no reliable count) the PTY is always live.
- **A reset never discards queued output.** The modes are applied with
  TCSANOW (``RESET_WHEN``), never TCSAFLUSH (the ``tty.setraw`` default). With
  TCSAFLUSH, a program that opened the PTY and left without reading (a probe,
  ``stty -F``, a screen quit at once) made the next client start blank: the
  "no banner" flake of 2026-09-24 (tests/unit/test_l2_pty.py has it on purpose).
- **No reader.** Board output keeps flowing into the PTY. When nobody reads it
  and the line's buffer stays full for ``stall_s``, the stale output is flushed
  (``flushed`` counts it), so a new client sees recent output, not a backlog.
- **Speed.** The watcher polls ``tcgetattr(master)`` (on Linux the master sees
  the slave's speed) every ``poll_s``. A change the client made (``screen
  <path> 57600``) goes to ``on_speed(pty, baud)``; the broker forwards a
  standard speed to ``set_baud`` for a serial console and ignores it (debug log)
  for an Ethernet one, whose rate the design or the harness sets. Linux has no
  B76800, so ``screen`` cannot carry the DUT UART rate: the GUI is the baud
  control, and the PTY carries bytes whatever speed is set on it.
- **Attached clients.** On Linux, inotify on ``/dev/pts/N`` counts opens minus
  closes, so ``clients`` is the number of open file descriptions other
  processes hold, updated the moment a client comes or goes. A ``/proc``
  scan cannot do it: GNU screen is setgid on RHEL, and its server's
  ``/proc/<pid>/fd`` is not readable by its own user (checked with screen
  4.06.02). Without inotify, a best-effort scan of ``/proc/<pid>/fd`` for the
  user's own processes runs every ``scan_s`` instead (it misses a setgid
  screen), and ``clients`` is ``None`` where there is no ``/proc`` or a scan
  takes longer than ``scan_budget_s``. Each change publishes
  ``console.pty {name, path, device, clients}``.
- **Windows** has no PTYs: ``UnavailableError`` whose hint is the TCP export.

Security: the directory is created 0700 and must be a real directory owned by
this user (a directory another user planted in /tmp is refused, never used).
``HARNESS_MANAGER_PTY_DIR`` moves it (tests; a system without a shared /tmp).
Each link has a hidden ``.<console>.owner`` beside it (``<pid> <device>``); a
link whose process is gone (a killed daemon) is swept when a PTY is next
opened, because its ``/dev/pts/N`` may since belong to another terminal.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import select
import shlex
import stat
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness_manager.core.errors import HarnessError, RefusedError, UnavailableError

log = logging.getLogger(__name__)

__all__ = ["ConsolePty", "PtyManager", "board_slug", "runtime_dir", "screen_command",
           "standard_speeds", "supported"]

PTY_DIR_ENV = "HARNESS_MANAGER_PTY_DIR"
#: The capability label on a refused PTY (an error label, not a negotiated capability).
PTY_CAPABILITY = "console_pty"
WINDOWS_HINT = ("use the TCP export instead: `harness-manager console TARGET NAME --export 0`, "
                "then a raw-TCP terminal (PuTTY 'Raw') on 127.0.0.1 and the port it prints")
_READ_SLICE_S = 0.2
_SETTLE_SLICE_S = 0.01                   # how often a settling PTY looks at the line modes
_NOTICE_EVERY_S = 2.0
#: Output modes of a PTY: IDLE (no client: keep, do not write), SETTLING (a client is
#: setting the line up: keep), LIVE (write as it comes).
IDLE, SETTLING, LIVE = "idle", "settling", "live"
#: How the line modes are (re)applied: TCSANOW keeps the input queue, the board output
#: waiting for the next client. NEVER TCSAFLUSH (``tty.setraw``'s default): it discards it.
try:
    import termios as _termios

    RESET_WHEN: int = _termios.TCSANOW
except ImportError:                     # Windows: no PTYs at all
    RESET_WHEN = 0
#: TIOCGEXCL (Linux >= 3.8, asm-generic/ioctls.h: _IOR('T', 0x40, int)); Python's termios lacks it.
_TIOCGEXCL = 0x80045440 if sys.platform.startswith("linux") else None


# --- platform ----------------------------------------------------------------------------


def supported() -> bool:
    """A PTY needs POSIX ``openpty`` and ``termios``."""
    if os.name != "posix" or not hasattr(os, "openpty"):
        return False
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


def unavailable() -> UnavailableError:
    err = UnavailableError(PTY_CAPABILITY, "a PTY needs a POSIX system (Linux or macOS); "
                                           "Windows has none")
    err.hint = WINDOWS_HINT
    return err


@functools.lru_cache(maxsize=1)
def standard_speeds() -> dict[int, int]:
    """termios speed constant -> baud, for every standard ``B<n>`` this system defines."""
    import termios

    out: dict[int, int] = {}
    for name in dir(termios):
        m = re.fullmatch(r"B(\d+)", name)
        if m and int(m.group(1)) > 0:
            out[getattr(termios, name)] = int(m.group(1))
    return out


def baud_constant(baud: int) -> int | None:
    """The termios constant for ``baud``, or None (76800 has none on Linux)."""
    for const, rate in standard_speeds().items():
        if rate == baud:
            return const
    return None


def is_standard(baud: int | None) -> bool:
    return bool(baud) and baud_constant(int(baud)) is not None      # type: ignore[arg-type]


# --- paths -------------------------------------------------------------------------------


def _user() -> str:
    for var in ("USER", "LOGNAME", "USERNAME"):
        if os.environ.get(var):
            return re.sub(r"[^A-Za-z0-9._-]", "_", os.environ[var])
    return str(os.getuid()) if hasattr(os, "getuid") else "user"


def runtime_dir() -> Path:
    """``/tmp/harness-manager-$USER`` (docs/API.md), or ``$HARNESS_MANAGER_PTY_DIR``."""
    override = os.environ.get(PTY_DIR_ENV, "").strip()
    return Path(override) if override else Path("/tmp") / f"harness-manager-{_user()}"


def board_slug(board_id: str) -> str:
    """A safe, readable file name for a board id: ``mps3@192.168.10.101:6900`` ->
    ``mps3@192.168.10.101-6900``. Anything but ``A-Za-z0-9 . _ @ -`` becomes ``-``."""
    slug = re.sub(r"[^A-Za-z0-9._@-]", "-", board_id).strip(".-") or "board"
    return slug[:120]


def screen_command(path: str | Path, baud: int | None = None) -> str:
    """``screen <path>``, plus the rate when it is a standard termios speed.

    ``screen`` with no rate sets B9600 on the line (screen 4.06.02, checked on a
    PTY), and for a serial console that speed would be forwarded as a rate
    change. So a serial console at a standard rate carries it; an Ethernet
    console (76800 is not a termios speed) does not.
    """
    cmd = f"screen {shlex.quote(str(path))}"
    return f"{cmd} {baud}" if baud and is_standard(baud) else cmd


def _own_dir(path: Path, *, parents: bool = False) -> None:
    """Create ``path`` 0700, or check an existing one is a real directory of ours."""
    try:
        if parents:
            path.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise RefusedError(f"cannot create {path}: {exc.strerror or exc}",
                           hint=f"set {PTY_DIR_ENV} to a directory you own") from exc
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RefusedError(f"{path} is not a directory (a symlink or a file is there)",
                           hint=f"remove it, or set {PTY_DIR_ENV} to a directory you own")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise RefusedError(f"{path} belongs to uid {st.st_uid}, not to you",
                           hint=f"remove it, or set {PTY_DIR_ENV} to a directory you own")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


def owner_file(link: Path) -> Path:
    """``.<name>.owner`` beside a PTY link: ``"<pid> <device>"`` of the process that holds it."""
    return link.with_name(f".{link.name}.owner")


def _pid_alive(pid: int) -> bool:
    """Alive and not a zombie (``core.session.pid_alive``): a killed daemon whose parent
    has not reaped it yet holds no PTY."""
    from harness_manager.core.session import pid_alive

    return pid_alive(pid)


def sweep_stale(root: Path) -> list[Path]:
    """Remove the PTY links a dead process left behind (a daemon that was killed).

    A left-over link points at a ``/dev/pts/N`` the system may give to another
    terminal of this user, so ``screen <path>`` would attach to the wrong thing.
    Returns the links removed. Links of live processes are never touched.
    """
    removed: list[Path] = []
    try:
        owners = list(root.glob("*/.*.owner"))
    except OSError:
        return removed
    for record in owners:
        try:
            pid_s, device = record.read_text().split()
            pid = int(pid_s)
        except (OSError, ValueError):
            continue
        if pid == os.getpid() or _pid_alive(pid):
            continue
        link = record.with_name(record.name[1:-len(".owner")])
        try:
            if os.readlink(link) == device:
                os.unlink(link)
                removed.append(link)
        except OSError:
            pass
        record.unlink(missing_ok=True)
        try:
            os.rmdir(record.parent)
        except OSError:
            pass
    return removed


# --- attached clients --------------------------------------------------------------------


def scan_clients(devices: set[str], *, exclude_pid: int, budget_s: float,
                 proc: str = "/proc") -> dict[str, int] | None:
    """How many of this user's processes (other than ``exclude_pid``) hold each device.

    ``None`` when there is no ``/proc`` or the scan runs over ``budget_s``.
    """
    if not devices or not os.path.isdir(proc):
        return None if not os.path.isdir(proc) else {}
    uid = os.getuid()
    counts = dict.fromkeys(devices, 0)
    started = time.monotonic()
    try:
        entries = list(os.scandir(proc))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == exclude_pid:
            continue
        try:
            if entry.stat(follow_symlinks=False).st_uid != uid:
                continue
            seen: set[str] = set()
            with os.scandir(os.path.join(entry.path, "fd")) as fds:
                for fd in fds:
                    try:
                        target = os.readlink(fd.path)
                    except OSError:
                        continue
                    if target in counts and target not in seen:
                        seen.add(target)
                        counts[target] += 1
        except OSError:
            continue                        # the process ended, or its fds are not ours to read
        if time.monotonic() - started > budget_s:
            return None
    return counts


class Inotify:
    """Linux inotify through libc: open/close events on the PTY devices.

    One open file description = one IN_OPEN, and its last close = one IN_CLOSE_*,
    whoever the opener is (a setgid ``screen`` included); a refused open (EBUSY)
    makes no event. So opens minus closes since the watch was added is how many
    file descriptions other processes hold.
    """

    IN_CLOSE_WRITE = 0x08
    IN_CLOSE_NOWRITE = 0x10
    IN_OPEN = 0x20
    IN_Q_OVERFLOW = 0x4000
    IN_IGNORED = 0x8000
    _EVENT = struct.Struct("iIII")

    def __init__(self, libc: Any, fd: int) -> None:
        self._libc = libc
        self.fd = fd

    @classmethod
    def create(cls) -> Inotify | None:
        if not sys.platform.startswith("linux"):
            return None
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        except (OSError, AttributeError):
            return None
        return cls(libc, fd) if fd >= 0 else None

    def add(self, path: str) -> int:
        """A watch on ``path``; -1 when it cannot be added."""
        mask = self.IN_OPEN | self.IN_CLOSE_WRITE | self.IN_CLOSE_NOWRITE
        return int(self._libc.inotify_add_watch(self.fd, os.fsencode(path), mask))

    def remove(self, wd: int) -> None:
        if wd >= 0 and self.fd >= 0:
            self._libc.inotify_rm_watch(self.fd, wd)

    def read(self) -> tuple[dict[int, int], bool]:
        """({wd: opens - closes} since the last read, whether the queue overflowed)."""
        deltas: dict[int, int] = {}
        overflow = False
        while True:
            try:
                buf = os.read(self.fd, 64 * 1024)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if not buf:
                break
            off = 0
            while off + self._EVENT.size <= len(buf):
                wd, mask, _cookie, length = self._EVENT.unpack_from(buf, off)
                off += self._EVENT.size + length
                if mask & self.IN_Q_OVERFLOW:
                    overflow = True
                if mask & self.IN_OPEN:
                    deltas[wd] = deltas.get(wd, 0) + 1
                if mask & (self.IN_CLOSE_WRITE | self.IN_CLOSE_NOWRITE):
                    deltas[wd] = deltas.get(wd, 0) - 1
        return deltas, overflow

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


class ClientCounter:
    """ONE inotify instance per process, and the thread that reads it.

    inotify instances are a per-user resource (``max_user_instances``, 128 by
    default, shared with editors and file watchers), so every PTY of every
    broker in this process shares one. ``watch(device, on_delta)`` calls
    ``on_delta(opens - closes)`` on this thread as events arrive, or
    ``on_delta(None)`` when the queue overflowed and counts are unknown.
    """

    def __init__(self, ino: Inotify) -> None:
        self._ino = ino
        self._lock = threading.Lock()
        self._cbs: dict[int, Callable[[int | None], None]] = {}
        self._orphans: dict[int, int] = {}      # deltas read before their watch was registered
        self._thread = threading.Thread(target=self._run, daemon=True, name="console-pty-inotify")
        self._thread.start()

    def watch(self, device: str, on_delta: Callable[[int | None], None]) -> int:
        """A watch on ``device``; -1 when the kernel refuses one (the caller falls back)."""
        with self._lock:
            wd = self._ino.add(device)
            if wd < 0:
                return -1
            self._cbs[wd] = on_delta
            pending = self._orphans.pop(wd, 0)
        if pending:
            on_delta(pending)
        return wd

    def unwatch(self, wd: int) -> None:
        if wd < 0:
            return
        with self._lock:
            self._cbs.pop(wd, None)
            self._orphans.pop(wd, None)
            self._ino.remove(wd)

    def _run(self) -> None:
        while self._ino.fd >= 0:
            try:
                ready, _, _ = select.select([self._ino.fd], [], [], 1.0)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            deltas, overflow = self._ino.read()
            calls: list[tuple[Callable[[int | None], None], int | None]] = []
            with self._lock:
                for wd, delta in deltas.items():
                    cb = self._cbs.get(wd)
                    if cb is None:
                        self._orphans[wd] = self._orphans.get(wd, 0) + delta
                    else:
                        calls.append((cb, delta))
                if overflow:
                    log.warning("inotify queue overflow: PTY client counts are unknown for now")
                    calls = [(cb, None) for cb in self._cbs.values()]
            for cb, delta in calls:
                try:
                    cb(delta)
                except Exception:  # noqa: BLE001 - one PTY must not stop the others' counts
                    log.exception("counting PTY clients failed")


_COUNTER: ClientCounter | None = None
_COUNTER_LOCK = threading.Lock()
_COUNTER_TRIED = False


def client_counter() -> ClientCounter | None:
    """The process's ``ClientCounter`` (created on first use), or None without inotify."""
    global _COUNTER, _COUNTER_TRIED
    with _COUNTER_LOCK:
        if not _COUNTER_TRIED:
            _COUNTER_TRIED = True
            ino = Inotify.create()
            if ino is None:
                log.info("no inotify here: PTY clients are counted from /proc (best effort)")
            else:
                _COUNTER = ClientCounter(ino)
        return _COUNTER


# --- one PTY -----------------------------------------------------------------------------


class ConsolePty:
    """One console's PTY: the master/slave pair, the symlink, and the two pump threads."""

    def __init__(self, manager: PtyManager, session: Any, name: str, sub: Any, link: Path,
                 *, kind: str, baud: int | None) -> None:
        import termios
        import tty

        self.manager = manager
        self.session = session
        self.board_id = session.candidate.board_id
        self.name = name
        self.kind = kind                   # "serial" | "ethernet": whose rate a speed change is
        self.sub = sub
        self.link = link
        self.clients: int | None = 0
        self.flushed = 0                   # times stale output was flushed (nobody reading)
        self.replays = 0                   # times the recent output was written for a new client
        self.created_at = time.time()
        self._stop = threading.Event()
        self._last_notice = 0.0
        self._ring = bytearray()           # the last manager.replay_bytes of board output
        self._wlock = threading.Lock()     # output mode changes vs writes to the line
        self.mode = LIVE
        self._settle_until = 0.0
        self._attach_attrs: list | None = None
        self._configured_at: float | None = None
        self.master, self.slave = os.openpty()
        try:
            self.device = os.ttyname(self.slave)
            tty.setraw(self.slave, RESET_WHEN)
            self.preset = baud_constant(baud) if baud else None
            if self.preset is not None:
                self._set_speed(self.preset)
            self.seen_speed = termios.tcgetattr(self.master)[5]
            self._raw = termios.tcgetattr(self.slave)[:4]
            os.set_blocking(self.master, False)
            self.opens = 0                           # other openers' file descriptions (inotify)
            self.count_lock = threading.Lock()
            self.wd = manager._track(self)           # before the path exists: no open is missed
            if self.wd >= 0 and manager.replay_on_attach:
                self.mode = IDLE                     # a reliable count: hold output for a client
            self._link()
        except BaseException:
            manager._untrack(getattr(self, "wd", -1))
            self._close_fds()
            raise
        self._threads = [
            threading.Thread(target=self._to_pty, daemon=True,
                             name=f"console-pty-{self.board_id}-{name}-out"),
            threading.Thread(target=self._from_pty, daemon=True,
                             name=f"console-pty-{self.board_id}-{name}-in"),
        ]

    # -- the symlink --------------------------------------------------------------------

    @property
    def owner_file(self) -> Path:
        return owner_file(self.link)

    def _link(self) -> None:
        tmp = self.link.with_name(f".{self.link.name}.{os.getpid()}.{threading.get_ident()}")
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        # Who holds this link: a later process sweeps it if this one dies (see sweep_stale).
        self.owner_file.write_text(f"{os.getpid()} {self.device}\n")
        os.symlink(self.device, tmp)
        os.replace(tmp, self.link)          # atomic: a stale link from a dead daemon is replaced

    def _unlink(self) -> None:
        try:
            if os.readlink(self.link) == self.device:
                os.unlink(self.link)
                self.owner_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            os.rmdir(self.link.parent)      # only when empty
        except OSError:
            pass

    # -- line state -------------------------------------------------------------------

    def _set_speed(self, const: int) -> None:
        import termios

        attrs = termios.tcgetattr(self.slave)
        attrs[4] = attrs[5] = const
        termios.tcsetattr(self.slave, termios.TCSANOW, attrs)

    def set_preset(self, baud: int | None) -> None:
        """The speed the line is reset to (the console's rate, when it is a standard speed)."""
        import termios

        const = baud_constant(baud) if baud else None
        self.preset = const
        if const is None:
            return
        try:
            self._set_speed(const)
            self.seen_speed = termios.tcgetattr(self.master)[5]
        except OSError:
            pass

    def exclusive(self) -> bool:
        """TIOCEXCL is set on the line (``screen`` sets it and leaves it behind)."""
        import fcntl

        if _TIOCGEXCL is None or self.slave < 0:
            return False
        try:
            raw = fcntl.ioctl(self.slave, _TIOCGEXCL, struct.pack("i", 0))
        except OSError:
            return False
        return bool(struct.unpack("i", raw)[0])

    def needs_reset(self) -> bool:
        """A client left something behind: TIOCEXCL, its line modes, or its speed."""
        import termios

        if self.slave < 0:
            return False
        try:
            attrs = termios.tcgetattr(self.slave)
        except OSError:
            return False
        if attrs[:4] != self._raw:
            return True
        if self.preset is not None and attrs[5] != self.preset:
            return True
        return self.exclusive()

    def reset_line(self) -> None:
        """No client any more: clear TIOCEXCL, raw mode, the preset speed."""
        import fcntl
        import termios
        import tty

        try:
            if hasattr(termios, "TIOCNXCL"):
                fcntl.ioctl(self.slave, termios.TIOCNXCL)
            tty.setraw(self.slave, RESET_WHEN)       # keeps the queued board output
            if self.preset is not None:
                self._set_speed(self.preset)
            self._raw = termios.tcgetattr(self.slave)[:4]
            self.seen_speed = termios.tcgetattr(self.master)[5]
        except OSError as exc:
            log.debug("resetting the PTY of %s/%s failed: %s", self.board_id, self.name, exc)

    def speed(self) -> int | None:
        import termios

        try:
            return termios.tcgetattr(self.master)[5]
        except OSError:
            return None

    # -- pumps --------------------------------------------------------------------------

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def _to_pty(self) -> None:
        """Board bytes (the broker subscription) into the PTY, or into the replay ring."""
        while not self._stop.is_set():
            slice_s = _SETTLE_SLICE_S if self.mode == SETTLING else _READ_SLICE_S
            try:
                data = self.sub.read(slice_s)
            except HarnessError:
                break                                   # a stream that ends by raising
            if data:
                self._remember(data)
            with self._wlock:
                if self.mode == LIVE:
                    if data:
                        self._write_master(data)
                elif self.mode == SETTLING and self._settled():
                    self.mode = LIVE
                    replay = bytes(self._ring)
                    if replay:
                        self.replays += 1
                        self._write_master(replay)
            if not data and getattr(self.sub, "closed", False):
                break

    def _remember(self, data: bytes) -> None:
        self._ring += data
        excess = len(self._ring) - self.manager.replay_bytes
        if excess > 0:
            del self._ring[:excess]

    def _settled(self) -> bool:
        """The client has set the line up (its modes changed, plus a grace), or time is up."""
        import termios

        now = time.monotonic()
        if now >= self._settle_until:
            return True
        if self._configured_at is None:
            try:
                attrs = termios.tcgetattr(self.slave)[:6]
            except OSError:
                return True
            if attrs != self._attach_attrs:
                self._configured_at = now
        return (self._configured_at is not None
                and now - self._configured_at >= self.manager.settle_grace_s)

    # -- clients come and go (the manager's counter calls these) -------------------------

    def attached(self) -> None:
        """The first client opened the line: replay the recent output once it is set up."""
        import termios

        with self._wlock:
            if self.mode != IDLE:
                return
            try:
                self._attach_attrs = termios.tcgetattr(self.slave)[:6]
            except OSError:
                self._attach_attrs = None
            self._configured_at = None
            self._settle_until = time.monotonic() + self.manager.settle_s
            self.mode = SETTLING

    def detached(self) -> None:
        """The last client left: stop writing, and drop what it left unread (the ring has it)."""
        import termios

        with self._wlock:
            if self.mode == IDLE or self.wd < 0 or not self.manager.replay_on_attach:
                return
            self.mode = IDLE
            try:
                termios.tcflush(self.slave, termios.TCIFLUSH)
            except OSError:
                pass

    def go_live(self) -> None:
        """Counts are unknown (inotify overflow): write everything, as without a count."""
        with self._wlock:
            self.mode = LIVE

    def _write_master(self, data: bytes) -> None:
        import termios

        view = memoryview(data)
        stalled_since: float | None = None
        while view and not self._stop.is_set():
            try:
                n = os.write(self.master, view)
                view = view[n:]
                stalled_since = None
                continue
            except BlockingIOError:
                pass
            except OSError:
                return                                  # closed under us
            try:
                _, writable, _ = select.select([], [self.master], [], 0.1)
            except (OSError, ValueError):
                return
            if writable:
                continue
            now = time.monotonic()
            stalled_since = stalled_since or now
            if now - stalled_since >= self.manager.stall_s:
                # Nobody has read the line for stall_s: drop the stale output, keep the new.
                try:
                    termios.tcflush(self.slave, termios.TCIFLUSH)
                except OSError:
                    return
                self.flushed += 1
                stalled_since = None

    def _from_pty(self) -> None:
        """Keystrokes from the client into the broker (paced there for the DUT UARTs)."""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], _READ_SLICE_S)
                if not ready:
                    continue
                data = os.read(self.master, 4096)
            except BlockingIOError:
                continue
            except (OSError, ValueError):
                break
            if not data:
                continue
            try:
                self.sub.write(data)
            except HarnessError as exc:
                self._notice(exc.message)

    def _notice(self, text: str) -> None:
        """Tell the terminal why its keystrokes went nowhere (at most every 2 s)."""
        now = time.monotonic()
        if now - self._last_notice < _NOTICE_EVERY_S:
            return
        self._last_notice = now
        try:
            os.write(self.master, f"\r\n[harness-manager: {text}]\r\n".encode())
        except OSError:
            pass

    # -- lifecycle ------------------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "path": str(self.link), "device": self.device,
                "clients": self.clients}

    def close(self) -> None:
        self._stop.set()
        try:
            self.sub.close()
        except Exception:  # noqa: BLE001 - closing must finish
            log.exception("closing the PTY subscription of %s/%s failed", self.board_id, self.name)
        for t in self._threads:
            if t.is_alive() and t is not threading.current_thread():
                t.join(timeout=2.0)
        self.manager._untrack(self.wd)
        self.wd = -1
        self._unlink()
        self._close_fds()

    def _close_fds(self) -> None:
        for fd in (getattr(self, "master", -1), getattr(self, "slave", -1)):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master = self.slave = -1


# --- the manager ---------------------------------------------------------------------------


class PtyManager:
    """Every PTY of one broker, and the one watcher thread that polls them.

    ``publish(topic, board_id, data)`` sends events; ``on_speed(pty, baud)`` is
    called (never under this manager's lock) when a client sets a standard speed.
    """

    def __init__(self, publish: Callable[[str, str, dict[str, Any]], None],
                 on_speed: Callable[[ConsolePty, int], None], *,
                 root: Path | None = None, poll_s: float = 0.5, scan_s: float = 2.0,
                 scan_budget_s: float = 0.25, stall_s: float = 1.0,
                 replay_on_attach: bool = True, replay_bytes: int = 4096,
                 settle_s: float = 1.0, settle_grace_s: float = 0.05) -> None:
        self._publish = publish
        self._on_speed = on_speed
        self._root = root
        self.replay_on_attach = replay_on_attach   # False: always live (the pre-2026-09-24 way)
        self.replay_bytes = replay_bytes           # recent output a new client is shown
        self.settle_s = settle_s                   # the longest a new client waits for it
        self.settle_grace_s = settle_grace_s       # after the client set the line up
        self.poll_s = poll_s
        self.scan_s = scan_s
        self.scan_budget_s = scan_budget_s
        self.stall_s = stall_s
        self.scan_ok = True                 # False once a scan was too costly or had no /proc
        self._counter = client_counter()    # None: count clients with the /proc scan
        self._lock = threading.RLock()
        self._ptys: dict[tuple[str, str], ConsolePty] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._watcher: threading.Thread | None = None
        self._last_scan = 0.0

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else runtime_dir()

    @property
    def counting(self) -> str:
        """How attached clients are counted: ``inotify``, ``proc`` or ``none``."""
        return "inotify" if self._counter is not None else ("proc" if self.scan_ok else "none")

    def _track(self, pty: ConsolePty) -> int:
        counter = self._counter
        if counter is None:
            return -1
        wd = counter.watch(pty.device, lambda delta: self._on_opens(pty, delta))
        if wd < 0:
            log.info("no inotify watch on %s: counting PTY clients with /proc instead",
                     pty.device)
        return wd

    def _untrack(self, wd: int) -> None:
        counter = self._counter
        if counter is not None and wd >= 0:
            counter.unwatch(wd)

    def _on_opens(self, pty: ConsolePty, delta: int | None) -> None:
        """inotify: ``delta`` more (or fewer) open file descriptions; None: unknown."""
        with pty.count_lock:
            if delta is None:
                pty.opens = 0
                now: int | None = None
            else:
                pty.opens = max(0, pty.opens + delta)
                now = pty.opens
            if pty.master < 0:
                return                                   # closed meanwhile
            self._set_clients(pty, now)

    # -- open / close ---------------------------------------------------------------------

    def get(self, board_id: str, name: str) -> ConsolePty | None:
        with self._lock:
            return self._ptys.get((board_id, name))

    def open(self, session: Any, name: str, subscribe: Callable[[], Any], *,
             kind: str, baud: int | None) -> ConsolePty:
        """The PTY of console ``name`` (created, with a new subscription, if needed)."""
        if not supported():
            raise unavailable()
        board_id = session.candidate.board_id
        with self._lock:
            existing = self._ptys.get((board_id, name))
            if existing is not None:
                return existing
            root = self.root
            _own_dir(root, parents=True)
            for stale in sweep_stale(root):
                log.info("removed the stale PTY link %s (its process is gone)", stale)
            board_dir = root / board_slug(board_id)
            _own_dir(board_dir)
            sub = subscribe()
            try:
                pty = ConsolePty(self, session, name, sub,
                                 board_dir / re.sub(r"[^A-Za-z0-9._-]", "-", name),
                                 kind=kind, baud=baud)
            except BaseException:
                sub.close()
                raise
            self._ptys[(board_id, name)] = pty
            if pty.wd < 0 and not self.scan_ok:
                pty.clients = None                   # no inotify, and /proc gave up
            pty.start()
            self._ensure_watcher()
        log.info("console %s of %s: PTY %s -> %s", name, board_id, pty.link, pty.device)
        self._event(pty, opened=True)
        return pty

    def close(self, board_id: str, name: str) -> bool:
        with self._lock:
            pty = self._ptys.pop((board_id, name), None)
        if pty is None:
            return False
        pty.close()
        self._event(pty, opened=False)
        return True

    def close_board(self, board_id: str) -> None:
        with self._lock:
            names = [n for (b, n) in self._ptys if b == board_id]
        for name in names:
            self.close(board_id, name)

    def shutdown(self) -> None:
        with self._lock:
            keys = list(self._ptys)
        for board_id, name in keys:
            self.close(board_id, name)
        self._stop.set()
        self._wake.set()
        watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=2.0)

    def ptys(self, board_id: str | None = None) -> list[ConsolePty]:
        with self._lock:
            return [p for (b, _), p in self._ptys.items() if board_id is None or b == board_id]

    # -- the watcher --------------------------------------------------------------------------

    def _ensure_watcher(self) -> None:
        if self._watcher is None or not self._watcher.is_alive():
            self._stop.clear()
            self._watcher = threading.Thread(target=self._watch, daemon=True,
                                             name="console-pty-watch")
            self._watcher.start()

    def rescan(self) -> None:
        """Scan for attached clients now (tests; after a change a caller knows about)."""
        self._last_scan = 0.0
        self._wake.set()

    def _watch(self) -> None:
        while not self._stop.is_set():
            ptys = self.ptys()
            if not ptys:
                with self._lock:
                    if not self._ptys:
                        self._watcher = None
                        return
            for pty in ptys:
                self._check_speed(pty)
            untracked = [p for p in ptys if p.wd < 0]
            if untracked and self.scan_ok and time.monotonic() - self._last_scan >= self.scan_s:
                self._scan(untracked)
            self._wake.wait(self.poll_s)
            self._wake.clear()

    def _set_clients(self, pty: ConsolePty, now: int | None) -> None:
        if now is None:
            pty.go_live()
        elif now > 0 and not pty.clients:
            pty.attached()
        elif now == 0 and pty.clients != 0:
            pty.detached()
            if pty.needs_reset():
                pty.reset_line()               # the last client left: ready for the next one
        if now == pty.clients:
            return
        pty.clients = now
        self._event(pty, opened=True)

    def _check_speed(self, pty: ConsolePty) -> None:
        speed = pty.speed()
        if speed is None or speed == pty.seen_speed:
            return
        pty.seen_speed = speed
        baud = standard_speeds().get(speed)
        if not baud:
            return
        try:
            self._on_speed(pty, baud)
        except Exception:  # noqa: BLE001 - a failed forward must not stop the watcher
            log.exception("forwarding %d baud from the PTY of %s/%s failed", baud, pty.board_id,
                          pty.name)
        self._last_scan = 0.0                   # a speed change means a client came: scan now

    def _scan(self, ptys: list[ConsolePty]) -> None:
        self._last_scan = time.monotonic()
        live = [p for p in ptys if p.master >= 0]
        # Whether a client left something behind, read BEFORE the scan: a client that
        # opens (and sets TIOCEXCL) while the scan runs must not be reset under it.
        dirty = {id(p): p.needs_reset() for p in live}
        counts = scan_clients({p.device for p in live}, exclude_pid=os.getpid(),
                              budget_s=self.scan_budget_s)
        if counts is None:
            self.scan_ok = False
            log.info("PTY client counts are unavailable (no /proc, or a scan took over %.2f s)",
                     self.scan_budget_s)
            for pty in live:
                if pty.clients is not None:
                    pty.clients = None
                    self._event(pty, opened=True)
            return
        for pty in live:
            now = counts.get(pty.device, 0)
            before = pty.clients
            if now == 0 and dirty[id(pty)]:
                pty.reset_line()               # the last client left: ready for the next one
            if now == before:
                continue
            pty.clients = now
            self._event(pty, opened=True)

    def _event(self, pty: ConsolePty, *, opened: bool) -> None:
        data = {"name": pty.name, "path": str(pty.link), "device": pty.device,
                "clients": pty.clients if opened else 0, "open": opened}
        try:
            self._publish("console.pty", pty.board_id, data)
        except Exception:  # noqa: BLE001
            log.exception("publishing console.pty failed")
