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
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import ActionFailedError, HeldError


def default_lock_dir() -> Path:
    base = os.environ.get("HARNESS_MANAGER_STATE_DIR")
    if base:
        return Path(base) / "locks"
    return Path.home() / ".config" / "harness-manager" / "locks"


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
    return not _zombie(pid)


def _zombie(pid: int) -> bool:
    """True for a process that has exited but whose parent has not reaped it yet.

    ``kill(pid, 0)`` succeeds on a zombie, yet it runs nothing and holds nothing (no
    fds, no ports, no locks). A daemon started by a front-end that stays open (the
    app window) is such a zombie from the moment it exits until that front-end polls
    it, and counting it as alive made ``daemon stop`` wait, signal it, then report
    it "did not stop"; a new daemon then refused to start (Q2, 2026-09-24).
    Linux reads ``/proc/<pid>/stat``. macOS and the BSDs ask ``ps`` (install lane Q3:
    CI's macOS run left a SIGTERMed daemon a zombie of the test that started it, and
    ``daemon stop`` reported "did not stop"). Windows cannot tell, and says no.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return _zombie_by_ps(pid)
    try:
        return stat.rsplit(")", 1)[1].split()[0] == "Z"
    except IndexError:
        return False


def _zombie_by_ps(pid: int) -> bool:
    if os.name == "nt" or sys.platform.startswith("linux"):
        return False          # Linux without /proc for it: gone, or cannot tell
    import subprocess

    try:
        res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0 and res.stdout.strip().startswith("Z")


#: Public name (T4-6): other modules need the same Windows-safe liveness check.
pid_alive = _pid_alive


def storage_error(path: Path, exc: OSError) -> ActionFailedError:
    """A lock or state file that cannot be written, as a message with the next step.

    It is the machine, not a bug: the CLI used to print "internal error: PermissionError"
    and the daemon a traceback (Q2, 2026-09-24).
    """
    import errno

    if exc.errno == errno.ENOSPC:
        why, hint = "the disk is full", f"free space on the disk holding {path}"
    elif exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        why, hint = "it is not writable", ("point HARNESS_MANAGER_STATE_DIR at a directory "
                                           "you can write")
    else:
        why = exc.strerror or str(exc)
        hint = "check the state directory (HARNESS_MANAGER_STATE_DIR)"
    return ActionFailedError(f"cannot write {path}: {why}", hint=hint)


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
        try:
            self.lock_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise storage_error(self.lock_dir, exc) from None
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
            except OSError as exc:
                raise storage_error(self.path, exc) from None
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(me.__dict__, fh)
            except OSError as exc:        # a full disk: never leave an empty lock behind
                self.path.unlink(missing_ok=True)
                raise storage_error(self.path, exc) from None
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
