"""Running a short-lived tool the way every probe should (REVIEW-W5 12, 14).

- ``no_window()``: the ``Popen`` keyword that keeps Windows from flashing a console window
  for a child (``CREATE_NO_WINDOW``); nothing elsewhere. Spread it into every
  ``subprocess.run``/``Popen`` a background or UI action makes.
- ``run_to_file(argv, timeout)``: the output goes to a file, not a pipe (a child the tool
  leaves behind cannot hold the probe open), the child runs in a session (POSIX) or process
  group (Windows) of its own, and on timeout the whole group is killed.
- ``probe_run``: ``run_to_file`` behind ``subprocess.run``'s shape (``capture_output``,
  ``text``, ``timeout`` -> ``CompletedProcess``, or ``TimeoutExpired``), for code written
  against ``subprocess.run`` (the Settings menu's tool Detect, ``openocd_probe``'s seam).

Never a shell; stdin is ``/dev/null``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from typing import Any

__all__ = ["no_window", "probe_run", "run_to_file"]


def no_window() -> dict[str, Any]:
    """``{"creationflags": CREATE_NO_WINDOW}`` on Windows, else ``{}``."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _group_flags() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}             # a wrapper's children die with it
    if sys.platform == "win32":
        return {"creationflags": no_window().get("creationflags", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {}


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)         # start_new_session: the group is ours
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(5.0)


def run_to_file(argv: Sequence[str], timeout: float, *,
                cwd: str | os.PathLike[str] | None = None) -> tuple[int | None, str]:
    """``(exit code, stdout + stderr)``; ``None`` for the code when it timed out (the group
    was killed). ``OSError`` when it cannot start. ``cwd``: where the child runs (a tool
    that drops files where it starts, such as Vivado, is given a scratch directory)."""
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, cwd=cwd, **_group_flags())
        try:
            rc: int | None = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            rc = None
        out.seek(0)
        return rc, out.read().decode(errors="replace")


def probe_run(argv: Sequence[str], *, timeout: float | None = None, text: bool = True,
              **_ignored: Any) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run(argv, capture_output=True, text=True, timeout=...)`` done the probe
    way (``run_to_file``). stdout carries stdout and stderr together; stderr is "".
    ``subprocess.TimeoutExpired`` on timeout (after the group is killed)."""
    limit = 60.0 if timeout is None else float(timeout)
    rc, out = run_to_file(argv, limit)
    if rc is None:
        raise subprocess.TimeoutExpired(list(argv), limit, output=out)
    return subprocess.CompletedProcess(list(argv), rc, out if text else out.encode(),
                                       "" if text else b"")
