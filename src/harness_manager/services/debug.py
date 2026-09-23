"""The OpenOCD session manager: detect, up, down, status, and swap awareness.

One OpenOCD per board, bound to 127.0.0.1, on a per-board block of local ports,
recorded in ``<state_dir>/debug/<board>.json`` so any process can find it and
an orphan can be reaped. Board-agnostic: the board pack's ``DebugAdapter``
says which configs to load (``core.pack.DebugAdapter``).

The OpenOCD command line
------------------------
::

    openocd -s <search path>...                      the pack's config directory
            -c <probe arg>...                        e.g. set RBB_HOST / RBB_PORT
            -f <target cfg>...                       the target half
            -c <post-config>...                      e.g. the gdb-attach hook
            -c "gdb_port G; telnet_port T; tcl_port C; bindto 127.0.0.1"

Why this order (OpenOCD v0.12.0 source, and measured against a real 0.12 build
with ``tests/fakes/t4_rbb_jtag.py``):

- ``-f`` and ``-c`` run strictly in argv order: both become config commands
  (src/helper/options.c:288-311). ``-s`` is applied while parsing arguments,
  so its position does not matter.
- **Probe overrides before any ``-f``.** The MPS3 target halves read
  ``RBB_HOST``/``RBB_PORT`` with ``if {![info exists ...]}``, so a ``set``
  after the ``-f`` is silently ignored (harness handover B4).
- **Ports and bindto before ``init``.** ``gdb_port``, ``telnet_port``,
  ``tcl_port`` and ``bindto`` are ``COMMAND_CONFIG`` commands
  (gdb_server.c:4034, telnet_server.c:990, tcl_server.c:328, server.c:818).
  After ``init`` OpenOCD refuses them: ``Error: The 'gdb_port' command must be
  used before 'init'.`` (measured), and by then the gdb server has already
  bound the default 3333. We pass no explicit ``init``: OpenOCD runs it after
  the whole command line (openocd.c: parse_config_file, server_init, then
  ``init``), so the port line only has to come before any ``init`` a config
  might contain. It goes LAST so that it also overrides any port a target
  config pins for itself.
- The telnet and tcl servers listen before ``init``; the gdb server listens
  inside ``init``, after the adapter has connected and the JTAG chain was
  scanned (openocd.c:303-315; ``Listening on port N for gdb connections`` is
  logged last, measured). So "up" means the gdb line appeared and the tcl port
  answers ``version``.

Traps handled
-------------
- **A taken gdb port does NOT stop OpenOCD** (measured, 0.12): it logs
  ``couldn't bind gdb to socket on port N: Address already in use`` and keeps
  running with no gdb server, still holding the board's JTAG port. So ``up``
  watches the log for that line and kills the process. A taken telnet/tcl port
  makes OpenOCD exit.
- **Port policy:** with no pinned base, ports come from a stable per-board
  block (``DEFAULT_PORT_BASE`` + 4 x slot, slot = crc32(board_id) % 64, so a
  board keeps its gdb port across restarts and swaps). A block with any port in
  use is skipped (REALLOCATION); if all 64 are taken, ``PortBoundError``. A
  base pinned by the caller (``port_base=`` or ``$HARNESS_MANAGER_DEBUG_PORT_BASE``)
  is never moved: a taken port there is ``PortBoundError`` (exit 5), because
  a debugger profile points at it.
- **One client per board port** (jtag_server.c:185-192): a second OpenOCD is
  accepted and closed at once, which OpenOCD logs as
  ``Error on socket 'remote_bitbang_fill_buf' ... Connection reset by peer``
  (measured). That is reported as ``HeldError`` (exit 4). ``detect`` while a
  session is up asks the running OpenOCD (``scan_chain`` over its tcl port)
  instead of dialling the board a second time.
- **A swap invalidates the DAP.** ``deploy.started`` closes the session
  (synchronously, before the swap proceeds); ``deploy.done`` with
  ``verified: true`` reopens it on the same ports, in a worker thread.
- **Orphans** (the HAPS lesson: an OpenOCD held a probe for an hour after its
  owner was gone, HAPS-work openocd/README.md "Orphaned sessions"). A session
  belongs to the process that started it (the engine's lifetime: the engine
  calls ``down`` when it closes the board). A recorded OpenOCD whose owner
  process is gone is an ORPHAN and is reaped by ``status``/``up``/``down``/
  ``reap_orphans``; so is one whose ports have all gone (it serves nothing but
  may still hold the board). One whose owner is alive is HELD: reported with
  its holder, never killed without ``force``. A recorded pid that now belongs
  to another program (pid reuse; checked on Linux via /proc) is never killed.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    HarnessError,
    HeldError,
    NothingOnTargetError,
    PortBoundError,
    UnavailableError,
    UnreachableError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.pack import BoardSession, DebugAdapter
from harness_manager.core.services import DebugStatus

log = logging.getLogger(__name__)

__all__ = ["DebugService", "DebugPorts", "find_openocd", "up_argv", "detect_argv",
           "parse_idcodes", "classify_failure"]

OPENOCD_ENV = "HARNESS_MANAGER_OPENOCD"
PORT_BASE_ENV = "HARNESS_MANAGER_DEBUG_PORT_BASE"
STATE_DIR_ENV = "HARNESS_MANAGER_STATE_DIR"
# 23300-23555: below the Linux ephemeral range (32768+) and Windows' (49152+), so
# the kernel never hands these out to someone else's connection; clear of the
# HAPS tools' local blocks (3200-3999 JTAG, 24000-27999 serial, 29200-32399 openocd).
DEFAULT_PORT_BASE = 23300
PORT_SLOTS = 64
PORT_BLOCK = 4          # gdb (first target), gdb+1 (a second target), telnet, tcl
CAPABILITY = "debug_dut"


# --- small portable helpers ------------------------------------------------------------


def find_openocd() -> str:
    """``$HARNESS_MANAGER_OPENOCD``, else ``openocd`` on PATH, else ``UnavailableError`` (12)."""
    env = os.environ.get(OPENOCD_ENV)
    if env:
        if Path(env).is_file():
            return str(Path(env))
        found = shutil.which(env)
        if found:
            return found
        raise UnavailableError(CAPABILITY, f"{OPENOCD_ENV}={env} does not exist")
    found = shutil.which("openocd")
    if found:
        return found
    raise UnavailableError(CAPABILITY, f"OpenOCD not found — install it or set {OPENOCD_ENV}")


def port_in_use(port: int) -> bool:
    """Is anything bound to 127.0.0.1:``port``? Never connects, so never disturbs a server.

    POSIX: SO_REUSEADDR makes only a live listener (not TIME_WAIT) block the bind.
    Windows: SO_EXCLUSIVEADDRUSE, because SO_REUSEADDR there would steal the port.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def pid_alive(pid: int) -> bool:
    """True if ``pid`` is a live (non-zombie) process. Never signals it on Windows.

    (``os.kill(pid, 0)`` on Windows is TerminateProcess; mirrors core.session's check.)
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5         # ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                    # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:                                                # a zombie is dead for our purposes
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _cmdline(pid: int) -> list[str] | None:
    """The process's argv on Linux; ``None`` where /proc is unavailable."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _kill_pid(pid: int, timeout: float) -> None:
    """Terminate a process we have no handle for. TerminateProcess on Windows."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if os.name == "nt":
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _terminate(proc: subprocess.Popen, timeout: float) -> int | None:
    if proc.poll() is None:
        proc.terminate()                    # SIGTERM; TerminateProcess on Windows
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(2.0)
            except subprocess.TimeoutExpired:
                log.warning("OpenOCD pid %d did not exit after SIGKILL", proc.pid)
    return proc.returncode


def tcl_rpc(port: int, command: str, timeout: float = 3.0) -> str:
    """One OpenOCD Tcl RPC round trip on 127.0.0.1 (commands and replies end in 0x1a)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(command.encode() + b"\x1a")
        buf = bytearray()
        while not buf.endswith(b"\x1a"):
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError(f"OpenOCD tcl port {port} closed mid-reply")
            buf += chunk
    return buf[:-1].decode(errors="replace")


