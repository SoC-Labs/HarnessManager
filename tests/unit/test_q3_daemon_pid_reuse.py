"""Q3 install (lane Q1's finding): `daemon stop` never signals a process that reused the pid.

A daemon.json left behind names a pid that may since belong to another process. Before
this, `daemon stop` (and so every install.sh upgrade, which stops the service first)
posted a shutdown to the port, waited, and then SIGTERMed that pid, whatever it was now.
Now it signals only a pid that is provably this state dir's harness-manager-daemon; a
foreign pid means the daemon is gone: daemon.json is removed and nothing is signalled.
"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from harness_manager.daemon import control
from harness_manager.daemon.state import DaemonInfo, daemon_json_path, write_info

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="the command-line check is /proc or ps (POSIX)")

SLEEPER = "import time; time.sleep(60)"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]          # closed again: nothing answers there


def _record(state: Path, pid: int) -> None:
    write_info(state, DaemonInfo(pid=pid, port=_free_port(), token="t", started_at=0.0,
                                 version="0.1.0", hostname=socket.gethostname()))


@pytest.fixture
def procs():
    started: list[subprocess.Popen] = []

    def start(*argv: str) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", SLEEPER, *argv])
        started.append(p)
        return p

    yield start
    for p in started:
        p.kill()
        p.wait()


def test_a_reused_pid_is_never_signalled(tmp_path: Path, procs):
    state = tmp_path / "state"
    other = procs()                        # an unrelated process now holds the pid
    _record(state, other.pid)
    assert control.is_our_daemon(control.read_info(state), state) is False
    assert control.stop(state, timeout=0.5) == "stale-removed"
    assert other.poll() is None            # still running: never signalled
    assert not daemon_json_path(state).exists()


def test_a_daemon_of_another_state_dir_is_never_signalled(tmp_path: Path, procs):
    state = tmp_path / "state"
    other = procs("harness_manager.daemon", "--state-dir", str(tmp_path / "someone-else"))
    _record(state, other.pid)
    assert control.stop(state, timeout=0.5) == "stale-removed"
    assert other.poll() is None


def test_negative_twin_our_hung_daemon_is_still_terminated(tmp_path: Path, procs):
    state = tmp_path / "state"
    state.mkdir()
    # Looks like the daemon for THIS state dir, and answers nothing: stop must end it.
    ours = procs("harness_manager.daemon", "--state-dir", str(state), "--port", "0")
    _record(state, ours.pid)
    assert control.is_our_daemon(control.read_info(state), state) is True
    assert control.stop(state, timeout=0.5) == "stopped"
    assert ours.wait(timeout=10) == -signal.SIGTERM
    assert not daemon_json_path(state).exists()


def test_a_relative_state_dir_on_the_command_line_still_matches(tmp_path: Path):
    state = tmp_path / "a" / "state"
    assert control._same_dir("state", state) and control._same_dir("a/state", state)
    assert not control._same_dir("other", state)
    assert control._same_dir(str(state), state)
