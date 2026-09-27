"""No test process outlives its test: find, stop and guard against processes by what they NAME.

Every daemon and apply helper a test starts is detached on purpose (a new session, as a real
service is), so a process-group kill of the test never reaches it, and its parent (the helper)
may be gone. What they all carry is their state dir on the command line (``--state-dir DIR``,
``--resume DIR/update/resume.json``). So:

- ``naming(marker)``: this user's processes whose command line contains ``marker`` (never
  this process or its ancestors: a shell that mentions the path is not a daemon);
- ``sweep(marker, first=...)``: SIGTERM, then SIGKILL, each daemon or apply helper among them
  (``python -m harness_manager.daemon…``; ``first`` ones before the rest: the apply helper,
  which would otherwise start or roll back to another daemon), again until none is left.
  Only a daemon naming a marker the caller made is ever signalled, never a ``tail`` of its log;
- ``track(path)`` / ``session_markers()``: the ``/tmp/otad-*`` installs this session made, for
  the session guard in ``tests/conftest.py`` (a daemon under one, or under this session's pytest
  tmp dir, still alive at the end fails the run);
- ``start_reaper(base)``: a detached watcher for when the test process itself is killed (a gate
  or tool timeout: no teardown runs at all). When that process is gone and ``base`` still
  exists, it stops every daemon naming a path inside ``base`` and removes ``base``. It exits
  once ``base`` is removed (a normal teardown). ``base`` goes in its environment, so no sweep
  names it.

Linux (``/proc``) only; elsewhere every function finds nothing.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DAEMON_MODULE = "harness_manager.daemon"       # the daemon, and harness_manager.daemon.update_apply
_TRACKED: list[str] = []


def _ancestors() -> set[int]:
    pids, pid = set(), os.getpid()
    for _ in range(64):
        pids.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            break
    return pids


def naming(marker: str) -> list[tuple[int, str]]:
    """``[(pid, command line)]`` of this user's processes naming ``marker``: never this one
    or an ancestor of it (the shell that ran pytest may mention the path)."""
    if not marker or not os.path.isdir("/proc"):
        return []
    mine, uid = _ancestors(), os.getuid()
    out: list[tuple[int, str]] = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in mine:
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid != uid:
                continue
            raw = Path(f"/proc/{d}/cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\0", b" ").decode(errors="replace").strip()
        if marker in cmd:                     # a zombie's command line is empty: it is gone
            out.append((int(d), cmd))
    return out


def _signal(pids: list[int], marker: str, sig: int) -> None:
    for pid in pids:
        if any(p == pid for p, _ in naming(marker)):     # still that process, not a reused pid
            try:
                os.kill(pid, sig)
            except OSError:
                pass


def sweep(marker: str, *, first: tuple[str, ...] = (), only: str = DAEMON_MODULE,
          grace_s: float = 5.0, rounds: int = 3) -> list[tuple[int, str]]:
    """Stop every process naming ``marker`` and ``only`` (a daemon or the apply helper, by
    default): ``first`` (substrings of the command line) before the rest, SIGTERM then SIGKILL
    after ``grace_s``; again (a helper may have started one meanwhile) up to ``rounds``
    times. Returns what it found."""
    seen: list[tuple[int, str]] = []
    for _ in range(rounds):
        found = [(p, c) for p, c in naming(marker) if only in c]
        if not found:
            break
        seen += found
        early = [p for p, c in found if any(f in c for f in first)]
        for group in (early, [p for p, _ in found if p not in early]):
            if not group:
                continue
            _signal(group, marker, signal.SIGTERM)
            deadline = time.monotonic() + grace_s
            while time.monotonic() < deadline and \
                    any(p in group for p, _ in naming(marker)):
                time.sleep(0.1)
            _signal(group, marker, signal.SIGKILL)
    return seen


def track(path: Path | str) -> str:
    """Record an install dir this session made (the session guard checks it at the end)."""
    marker = str(path).rstrip("/") + "/"
    _TRACKED.append(marker)
    return marker


def session_markers() -> list[str]:
    return list(_TRACKED)


def daemons_naming(markers: list[str]) -> list[tuple[int, str]]:
    """Daemons and apply helpers (``python -m harness_manager.daemon…``) naming any marker."""
    out: dict[int, str] = {}
    for m in markers:
        for pid, cmd in naming(m):
            if DAEMON_MODULE in cmd:
                out[pid] = cmd
    return sorted(out.items())


def leaked_daemons(markers: list[str], settle_s: float = 10.0) -> list[tuple[int, str]]:
    """The daemons still alive after ``settle_s`` (one told to stop a moment ago may still be
    on its way out). Empty at once when there is none."""
    deadline = time.monotonic() + settle_s
    while True:
        left = daemons_naming(markers)
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.2)


def _start_time(pid: int) -> str:
    """``/proc/<pid>/stat`` field 22 (start time): a pid and this identify one process."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


#: The reaper (runs detached, stdlib only). Its command line names nothing it watches.
REAPER_PY = r'''
import os, shutil, signal, sys, time
BASE = os.environ["HM_TEST_REAPER_BASE"].rstrip("/")
INSIDE = BASE + "/"
DAEMON = "harness_manager.daemon"          # a daemon or the apply helper, never a `tail`
OWNER = int(os.environ["HM_TEST_REAPER_OWNER"])
START = os.environ["HM_TEST_REAPER_OWNER_START"]
POLL = float(os.environ.get("HM_TEST_REAPER_POLL_S", "1"))

def owner_alive():
    try:
        with open("/proc/%d/stat" % OWNER) as fh:
            return fh.read().rsplit(")", 1)[1].split()[19] == START
    except (OSError, IndexError):
        return False

def naming():
    me, uid, out = os.getpid(), os.getuid(), []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == me:
            continue
        try:
            if os.stat("/proc/" + d).st_uid != uid:
                continue
            with open("/proc/%s/cmdline" % d, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if INSIDE in cmd and DAEMON in cmd:
            out.append(int(d))
    return out

while os.path.isdir(BASE):
    if not owner_alive():
        # the test process died without its teardown: stop what it started, then remove it
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in naming():
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            time.sleep(3 * POLL)
        shutil.rmtree(BASE, ignore_errors=True)
        break
    time.sleep(POLL)
'''


def start_reaper(base: Path | str, owner: int | None = None,
                 poll_s: float = 1.0) -> subprocess.Popen | None:
    """Watch ``owner`` (this process): if it dies while ``base`` exists, stop every daemon
    naming a path inside ``base`` and remove it. None where there is no ``/proc``."""
    if not os.path.isdir("/proc"):
        return None
    owner = os.getpid() if owner is None else owner
    env = {**os.environ, "HM_TEST_REAPER_BASE": str(base), "HM_TEST_REAPER_OWNER": str(owner),
           "HM_TEST_REAPER_OWNER_START": _start_time(owner), "HM_TEST_REAPER_POLL_S": str(poll_s)}
    return subprocess.Popen([sys.executable, "-c", REAPER_PY], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                            start_new_session=True)
