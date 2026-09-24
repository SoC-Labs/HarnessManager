"""Lane Q2: the real daemon process leaves nothing behind (docs/assessment/2026-09-24/Q2_ROBUSTNESS.md).

- ``kill -9`` mid-session, then a restart: the killed daemon's SSH tunnels, OpenOCD
  and PTY links are cleaned up by the new daemon when it starts. Twin: a second daemon
  for a LIVE one's state dir is refused and touches none of its children.
- A signal while the boards close (``daemon stop``'s SIGTERM fallback, a second
  Ctrl-C) no longer kills the process half way. Twin: without the fix it does.
- SIGHUP (a foreground daemon's terminal closed) stops it cleanly. Twin: the default
  action, without the fix, leaves daemon.json and the lock behind.

Every process here is started by the test, on 127.0.0.1 and ephemeral ports, with a
private state dir and PTY dir; the fake ssh and the OpenOCD stub stand in for the
real programs (tests/soak/q2_soak.py builds the rig).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.soak.q2_soak import Rig, alive

pytestmark = [pytest.mark.timeout(240),
              pytest.mark.skipif(not sys.platform.startswith("linux"),
                                 reason="real processes, /proc, a /bin/sh ssh shim")]
REPO = Path(__file__).resolve().parents[2]


def _wait(predicate, timeout: float = 20.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _job(c, method: str, path: str, **kw) -> dict:
    r = c.request(method, path, **kw)
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    return _wait(lambda: (lambda j: j if j["state"] != "running" else None)(
        c.get(f"/api/v1/jobs/{job}").json()), timeout=60, what=f"job {job}")


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path / "rig", n_boards=1)
    try:
        r.setup()
        r.start_boards()
        yield r
    finally:
        r.teardown()


def _session(rig: Rig) -> dict[int, str]:
    """Open the board, load nanosoc, start debug, open uart0's PTY: the daemon's children."""
    with rig.client(timeout=60) as c:
        assert c.post("/api/v1/boards", json={"target": rig.ips[0]}).status_code == 200
        p = rig.bpath(rig.board_ids[0])
        assert _job(c, "POST", f"{p}/deploy", json={"overlay": "nanosoc"})["state"] == "done"
        assert _job(c, "POST", f"{p}/debug/up")["result"]["state"] == "up"
        assert c.post(f"{p}/consoles/uart0/pty").status_code == 200
    kids = rig.children()
    return {k: rig.seen_children[k] for k in kids}


def test_kill_9_then_restart_cleans_up_the_tunnel_openocd_and_pty_links(rig):
    rig.start_daemon()
    kids = _session(rig)
    assert any("l1_fake_ssh_bin" in c for c in kids.values())
    assert any("stub_openocd" in c for c in kids.values())
    os.kill(rig.pid, signal.SIGKILL)
    rig.daemon.wait(10)
    time.sleep(0.5)
    left = rig.debris()
    assert set(left["children_alive"]) == set(kids)              # the debris a crash leaves
    assert left["debug_records"] and any(e.endswith("/uart0") for e in left["pty_entries"])

    rig.start_daemon()                                           # the next start cleans up
    _wait(lambda: not any(alive(p) for p in kids), what="the orphans to be stopped")
    after = rig.debris()
    assert after["debug_records"] == []
    assert not any(e.endswith("uart0") for e in after["pty_entries"])
    log = (rig.state / "daemon.log").read_text()
    assert "clean-up after a daemon that did not stop cleanly" in log
    with rig.client() as c:                                      # and the board opens again
        assert c.post("/api/v1/boards", json={"target": rig.ips[0]}).status_code == 200
    assert rig.stop_daemon().startswith("stopped")


def test_negative_twin_a_second_daemon_for_a_live_one_touches_nothing(rig):
    rig.start_daemon()
    kids = _session(rig)
    second = subprocess.run([sys.executable, "-m", "harness_manager.daemon", "--state-dir",
                             str(rig.state), "--port", "0"], env=rig.env(), capture_output=True,
                            text=True, timeout=60)
    assert second.returncode == 4 and "already running" in second.stderr
    assert all(alive(p) for p in kids)                           # nothing of the live one
    with rig.client() as c:
        assert c.get(f"{rig.bpath(rig.board_ids[0])}/debug").json()["state"] == "up"
    assert rig.stop_daemon().startswith("stopped")
    _wait(lambda: not any(alive(p) for p in kids), what="a clean stop to take its children")
    debris = rig.debris()
    assert not debris["daemon_json"] and not debris["daemon_lock"]
    assert debris["board_locks"] == [] and debris["debug_records"] == []


# --- signals during the clean-up -----------------------------------------------------------------

_SCRIPT = r"""
import os, signal, sys, threading, time
from pathlib import Path
from harness_manager import demo
from harness_manager.daemon import server

state, marker, first, fixed = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
original = demo.DemoEngine.close_all

def slow_close(self):
    os.kill(os.getpid(), signal.SIGTERM)       # `daemon stop`'s fallback, mid clean-up
    time.sleep(0.5)
    original(self)
    marker.write_text("closed")

demo.DemoEngine.close_all = slow_close
if fixed == "no":
    server._hold_signals = lambda: None
    server._stop_on_hangup = lambda _server: None

def stopper():
    while not (state / "daemon.json").exists():
        time.sleep(0.05)
    time.sleep(0.3)
    os.kill(os.getpid(), getattr(signal, first))

threading.Thread(target=stopper, daemon=True).start()
sys.exit(server.run_daemon(state, demo=True))
"""


def _run(tmp_path: Path, first: str, fixed: bool) -> tuple[int, Path, Path]:
    state, marker = tmp_path / "state", tmp_path / "closed"
    script = tmp_path / "daemon.py"
    script.write_text(_SCRIPT)
    env = dict(os.environ, PYTHONPATH=f"{REPO / 'src'}{os.pathsep}{REPO}")
    proc = subprocess.run([sys.executable, str(script), str(state), str(marker), first,
                           "yes" if fixed else "no"], env=env, capture_output=True, text=True,
                          timeout=90)
    return proc.returncode, state, marker


@pytest.mark.parametrize("first", ["SIGTERM", "SIGINT"])
def test_a_signal_while_the_boards_close_lets_the_clean_up_finish(tmp_path, first):
    rc, state, marker = _run(tmp_path, first, fixed=True)
    assert rc == 0
    assert marker.read_text() == "closed"
    assert not (state / "daemon.json").exists()
    assert not (state / "harness-manager-daemon.lock").exists()


def test_negative_twin_without_the_fix_that_signal_kills_it_half_way(tmp_path):
    rc, state, marker = _run(tmp_path, "SIGTERM", fixed=False)
    assert rc == -signal.SIGTERM
    assert not marker.exists()                                   # close_all never finished
    assert (state / "daemon.json").exists()                      # and the debris is left


def test_sighup_stops_a_foreground_daemon_cleanly(tmp_path):
    rc, state, marker = _run(tmp_path, "SIGHUP", fixed=True)
    assert rc == 0 and marker.read_text() == "closed"
    assert not (state / "daemon.json").exists()


def test_negative_twin_without_the_fix_sighup_leaves_daemon_json(tmp_path):
    rc, state, marker = _run(tmp_path, "SIGHUP", fixed=False)
    assert rc == -signal.SIGHUP
    assert (state / "daemon.json").exists() and not marker.exists()
