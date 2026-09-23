"""Local session lock: one Harness Manager process owns a board at a time.

The board's own ports accept one client each, so two local processes that
both think they own a board will break each other's swaps and consoles. This
lock is the standalone counterpart of the fpgahub lease.

It follows the rules from the HAPS work:

- the lock is atomic (``O_CREAT|O_EXCL``);
- the holder is named;
- a lock whose PID is gone on this host is *stale* and may be taken over;
- we only ever release a lock we created.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import HeldError


def default_lock_dir() -> Path:
    base = os.environ.get("SOCHARNESS_STATE_DIR")
    if base:
        return Path(base) / "locks"
    return Path.home() / ".config" / "socharness" / "locks"


def _safe_name(board_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in board_id)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows sends CTRL_C_EVENT (== 0): never use it there.
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5      # ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                 # STILL_ACTIVE
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
    return True


#: A lock file with no readable owner may simply be mid-write by its creator.
#: Only treat it as stale once it is older than this.
TORN_LOCK_GRACE_S = 5.0


@dataclass(frozen=True)
class LockOwner:
    user: str
    host: str
    pid: int
    since: float
    note: str = ""

    def describe(self) -> str:
        age = int(time.time() - self.since)
        return f"{self.user} on {self.host} (pid {self.pid}, {age}s)" + (
            f": {self.note}" if self.note else ""
        )


class SessionLock:
    def __init__(self, board_id: str, *, lock_dir: Path | None = None, note: str = "") -> None:
        self.board_id = board_id
        self.lock_dir = lock_dir or default_lock_dir()
        self.path = self.lock_dir / f"{_safe_name(board_id)}.lock"
        self.note = note
        self._held = False

    def owner(self) -> LockOwner | None:
        try:
            data = json.loads(self.path.read_text())
            return LockOwner(**data)
        except FileNotFoundError:
            return None
        except (ValueError, TypeError):
            # A torn/corrupt lock file names nobody: treat as stale.
            return LockOwner(user="?", host=socket.gethostname(), pid=-1, since=0.0)

    def _is_stale(self, owner: LockOwner) -> bool:
        if owner.pid == -1:
            # Torn or empty: its creator may still be writing it (O_EXCL, then write).
            try:
                return time.time() - self.path.stat().st_mtime > TORN_LOCK_GRACE_S
            except FileNotFoundError:
                return True
        return owner.host == socket.gethostname() and not _pid_alive(owner.pid)

    def acquire(self) -> None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        me = LockOwner(
            user=os.environ.get("USER") or os.environ.get("USERNAME") or "user",
            host=socket.gethostname(),
            pid=os.getpid(),
            since=time.time(),
            note=self.note,
        )
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                owner = self.owner()
                if owner is None:
                    continue  # vanished between open and read: retry once
                if owner.pid == me.pid and owner.host == me.host:
                    self._held = True
                    return
                if self._is_stale(owner):
                    self.path.unlink(missing_ok=True)
                    continue
                raise HeldError(
                    f"{self.board_id} is in use",
                    holder=owner.describe(),
                    hint=f"held by {owner.describe()}",
                ) from None
            with os.fdopen(fd, "w") as fh:
                json.dump(me.__dict__, fh)
            self._held = True
            return
        raise HeldError(f"{self.board_id} lock could not be taken", hint="retry")

    def release(self) -> None:
        if not self._held:
            return
        owner = self.owner()
        if owner is not None and owner.pid == os.getpid() and owner.host == socket.gethostname():
            self.path.unlink(missing_ok=True)
        self._held = False

    def __enter__(self) -> SessionLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
