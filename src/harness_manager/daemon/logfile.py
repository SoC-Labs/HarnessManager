"""daemon.log rotation: a size cap and a few backups.

Install lane Q3, from lane Q2's soak (2026-09-24): daemon.log grew 0.5-0.6 MB an hour
under console churn and nothing ever trimmed it. Two places rotate it:

- ``daemon start`` (``control._spawn``), before the new daemon opens it;
- the running daemon, once a minute (``LogRotator``). Its stdout and stderr ARE
  daemon.log (the start redirects them), so it renames the file and points fds 1 and
  2 at a fresh one with ``dup2``. Every writer (logging, uvicorn, a stray print) follows.
  It only does that when fd 1 really is daemon.log: a ``--foreground`` daemon writes to
  a terminal and is left alone. On Windows an open file cannot be renamed; the running
  daemon's rotation then does nothing and the next start rotates.

``daemon.log`` -> ``daemon.log.1`` -> ... -> ``daemon.log.<backups>``; the oldest drops.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_BACKUPS = 3
CHECK_EVERY_S = 60.0

log = logging.getLogger("harness_manager.daemon")


def backup_path(path: Path, n: int) -> Path:
    return path.with_name(f"{path.name}.{n}")


def rotate(path: Path, *, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS) -> bool:
    """Shift ``path`` to ``path.1`` (and each backup up one) if it is over ``max_bytes``.

    True if it rotated. An OSError (a read-only or vanished file) propagates.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size <= max_bytes:
        return False
    backups = max(1, backups)
    for n in range(backups - 1, 0, -1):
        older = backup_path(path, n)
        if older.exists():
            os.replace(older, backup_path(path, n + 1))
    os.replace(path, backup_path(path, 1))
    return True


def _same_file(fd: int, path: Path) -> bool:
    try:
        a, b = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def rotate_running(path: Path, *, max_bytes: int = LOG_MAX_BYTES,
                   backups: int = LOG_BACKUPS) -> bool:
    """In the daemon: rotate the log that its stdout and stderr write to. True if it did."""
    if not _same_file(1, path):
        return False                     # a foreground daemon, or someone moved the file
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError, AttributeError):
            pass
    try:
        if not rotate(path, max_bytes=max_bytes, backups=backups):
            return False
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    except OSError:
        return False                     # Windows (open file), or the disk: try next time
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)
    log.info("daemon.log rotated (over %d bytes); the previous one is %s", max_bytes,
             backup_path(path, 1).name)
    return True


class LogRotator:
    """Checks the running daemon's log every ``every_s`` seconds, on a daemon thread."""

    def __init__(self, path: Path, *, every_s: float = CHECK_EVERY_S,
                 max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS) -> None:
        self.path = Path(path)
        self.every_s = every_s
        self.max_bytes = max_bytes
        self.backups = backups
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="daemon-log-rotator",
                                        daemon=True)

    def start(self) -> LogRotator:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.every_s):
            try:
                rotate_running(self.path, max_bytes=self.max_bytes, backups=self.backups)
            except Exception:  # noqa: BLE001 - never let the log take the daemon down
                pass


__all__ = ["LOG_BACKUPS", "LOG_MAX_BYTES", "LogRotator", "backup_path", "rotate",
           "rotate_running"]
