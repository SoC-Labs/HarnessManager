"""Start, stop and ask about socharnessd, for the CLI verbs. Stdlib only.

- ``start`` launches ``python -m socharness.daemon`` DETACHED: a new session on
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

from socharness.core.errors import (
    ActionFailedError,
    AlreadyError,
    HarnessError,
    UnreachableError,
)
from socharness.core.session import pid_alive

from .state import (
    LOOPBACK,
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


def _spawn(state_dir: Path, port: int, listen: str, demo: bool = False) -> subprocess.Popen:
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = daemon_log_path(state_dir)
    fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    cmd = [sys.executable, "-m", "socharness.daemon", "--state-dir", str(state_dir),
           "--port", str(port), "--listen", listen] + (["--demo"] if demo else [])
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": fd,
                              "stderr": subprocess.STDOUT, "cwd": str(state_dir),
                              "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        with os.fdopen(fd, "ab") as log:
            log.write(f"--- socharness daemon start: {' '.join(cmd)}\n".encode())
            log.flush()
            kwargs["stdout"] = log
            child = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        raise ActionFailedError(f"cannot start socharnessd: {exc}",
                                hint=f"check that {sys.executable} can run") from exc
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


def start(state_dir: Path, *, port: int = 0, listen: str = LOOPBACK,
          wait_s: float = START_WAIT_S, demo: bool = False) -> DaemonInfo:
    """Launch a detached daemon and wait until it answers. ``AlreadyError`` if one runs."""
    state_dir = Path(state_dir)
    current = running(state_dir)
    if current is not None:
        raise AlreadyError(f"socharnessd is already running (pid {current.pid}, "
                           f"{current.base_url})",
                           hint="`socharness daemon stop` stops it")
    child = _spawn(state_dir, port, listen, demo)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        code = child.poll()
        if code is not None:
            raise ActionFailedError(
                f"socharnessd exited while starting (exit code {code})",
                hint=f"{daemon_log_path(state_dir)}: {_log_tail(state_dir) or 'no log'}")
        info = read_info(state_dir)
        if info is not None and info.pid == child.pid and health(info) is not None:
            return info
        time.sleep(0.05)
    raise ActionFailedError(f"socharnessd did not answer within {wait_s:g}s (pid {child.pid})",
                            hint=f"see {daemon_log_path(state_dir)}")


def ensure_running(state_dir: Path, *, port: int = 0, listen: str = LOOPBACK,
                   demo: bool = False) -> tuple[DaemonInfo, bool]:
    """(the running daemon, whether this call started it)."""
    current = running(Path(state_dir))
    if current is not None:
        return current, False
    return start(Path(state_dir), port=port, listen=listen, demo=demo), True


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
        raise AlreadyError("socharnessd is not running", hint="nothing to stop")
    if not info.on_this_host() or _gone(info.pid):
        if info.on_this_host():
            daemon_json_path(state_dir).unlink(missing_ok=True)
            return "stale-removed"
        raise AlreadyError(f"socharnessd for {state_dir} runs on {info.hostname}, not here",
                           hint=f"stop it on {info.hostname}")
    try:
        code, payload = _request(info, "POST", "/api/v1/daemon/shutdown",
                                 body={"force": force}, timeout=5.0)
    except (OSError, http.client.HTTPException):
        code, payload = 0, {}
    if code and code != 200:
        from socharness.client.codec import error_from_json

        err = payload.get("error") if isinstance(payload, dict) else None
        if err:
            raise error_from_json(err)
        raise UnreachableError(f"socharnessd refused to stop (HTTP {code})")
    if _wait_gone(info.pid, timeout):
        return "stopped"
    # It did not go (or never answered): terminate it. Board locks it held go stale
    # and are taken over by the next owner; daemon.json is removed here.
    try:
        os.kill(info.pid, signal.SIGTERM)     # TerminateProcess on Windows
    except OSError:
        pass
    if not _wait_gone(info.pid, 5.0):
        raise ActionFailedError(f"socharnessd (pid {info.pid}) did not stop",
                                hint=f"stop pid {info.pid} by hand")
    daemon_json_path(state_dir).unlink(missing_ok=True)
    return "stopped"


def describe(info: DaemonInfo) -> str:
    return f"socharnessd at {info.base_url} (pid {info.pid})"


__all__ = ["HarnessError", "describe", "ensure_running", "health", "running", "start",
           "status", "stop"]
