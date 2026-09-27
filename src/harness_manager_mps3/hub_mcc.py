"""The MCC of a board behind the hub, reached ON the hub through pyverify (lane MCC-FIX).

**The rule** (the Linux lead and the lead, 2026-09-26): Harness Manager never holds an
fpgahub share on ``tty_00``, on any board. The paced REBOOT works only with exactly one reader
on ``tty_00``. A share is a reader (fpgahub's broker holds the tty open), it cannot be stopped
on its own (``share stop`` stops every share on the board, and Harness Manager never runs it),
and while one exists the platform's own tools refuse to REBOOT (pyverify's hub-side writer,
rc 3 or 4). So ``hub.share_links`` makes no MCC link, ``hub.resolve_share`` and
``HubClient.share_start`` refuse ``tty_00``, and in hub mode every MCC operation runs on the
hub through the hub runner (``ssh HUB 'sg fpga -c …'``; the console is ``root:fpga``):

- **REBOOT**: pyverify's ``MccTtyResetter``, i.e. ``pyverify.bootrate.HUB_MCC_REBOOT_PY``, run by
  a Python 3.10+ on the hub (``PY310_PROBE``: ``python3.11`` first; the lab hub's system
  ``python3`` is 3.6 and is never used for pyverify's code: no 3.10+ is a clear refusal). It
  refuses a second reader of the tty (rc 3), refuses
  when a bare CR does not bring back an intact ``Cmd>`` (rc 4), then types ``REBOOT`` at 100 ms
  a character, waits for ``Rebooting`` and captures the boot log to ``FPGA configuration
  complete``. The log goes to a file in the hub's ``/tmp`` for the call; it is read back,
  parsed (``mcc.parse_boot_log``: which ``.bit`` the MCC loaded) and removed.
- **after an SD write through the hub door**: pyverify's ``SdFielder.field(bit,
  already_written=True)``, the library form of ``pyverify sd field BIT --already-written``: the
  lease held by us, the staged bit's sha256, fpgahubd's journal witness (the board's last SD
  event is an ``ok=True`` dispatch of that sha), the one-reader scan, then the same paced
  REBOOT. When this account cannot read the journal, the hub door's own proof of the write
  (the hub's record naming our sha) stands, and the plain paced REBOOT above runs instead.
- **the post-SD-write quirk** (silicon, twice, 2026-09-26): straight after an SD write the MCC
  answers a bare CR with only ``\\r\\n`` for a few seconds, and the writer refuses (rc 4). The
  REBOOT is tried again, ``mcc.POST_WRITE_TRIES`` times ``mcc.POST_WRITE_GAP_S`` apart, only
  while the answer is bare CR/LF. Every other refusal is final. The write is never retried.
- **reads** (``CFG R TEMP/OSC/V/SCC``, ``HELP``): ``HUB_MCC_READ_PY`` below (Harness Manager's
  own, proven under a real Python 3.6: ``tests/unit/test_mcc_fix_hub.py``), shipped the way
  LINUX-CLAIM ships pyverify's modules (``sh -c`` picks the hub's newest ``python3``). It keeps
  pyverify's one-reader rule and ``Cmd>`` check, listens first for ``READ_LISTEN_S`` (3.5 s:
  an idle MCC is silent, a talking one is booting, and its ~3 s auto-boot window is silent
  too, where a key would stop the boot), types each
  read at 100 ms a character, enters DEBUG for ``CFG R`` and always leaves with EXIT. It
  refuses anything but those reads itself: nothing that changes state goes this way.

Every REBOOT first asks SLOT-TIMING's reset guard (``mcc.guard_reset``): never while the
board's card job writes or reads back. The local Debug USB path (``mcc.Mps3Controller``) is
unchanged, apart from the same post-write retry and the same guard.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    HeldError,
    NothingOnTargetError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import Reading
from harness_manager.core.pack import Progress

from .mcc import (
    MCC_QUIET_BEFORE_S,
    OSC_CAVEAT,
    OSC_COUNT,
    POST_WRITE_GAP_S,
    POST_WRITE_TRIES,
    SHARE_PACE_S,
    TEMP_CAVEAT,
    BootRecord,
    RebootWitness,
    bare_crlf,
    classify,
    guard_reset,
    parse_boot_log,
    parse_osc,
    parse_temp,
    reply_error,
    strip_echo,
)

log = logging.getLogger(__name__)

ROUTE_TOOL = "hub-tool"
#: 100 ms a character on the hub: the rate W1 proved (pyverify DEFAULT_MCC_PACE_S).
HUB_PACE_S = SHARE_PACE_S
#: The boot log is captured at most this long (pyverify fielding.DEFAULT_CAPTURE_S).
CAPTURE_MAX_S = 150.0
READ_TIMEOUT_S = 90.0
READ_PROMPT_S = 3.0
READ_REPLY_S = 5.0
#: Silence before the first CR of a read (REVIEW-W5 3; ``mcc.MCC_QUIET_BEFORE_S``): the MCC's
#: auto-boot window is ~3 s of silence, and a CR in it STOPS the FPGA boot. The reader script
#: holds the same floor (``LISTEN_MIN_S``) whatever it is sent.
READ_LISTEN_S = MCC_QUIET_BEFORE_S
PING_INTERVAL_S = 1.0
PING_TIMEOUT_S = 1.0
LOG_DIR = "/tmp"
#: ``$1`` is the log file on the hub: print it, then remove it.
_CAT_RM = 'cat -- "$1"; rm -f -- "$1"'
#: A Python 3.10+ on the hub for pyverify's hub-side code (the Linux lead, C4: the lab hub's
#: ``python3`` is 3.6; ``/usr/bin/python3.11`` and fpgahub's ``/opt/fpgahub/bin/python3.11``
#: exist). Prints the interpreter's path; exit 127 when there is none. Never bare python3.
PY310_CANDIDATES = ("python3.11", "python3.12", "python3.13", "python3.10",
                    "/opt/fpgahub/bin/python3.11")


def py310_probe(candidates: Sequence[str] = PY310_CANDIDATES) -> str:
    """The ``sh`` script that prints the first of ``candidates`` that is Python 3.10+."""
    return ("for p in " + " ".join(candidates) + "; do "
            'command -v "$p" >/dev/null 2>&1 && "$p" -c '
            '"import sys; sys.exit(sys.version_info < (3, 10))" >/dev/null 2>&1 '
            '&& { command -v "$p"; exit 0; }; done; exit 127')


PY310_PROBE = py310_probe()


def is_mcc_tty(tty: str) -> bool:
    """``…/tty_00`` in any spelling, or its by-id alias: the MCC console (FT4232H interface
    00). The one rule: ``transports.tcp_serial.mcc_tty_reason`` (REVIEW-W5 10)."""
    from harness_manager.transports.tcp_serial import is_mcc_tty as _rule

    return _rule(tty)


def mcc_tty_for(cfg: Any) -> str:
    """The MCC console's path ON the hub: a ``shares.mcc`` entry still names it (it is never
    shared), else ``/dev/<target>/tty_00`` (fpgahub's udev naming)."""
    shares = dict(getattr(cfg, "shares", {}) or {})
    named = shares.get("mcc") or next((t for t in shares.values() if is_mcc_tty(t)), "")
    return str(named) if named else f"/dev/{getattr(cfg, 'target', '') or 'mps3_01_pl'}/tty_00"


def runner_for(cfg: Any) -> Any:
    """The hub runner for a ``HubConfig``: SSH (``sg <group>``), or the SSH fallback of a REST
    hub (``host`` beside ``url``). None for a REST-only hub: ``tty_00`` needs a login there."""
    from . import hub as _hub

    rest = getattr(cfg, "rest", None)
    if rest is not None:
        host = str(getattr(rest, "ssh_host", "") or "")
        group: str | None = _hub.DEFAULT_GROUP
    else:
        host = str(getattr(cfg, "host", "") or "")
        group = getattr(cfg, "group", _hub.DEFAULT_GROUP)
    if not host:
        return None
    jump = str(getattr(cfg, "jump", "") or "")
    fac = _hub.DEFAULT_RUNNER_FACTORY
    return fac(host, group, jump=jump) if jump else fac(host, group)   # type: ignore[call-arg]


def runner_from_hub(hub: Any) -> Any:
    """``session.hub``'s own SSH runner (``HubClient._run``), else one built from its config."""
    client = getattr(hub, "client", None)
    run = getattr(client, "_run", None)
    if callable(run) and getattr(client, "transport", "ssh") == "ssh":
        return run
    cfg = getattr(hub, "config", None)
    return runner_for(cfg) if cfg is not None else None


# --- the hub-side reader --------------------------------------------------------------------------

#: Runs ON THE HUB (``sh -c <pick python> HUB_MCC_READ_PY '<json args>'``): stdlib only,
#: Python 3.6+. Prints ONE JSON line; the exit code is the verdict: 0 read, 2 the tty is
#: missing or will not open, 3 another process names or holds the tty (NOTHING sent), 4 a bare
#: CR did not bring back an intact prompt (NOTHING else sent), 5 a read got no prompt back,
#: 6 the MCC was talking before we typed (booting: NOTHING sent), 7 a line that is not a read.
#: The second-reader scan and the raw open are pyverify's (bootrate.HUB_MCC_REBOOT_PY).
HUB_MCC_READ_PY = r'''
import json, os, re, select, sys, time
A = json.loads(sys.argv[1])
TTY = A["tty"]
LISTEN_MIN_S = 3.5
OUT = {"tty": TTY, "others": [], "prompt": "", "menu": "", "replies": [], "heard": ""}
PROMPT = re.compile(br"(Cmd|Debug)>\s*$")
READ = re.compile(r"^(HELP|\?|CFG R (OSC|TEMP|V|SCC) \d{1,2})$")

def done(rc, reason=None):
    OUT["rc"] = rc
    OUT["reason"] = reason
    sys.stdout.write(json.dumps(OUT) + "\n")
    sys.stdout.flush()
    sys.exit(rc)

for line in A["lines"]:
    if not READ.match(line):
        done(7, "not a read: %r" % line)

def ancestors():
    pids, pid = set(), os.getpid()
    for _ in range(64):
        pids.add(pid)
        try:
            with open("/proc/%d/stat" % pid) as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if ppid <= 1:
            break
        pid = ppid
    return pids

def others():
    real = os.path.realpath(TTY)
    names = set([TTY, real])
    mine = ancestors()
    hits = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in mine:
            continue
        pid = int(d)
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                argv = [x.decode("utf-8", "replace") for x in fh.read().split(b"\0") if x]
        except OSError:
            continue
        cmd = " ".join(argv)[:160]
        if any(c in names or c.split(",", 1)[0] in names
               for x in argv[1:] for c in (x, x.split("=", 1)[-1])):
            hits.append([pid, cmd])
            continue
        try:
            fds = os.listdir("/proc/%d/fd" % pid)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath("/proc/%d/fd/%s" % (pid, fd)) == real:
                    hits.append([pid, "(holds it open) " + cmd])
                    break
            except OSError:
                pass
    return hits

def open_raw():
    import termios, tty
    fd = os.open(TTY, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    at = termios.tcgetattr(fd)
    speed = getattr(termios, "B%d" % A["baud"])
    at[2] |= termios.CLOCAL | termios.CREAD
    at[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG)
    at[4] = at[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, at)
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd

def read_for(fd, secs, prompt):
    buf = b""
    end = time.monotonic() + secs
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return buf, False
        try:
            r, _, _ = select.select([fd], [], [], min(0.05, left))
            if r:
                chunk = os.read(fd, 4096)
                if not chunk:
                    return buf, False
                buf += chunk
                if prompt and PROMPT.search(buf):
                    return buf, True
        except OSError:
            return buf, False

def menu_of(buf):
    return "debug" if buf.rstrip().endswith(b"Debug>") else "main"

def send(fd, text):
    # The MCC drops a character that comes < 50 ms after the previous one, and that includes
    # the CR it just answered: pace BEFORE every character.
    for ch in (text + "\r").encode("ascii"):
        time.sleep(A["pace"])
        os.write(fd, bytes([ch]))
    return read_for(fd, A["reply_s"], True)

if not os.path.exists(TTY):
    done(2, "no such tty %s on this host" % TTY)
OUT["others"] = others()
if OUT["others"]:
    done(3, "another process reads %s" % TTY)
try:
    fd = open_raw()
except OSError as exc:
    done(2, "cannot open %s: %s" % (TTY, exc))
heard, _ = read_for(fd, max(float(A.get("listen_s") or 0), LISTEN_MIN_S), False)
if heard:
    OUT["heard"] = heard.decode("utf-8", "replace")[-200:]
    done(6, "the MCC is talking (booting?): not typing")
os.write(fd, b"\r")
p, ok = read_for(fd, A["prompt_s"], True)
OUT["prompt"] = p.decode("utf-8", "replace")[-120:]
if not ok:
    done(4, "no intact Cmd>/Debug> after a bare CR")
menu = menu_of(p)
entered = False
if A["menu"] != menu:
    b, ok = send(fd, "DEBUG" if A["menu"] == "debug" else "EXIT")
    if not ok or menu_of(b) != A["menu"]:
        done(5, "the MCC did not enter its %s menu" % A["menu"])
    entered = A["menu"] == "debug"
    menu = A["menu"]
rc, why = 0, None
for line in A["lines"]:
    b, ok = send(fd, line)
    OUT["replies"].append(b.decode("utf-8", "replace"))
    if not ok:
        rc, why = 5, "no prompt after %r" % line
        break
    menu = menu_of(b)
if entered and menu == "debug":
    b, ok = send(fd, "EXIT")
    menu = menu_of(b) if ok else menu
OUT["menu"] = menu
done(rc, why)
'''


def _py_pick() -> str:
    from .claim import _PY_PICK

    return _PY_PICK


def _last_json(text: str) -> dict[str, Any] | None:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None
    return None


def _others(info: dict[str, Any]) -> str:
    return "; ".join(f"pid {p}: {c}" for p, c in info.get("others") or []) or "?"


# --- the controller -------------------------------------------------------------------------------


class HubMccController:
    """``ControllerAdapter`` for the MCC of a board behind the hub (module docstring)."""

    route = ROUTE_TOOL

    def __init__(self, runner: Callable[..., Any], *, target: str, tty: str, host: str = "",
                 session: Any = None, holder_fn: Callable[[], str] | None = None,
                 shell_probe: Callable[[], str | None] | None = None,
                 identity_fn: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 pace_s: float = HUB_PACE_S) -> None:
        if not tty.startswith("/dev/"):
            raise UsageError(f"the MCC console {tty!r} is not a /dev path on the hub")
        self._runner = runner
        self.target = target
        self.tty = tty
        self.host = host
        self.url = f"hub-tool://{host or 'hub'}{tty}"
        self._session = session
        self._holder_fn = holder_fn
        self._shell_probe = shell_probe
        self.identity_fn = identity_fn
        self._clock = clock
        self._sleep = sleep
        self.pace_s = pace_s
        self.last_reboot: RebootWitness | None = None
        self.last_info: dict[str, Any] | None = None
        self.last_transcript = b""
        self.attempts = 0
        self._python: str | None = None
        #: CCR QUIET-1: hears each read's outcome (rc 3, another reader of the tty, is a
        #: HeldError: another client). ``Mps3Session.set_observer`` sets it.
        self.observer: Callable[[BaseException | None], None] | None = None

    def hub_python(self) -> str:
        """The hub's Python 3.10+ for pyverify's hub-side code (``PY310_PROBE``), found once.
        ``UnavailableError`` when there is none: never the bare (3.6) ``python3``."""
        if self._python:
            return self._python
        try:
            res = self._runner(["sh", "-c", PY310_PROBE], timeout=60.0)
        except Exception as exc:  # noqa: BLE001 - ssh/pyverify failures: the hub is not reached
            raise UnreachableError(f"cannot reach the hub to find its Python: {exc}") from exc
        words = (getattr(res, "stdout", "") or "").split()
        if getattr(res, "returncode", 1) != 0 or not words or not words[-1].startswith("/"):
            raise UnavailableError(
                "reboot_board", "the hub has no Python 3.10+ for pyverify's MCC tools (tried "
                + ", ".join(PY310_CANDIDATES) + "); its system python3 (3.6) is never used")
        self._python = words[-1]
        return self._python

    # -- the one-reader scan (sends nothing) --

    def _resetter(self, capture_s: float = 0.0, log_path: str | None = None) -> Any:
        from pyverify.bootrate import MccTtyResetter

        return MccTtyResetter(self._runner, tty_path=self.tty, pace_s=self.pace_s,
                              python=self.hub_python(), capture_s=capture_s, log_path=log_path)

    def scan(self) -> None:
        """pyverify's second-reader check of ``tty_00`` on the hub. Sends nothing; raises
        ``HeldError`` when another process reads the tty (a share, a ``cat``, a console)."""
        r = self._resetter()
        out = r.scan()
        info = r.last_info or {}
        if out.ok:
            return
        if info.get("rc") == 3:
            raise HeldError(
                f"another process reads the MCC console {self.tty} on the hub ({_others(info)}): "
                "a second reader on tty_00 splits the REBOOT, so nothing was sent",
                holder=_others(info),
                hint="stop that reader (an fpgahub share on tty_00 goes only with `share stop`, "
                     "which stops every share: ask whoever started it)")
        if info.get("rc") == 2:
            raise UnavailableError("reboot_board", f"the MCC console on the hub: {info.get('reason')} "
                                   "(the hub account needs group fpga)")
        raise UnreachableError(f"could not check the MCC console on the hub: {out.detail}",
                               hint="check `ssh HUB true` works without a prompt")

    # -- reboot --

    def _take_written(self) -> dict[str, Any] | None:
        door = getattr(self._session, "hub_sd", None) if self._session is not None else None
        take = getattr(door, "take_written", None)
        return take() if callable(take) else None

    def _holder(self) -> str:
        if self._holder_fn is not None:
            return self._holder_fn()
        hub = getattr(self._session, "hub", None)
        client = getattr(hub, "client", None)
        if client is not None and callable(getattr(client, "principal", None)):
            return str(client.principal())
        from .hub import HubClient

        return HubClient(self.host, self.target, runner=self._runner).principal()

    def _probe_shell(self) -> str | None:
        if self._shell_probe is None:
            return None
        try:
            return self._shell_probe()
        except Exception:  # noqa: BLE001 - a probe failure means "not answering"
            return None

    def _read_log(self, path: str) -> str:
        try:
            res = self._runner(["sh", "-c", _CAT_RM, "sh", path], timeout=60.0)
        except Exception as exc:  # noqa: BLE001 - the verdict's tail is the fallback
            log.warning("MCC boot log %s on the hub could not be read: %s", path, exc)
            return ""
        if getattr(res, "returncode", 1) != 0:
            return ""
        return str(getattr(res, "stdout", "") or "")

    def _field(self, written: dict[str, Any], resetter: Any) -> tuple[Any, str]:
        """``pyverify sd field BIT --already-written``: ``(outcome, route)``, where outcome is
        None when the REBOOT went out and was acknowledged."""
        from pyverify import fielding as fl
        from pyverify.lease import LeaseClient

        fielder = fl.SdFielder(self._runner, target=self.target,
                               lease=LeaseClient(self._runner, target=self.target),
                               holder=self._holder(), journal=fl.CommandJournal(self._runner),
                               mcc=resetter, now=self._clock, sleep=self._sleep,
                               log=lambda m: log.info("%s", m))
        res = fielder.field(str(written["ref"]), expect_sha256=str(written.get("sha256") or "")
                            or None, already_written=True)
        return res, "pyverify sd field --already-written"

    def reboot(self, progress: Progress | None = None, wait_s: float | None = None) -> dict:
        """The paced REBOOT on the hub, witnessed by the MCC's own boot log (and the shell
        answering again, when the board has one). After a hub SD write: ``sd field
        --already-written``. The post-write quirk is retried (module docstring)."""
        from pyverify import fielding as fl

        if wait_s is None:
            from .constants import reboot_wait_s
            try:
                wait_s = reboot_wait_s(self.identity_fn() if self.identity_fn else None)
            except Exception:  # noqa: BLE001 - an unreadable identity must not block a reboot
                wait_s = reboot_wait_s(None)
        guard_reset(self._session, "ACTION_MCC_REBOOT")    # SLOT-TIMING: not mid card job
        emit: Progress = progress or (lambda phase, done, total: None)
        written = self._take_written()
        shell_before = self._probe_shell()
        started = self._clock()
        capture_s = min(float(wait_s), CAPTURE_MAX_S)
        notes: list[str] = []
        attempt = 0
        while True:
            attempt += 1
            log_path = f"{LOG_DIR}/harness-manager-mcc-{secrets.token_hex(6)}.log"
            resetter = self._resetter(capture_s, log_path)
            route = "pyverify paced REBOOT"
            outcome: Any = None
            if written and written.get("ref"):
                res, route = self._field(written, resetter)
                if res.ok:
                    outcome = None
                elif (res.exit_code == fl.EXIT_REFUSED and res.stage == "preflight"
                      and "journal" in res.reason):
                    # The door proved the write by the hub's own record; pyverify can't read
                    # the journal here. The REBOOT keeps its gates (one reader, Cmd>).
                    notes.append(f"sd field: {res.reason.split('. ')[0]}; the hub door proved "
                                 f"the write ({written.get('source') or 'the hub record'})")
                    written = None
                    route = "pyverify paced REBOOT"
                    outcome = self._plain(resetter)
                else:
                    outcome = res
            else:
                outcome = self._plain(resetter)
            info = resetter.last_info or {}
            self.last_info = info
            if outcome is None:
                break
            if bare_crlf(str(info.get("prompt") or "")) and info.get("rc") == 4 \
                    and attempt < POST_WRITE_TRIES:
                notes.append(f"attempt {attempt}: the MCC answered a bare CR with CR/LF only "
                             f"(the post-SD-write quirk); again in {POST_WRITE_GAP_S:.0f} s")
                log.info("MCC %s on the hub: %s", self.tty, notes[-1])
                self._sleep(POST_WRITE_GAP_S)
                continue
            self.attempts = attempt
            raise self._refusal(outcome, info, route)
        self.attempts = attempt
        emit("sent", 1, 3)
        emit("down", 2, 3)
        text = self._read_log(log_path) or str(info.get("tail") or "")
        self.last_transcript = text.encode("utf-8", "replace")
        recs = parse_boot_log(text)
        boot = recs[-1] if recs else BootRecord()
        if not info.get("complete"):
            raise ActionFailedError(
                "the MCC rebooted but its boot log has no 'FPGA configuration complete'"
                + (" (it reports a configuration FAILURE)" if info.get("failed") else "")
                + (f": {'; '.join(boot.errors)}" if boot.errors else "")
                + f" (tail {str(info.get('tail') or '')[-160:]!r})",
                hint="check the SD's board.txt / nanosoc.txt and the .bit it names")
        boot.fpga_configured = True
        up_evidence = "the MCC boot log reached 'FPGA configuration complete.'"
        shell_after: str | None = None
        if self._shell_probe is not None:
            deadline = started + float(wait_s)
            while True:
                shell_after = self._probe_shell()
                if shell_after not in (None, "(busy)"):
                    up_evidence = f"the shell answers ping again (shell_id {shell_after})"
                    break
                if self._clock() >= deadline:
                    raise ActionFailedError(
                        f"the board went down after REBOOT (the MCC reloaded the FPGA from "
                        f"{boot.fpga_file or 'the SD'}) but did not come back within "
                        f"{float(wait_s):.0f}s: the shell never answered ping again",
                        hint="check the SD contents (board.txt, the .bit it names) and the "
                             "harness's own console")
                self._sleep(PING_INTERVAL_S)
        emit("up", 3, 3)
        down_s = round(1.0 + 7 * self.pace_s, 3)         # the settle, then R-E-B-O-O-T-CR
        self.last_reboot = RebootWitness(
            sent_at=started, down_after_s=down_s, up_after_s=self._clock() - started,
            down_evidence=(f"the MCC answered REBOOT with 'Rebooting' on {self.tty} "
                           f"(pyverify's hub-side writer, the only reader)",),
            up_evidence=up_evidence, shell_id_before=shell_before, shell_id_after=shell_after,
            boot=boot)
        out = self.last_reboot.as_dict()
        out.update(route=route, tty=self.tty, hub=self.host, attempts=attempt,
                   notes=notes, via="hub")
        return out

    def _plain(self, resetter: Any) -> Any:
        """The paced REBOOT alone: None when acknowledged, else the ``ResetOutcome``."""
        out = resetter()
        return None if (out.ok and out.sent is True) else out

    def _refusal(self, outcome: Any, info: dict[str, Any], route: str) -> HarnessError:
        from pyverify import fielding as fl

        if isinstance(outcome, fl.FieldResult):
            nxt = str(outcome.record.get("next") or "")
            if outcome.exit_code in (fl.EXIT_REBOOT, fl.EXIT_REFUSED) and \
                    info.get("rc") in (2, 3, 4):
                return self._mcc_refusal(info, nxt)
            if outcome.exit_code == fl.EXIT_REFUSED:
                return RefusedError(f"{route} refused the REBOOT: {outcome.reason}",
                                    hint=nxt or "nothing was sent")
            if outcome.exit_code == fl.EXIT_HUB:
                return UnreachableError(f"{route}: {outcome.reason}", hint=nxt)
            return ActionFailedError(f"{route}: {outcome.reason}", hint=nxt)
        if info.get("rc") in (2, 3, 4):
            return self._mcc_refusal(info, "")
        if outcome.sent is None:
            return ActionFailedError(
                f"REBOOT sent on {self.tty} but no restart observed: {outcome.detail}",
                hint="an unacknowledged REBOOT did not happen: check the board's identity in "
                     "2 min before anything else; never send a second one on top of the first")
        return UnreachableError(f"the paced REBOOT could not run on the hub: {outcome.detail}",
                                hint="check `ssh HUB true` works without a prompt")

    def _mcc_refusal(self, info: dict[str, Any], nxt: str) -> HarnessError:
        rc = info.get("rc")
        if rc == 3:
            return HeldError(
                f"refusing the MCC REBOOT: another process reads {self.tty} on the hub "
                f"({_others(info)}). Nothing was sent",
                holder=_others(info),
                hint="a second reader eats the MCC's echo; stop it (an fpgahub share on "
                     "tty_00 counts), then retry")
        if rc == 4:
            return NothingOnTargetError(
                f"refusing the MCC REBOOT: {info.get('reason') or 'no intact Cmd>'} on "
                f"{self.tty} (got {str(info.get('prompt') or '')!r}). Nothing was sent",
                hint=nxt or "a reader this account cannot see, or the MCC is not at Cmd>")
        return UnavailableError("reboot_board", f"{info.get('reason')} (the MCC console is "
                                "root:fpga: the hub account needs group fpga)")

    def command(self, line: str, *, arm: bool = False) -> str:
        """One allowlisted command. ``REBOOT`` is the witnessed ``reboot()``; over the hub the
        MCC is read-only otherwise (``CFG W`` needs the Debug USB), and DEBUG/EXIT are the
        reader's own business (a menu left behind would refuse the next REBOOT)."""
        cmd = classify(line, arm=arm)
        if cmd.head == "REBOOT":
            self.reboot()
            assert self.last_reboot is not None
            return self.last_reboot.summary()
        if cmd.head in ("DEBUG", "EXIT"):
            raise RefusedError(f"{cmd.head} over the hub is refused: each read enters and leaves "
                               "DEBUG itself, and a menu left open would refuse the next REBOOT")
        if cmd.head == "CFG" and cmd.text.split()[1] == "W":
            raise RefusedError(f"{cmd.text} changes the board: over the hub the MCC is read-only",
                               hint="use the Debug USB on this machine")
        (reply,) = self._read(cmd.menu, [cmd.text])
        err = reply_error(reply)
        if err:
            raise ActionFailedError(f"the MCC refused {cmd.text!r}: {err}")
        return reply

    def _read(self, menu: str, lines: Sequence[str]) -> list[str]:
        """Run reads on the hub; each reply without its echo and prompt. The outcome goes to
        ``observer`` (QUIET-POLL)."""
        from .mcc import _tell

        try:
            out = self._read_on_hub(menu, lines)
        except BaseException as exc:
            _tell(self.observer, exc)
            raise
        _tell(self.observer, None)
        return out

    def _read_on_hub(self, menu: str, lines: Sequence[str]) -> list[str]:
        args = {"tty": self.tty, "baud": 115200, "pace": self.pace_s, "menu": menu,
                "lines": list(lines), "prompt_s": READ_PROMPT_S, "reply_s": READ_REPLY_S,
                "listen_s": READ_LISTEN_S}
        argv = ["sh", "-c", _py_pick(), HUB_MCC_READ_PY, json.dumps(args, sort_keys=True)]
        try:
            res = self._runner(argv, timeout=READ_TIMEOUT_S)
        except TimeoutError as exc:
            raise UnreachableError(f"the MCC read on the hub timed out: {exc}") from exc
        except OSError as exc:
            raise UnreachableError(f"cannot run the MCC read on the hub: {exc}") from exc
        except HarnessError:
            raise
        except Exception as exc:  # noqa: BLE001 - pyverify's LeaseError ("cannot run ssh")
            raise UnreachableError(f"cannot reach the hub for the MCC read: {exc}") from exc
        info = _last_json(getattr(res, "stdout", "") or "")
        self.last_info = info
        if info is None:
            err = (getattr(res, "stderr", "") or "").strip().splitlines()
            raise UnreachableError(
                f"the MCC reader on the hub gave no verdict (status "
                f"{getattr(res, 'returncode', '?')}{': ' + err[-1] if err else ''})",
                hint="check `ssh HUB true` works and that the hub has python3")
        rc = info.get("rc")
        replies = [str(r) for r in info.get("replies") or []]
        self.last_transcript = "".join(replies).encode("utf-8", "replace")
        if rc == 0 and len(replies) == len(lines):
            return [strip_echo(raw, line) for raw, line in zip(replies, lines, strict=True)]
        if rc == 3:
            raise HeldError(f"another process reads the MCC console {self.tty} on the hub "
                            f"({_others(info)}): nothing was typed", holder=_others(info),
                            hint="the MCC takes one reader; ask whoever holds it")
        if rc == 4:
            raise NothingOnTargetError(f"no MCC prompt on {self.tty} after a bare CR (heard "
                                       f"{str(info.get('prompt') or '')!r})",
                                       hint="is the MCC at Cmd>? A reader this account cannot "
                                            "see holds it, or it is mid-boot")
        if rc == 6:
            raise ActionFailedError(f"the MCC on {self.tty} is talking (booting?): not typing "
                                    "(a key in the auto-boot window stops the boot)",
                                    hint="wait for the boot to finish, then retry")
        if rc == 2:
            raise UnavailableError("console_controller", f"the MCC console on the hub: "
                                   f"{info.get('reason')} (the hub account needs group fpga)")
        raise UnreachableError(f"the MCC read on the hub failed: {info.get('reason') or rc}")

    def cfg_read(self, items: Iterable[tuple[str, int]]) -> list[str | HarnessError]:
        """Several ``CFG R`` reads in one DEBUG visit, on the hub."""
        cmds = [classify(f"CFG R {kind} {dev}") for kind, dev in items]
        replies = self._read("debug", [c.text for c in cmds])
        out: list[str | HarnessError] = []
        for reply in replies:
            err = reply_error(reply)
            out.append(ActionFailedError(f"MCC: {err}") if err else reply)
        return out

    def temperatures(self) -> Sequence[Reading]:
        name, unit, source = "mcc_temp", "degC", "mcc-console (hub)"
        try:
            (result,) = self.cfg_read([("TEMP", 0)])
        except HarnessError as exc:
            return [Reading.unavailable(name, unit, str(exc), source=source)]
        if isinstance(result, HarnessError):
            return [Reading.unavailable(name, unit, str(result), source=source)]
        value = parse_temp(result)
        if value is None:
            return [Reading.unavailable(name, unit, f"unrecognised MCC reply {result!r}",
                                        source=source)]
        return [Reading(name=name, value=value, unit=unit, source=source, reason=TEMP_CAVEAT)]

    def oscillators(self) -> Sequence[Reading]:
        source = "mcc-console setpoint (hub)"
        try:
            results = self.cfg_read([("OSC", n) for n in range(OSC_COUNT)])
        except HarnessError as exc:
            return [Reading.unavailable(f"osc{n}", "MHz", str(exc), source=source)
                    for n in range(OSC_COUNT)]
        out: list[Reading] = []
        for n, result in enumerate(results):
            parsed = None if isinstance(result, HarnessError) else parse_osc(result)
            if parsed is None or parsed[0] != n:
                why = str(result) if isinstance(result, HarnessError) else \
                    f"unrecognised MCC reply {result!r}"
                out.append(Reading.unavailable(f"osc{n}", "MHz", why, source=source))
            else:
                out.append(Reading(name=f"osc{n}", value=parsed[1], unit="MHz", source=source,
                                   reason=OSC_CAVEAT))
        return out


# --- the hooks ------------------------------------------------------------------------------------


def make_hub_controller(session: Any) -> HubMccController | None:
    """The hub-mode controller for a session, or None (no hub, or a REST-only hub)."""
    from . import hub as _hub
    from .mcc import _shell_probe_for

    cand = getattr(session, "candidate", None)
    if cand is None:
        return None
    try:
        cfg = _hub.hub_config_for(cand)
    except UsageError:
        return None
    if cfg is None:
        return None
    runner = runner_for(cfg)
    if runner is None:
        return None
    shell = getattr(session, "shell", None)
    probe = _shell_probe_for(shell, PING_TIMEOUT_S) if shell is not None else None
    host = str(getattr(getattr(cfg, "rest", None), "ssh_host", "") or cfg.host)
    return HubMccController(runner, target=cfg.target, tty=mcc_tty_for(cfg), host=host,
                            session=session, shell_probe=probe,
                            identity_fn=session.identity if shell is not None else None)


def scan_for_hub(hub: Any, tty: str) -> None:
    """The one-reader check of ``tty`` on ``hub`` (``session.hub``): what the hub door's
    preflight asks before anything is uploaded. Raises; sends nothing."""
    runner = runner_from_hub(hub)
    if runner is None:
        raise UnavailableError("hub SD install", "the MCC is reached on the hub over SSH, and "
                               "this hub has no SSH login (a REST-only hub)")
    HubMccController(runner, target=str(getattr(hub, "target", "")), tty=tty,
                     host=str(getattr(hub, "host", ""))).scan()


__all__ = [
    "HUB_MCC_READ_PY", "HUB_PACE_S", "HubMccController", "PY310_PROBE", "ROUTE_TOOL", "is_mcc_tty",
    "make_hub_controller", "mcc_tty_for", "runner_for", "runner_from_hub", "scan_for_hub",
]
