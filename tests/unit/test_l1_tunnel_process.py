"""L1: the tunnel's REAL subprocess path (``popen_launcher``), with a fake ``ssh`` executable on PATH.

The in-process fake (``FakeSsh``) proves the logic; this proves the parts only a
real process has: PATH lookup, the stderr pipe drained line by line (auth
failures, refused channels), SIGTERM on close, and no process left behind.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from harness_manager.core.errors import UnreachableError
from harness_manager_mps3 import tunnel as T

REPO = Path(__file__).resolve().parents[2]
HUB = "mapstone-dev.ecs.soton.ac.uk"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="a /bin/sh shim stands in for ssh")


@pytest.fixture
def board():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def serve() -> None:
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                data = c.recv(100)
                c.sendall(b"board:" + data)

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


@pytest.fixture
def fake_ssh_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "ssh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m tests.fakes.l1_fake_ssh_bin "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    monkeypatch.setenv("L1_FAKE_SSH_G", "hostname mapstone-dev.ecs.soton.ac.uk\n")
    return monkeypatch


def test_a_real_ssh_process_forwards_logs_refusals_and_dies_on_close(fake_ssh_on_path, board):
    fake_ssh_on_path.setenv("L1_FAKE_SSH_ROUTES", json.dumps({"192.168.10.101:6900": f"127.0.0.1:{board}"}))
    t = T.SshTunnel(HUB, [T.Forward("control", "192.168.10.101", 6900),
                          T.Forward("rbb", "192.168.10.101", 6921)],
                    launcher=T.popen_launcher, ssh_g=T.run_ssh_g, ready_timeout_s=20)
    with t:
        pid = t.status()["pid"]
        os.kill(pid, 0)                                            # a real process
        with socket.create_connection(("127.0.0.1", t.local_port("control")), timeout=3) as s:
            s.sendall(b"ping")
            s.settimeout(3)
            assert s.recv(100) == b"board:ping"
        t0 = time.monotonic()
        with socket.create_connection(("127.0.0.1", t.local_port("rbb")), timeout=3) as s:
            s.settimeout(3)
            assert s.recv(10) == b""                               # refused at the far end
        deadline = time.monotonic() + 5
        while not t.open_failures_since(t0) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "open failed: connect failed" in t.open_failures_since(t0)[0]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the ssh process outlived its tunnel")


def test_negative_twin_a_refused_login_fails_the_start_with_sshs_words(fake_ssh_on_path):
    fake_ssh_on_path.setenv("L1_FAKE_SSH_FAIL", "auth")
    t = T.SshTunnel(HUB, [T.Forward("control", "192.168.10.101", 6900)],
                    launcher=T.popen_launcher, ssh_g=T.run_ssh_g, ready_timeout_s=20)
    with pytest.raises(UnreachableError) as exc:
        t.start()
    assert "Permission denied (publickey)" in exc.value.message


def test_a_missing_ssh_binary_is_unreachable_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(UnreachableError) as exc:
        T.popen_launcher(["ssh", "-N", HUB])
    assert "OpenSSH" in exc.value.hint


# FLAKE 2026-09-24: under load the refused-login test above read "ssh exited with status 255
# (no message)". poll() saw the exit before the stderr reader thread had read ssh's last
# words; now an exited process's poll() lets the reader reach the end of the pipe first.

_SAYS_NO = [sys.executable, "-c",
            "import sys; sys.stderr.write('u@hub: Permission denied (publickey).\\n')"]


def _lagging_reader(monkeypatch) -> threading.Event:
    """The stderr reader starts only when the returned event is set (a starved thread)."""
    go = threading.Event()
    original = T._PopenProcess._drain

    def drain(self) -> None:
        go.wait(10)
        original(self)

    monkeypatch.setattr(T._PopenProcess, "_drain", drain)
    return go


def test_an_exited_ssh_is_reported_with_what_it_wrote_even_when_its_reader_lags(monkeypatch):
    go = _lagging_reader(monkeypatch)
    proc = T._PopenProcess(_SAYS_NO)
    proc._proc.wait(timeout=20)                       # exited; its reader has read nothing
    threading.Timer(0.1, go.set).start()
    assert proc.poll() == 0
    assert "Permission denied (publickey)" in T._stderr_of(proc)


def test_negative_twin_without_the_wait_the_exit_is_reported_before_the_words(monkeypatch):
    go = _lagging_reader(monkeypatch)
    monkeypatch.setattr(T, "_DRAIN_WAIT_S", 0.0)
    proc = T._PopenProcess(_SAYS_NO)
    try:
        proc._proc.wait(timeout=20)
        assert proc.poll() == 0
        assert T._stderr_of(proc) == ""               # "no message": the flake
    finally:
        go.set()
