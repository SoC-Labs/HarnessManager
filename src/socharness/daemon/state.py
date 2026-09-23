"""socharnessd's files in the state dir. Stdlib only: the CLI imports this.

- ``daemon.json`` (mode 0600): ``{pid, port, token, started_at, version}`` as
  docs/API.md fixes it, plus ``host`` (the address a client connects to) and
  ``hostname`` (the machine it runs on). It is written with write-then-rename,
  so a reader never sees half of it, and it is removed on a clean shutdown.
- ``socharnessd.lock``: the single-instance lock. It is a ``SessionLock``
  (atomic create, named holder, stale-pid takeover, only our own lock is
  released), so one daemon runs per state dir and a crashed one never blocks
  the next.
- ``daemon.log``: where a detached daemon writes its log.

A ``daemon.json`` whose pid is gone, or which was written on another machine
(a state dir on a shared home directory), names no running daemon here:
``discover`` returns ``None`` for it.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from socharness.core.errors import HeldError
from socharness.core.session import SessionLock, pid_alive

DAEMON_JSON = "daemon.json"
DAEMON_LOG = "daemon.log"
LOCK_ID = "socharnessd"
LOOPBACK = "127.0.0.1"


def default_state_dir() -> Path:
    """The engine's rule: ``$SOCHARNESS_STATE_DIR``, else ``~/.config/socharness``."""
    base = os.environ.get("SOCHARNESS_STATE_DIR")
    return Path(base) if base else Path.home() / ".config" / "socharness"


def daemon_json_path(state_dir: Path) -> Path:
    return Path(state_dir) / DAEMON_JSON


def daemon_log_path(state_dir: Path) -> Path:
    return Path(state_dir) / DAEMON_LOG


def connect_host(listen: str) -> str:
    """Where a local client connects for a daemon listening on ``listen``."""
    return LOOPBACK if listen in ("", "0.0.0.0", "::", "localhost") else listen


def is_loopback(listen: str) -> bool:
    return listen in ("127.0.0.1", "::1", "localhost") or listen.startswith("127.")


@dataclass(frozen=True)
class DaemonInfo:
    pid: int
    port: int
    token: str
    started_at: float
    version: str
    host: str = LOOPBACK
    hostname: str = ""

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def ui_url(self) -> str:
        """The browser URL. The token rides in the fragment, which is never sent to a server."""
        return f"{self.base_url}/#token={self.token}"

    def on_this_host(self) -> bool:
        return not self.hostname or self.hostname == socket.gethostname()

    def alive(self) -> bool:
        return self.on_this_host() and pid_alive(self.pid)


def read_info(state_dir: Path) -> DaemonInfo | None:
    """What ``daemon.json`` says, or ``None`` if it is absent or unreadable."""
    try:
        data = json.loads(daemon_json_path(state_dir).read_text(encoding="utf-8"))
        return DaemonInfo(
            pid=int(data["pid"]), port=int(data["port"]), token=str(data["token"]),
            started_at=float(data.get("started_at", 0.0)), version=str(data.get("version", "")),
            host=str(data.get("host") or LOOPBACK), hostname=str(data.get("hostname", "")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_info(state_dir: Path, info: DaemonInfo) -> Path:
    """Write ``daemon.json`` atomically with mode 0600 (it holds the token)."""
    path = daemon_json_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{DAEMON_JSON}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(info), fh, indent=2, sort_keys=True)
            fh.write("\n")
        if os.name != "nt":
            os.chmod(tmp, 0o600)          # umask cannot widen it, but be explicit
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def remove_info(state_dir: Path, pid: int) -> bool:
    """Remove ``daemon.json`` only if it names ``pid`` (never another daemon's file)."""
    info = read_info(state_dir)
    if info is None or info.pid != pid:
        return False
    daemon_json_path(state_dir).unlink(missing_ok=True)
    return True


def discover(state_dir: Path | None = None) -> DaemonInfo | None:
    """The daemon ``daemon.json`` names, if its process is alive on this machine.

    File and pid only: no network. A caller that needs to know it answers
    asks ``/api/v1/health`` (``control.health``).
    """
    info = read_info(Path(state_dir) if state_dir is not None else default_state_dir())
    if info is None or not info.alive():
        return None
    return info


class DaemonInstance:
    """One daemon per state dir: the ``SessionLock`` rules on ``<state_dir>/socharnessd.lock``."""

    def __init__(self, state_dir: Path, note: str = "") -> None:
        self.state_dir = Path(state_dir)
        self.lock = SessionLock(LOCK_ID, lock_dir=self.state_dir, note=note or "socharnessd")

    @property
    def path(self) -> Path:
        return self.lock.path

    def acquire(self) -> None:
        try:
            self.lock.acquire()
        except HeldError as exc:
            owner = self.lock.owner()
            who = owner.describe() if owner else exc.holder or "another process"
            raise HeldError(
                f"socharnessd is already running for {self.state_dir} ({who})",
                holder=who,
                hint="`socharness daemon status` shows it; `socharness daemon stop` stops it") \
                from None

    def release(self) -> None:
        self.lock.release()

    def __enter__(self) -> DaemonInstance:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def new_info(*, port: int, token: str, version: str, listen: str = LOOPBACK) -> DaemonInfo:
    return DaemonInfo(pid=os.getpid(), port=port, token=token, started_at=time.time(),
                      version=version, host=connect_host(listen),
                      hostname=socket.gethostname())