def _safe_name(board_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in board_id)


def _bus_of(engine: Any) -> EventBus | None:
    if engine is None:
        return None
    if isinstance(engine, EventBus):
        return engine
    return getattr(engine, "bus", None)


# --- ports and argv ----------------------------------------------------------------------


@dataclass(frozen=True)
class DebugPorts:
    gdb: int
    telnet: int
    tcl: int

    @classmethod
    def block(cls, base: int) -> DebugPorts:
        return cls(gdb=base, telnet=base + 2, tcl=base + 3)

    def reserved(self) -> tuple[int, ...]:
        """Every port the block claims: gdb+1 is kept for a second gdb target."""
        return (self.gdb, self.gdb + 1, self.telnet, self.tcl)

    def as_dict(self) -> dict[str, int]:
        return {"gdb": self.gdb, "telnet": self.telnet, "tcl": self.tcl}

    def command(self) -> str:
        return (f"gdb_port {self.gdb}; telnet_port {self.telnet}; tcl_port {self.tcl}; "
                "bindto 127.0.0.1")


def _head(binary: str, adapter: DebugAdapter, cfgs: Sequence[str]) -> list[str]:
    argv = [binary]
    for path in adapter.openocd_search_paths():
        argv += ["-s", str(path)]
    for arg in adapter.openocd_probe_args():      # before any -f: see module docstring
        argv += ["-c", arg]
    for cfg in cfgs:
        argv += ["-f", cfg]
    return argv


