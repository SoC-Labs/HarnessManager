"""SMALL-5: no daemon a test starts outlives it (``tests/fakes/proc_sweep.py``). Each check has
a negative twin.

The leak (2026-09-26): ``test_apply_restarts_on_the_same_port_and_token…`` left its restarted
0.2.0 daemon running for days. Every process of an apply is detached (a new session), the
restarted daemon's parent (the helper) was gone, and cleanup lived only in the test's own
teardown, which found the daemon only through ``daemon.json``. The stand-ins here are plain
Python sleepers whose command lines look like a daemon's and a helper's.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.fakes import proc_sweep

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc (Linux)")

#: Sleeps; on SIGTERM appends its name to $LOG and exits (unless $IGNORE_TERM).
_SLEEPER = r'''
import os, signal, sys, time
def term(*_):
    if os.environ.get("IGNORE_TERM"):
        return
    with open(os.environ["LOG"], "a") as fh:
        fh.write(os.environ["NAME"] + "\n")
    sys.exit(0)
signal.signal(signal.SIGTERM, term)
sys.stdout.write("ready\n"); sys.stdout.flush()
end = time.monotonic() + float(os.environ.get("LIVE_S", "120"))
while time.monotonic() < end:
    time.sleep(0.05)
'''


def sleeper(name: str, argv: list[str], log: Path, **env: str) -> subprocess.Popen:
    """A detached stand-in (its own session, as a daemon and the helper are)."""
    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER, *argv], stdout=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, start_new_session=True,
                            env={**os.environ, "LOG": str(log), "NAME": name, **env})
    assert proc.stdout.readline().strip() == "ready"
    return proc


def daemon_argv(state: Path) -> list[str]:
    return ["-m", "harness_manager.daemon", "--state-dir", str(state), "--resume",
            str(state / "update" / "resume.json")]


def helper_argv(state: Path) -> list[str]:
    return ["-m", "harness_manager.daemon.update_apply", "--state-dir", str(state)]


def gone(proc: subprocess.Popen, timeout: float = 15) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


@pytest.fixture
def started(tmp_path):
    procs: list[subprocess.Popen] = []
    yield procs
    for p in procs:                                  # whatever a failing test left
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)
        p.stdout.close()


def test_sweep_stops_every_process_naming_the_world_helper_first(tmp_path, started):
    world, other = tmp_path / "w1-test", tmp_path / "w11-test"
    log = tmp_path / "order.log"
    daemon = sleeper("daemon", daemon_argv(world / "state"), log)
    helper = sleeper("helper", helper_argv(world / "state"), log)
    started += [daemon, helper]
    # twins: a daemon naming another world (w11 starts with w1), and a `tail` of this
    # world's log (not a daemon: someone reading it), are never signalled
    bystander = sleeper("bystander", daemon_argv(other / "state"), log)
    tail = sleeper("tail", ["-f", str(world / "state" / "daemon.log")], log)
    started += [bystander, tail]
    found = proc_sweep.sweep(str(world) + "/", first=("harness_manager.daemon.update_apply",))
    assert sorted(p for p, _ in found) == sorted([daemon.pid, helper.pid])
    assert gone(daemon) and gone(helper)
    assert log.read_text().split() == ["helper", "daemon"]         # the helper went first
    assert bystander.poll() is None and proc_sweep.naming(str(other) + "/")
    assert tail.poll() is None


def test_twin_a_process_that_ignores_sigterm_is_killed(tmp_path, started):
    world = tmp_path / "w2-test"
    stubborn = sleeper("stubborn", daemon_argv(world / "state"), tmp_path / "log",
                       IGNORE_TERM="1")
    started.append(stubborn)
    proc_sweep.sweep(str(world) + "/", grace_s=0.5)
    assert gone(stubborn) and stubborn.returncode == -signal.SIGKILL
    assert proc_sweep.naming(str(world) + "/") == []


def test_the_session_guard_counts_this_sessions_daemons_only(tmp_path, started):
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    log = tmp_path / "log"
    daemon = sleeper("daemon", daemon_argv(mine / "state"), log)
    started.append(daemon)
    # twins: a non-daemon naming my dir, and a daemon under a dir that is not this session's
    tool = sleeper("tool", ["--watch", str(mine / "state")], log)
    other = sleeper("other", daemon_argv(theirs / "state"), log)
    started += [tool, other]
    left = proc_sweep.leaked_daemons([str(mine) + "/"], settle_s=0)
    assert [p for p, _ in left] == [daemon.pid]
    # the guard stops only the daemons it names
    proc_sweep.sweep(str(mine) + "/", only=proc_sweep.DAEMON_MODULE)
    assert gone(daemon) and tool.poll() is None and other.poll() is None


def test_twin_a_daemon_on_its_way_out_is_not_a_leak(tmp_path, started):
    mine = tmp_path / "mine"
    leaving = sleeper("leaving", daemon_argv(mine / "state"), tmp_path / "log", LIVE_S="0.5")
    started.append(leaving)
    assert proc_sweep.leaked_daemons([str(mine) + "/"], settle_s=10) == []


def test_the_reaper_stops_and_removes_everything_when_the_test_process_is_killed(tmp_path,
                                                                                started):
    base = tmp_path / "otad-reap"
    (base / "w1" / "state").mkdir(parents=True)
    owner = sleeper("owner", [], tmp_path / "log")                 # stands in for pytest
    daemon = sleeper("daemon", daemon_argv(base / "w1" / "state"), tmp_path / "log")
    tail = sleeper("tail", ["-f", str(base / "w1" / "state" / "daemon.log")], tmp_path / "log")
    started += [owner, daemon, tail]
    reaper = proc_sweep.start_reaper(base, owner=owner.pid, poll_s=0.1)
    assert reaper is not None
    try:
        time.sleep(0.5)
        assert daemon.poll() is None and base.exists()             # the owner is alive
        owner.kill()                                               # SIGKILL: no teardown
        owner.wait(timeout=10)
        assert gone(daemon) and gone(reaper) and not base.exists()
        assert str(base) not in " ".join(reaper.args)              # no sweep ever names it
        assert tail.poll() is None                                 # twin: not a daemon
    finally:
        if reaper.poll() is None:
            reaper.kill()
        reaper.wait(timeout=10)


def test_twin_the_reaper_leaves_quietly_after_a_normal_teardown(tmp_path, started):
    base = tmp_path / "otad-keep"
    (base / "w1").mkdir(parents=True)
    daemon = sleeper("daemon", daemon_argv(base / "w1" / "state"), tmp_path / "log")
    started.append(daemon)
    reaper = proc_sweep.start_reaper(base, poll_s=0.1)             # watches this process
    assert reaper is not None
    try:
        time.sleep(0.5)
        assert reaper.poll() is None
        (base / "w1").rmdir()
        base.rmdir()                                               # the teardown removed it
        assert gone(reaper) and reaper.returncode == 0
        assert daemon.poll() is None                               # nothing was signalled
    finally:
        if reaper.poll() is None:
            reaper.kill()
        reaper.wait(timeout=10)
