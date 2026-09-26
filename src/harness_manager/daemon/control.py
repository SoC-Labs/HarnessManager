"""Start, stop and ask about harness-manager-daemon, for the CLI verbs. Stdlib only.

- ``start`` launches ``python -m harness_manager.daemon`` DETACHED: a new session on
  POSIX (``start_new_session``), ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``
  on Windows (no fork anywhere), with stdin closed and stdout/stderr appended to
  ``<state_dir>/daemon.log``. It returns once the new daemon has written
  ``daemon.json`` AND answered ``/api/v1/health`` with its own pid.
- ``stop`` asks the daemon to shut down over the API (a clean exit on every OS),
  refused while a job runs unless ``force``; then waits for the pid to go, and
  only if it will not, terminates it.
- ``status`` reads ``daemon.json`` and asks ``/api/v1/health``.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    HarnessError,
    UnreachableError,
)
from harness_manager.core.session import pid_alive, storage_error

from . import logfile
from .state import (
    DaemonInfo,
    daemon_json_path,
    daemon_log_path,
    read_info,
)

START_WAIT_S = 30.0
STOP_WAIT_S = 10.0
HEALTH_TIMEOUT_S = 1.0

#: Daemons this process launched. A child that exited stays a zombie (and looks alive
#: to a pid check) until its parent reaps it, which only matters when the parent lives
#: on, as a test run or a long-lived front-end does.
_CHILDREN: dict[int, subprocess.Popen] = {}

# Windows process-creation flags (subprocess only defines them on Windows).
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def _request(info: DaemonInfo, method: str, path: str, *, body: Any = None,
             auth: bool = True, timeout: float = HEALTH_TIMEOUT_S) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection(info.host, info.port, timeout=timeout)
    headers = {"Accept": "application/json"}
    data = None
    if auth:
        headers["Authorization"] = f"Bearer {info.token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        return resp.status, payload if isinstance(payload, dict) else {}
    finally:
        conn.close()


def health(info: DaemonInfo, timeout: float = HEALTH_TIMEOUT_S) -> dict[str, Any] | None:
    """``/api/v1/health`` from the daemon ``info`` names, or ``None`` if it does not answer as it."""
    try:
        status, payload = _request(info, "GET", "/api/v1/health", auth=False, timeout=timeout)
    except (OSError, http.client.HTTPException):
        return None
    if status != 200 or not payload.get("ok") or payload.get("pid") not in (None, info.pid):
        return None
    return payload


def status(state_dir: Path) -> dict[str, Any]:
    """``{state: running | unresponsive | stale | stopped, ...}`` without the token."""
    info = read_info(state_dir)
    out: dict[str, Any] = {"state": "stopped", "state_dir": str(state_dir), "pid": None,
                           "port": None, "url": None, "version": None, "started_at": None,
                           "boards_open": None}
    if info is None:
        return out
    out.update(pid=info.pid, port=info.port, url=info.base_url, version=info.version,
               started_at=info.started_at)
    if not info.on_this_host():
        out["state"] = "stale"
        out["detail"] = f"daemon.json was written on {info.hostname}, not this machine"
        return out
    if _gone(info.pid):
        out["state"] = "stale"
        out["detail"] = f"pid {info.pid} is gone; daemon.json is left over"
        return out
    if health(info) is None:
        out["state"] = "unresponsive"
        out["detail"] = f"pid {info.pid} is alive but {info.base_url} does not answer"
        return out
    out["state"] = "running"
    try:
        code, payload = _request(info, "GET", "/api/v1/boards", timeout=5.0)
        if code == 200:
            out["boards_open"] = sum(1 for b in payload.get("boards", []) if b.get("open"))
    except (OSError, http.client.HTTPException):
        pass
    return out


def running(state_dir: Path) -> DaemonInfo | None:
    """The daemon for ``state_dir`` if it is alive here AND answers health."""
    info = read_info(state_dir)
    if info is None or not info.alive() or health(info) is None:
        return None
    return info


def daemon_python(*, windows: bool = os.name == "nt", executable: str = "",
                  base: str = "") -> tuple[str, dict[str, str] | None]:
    """The Python to run the daemon with, and its environment (``None``: inherit ours).

    Windows: a venv's ``python.exe`` (every install.ps1 install) is a redirector that
    runs the base interpreter as a CHILD process, so ``Popen.pid`` would be the
    redirector's and never the pid the daemon writes to daemon.json, which ``start``
    waits for. Start the base interpreter itself, as multiprocessing does (bpo-35797):
    ``__PYVENV_LAUNCHER__`` makes it the venv's Python.
    """
    executable = executable or sys.executable
    base = base or getattr(sys, "_base_executable", "") or executable
    if windows and os.path.normcase(base) != os.path.normcase(executable):
        return base, dict(os.environ, __PYVENV_LAUNCHER__=executable)
    return executable, None


def service_argv(state_dir: Path, port: int | None = None, listen: str | None = None,
                 demo: bool = False, *, log_level: str | None = None,
                 pack_overrides: dict[str, Any] | None = None) -> list[str]:
    """The service's own flags, as ``daemon start`` passes them on. A flag left None is not
    passed, so the service reads its setting (``advanced.port``/``listen``/``log_level``,
    lane SET-WIRE); ``--pack-overrides`` (the developers' pack kwargs) and ``--log-level``
    reach the service too (SETTINGS.md §12.7)."""
    out = ["--state-dir", str(state_dir)]
    if port is not None:
        out += ["--port", str(port)]
    if listen is not None:
        out += ["--listen", listen]
    if log_level is not None:
        out += ["--log-level", log_level]
    if pack_overrides:
        out += ["--pack-overrides", json.dumps(pack_overrides, sort_keys=True)]
    return out + (["--demo"] if demo else [])


def _spawn(state_dir: Path, port: int | None, listen: str | None, demo: bool = False, *,
           log_level: str | None = None,
           pack_overrides: dict[str, Any] | None = None) -> subprocess.Popen:
    log_path = daemon_log_path(state_dir)
    # Install lane Q3 (from Q2): rotate daemon.log (size cap, backups), and a state dir
    # that cannot be written is a message (exit 6), not "internal error: PermissionError".
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        logfile.rotate(log_path)
        fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    except OSError as exc:
        err = storage_error(Path(exc.filename) if exc.filename else log_path, exc)
        raise ActionFailedError(f"cannot start harness-manager-daemon: {err.message}",
                                hint=err.hint) from None
    python, env = daemon_python()
    cmd = [python, "-m", "harness_manager.daemon",
           *service_argv(state_dir, port, listen, demo, log_level=log_level,
                         pack_overrides=pack_overrides)]
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": fd,
                              "stderr": subprocess.STDOUT, "cwd": str(state_dir),
                              "close_fds": True, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        with os.fdopen(fd, "ab") as log:
            log.write(f"--- harness-manager daemon start: {' '.join(cmd)}\n".encode())
            log.flush()
            kwargs["stdout"] = log
            child = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        raise ActionFailedError(f"cannot start harness-manager-daemon: {exc}",
                                hint=f"check that {python} can run") from exc
    _CHILDREN[child.pid] = child
    return child


def _gone(pid: int) -> bool:
    child = _CHILDREN.get(pid)
    if child is not None and child.poll() is not None:
        _CHILDREN.pop(pid, None)
        return True
    return not pid_alive(pid)


def _log_tail(state_dir: Path, lines: int = 5) -> str:
    try:
        text = daemon_log_path(state_dir).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " | ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip())


def start(state_dir: Path, *, port: int | None = None, listen: str | None = None,
          wait_s: float = START_WAIT_S, demo: bool = False, log_level: str | None = None,
          pack_overrides: dict[str, Any] | None = None) -> DaemonInfo:
    """Launch a detached daemon and wait until it answers. ``AlreadyError`` if one runs.
    ``port``/``listen``/``log_level`` None: the service reads its settings."""
    state_dir = Path(state_dir)
    current = running(state_dir)
    if current is not None:
        raise AlreadyError(f"harness-manager-daemon is already running (pid {current.pid}, "
                           f"{current.base_url})",
                           hint="`harness-manager daemon stop` stops it")
    child = _spawn(state_dir, port, listen, demo, log_level=log_level,
                   pack_overrides=pack_overrides)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        code = child.poll()
        if code is not None:
            raise ActionFailedError(
                f"harness-manager-daemon exited while starting (exit code {code})",
                hint=f"{daemon_log_path(state_dir)}: {_log_tail(state_dir) or 'no log'}")
        info = read_info(state_dir)
        if info is not None and info.pid == child.pid and health(info) is not None:
            return info
        time.sleep(0.05)
    raise ActionFailedError(f"harness-manager-daemon did not answer within {wait_s:g}s (pid {child.pid})",
                            hint=f"see {daemon_log_path(state_dir)}")


def ensure_running(state_dir: Path, *, port: int | None = None, listen: str | None = None,
                   demo: bool = False) -> tuple[DaemonInfo, bool]:
    """(the running daemon, whether this call started it)."""
    current = running(Path(state_dir))
    if current is not None:
        return current, False
    return start(Path(state_dir), port=port, listen=listen, demo=demo), True


DAEMON_MODULE = "harness_manager.daemon"


def _cmdline(pid: int) -> list[str] | None:
    """The argv of ``pid``: /proc on Linux, ``ps`` on other POSIX systems; ``None`` where
    neither can tell (Windows, a vanished process, no ``ps``)."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        raw = None
    if raw is not None:
        return [a.decode(errors="replace") for a in raw.split(b"\0") if a] or None
    if os.name == "nt" or sys.platform.startswith("linux"):
        return None
    try:
        res = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.split() if res.returncode == 0 and res.stdout.strip() else None


def _same_dir(arg: str, state_dir: Path) -> bool:
    mine = os.path.realpath(state_dir)
    if os.path.isabs(arg):
        return os.path.realpath(arg) == mine
    return mine.endswith(os.sep + os.path.normpath(arg))   # started with a relative dir


def is_our_daemon(info: DaemonInfo, state_dir: Path) -> bool | None:
    """Is ``info.pid`` really this state dir's harness-manager-daemon? ``None``: cannot tell.

    Install lane Q3 (from lane Q1's finding): a pid in a leftover daemon.json may since
    belong to another process. ``daemon stop`` (and so every upgrade, which stops the
    service first) must never signal that one. The daemon answering /health with this
    pid settles it; else its command line: ``-m harness_manager.daemon --state-dir DIR``,
    or ``harness-manager daemon start --foreground``.
    """
    payload = health(info)
    if payload is not None and payload.get("pid") == info.pid:
        return True
    argv = _cmdline(info.pid)
    if argv is None:
        return None
    if DAEMON_MODULE in argv:
        if "--state-dir" not in argv[:-1]:
            return False
        return _same_dir(argv[argv.index("--state-dir") + 1], state_dir)
    if all(w in argv for w in ("daemon", "start", "--foreground")) and \
            any(Path(a).name.startswith("harness-manager") for a in argv[:2]):
        return True                      # the foreground daemon: its state dir is its env's
    return False


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return _gone(pid)


def stop(state_dir: Path, *, force: bool = False, timeout: float = STOP_WAIT_S) -> str:
    """Stop the daemon: ``"stopped"``, or ``"stale-removed"`` for a leftover daemon.json."""
    state_dir = Path(state_dir)
    info = read_info(state_dir)
    if info is None:
        raise AlreadyError("harness-manager-daemon is not running", hint="nothing to stop")
    if not info.on_this_host() or _gone(info.pid):
        if info.on_this_host():
            daemon_json_path(state_dir).unlink(missing_ok=True)
            return "stale-removed"
        raise AlreadyError(f"harness-manager-daemon for {state_dir} runs on {info.hostname}, not here",
                           hint=f"stop it on {info.hostname}")
    if is_our_daemon(info, state_dir) is False:
        # The pid was reused by another process: our daemon is gone. Never signal it.
        daemon_json_path(state_dir).unlink(missing_ok=True)
        return "stale-removed"
    try:
        code, payload = _request(info, "POST", "/api/v1/daemon/shutdown",
                                 body={"force": force}, timeout=5.0)
    except (OSError, http.client.HTTPException):
        code, payload = 0, {}
    if code and code != 200:
        from harness_manager.client.codec import error_from_json

        err = payload.get("error") if isinstance(payload, dict) else None
        if err:
            raise error_from_json(err)
        raise UnreachableError(f"harness-manager-daemon refused to stop (HTTP {code})")
    if _wait_gone(info.pid, timeout):
        return "stopped"
    # It did not go (or never answered): terminate it. Board locks it held go stale
    # and are taken over by the next owner; daemon.json is removed here. Only when the
    # pid is still, provably, this daemon: never a process that reused the pid.
    ours = is_our_daemon(info, state_dir)
    if ours is False:
        daemon_json_path(state_dir).unlink(missing_ok=True)
        return "stopped"
    if ours is None:
        raise ActionFailedError(
            f"harness-manager-daemon (pid {info.pid}) did not stop, and pid {info.pid} "
            "cannot be checked to be it on this system",
            hint=f"if pid {info.pid} is harness-manager-daemon, stop it by hand")
    try:
        os.kill(info.pid, signal.SIGTERM)     # TerminateProcess on Windows
    except OSError:
        pass
    if not _wait_gone(info.pid, 5.0):
        raise ActionFailedError(f"harness-manager-daemon (pid {info.pid}) did not stop",
                                hint=f"stop pid {info.pid} by hand")
    daemon_json_path(state_dir).unlink(missing_ok=True)
    return "stopped"


def describe(info: DaemonInfo) -> str:
    return f"harness-manager-daemon at {info.base_url} (pid {info.pid})"


__all__ = ["HarnessError", "describe", "ensure_running", "health", "running", "start",
           "status", "stop"]