def _post_config(adapter: DebugAdapter) -> tuple[str, ...]:
    hook = getattr(adapter, "openocd_post_config", None)
    return tuple(hook()) if callable(hook) else ()


def up_argv(binary: str, adapter: DebugAdapter, cfgs: Sequence[str],
            ports: DebugPorts, post: Sequence[str] = ()) -> list[str]:
    argv = _head(binary, adapter, cfgs)
    for cmd in post:
        argv += ["-c", cmd]
    return argv + ["-c", ports.command()]


def detect_argv(binary: str, adapter: DebugAdapter, cfgs: Sequence[str]) -> list[str]:
    """Scan the chain and quit. The core is ``-defer-examine``, so ``init`` never halts
    it; ``shutdown`` runs in the config stage, so no telnet/tcl server ever binds, and
    the gdb server (bound inside ``init``) is disabled."""
    return _head(binary, adapter, cfgs) + [
        "-c", "gdb_port disabled; telnet_port disabled; tcl_port disabled",
        "-c", "init", "-c", "scan_chain", "-c", "shutdown",
    ]


# --- reading OpenOCD's log ------------------------------------------------------------

_FOUND_RE = re.compile(r"tap/device found: (0x[0-9a-fA-F]{8})")
_UNEXPECTED_RE = re.compile(r"UNEXPECTED: (0x[0-9a-fA-F]{8})")
_SCAN_ROW_RE = re.compile(r"^\s*\d+\s+\S+\s+[YN]\s+(0x[0-9a-fA-F]{8})\s", re.M)
_LISTEN_RE = re.compile(r"Listening on port (\d+) for (\w+) connections")
_BIND_RE = re.compile(r"couldn't bind (\w+) to socket on port (\d+)")
_NO_CHAIN_RE = re.compile(r"JTAG scan chain interrogation failed: all (zeroes|ones)")
_CANT_FIND_RE = re.compile(r"Can't find (\S+)")
_BEFORE_INIT_RE = re.compile(r"The '(\w+)' command must be used before 'init'")
_NO_DRIVER_RE = re.compile(r"The specified debug interface was not found \((\S+)\)")
_DEAD_IDS = {"0x00000000", "0xffffffff"}


def parse_idcodes(text: str) -> list[str]:
    """IDCODEs in chain order: the ``tap/device found`` lines, else the scan_chain rows."""
    found = [m.lower() for m in _FOUND_RE.findall(text)]
    if not found:
        found = [m.lower() for m in _SCAN_ROW_RE.findall(text)]
    return [i for i in found if i not in _DEAD_IDS]


def _last_errors(text: str, n: int = 3) -> str:
    errs = [ln.strip() for ln in text.splitlines() if ln.startswith("Error")]
    if not errs:
        errs = [ln.strip() for ln in text.splitlines() if ln.strip()][-n:]
    return " | ".join(errs[-n:])


def classify_failure(text: str, returncode: int | None, *, target: str = "") -> HarnessError:
    """Map an OpenOCD log to the error (and exit code) it means."""
    where = f" ({target})" if target else ""
    m = _BIND_RE.search(text)
    if m:
        return PortBoundError(f"local {m.group(1)} port {m.group(2)} is already in use",
                              hint="another program holds it; stop it or pick another port base")
    m = _BEFORE_INIT_RE.search(text)
    if m:
        return ActionFailedError(f"OpenOCD refused '{m.group(1)}' after init",
                                 hint="a target config runs 'init' itself; remove it")
    m = _CANT_FIND_RE.search(text)
    if m:
        return UnavailableError(CAPABILITY, f"OpenOCD cannot find {m.group(1)}")
    m = _NO_DRIVER_RE.search(text)
    if m:
        # Measured: a v0.12.0 build configured without it says exactly this.
        return UnavailableError(CAPABILITY, f"this OpenOCD was built without the {m.group(1)} "
                                            "adapter; install one that has it (0.12 builds "
                                            "with --enable-remote-bitbang)")
    if "Failed to connect" in text or "Connection refused" in text:
        return UnreachableError(f"OpenOCD could not connect to the board's JTAG server{where}",
                                hint="is the board up and the harness loaded? (6921 on the shell)")
    if "remote_bitbang_fill_buf" in text or "Connection reset by peer" in text:
        return HeldError(f"the board's JTAG server{where} closed the connection at once",
                         hint="another debugger probably holds it: the board serves one "
                              "client per port (jtag_server.c:185)")
    m = _NO_CHAIN_RE.search(text)
    if m:
        return NothingOnTargetError(f"nothing answered on the JTAG chain (all {m.group(1)})",
                                    hint="the loaded design may have no debug port")
    code = "no exit code" if returncode is None else f"exit code {returncode}"
    return ActionFailedError(f"OpenOCD failed ({code}): {_last_errors(text)}")


