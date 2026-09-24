"""Q3 install: `daemon stop` when the stopped daemon stays a zombie.

In a container whose PID 1 never reaps orphans (``docker run`` with no init, and
GitHub's container jobs: CI install matrix run 35993990472), the daemon that
``harness-manager ui`` started is re-parented to PID 1 when the CLI exits. When it
stops, it stays a zombie, and ``os.kill(pid, 0)`` still succeeds on a zombie. So
``daemon stop`` waited 15 s, said "did not stop" (exit 6), and every upgrade failed,
because install.sh stops the service first.

Here the test process plays that PID 1: it starts a stand-in daemon and never reaps it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from harness_manager.core.session import pid_alive
from harness_manager.daemon import control
from harness_manager.daemon.state import DaemonInfo, write_info

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="zombies are visible through /proc on Linux")

FAKE_DAEMON = textwrap.dedent("""
    import http.server, threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            threading.Thread(target=self.server.shutdown).start()

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    print(srv.server_address[1], flush=True)
    srv.serve_forever()
""")


def _state(proc: subprocess.Popen) -> str:
    return Path(f"/proc/{proc.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]


def _wait_zombie(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while _state(proc) != "Z":
        assert time.monotonic() < deadline, "the child never exited"
        time.sleep(0.02)


def test_a_zombie_is_not_alive():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        _wait_zombie(child)
        os.kill(child.pid, 0)                 # the kernel still has it: kill(0) succeeds
        assert pid_alive(child.pid) is False  # ...but it is dead for every purpose here
    finally:
        child.wait()


def test_negative_twin_a_running_process_is_alive():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait()


def test_daemon_stop_returns_when_the_daemon_is_left_a_zombie(tmp_path: Path):
    daemon = subprocess.Popen([sys.executable, "-c", FAKE_DAEMON], stdout=subprocess.PIPE,
                              text=True)
    try:
        port = int(daemon.stdout.readline())
        state = tmp_path / "state"
        write_info(state, DaemonInfo(pid=daemon.pid, port=port, token="t", started_at=0.0,
                                     version="0.1.0", hostname=socket.gethostname()))
        t0 = time.monotonic()
        # Not started by control.start in this process, as after the CLI that started it
        # exited: only the pid tells whether it is gone. Nobody reaps it.
        assert control.stop(state, timeout=5.0) == "stopped"
        assert time.monotonic() - t0 < 4.0, "stop waited for a zombie"
        assert _state(daemon) == "Z"
        # (a real daemon removes its daemon.json as it stops; this stand-in does not)
        assert control.running(state) is None
    finally:
        daemon.kill()
        daemon.wait()


def test_a_leftover_daemon_json_naming_a_zombie_is_stale(tmp_path: Path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        _wait_zombie(child)
        state = tmp_path / "state"
        write_info(state, DaemonInfo(pid=child.pid, port=1, token="t", started_at=0.0,
                                     version="0.1.0", hostname=socket.gethostname()))
        assert control.running(state) is None
        assert control.stop(state, timeout=1.0) == "stale-removed"
    finally:
        child.wait()