# --- the service -----------------------------------------------------------------------


@dataclass
class _Live:
    proc: subprocess.Popen
    ports: DebugPorts
    session: BoardSession
    config: tuple[str, ...]
    board_id: str = ""


# OpenOCDs started by THIS process, keyed by registry file. Process-wide, so a
# second DebugService in the same process sees them as this process's own and
# never mistakes them for orphans.
_PROCESS_LIVE: dict[str, _Live] = {}
_PROCESS_LIVE_LOCK = threading.Lock()


class DebugService:
    """Implements ``core.services.DebugService``.

    ``engine`` may be the Engine (uses ``engine.bus``, ``engine.state_dir`` and,
    to reopen after a swap, ``engine.session(board_id)``), an ``EventBus``, or
    ``None``. The state directory falls back to ``$HARNESS_MANAGER_STATE_DIR``, then
    ``~/.config/harness-manager``, read at call time.
    """

    def __init__(self, engine: Any = None, *, port_base: int | None = None,
                 start_timeout: float = 30.0, detect_timeout: float = 30.0,
                 stop_timeout: float = 3.0) -> None:
        self.engine = None if isinstance(engine, EventBus) else engine
        self.bus = _bus_of(engine)
        self.port_base = port_base
        self.start_timeout = start_timeout
        self.detect_timeout = detect_timeout
        self.stop_timeout = stop_timeout
        self._resume: dict[str, tuple[BoardSession | None, DebugPorts]] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._reserved: set[int] = set()
        self.threads: list[threading.Thread] = []      # swap reopen workers (tests join them)
        self._unsubs: list[Callable[[], None]] = []
        if self.bus is not None:
            self._unsubs += [
                self.bus.subscribe("deploy.started", self._on_deploy_started),
                self.bus.subscribe("deploy.done", self._on_deploy_done),
                self.bus.subscribe("deploy.failed", self._on_deploy_failed),
            ]

    # -- where things live -------------------------------------------------------------

    @property
    def state_dir(self) -> Path:
        sd = getattr(self.engine, "state_dir", None)
        if sd is not None:
            return Path(sd)
        env = os.environ.get(STATE_DIR_ENV)
        return Path(env) if env else Path.home() / ".config" / "harness-manager"

    @property
    def registry_dir(self) -> Path:
        return self.state_dir / "debug"

    def _record_path(self, board_id: str) -> Path:
        return self.registry_dir / f"{_safe_name(board_id)}.json"

    def log_path(self, board_id: str) -> Path:
        return self.registry_dir / f"{_safe_name(board_id)}.log"

    def _read_record(self, board_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._record_path(board_id).read_text())
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            log.warning("debug registry for %s is unreadable; treating it as stale", board_id)
            return {"pid": 0, "owner": {}}

    def _write_record(self, board_id: str, record: dict[str, Any]) -> None:
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        path = self._record_path(board_id)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(record, indent=1))
        os.replace(tmp, path)

    def _drop_record(self, board_id: str) -> None:
        self._record_path(board_id).unlink(missing_ok=True)

    def _live_key(self, board_id: str) -> str:
        return str(self._record_path(board_id).resolve())

    def _live_get(self, board_id: str) -> _Live | None:
        with _PROCESS_LIVE_LOCK:
            return _PROCESS_LIVE.get(self._live_key(board_id))

    def _live_set(self, board_id: str, live: _Live) -> None:
        with _PROCESS_LIVE_LOCK:
            _PROCESS_LIVE[self._live_key(board_id)] = live

    def _live_pop(self, board_id: str) -> _Live | None:
        with _PROCESS_LIVE_LOCK:
            return _PROCESS_LIVE.pop(self._live_key(board_id), None)

    def _live_boards(self) -> list[str]:
        prefix = str(self.registry_dir.resolve())
        with _PROCESS_LIVE_LOCK:
            return [lv.board_id for key, lv in _PROCESS_LIVE.items() if key.startswith(prefix)]

    def _board_lock(self, board_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(board_id, threading.RLock())

    # -- events --------------------------------------------------------------------------

    def _publish(self, board_id: str, state: str, *, ports: DebugPorts | None = None,
                 pid: int = 0, detail: str = "", config: Sequence[str] = ()) -> None:
        if self.bus is None:
            return
        self.bus.publish(Event("debug.state", board_id, {
            "state": state, "ports": ports.as_dict() if ports else {}, "pid": pid,
            "detail": detail, "config": list(config)}))

    # -- registry inspection and orphan reaping ------------------------------------------------

    def _classify(self, board_id: str, rec: dict[str, Any]) -> str:
        """``ours | held | starting | orphan | stale | reused | exited | elsewhere | ports-gone``."""
        owner = rec.get("owner") or {}
        pid = int(rec.get("pid") or 0)
        if owner.get("host") and owner.get("host") != socket.gethostname():
            return "elsewhere"
        live = self._live_get(board_id)
        if live is not None and live.proc.pid == pid:
            if live.proc.poll() is not None:
                return "exited"
            return self._ports_verdict(rec, "ours")
        if not pid_alive(pid):
            return "stale"
        cmd = _cmdline(pid)
        ports_cmd = rec.get("ports_command", "")
        if cmd is not None and ports_cmd and ports_cmd not in cmd:
            return "reused"
        owner_pid = int(owner.get("pid") or 0)
        if owner_pid == os.getpid() or not pid_alive(owner_pid):
            return "orphan"             # its owner process is gone (or was an earlier engine here)
        return self._ports_verdict(rec, "held")

    @staticmethod
    def _ports_verdict(rec: dict[str, Any], alive: str) -> str:
        if rec.get("state") != "up":
            return "starting" if alive == "held" else alive
        ports = rec.get("ports") or {}
        if ports and not any(port_in_use(int(p)) for p in ports.values()):
            return "ports-gone"
        return alive

    def _inspect(self, board_id: str) -> tuple[dict[str, Any] | None, str, str]:
        """(live record or None, verdict, detail). Reaps what should not be running."""
        rec = self._read_record(board_id)
        if rec is None:
            return None, "none", ""
        verdict = self._classify(board_id, rec)
        pid = int(rec.get("pid") or 0)
        if verdict == "stale":
            self._drop_record(board_id)
            return None, verdict, f"the previous OpenOCD (pid {pid}) had exited"
        if verdict == "reused":
            self._drop_record(board_id)
            return None, verdict, f"pid {pid} now belongs to another program; record dropped"
        if verdict == "exited":
            live = self._live_pop(board_id)
            rc = live.proc.returncode if live is not None else None
            self._drop_record(board_id)
            self._release_ports(rec)
            text = self._log_text(board_id)
            detail = f"OpenOCD exited with code {rc}: {_last_errors(text)}"
            self._publish(board_id, "failed", ports=live.ports if live else None, pid=pid,
                          detail=detail)
            return None, verdict, detail
        if verdict in ("orphan", "ports-gone"):
            why = ("its owner process is gone" if verdict == "orphan"
                   else "none of its ports is listening")
            log.warning("reaping OpenOCD pid %d for %s: %s", pid, board_id, why)
            live = self._live_pop(board_id)
            if live is not None:
                _terminate(live.proc, self.stop_timeout)
            else:
                _kill_pid(pid, self.stop_timeout)
            self._drop_record(board_id)
            self._release_ports(rec)
            detail = f"reaped an orphaned OpenOCD (pid {pid}): {why}"
            self._publish(board_id, "down", detail=detail)
            return None, verdict, detail
        return rec, verdict, ""

    def reap_orphans(self) -> list[str]:
        """Sweep every recorded session; returns the boards whose OpenOCD was reaped."""
        reaped = []
        for path in sorted(self.registry_dir.glob("*.json")) if self.registry_dir.is_dir() else []:
            try:
                board_id = json.loads(path.read_text()).get("board_id") or path.stem
            except (ValueError, OSError):
                board_id = path.stem
            with self._board_lock(board_id):
                _, verdict, _ = self._inspect(board_id)
            if verdict in ("orphan", "ports-gone"):
                reaped.append(board_id)
        return reaped

    @staticmethod
    def _holder(rec: dict[str, Any]) -> str:
        o = rec.get("owner") or {}
        return f"{o.get('user', '?')} on {o.get('host', '?')} (pid {o.get('pid', '?')})"

    @staticmethod
    def _status_of(rec: dict[str, Any], state: str, detail: str = "") -> DebugStatus:
        ports = rec.get("ports") or {}
        return DebugStatus(state=state, gdb_port=int(ports.get("gdb", 0)),
                           telnet_port=int(ports.get("telnet", 0)),
                           tcl_port=int(ports.get("tcl", 0)),
                           config=tuple(rec.get("config") or ()), pid=int(rec.get("pid") or 0),
                           detail=detail)

    # -- protocol: status / up / down / detect ------------------------------------------------

    def status(self, session: BoardSession) -> DebugStatus:
        board_id = session.candidate.board_id
        with self._board_lock(board_id):
            rec, verdict, detail = self._inspect(board_id)
            if rec is None:
                state = "failed" if verdict == "exited" else "down"
                return DebugStatus(state=state, detail=detail)
            if verdict == "ours":
                gdb = (rec.get("ports") or {}).get("gdb")
                return self._status_of(rec, "up", f"gdb 127.0.0.1:{gdb}")
            if verdict == "starting":
                return self._status_of(rec, "starting", f"starting, held by {self._holder(rec)}")
            return self._status_of(rec, "up", f"held by {self._holder(rec)}")

    def up(self, session: BoardSession, *, _prefer: DebugPorts | None = None) -> DebugStatus:
        """Start OpenOCD for the loaded design and wait until it serves gdb.

        Exit codes: 12 no OpenOCD / no adapter, 13 no debug port on the loaded
        design, 8 already up (ours), 4 held by another process or the board's
        JTAG port is taken, 5 local port taken, 7 board unreachable, 6 other.
        """
        board_id = session.candidate.board_id
        with self._board_lock(board_id):
            rec, verdict, _ = self._inspect(board_id)
            if rec is not None:
                if verdict == "ours":
                    raise AlreadyError(f"the debug session for {board_id} is already up",
                                       hint=f"gdb on 127.0.0.1:{rec['ports']['gdb']}; "
                                            "'down' first to restart it")
                raise HeldError(f"the debug session for {board_id} is held",
                                holder=self._holder(rec),
                                hint=f"held by {self._holder(rec)}; 'down --force' takes it")
            try:
                adapter = self._adapter(session)
                binary = find_openocd()
                cfgs = tuple(adapter.openocd_config())      # 13 for a design with no DAP
                post = _post_config(adapter)
                target = getattr(adapter, "describe", lambda: "")()
                return self._start(board_id, session, binary, adapter, cfgs, post, target,
                                   _prefer)
            except HarnessError as exc:
                # "failed" carries the reason to the GUI (and after a swap, to anyone).
                self._publish(board_id, "failed", detail=str(exc))
                raise

    def down(self, session: BoardSession, *, force: bool = False,
             reason: str = "") -> DebugStatus:
        """Stop the board's OpenOCD. Safe when nothing is running.

        A session another live process owns is ``HeldError`` unless ``force``.
        """
        board_id = session.candidate.board_id
        with self._board_lock(board_id):
            return self._down_locked(board_id, force=force, reason=reason)

    def _down_locked(self, board_id: str, *, force: bool, reason: str) -> DebugStatus:
        rec, verdict, detail = self._inspect(board_id)
        if rec is None:
            return DebugStatus(state="down", detail=detail or "no debug session was running")
        if verdict in ("held", "starting", "elsewhere") and not force:
            raise HeldError(f"the debug session for {board_id} belongs to another process",
                            holder=self._holder(rec),
                            hint=f"held by {self._holder(rec)}; use force to stop it")
        pid = int(rec.get("pid") or 0)
        live = self._live_pop(board_id)
        if live is not None:
            _terminate(live.proc, self.stop_timeout)
        elif verdict != "elsewhere":
            _kill_pid(pid, self.stop_timeout)
        self._drop_record(board_id)
        detail = reason or "stopped"
        self._release_ports(rec)
        self._publish(board_id, "down", pid=pid, detail=detail)
        return DebugStatus(state="down", pid=pid, detail=detail)

    def detect(self, session: BoardSession) -> str:
        """The first TAP's IDCODE ("0x6ba00477"). Never halts the core.

        With a session up it asks that OpenOCD (the board serves one JTAG client);
        otherwise it runs OpenOCD once: init, scan_chain, shutdown.
        """
        board_id = session.candidate.board_id
        adapter = self._adapter(session)
        with self._board_lock(board_id):
            rec, verdict, _ = self._inspect(board_id)
            if rec is not None and verdict in ("ours", "held"):
                tcl = int((rec.get("ports") or {}).get("tcl", 0))
                try:
                    ids = parse_idcodes(tcl_rpc(tcl, "scan_chain"))
                except OSError as exc:
                    raise ActionFailedError(f"the running OpenOCD did not answer: {exc}") from exc
                if ids:
                    return ids[0]
                raise NothingOnTargetError("the running OpenOCD sees no TAP on the chain")
            if rec is not None:
                raise HeldError(f"the debug session for {board_id} is {verdict}",
                                holder=self._holder(rec))
            binary = find_openocd()
            cfgs = tuple(adapter.openocd_config())
            argv = detect_argv(binary, adapter, cfgs)
            try:
                res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                     text=True, errors="replace", timeout=self.detect_timeout,
                                     **_popen_flags())
            except subprocess.TimeoutExpired as exc:
                raise UnreachableError(
                    f"OpenOCD detect did not finish within {self.detect_timeout:.0f}s",
                    hint="the board's JTAG server may be wedged") from exc
            text = (res.stdout or "") + (res.stderr or "")
            ids = parse_idcodes(text)
            if ids:
                return ids[0]
            raise classify_failure(text, res.returncode,
                                   target=getattr(adapter, "describe", lambda: "")())

    # -- starting -------------------------------------------------------------------------------

    def _adapter(self, session: BoardSession) -> DebugAdapter:
        adapter = getattr(session, "debug", None)
        if adapter is None:
            raise UnavailableError(CAPABILITY, "this board session has no debug route "
                                               "(it needs the Ethernet link to the shell)")
        return adapter

    def _pinned_base(self) -> int | None:
        if self.port_base is not None:
            return self.port_base
        env = os.environ.get(PORT_BASE_ENV)
        return int(env) if env else None

    def _candidates(self, board_id: str, prefer: DebugPorts | None) -> Iterator[DebugPorts]:
        pinned = self._pinned_base()
        if pinned is not None:
            block = DebugPorts.block(pinned)
            busy = [p for p in block.reserved() if port_in_use(p) or p in self._reserved]
            if busy:
                raise PortBoundError(
                    f"local port {busy[0]} (debug block {pinned}-{pinned + PORT_BLOCK - 1}) "
                    "is already in use",
                    hint=f"the base is pinned ({PORT_BASE_ENV} / port_base), so it is not "
                         "moved; free it or pin another base")
            yield block
            return
        if prefer is not None:
            yield prefer
        first = zlib.crc32(board_id.encode()) % PORT_SLOTS
        for i in range(PORT_SLOTS):
            yield DebugPorts.block(DEFAULT_PORT_BASE + PORT_BLOCK * ((first + i) % PORT_SLOTS))

    def _start(self, board_id: str, session: BoardSession, binary: str, adapter: DebugAdapter,
               cfgs: tuple[str, ...], post: tuple[str, ...], target: str,
               prefer: DebugPorts | None) -> DebugStatus:
        spawns = 0
        for ports in self._candidates(board_id, prefer):
            with self._guard:
                if any(p in self._reserved for p in ports.reserved()):
                    continue
                if any(port_in_use(p) for p in ports.reserved()):
                    continue                              # reallocation: next block
                self._reserved.update(ports.reserved())
            spawns += 1
            try:
                return self._spawn(board_id, session, up_argv(binary, adapter, cfgs, ports, post),
                                   ports, cfgs, target)
            except PortBoundError:
                self._release_ports({"ports": ports.as_dict()})
                if self._pinned_base() is not None or spawns >= 3:
                    raise
            except BaseException:
                self._release_ports({"ports": ports.as_dict()})
                raise
        raise PortBoundError(
            f"no free local port block in {DEFAULT_PORT_BASE}-"
            f"{DEFAULT_PORT_BASE + PORT_BLOCK * PORT_SLOTS - 1}",
            hint=f"pin a free base with {PORT_BASE_ENV}")

    def _release_ports(self, rec: dict[str, Any]) -> None:
        ports = rec.get("ports") or {}
        if "gdb" in ports:
            block = DebugPorts(int(ports["gdb"]), int(ports["telnet"]), int(ports["tcl"]))
            with self._guard:
                self._reserved.difference_update(block.reserved())

    def _spawn(self, board_id: str, session: BoardSession, argv: list[str], ports: DebugPorts,
               cfgs: tuple[str, ...], target: str) -> DebugStatus:
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_path(board_id)
        self._publish(board_id, "starting", ports=ports, config=cfgs)
        with open(log_path, "wb") as logf:
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=logf,
                                        stderr=subprocess.STDOUT, cwd=str(self.registry_dir),
                                        **_popen_flags())
            except OSError as exc:
                raise UnavailableError(CAPABILITY, f"cannot run {argv[0]}: {exc}") from exc
        record = {
            "board_id": board_id, "pid": proc.pid, "state": "starting",
            "ports": ports.as_dict(), "ports_command": ports.command(),
            "config": list(cfgs), "argv": argv, "log": str(log_path),
            "target": target, "started_at": time.time(),
            "owner": {"user": getpass.getuser(), "host": socket.gethostname(),
                      "pid": os.getpid()},
        }
        self._write_record(board_id, record)
        self._live_set(board_id, _Live(proc, ports, session, cfgs, board_id))
        try:
            self._wait_until_serving(board_id, proc, ports, target)
        except BaseException:
            self._live_pop(board_id)
            _terminate(proc, self.stop_timeout)
            self._drop_record(board_id)
            raise
        record["state"] = "up"
        self._write_record(board_id, record)
        detail = f"gdb 127.0.0.1:{ports.gdb}"
        self._publish(board_id, "up", ports=ports, pid=proc.pid, detail=detail, config=cfgs)
        return DebugStatus(state="up", gdb_port=ports.gdb, telnet_port=ports.telnet,
                           tcl_port=ports.tcl, config=cfgs, pid=proc.pid, detail=detail)

    def _log_text(self, board_id: str) -> str:
        try:
            return self.log_path(board_id).read_text(errors="replace")
        except OSError:
            return ""

    def _wait_until_serving(self, board_id: str, proc: subprocess.Popen, ports: DebugPorts,
                            target: str) -> None:
        deadline = time.monotonic() + self.start_timeout
        want = {(ports.gdb, "gdb"), (ports.telnet, "telnet"), (ports.tcl, "tcl")}
        while True:
            text = self._log_text(board_id)
            rc = proc.poll()
            if rc is not None or _BIND_RE.search(text):
                if rc is not None:
                    time.sleep(0.05)
                    text = self._log_text(board_id)
                raise classify_failure(text, rc, target=target)
            heard = {(int(p), svc) for p, svc in _LISTEN_RE.findall(text)}
            if want <= heard:
                try:
                    tcl_rpc(ports.tcl, "version", timeout=2.0)
                    return
                except OSError:
                    pass
            if time.monotonic() > deadline:
                raise ActionFailedError(
                    f"OpenOCD did not start serving within {self.start_timeout:.0f}s",
                    hint=f"log {self.log_path(board_id)}: {_last_errors(text)}")
            time.sleep(0.05)

    # -- swap awareness ---------------------------------------------------------------------

    def _on_deploy_started(self, event: Event) -> None:
        board_id = event.board_id
        with self._board_lock(board_id):
            live = self._live_get(board_id)
            if live is None or live.proc.poll() is not None:
                return
            self._resume[board_id] = (live.session, live.ports)
            self._down_locked(board_id, force=True,
                              reason="closed for a partition swap (the DAP goes away)")

    def _on_deploy_done(self, event: Event) -> None:
        board_id = event.board_id
        with self._board_lock(board_id):
            pending = self._resume.pop(board_id, None)
        if pending is None:
            return
        if not event.data.get("verified"):
            self._publish(board_id, "down",
                          detail="not reopened: the swap was not verified by the board")
            return
        session, ports = pending
        if session is None and self.engine is not None:
            try:
                session = self.engine.session(board_id)
            except HarnessError:
                session = None
        if session is None:
            self._publish(board_id, "down", detail="not reopened: the board session is gone")
            return
        worker = threading.Thread(target=self._reopen, args=(session, ports), daemon=True,
                                  name=f"debug-reopen-{board_id}")
        self.threads.append(worker)
        worker.start()

    def _reopen(self, session: BoardSession, ports: DebugPorts) -> None:
        try:
            self.up(session, _prefer=ports)
        except HarnessError as exc:
            # up() already published "failed"; say why it stays down in plain words.
            log.info("debug session not reopened after the swap: %s", exc)

    def _on_deploy_failed(self, event: Event) -> None:
        # A preflight refusal publishes deploy.failed with no deploy.started: nothing
        # was closed, so there is nothing pending and nothing to say.
        with self._board_lock(event.board_id):
            pending = self._resume.pop(event.board_id, None)
        if pending is not None:
            stage = event.data.get("stage") or "an unknown stage"
            reason = event.data.get("reason", "")
            self._publish(event.board_id, "down",
                          detail=f"not reopened: the swap failed at {stage}"
                                 f"{f' ({reason})' if reason else ''}; check what is loaded")

    # -- shutdown ---------------------------------------------------------------------------

    def close(self) -> None:
        """Stop every OpenOCD this service started and stop listening for events."""
        for board_id in self._live_boards():
            with self._board_lock(board_id):
                try:
                    self._down_locked(board_id, force=True, reason="engine closed")
                except HarnessError:
                    log.exception("closing the debug session for %s", board_id)
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()


def _popen_flags() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}
