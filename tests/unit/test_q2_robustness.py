"""Lane Q2: robustness fixes, each with a negative twin (docs/assessment/2026-09-24/Q2_ROBUSTNESS.md).

- a zombie is not alive (``daemon stop`` from beside an open app window);
- ``Engine.close_all`` closes every board even when one fails;
- a full or read-only state dir is a message, never a traceback or an empty lock;
- the hub-share relay closes each connection's sockets when it ends (xfail: hub.py is
  the LR lanes'; the fix is in the report);
- an SSH tunnel whose owner was killed is stopped by the next process (records + reaper);
- a tunnel whose local port was taken while starting picks new ports;
- job failures are in daemon.log.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness_manager.core.errors import ActionFailedError, HarnessError, UnreachableError
from harness_manager.core.session import SessionLock, pid_alive
from harness_manager.engine import Engine
from harness_manager_mps3 import tunnel as T
from tests.fakes.l1_fake_ssh import FakeSsh
from tests.fakes.t1_fakes import FakePack, candidate

REPO = Path(__file__).resolve().parents[2]
HUB = "fakehub.invalid"
LINUX = sys.platform.startswith("linux")


def _proc_state(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return "gone"


def _wait(predicate, timeout: float = 10.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# --- a zombie is not alive -----------------------------------------------------------------


@pytest.mark.skipif(not LINUX, reason="zombies are read from /proc")
def test_an_exited_unreaped_child_is_not_alive():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        _wait(lambda: _proc_state(child.pid) == "Z", what="the child to become a zombie")
        os.kill(child.pid, 0)                    # the trap: the kernel still answers for it
        assert pid_alive(child.pid) is False
    finally:
        child.wait()


def test_negative_twin_a_running_child_is_alive():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(not LINUX, reason="zombies are read from /proc")
def test_daemon_stop_is_quick_and_true_when_its_parent_has_not_reaped_it(tmp_path):
    """The app window (pywebview) is the daemon's parent and does not reap it: ``daemon
    stop`` from a terminal waited 15 s, signalled a zombie and said "did not stop"."""
    from harness_manager.daemon import control

    state = tmp_path / "st"
    env = dict(os.environ, HARNESS_MANAGER_STATE_DIR=str(state))
    daemon = subprocess.Popen([sys.executable, "-m", "harness_manager.daemon", "--state-dir",
                               str(state), "--port", "0", "--demo"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    try:
        _wait(lambda: control.running(state), timeout=60, what="the daemon")
        t0 = time.monotonic()
        assert control.stop(state, timeout=10) == "stopped"
        assert time.monotonic() - t0 < 5
        assert _proc_state(daemon.pid) == "Z"            # nobody reaped it: we did not
        assert control.status(state)["state"] == "stopped"
        assert not (state / "harness-manager-daemon.lock").exists()
    finally:
        if daemon.poll() is None:
            daemon.kill()
        daemon.wait()


# --- close_all closes every board ------------------------------------------------------------


class _Boom(Exception):
    pass


def _engine_with_two_boards(tmp_path: Path) -> tuple[Engine, FakePack]:
    pack = FakePack()
    eng = Engine(packs={"fake": pack})
    eng.open(candidate("fake@a"))
    eng.open(candidate("fake@b"))
    return eng, pack


def test_close_all_closes_every_board_when_one_fails_then_says_so(tmp_path):
    eng, pack = _engine_with_two_boards(tmp_path)

    def boom() -> None:
        raise _Boom("the share would not close")

    pack.opened[0].close = boom                      # type: ignore[method-assign]
    with pytest.raises(_Boom):
        eng.close_all()
    assert eng.open_boards() == []
    assert pack.opened[1].closed == 1                # the second board WAS closed
    assert eng.lock_owner("fake@a") is None and eng.lock_owner("fake@b") is None


def test_negative_twin_close_all_without_failures_raises_nothing(tmp_path):
    eng, pack = _engine_with_two_boards(tmp_path)
    eng.close_all()
    assert [s.closed for s in pack.opened] == [1, 1]
    assert eng.open_boards() == []


# --- the state dir: full or read-only ---------------------------------------------------------


def test_a_full_disk_while_taking_a_lock_is_a_message_and_leaves_no_empty_lock(tmp_path,
                                                                                monkeypatch):
    lock = SessionLock("mps3@x", lock_dir=tmp_path / "locks")
    real_fdopen = os.fdopen

    class FullFile:
        def __init__(self, fd: int) -> None:
            self._fh = real_fdopen(fd, "w")

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            self._fh.close()

        def write(self, _data: str) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fdopen", lambda fd, mode="r", *a, **k: FullFile(fd))
    with pytest.raises(ActionFailedError) as exc:
        lock.acquire()
    assert "the disk is full" in exc.value.message and "free space" in exc.value.hint
    assert not lock.path.exists()                    # no empty lock for the next opener
    monkeypatch.undo()
    lock.acquire()                                   # twin: space again, the lock is taken
    assert lock.owner() is not None and lock.owner().pid == os.getpid()
    lock.release()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs file modes")
def test_a_read_only_state_dir_is_a_message_not_a_traceback(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        res = subprocess.run([sys.executable, "-m", "harness_manager.daemon", "--state-dir",
                              str(ro / "state")], capture_output=True, text=True, timeout=60)
        assert res.returncode == 6, res.stderr
        assert "Traceback" not in res.stderr
        assert "not writable" in res.stderr and "--state-dir" in res.stderr
        res = subprocess.run([sys.executable, "-m", "harness_manager.daemon", "--state-dir",
                              str(ro)], capture_output=True, text=True, timeout=60)
        assert res.returncode == 6 and "Traceback" not in res.stderr, res.stderr
        assert "harness-manager-daemon.lock" in res.stderr
    finally:
        ro.chmod(stat.S_IRWXU)


def test_negative_twin_the_lock_still_says_held_for_a_live_holder(tmp_path):
    first = SessionLock("mps3@x", lock_dir=tmp_path)
    first.acquire()
    other = SessionLock("mps3@x", lock_dir=tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        data = json.loads(first.path.read_text())
        data["pid"] = child.pid                       # a live holder that is not us
        first.path.write_text(json.dumps(data))
        with pytest.raises(HarnessError) as exc:
            other.acquire()
        assert exc.value.code == 4                    # HELD, not a storage error
    finally:
        child.kill()
        child.wait()


# --- the hub-share relay closes what it opened ------------------------------------------------


def _relay_sockets(relay_port: int, share_port: int) -> int:
    """This process's open sockets that are the relay's: accepted on ``relay_port``, or
    dialled to the share's forward (``share_port``). The fakes' own sockets are not counted."""
    ours: set[str] = set()
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:["):
            ours.add(target[8:-1])
    count = 0
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            lport, rport, inode = int(cols[1].rsplit(":", 1)[1], 16), \
                int(cols[2].rsplit(":", 1)[1], 16), cols[9]
            if inode in ours and cols[3] != "0A" and (
                    lport == relay_port or (rport == share_port and lport != share_port)):
                count += 1
    return count


@pytest.mark.skipif(not LINUX, reason="counts fds in /proc")
@pytest.mark.xfail(strict=True, reason="Q2 finding: hub.ShareRelay keeps both sockets of every "
                   "relayed connection open until the board closes. hub.py belongs to the LR "
                   "lanes (lead notice 2026-09-24); the fix is in Q2_ROBUSTNESS.md. Remove this "
                   "mark with it.")
def test_the_share_relay_closes_both_sockets_of_each_connection(tmp_path, monkeypatch):
    from harness_manager_mps3 import hub as hubmod
    from tests.fakes.l1_fake_hub import FakeLane
    from tests.fakes.l1_rig import HUB as LAB_HUB
    from tests.fakes.l1_rig import lab
    from tests.fakes.virtual_board import VirtualMps3

    tty = "/dev/mps3_01_pl/tty_02"
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=tmp_path / "state") as rig:
        rig.hub.add_tty(tty, FakeLane(), share=True)
        relay = hubmod.SHARES.relay(hubmod.ShareRef(LAB_HUB, "mps3_01_pl", tty))

        share = rig.hub.shares[tty]

        def once(text: bytes) -> None:
            # The share gives its write slot to its FIRST client: wait for the last one
            # to have left the share (fpgahub does the same), or our keys are dropped.
            _wait(lambda: share.readers == 0, what="the previous client to leave the share")
            with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as s:
                s.settimeout(5)
                s.sendall(text)
                got = b""
                while text not in got:
                    got += s.recv(100)

        once(b"warm\n")                              # the share's forward is made once
        share_port = hubmod.SHARES.status(LAB_HUB, "mps3_01_pl")[tty]["local"]
        for i in range(20):
            once(f"hello {i}\n".encode())
        _wait(lambda: not relay._conns, what="every relayed connection to be forgotten")
        _wait(lambda: _relay_sockets(relay.port, share_port) == 0,
              what="the relay's sockets to be closed")        # was 2 per connection, for good

        # twin: a connection that is still open keeps working while others come and go
        # (the share gives only its first client the write slot, so the others just visit)
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as live:
            live.settimeout(5)
            for _ in range(3):
                socket.create_connection(("127.0.0.1", relay.port), timeout=5).close()
            _wait(lambda: len(relay._conns) == 2, what="only the live connection to remain")
            live.sendall(b"still here\n")
            got = b""
            while b"still here" not in got:
                got += live.recv(100)


# --- orphaned ssh tunnels ------------------------------------------------------------------------


@pytest.fixture
def ssh_shim(tmp_path, monkeypatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "ssh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m tests.fakes.l1_fake_ssh_bin "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    monkeypatch.setenv("L1_FAKE_SSH_G", f"hostname {HUB}\n")
    monkeypatch.setenv("L1_FAKE_SSH_ROUTES", "{}")
    return shim


def _tunnel_argv(port: int) -> list[str]:
    return ["ssh", *T.SSH_OPTIONS, "-N", "-T", "-L", f"127.0.0.1:{port}:192.168.10.101:6900", HUB]


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _record(pid: int, owner: int, argv: list[str]) -> Path:
    d = T.procs_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{owner}-test.json"
    path.write_text(json.dumps({"pid": pid, "owner_pid": owner, "owner_host": socket.gethostname(),
                                "host": HUB, "argv": argv}))
    return path


pytestmark_posix = pytest.mark.skipif(not LINUX, reason="a /bin/sh shim and /proc")


@pytestmark_posix
def test_an_orphaned_tunnel_is_stopped_and_its_record_dropped(ssh_shim):
    argv = _tunnel_argv(T.free_local_port())
    ssh = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait(lambda: T.listening(int(argv[-2].split(":")[1])), what="the fake ssh to listen")
        record = _record(ssh.pid, _dead_pid(), argv)
        assert T.reap_orphans() == [ssh.pid]
        assert ssh.wait(5) is not None
        assert not record.exists()
    finally:
        if ssh.poll() is None:
            ssh.kill()
            ssh.wait()


@pytestmark_posix
def test_negative_twin_a_live_owners_tunnel_and_a_reused_pid_are_never_signalled(ssh_shim):
    argv = _tunnel_argv(T.free_local_port())
    ssh = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _wait(lambda: T.listening(int(argv[-2].split(":")[1])), what="the fake ssh to listen")
        live = _record(ssh.pid, owner.pid, argv)
        assert T.reap_orphans() == [] and ssh.poll() is None and live.exists()
        live.unlink()
        reused = _record(stranger.pid, _dead_pid(), argv)     # the pid now runs something else
        assert T.reap_orphans() == [] and stranger.poll() is None
        assert not reused.exists()                            # the stale record is dropped
    finally:
        for p in (ssh, owner, stranger):
            p.kill()
            p.wait()


@pytestmark_posix
def test_a_tunnel_whose_owner_is_killed_is_reaped_by_the_next_process(ssh_shim, tmp_path):
    """End to end: a process opens a real tunnel (popen launcher) and is ``kill -9``ed; its
    ssh lives on (the bug); the next process's ``reap_orphans`` stops it."""
    script = tmp_path / "owner.py"
    script.write_text(
        "import sys, time\n"
        "from harness_manager_mps3 import tunnel as T\n"
        f"t = T.SshTunnel({HUB!r}, [T.Forward('control', '192.168.10.101', 6900)],\n"
        "                launcher=T.popen_launcher, ssh_g=T.run_ssh_g, ready_timeout_s=30)\n"
        "t.start()\n"
        "print(t.status()['pid'], flush=True)\n"
        "time.sleep(120)\n")
    owner = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ, PYTHONPATH=f"{REPO / 'src'}{os.pathsep}{REPO}"))
    ssh_pid = 0
    try:
        ssh_pid = int(owner.stdout.readline())
        assert len(list(T.procs_dir().glob("*.json"))) == 1       # recorded while it runs
        owner.kill()
        owner.wait()
        time.sleep(0.5)
        assert pid_alive(ssh_pid)                                 # the orphan (was: forever)
        assert T.reap_orphans() == [ssh_pid]
        _wait(lambda: not pid_alive(ssh_pid), what="the orphaned ssh to stop")
        assert list(T.procs_dir().glob("*.json")) == []
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()
        if ssh_pid and pid_alive(ssh_pid) and "l1_fake_ssh_bin" in \
                Path(f"/proc/{ssh_pid}/cmdline").read_bytes().decode(errors="replace"):
            os.kill(ssh_pid, 9)


def test_a_closed_tunnel_leaves_no_record(tmp_path):
    ssh = FakeSsh()
    t = T.SshTunnel(HUB, [T.Forward("control", "192.168.10.101", 6900)], launcher=ssh,
                    ssh_g=ssh.ssh_g, restart=False)
    t.start()
    assert len(list(T.procs_dir().glob("*.json"))) == 1
    t.close()
    assert list(T.procs_dir().glob("*.json")) == []
    ssh.close()


# --- a local port taken while the tunnel starts -----------------------------------------------


def test_a_port_taken_between_choosing_and_binding_is_chosen_again(monkeypatch):
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    taken = squatter.getsockname()[1]
    picks = iter([taken])
    real = T.free_local_port
    monkeypatch.setattr(T, "free_local_port", lambda exclude=(): next(picks, None) or real(exclude))
    ssh = FakeSsh()
    try:
        t = T.SshTunnel(HUB, [T.Forward("control", "192.168.10.101", 6900)], launcher=ssh,
                        ssh_g=ssh.ssh_g, restart=False)
        assert t.local_port("control") == taken
        t.start()
        assert t.state == "up" and t.local_port("control") != taken
        assert len(ssh.launches) == 2
        t.close()
    finally:
        squatter.close()
        ssh.close()


def test_negative_twin_a_pinned_taken_port_fails_with_the_port_hint():
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    taken = squatter.getsockname()[1]
    ssh = FakeSsh()
    try:
        t = T.SshTunnel(HUB, [T.Forward("control", "192.168.10.101", 6900, taken)],
                        launcher=ssh, ssh_g=ssh.ssh_g, restart=False)
        with pytest.raises(UnreachableError) as exc:
            t.start()
        assert "Address already in use" in exc.value.message
        assert "local port" in exc.value.hint and "VPN" not in exc.value.hint
        assert len(ssh.launches) == 1
    finally:
        squatter.close()
        ssh.close()


# --- daemon.log ---------------------------------------------------------------------------------


def test_a_failed_job_is_in_the_log_with_its_error(caplog):
    from harness_manager.core.events import EventBus
    from harness_manager.daemon.jobs import BoardGates, JobManager

    jobs = JobManager(EventBus(), BoardGates())

    def fail(_progress):
        raise UnreachableError("the board stopped answering mid-push", hint="check the cable")

    with caplog.at_level(logging.INFO, logger="harness_manager.daemon.jobs"):
        job = jobs.submit("deploy", "mps3@b", fail)
        assert job.finished.wait(10)
        ok = jobs.submit("reset", "mps3@b", lambda _p: {"ok": True})
        assert ok.finished.wait(10)
    jobs.shutdown(wait=True)
    text = caplog.text
    assert f"deploy job {job.id} on mps3@b failed" in text
    assert "UNREACHABLE: the board stopped answering mid-push" in text
    assert f"reset job {ok.id} done" in text          # twin: success says so too


# --- a board that is off, through the tunnel, is not "held by another client" -----------------


class _LateLines(list):
    """``open_failures`` whose lines arrive late, as they do through ssh's stderr pipe."""

    def append(self, item) -> None:
        import threading

        threading.Timer(0.15, super().append, args=(item,)).start()


def test_a_board_that_is_off_through_the_tunnel_is_unreachable_even_when_ssh_says_so_late(
        tmp_path, monkeypatch):
    from harness_manager.core.errors import HeldError
    from harness_manager.core.services import EngineConfig
    from tests.fakes import l1_fake_ssh
    from tests.fakes.l1_rig import BOARD_IP, lab
    from tests.fakes.virtual_board import VirtualMps3

    real_init = l1_fake_ssh.FakeSshProcess.__init__

    def late_init(self, *a, **k) -> None:
        real_init(self, *a, **k)
        self.open_failures = _LateLines(self.open_failures)

    monkeypatch.setattr(l1_fake_ssh.FakeSshProcess, "__init__", late_init)
    state = tmp_path / "state"
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state):
        eng = Engine(EngineConfig(state_dir=state))
        try:
            cand = eng.candidate_for(BOARD_IP)
            eng.open(cand)
            from harness_manager_mps3.shell import Mps3Shell

            def held(_self):
                raise HeldError("another client holds the control port")

            with monkeypatch.context() as m:         # twin first: a real second client,
                m.setattr(Mps3Shell, "identity", held)   # and ssh says nothing: still HELD
                t0 = time.monotonic()
                with pytest.raises(HeldError):
                    eng.info(cand.board_id)
                assert time.monotonic() - t0 < 2
            vb.shell.stop()                          # the board is off: the hub is refused
            with pytest.raises(UnreachableError) as exc:
                eng.info(cand.board_id)
            assert "could not reach the shell" in exc.value.message
        finally:
            eng.close_all()


def test_open_failures_since_waits_only_as_long_as_asked():
    ssh = FakeSsh()
    t = T.SshTunnel(HUB, [T.Forward("control", "192.168.10.101", 6900)], launcher=ssh,
                    ssh_g=ssh.ssh_g, restart=False)
    t.start()
    try:
        t0 = time.monotonic()
        late = _LateLines()
        ssh.current.open_failures = late
        late.append((t0 + 0.01, "channel 3: open failed: connect failed: Connection refused"))
        assert t.open_failures_since(t0) == []                   # at once: the race
        assert t.open_failures_since(t0, wait_s=1.0)[0].startswith("channel 3")
    finally:
        t.close()
        ssh.close()


# --- a short request that waited must not run under a job that claimed the board meanwhile ----


def _race(claim: bool) -> str:
    """A deploy's preflight holds the board's op lock; a close waits for it; the deploy's
    job claims the board; the preflight lets go. Which runs: the close or the job?"""
    import threading

    from harness_manager.core.errors import HeldError
    from harness_manager.daemon.jobs import BoardGates, Job

    gates = BoardGates(op_wait_s=10)
    holding, release = threading.Event(), threading.Event()
    outcome: list[str] = []

    def preflight() -> None:
        with gates.op("b"):
            holding.set()
            release.wait(5)

    def close() -> None:
        try:
            with gates.op("b"):
                outcome.append("the close ran")
        except HeldError as exc:
            outcome.append(f"held: {exc.message}")

    t1 = threading.Thread(target=preflight)
    t1.start()
    assert holding.wait(5)
    t2 = threading.Thread(target=close)
    t2.start()
    time.sleep(0.2)                              # the close now waits for the op lock
    if claim:
        gates.claim("b", Job("deploy", "b"))     # what jobs.submit does after the preflight
    release.set()
    t1.join(5)
    t2.join(5)
    return outcome[0]


def test_a_close_that_waited_behind_a_preflight_is_refused_once_the_job_has_claimed():
    # In the concurrency run the close won the lock and closed the board under the
    # deploy job, which then failed UNREACHABLE mid-way.
    assert _race(claim=True).startswith("held: b is busy: deploy job")


def test_negative_twin_with_no_job_the_waiting_close_runs():
    assert _race(claim=False) == "the close ran"


# --- a job whose board was closed before it started sends nothing -----------------------------


@pytest.mark.parametrize("close_first", [True, False])
def test_a_deploy_whose_board_closed_before_the_job_ran_sends_nothing(tmp_path, monkeypatch,
                                                                       close_first):
    from fastapi.testclient import TestClient

    from harness_manager.daemon import jobs as jobsmod
    from harness_manager.daemon.app import create_app
    from tests.fakes.t2_overlays import make_overlay, use_overlay_dirs
    from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
    from tests.fakes.virtual_board import VirtualMps3

    make_overlay(tmp_path / "ov", "synth")
    use_overlay_dirs(monkeypatch, tmp_path / "ov")
    with VirtualMps3(tmp_path) as vb:
        eng = engine_for(vb)
        real_submit = jobsmod.JobManager.submit

        def submit(self, kind, board_id, fn, **kw):
            if close_first and kind == "deploy":
                eng.close(board_id)              # the close that won the race (interleaving a)
            return real_submit(self, kind, board_id, fn, **kw)

        monkeypatch.setattr(jobsmod.JobManager, "submit", submit)
        try:
            with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
                bid = c.post("/api/v1/boards", json={"target": vb.shell_endpoint},
                             headers=headers()).json()["board_id"]
                r = c.post(f"{bid_path(bid)}/deploy", json={"overlay": "synth"},
                           headers=headers())
                assert r.status_code == 202
                job = r.json()["job"]
                _wait(lambda: c.get(f"/api/v1/jobs/{job}", headers=headers())
                      .json()["state"] != "running", what="the job")
                j = c.get(f"/api/v1/jobs/{job}", headers=headers()).json()
                if close_first:
                    assert j["state"] == "failed" and j["error"]["name"] == "ABSENT"
                    assert "closed before the deploy started" in j["error"]["message"]
                    assert vb.shell.accepted_pushes == []          # nothing reached the board
                else:                                               # twin: it deploys
                    assert j["state"] == "done", j
        finally:
            eng.close_all()


# --- `console --for` that never connected is an error, not an empty success -----------------


def test_a_console_that_never_connected_fails_instead_of_returning_empty_text(capsys,
                                                                             monkeypatch):
    from harness_manager.cli import cmd_io
    from harness_manager.cli.main import main

    monkeypatch.setattr(cmd_io, "NOT_UP_NOTE_S", 0.2)
    rc = main(["--json", "console", "127.0.0.1:1", "uart0", "--for", "1"])   # refused
    out, err = capsys.readouterr()
    assert rc == 7, out
    body = json.loads(out)
    assert body["ok"] is False and "never connected" in body["error"]["message"]
    assert "not connected yet" in err                   # said while it waited


def test_negative_twin_a_console_that_connects_returns_what_it_read(capsys, tmp_path):
    from harness_manager.cli.engine import set_engine_factory
    from harness_manager.cli.main import main
    from tests.fakes.t13_daemon import engine_for
    from tests.fakes.virtual_board import VirtualMps3

    with VirtualMps3(tmp_path) as vb:
        previous = set_engine_factory(lambda _args: engine_for(vb))
        try:
            rc = main(["--json", "console", vb.shell_endpoint, "uart0", "--for", "1"])
        finally:
            set_engine_factory(previous)
    out, _ = capsys.readouterr()
    assert rc == 0, out
    assert "nanosoc boot" in json.loads(out)["text"]


# --- MCC reads over a hub share, back to back (finding for the LR lanes: hub.py) --------------


@pytest.mark.xfail(strict=False, reason="Q2 finding (a race, so not strict): open_hub_share "
                   "decides read-only from "
                   "`share list` readers, which still counts OUR previous connection for a few "
                   "ms after it closed; the next MCC read is refused as 'another client holds "
                   "the write slot'. hub.py is the LR lanes'; the fix is in Q2_ROBUSTNESS.md.")
def test_back_to_back_mcc_reads_over_a_hub_share_both_work(tmp_path, monkeypatch):
    from harness_manager.core.services import EngineConfig
    from tests.fakes.l1_rig import BOARD_IP, lab
    from tests.fakes.virtual_board import VirtualMps3

    state = tmp_path / "state"
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state):
        eng = Engine(EngineConfig(state_dir=state))
        try:
            s = eng.open(eng.candidate_for(BOARD_IP))
            first = s.controller.temperatures()[0]
            second = s.controller.oscillators()[0]           # at once, as telemetry does
            assert first.value is not None, first.reason
            assert second.value is not None, second.reason   # was: "... holds the write slot"
        finally:
            eng.close_all()


# --- the lease heartbeat thread (finding for the LR lanes: services/lease.py) ------------------


@pytest.mark.xfail(strict=False, reason="Q2 finding: LeaseService.beat_due says it never raises "
                   "but catches only HarnessError; one OSError (a full disk on store.put) ends "
                   "the heartbeat thread for good and the lease lapses with no lease.state "
                   "event. services/lease.py is the LR lanes'; not strict so their fix does not "
                   "break the merge; remove the mark with it.")
def test_the_lease_heartbeat_survives_one_failed_round(tmp_path):
    from harness_manager.services.lease import LeaseService, StoredLease

    class Client:
        beats = 0

        def lease_heartbeat(self, token: str, holder: str) -> str:
            Client.beats += 1
            return "2026-09-25T12:00:00+00:00"

    class Hub:
        host, target, client = "hub.invalid", "mps3_01_pl", Client()

    svc = LeaseService(tmp_path, tick_s=0.02, heartbeat_s=0.05)
    svc.store.put(StoredLease(hub=Hub.host, target=Hub.target, holder="me", token="t",
                              ttl_s=600, expires_at="x", acquired_at=time.time()))
    real_put = svc.store.put

    def full(_lease) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    svc.store.put = full                           # type: ignore[method-assign]
    try:
        svc.track("b", Hub())
        _wait(lambda: Client.beats >= 1, what="the first beat")
        time.sleep(0.2)
        svc.store.put = real_put                   # type: ignore[method-assign]
        before = Client.beats
        time.sleep(0.4)
        assert Client.beats > before, "the heartbeat thread died on the OSError"
    finally:
        svc.close()


# --- a console's queued input dropped at close is a close, not "link down" --------------------


def _paced_console(name: str):
    from harness_manager.core.events import EventBus
    from harness_manager.core.transport import register_fake_serial
    from harness_manager.services.console import ConsoleBroker
    from tests.fakes.t4_console_rig import BareSession, FakeUart, _Consoles

    class Paced(_Consoles):
        def console_write_pace_s(self) -> dict[str, float]:
            return {"u": 0.05}

    uart = FakeUart()
    session = BareSession(f"q2@{name}")
    session.consoles = Paced({"u": register_fake_serial(name, uart)})
    broker = ConsoleBroker(EventBus())
    sub = broker.subscribe(session, "u")
    _wait(lambda: sub.state == "up", what="the console to connect")
    sub.write(b"x" * 40)                           # 2 s of paced input
    _wait(lambda: len(uart.written) >= 1, what="the first paced byte")
    return broker, sub, uart


def test_paced_input_left_when_the_last_reader_leaves_is_logged_as_a_close(caplog):
    from harness_manager.core.transport import unregister_fake_serial

    broker, sub, _uart = _paced_console("q2-close")
    try:
        with caplog.at_level(logging.INFO, logger="harness_manager.services.console"):
            sub.close()                            # the last reader leaves
            _wait(lambda: "unsent byte" in caplog.text, what="the drop to be logged")
        assert "closed with" in caplog.text and "link down" not in caplog.text
    finally:
        broker.shutdown()
        unregister_fake_serial("q2-close")


def test_negative_twin_a_link_that_drops_under_queued_input_still_warns(caplog):
    from harness_manager.core.transport import unregister_fake_serial

    broker, sub, uart = _paced_console("q2-drop")
    try:
        with caplog.at_level(logging.INFO, logger="harness_manager.services.console"):
            uart.unplug()                          # the link dies while input is queued
            _wait(lambda: "unsent byte" in caplog.text, what="the drop to be logged")
        assert "link down" in caplog.text
    finally:
        sub.close()
        broker.shutdown()
        unregister_fake_serial("q2-drop")


# --- Reboot is claimed at once, not after the UI's MCC reads (Q1's finding) -------------------


@pytest.mark.parametrize("busy", [True, False])
def test_reboot_is_accepted_at_once_while_an_mcc_read_is_in_flight(tmp_path, monkeypatch, busy):
    import threading

    from fastapi.testclient import TestClient

    from harness_manager.cli.output import jsonable
    from harness_manager.core.model import Candidate
    from harness_manager.daemon.app import create_app
    from harness_manager.daemon.jobs import WAITING
    from harness_manager_mps3 import mcc as mccmod
    from tests.fakes.t3_clock import FakeClock
    from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
    from tests.fakes.virtual_board import VirtualMps3

    clock = FakeClock()
    monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
    monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
    with VirtualMps3(tmp_path / "usb", usb=True) as vb:
        vb.mcc.clock = clock
        vb.mcc.down_s, vb.mcc.boot_s, vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
        counted = vb.mcc.on_reboot
        vb.mcc.on_reboot = lambda: (counted(), vb.shell.stop())
        vb.mcc.on_boot = vb.shell.start
        base = vb.candidate(ethernet=True, usb=True)
        cand = Candidate(pack="mps3", board_id=f"mps3@usb:{vb.mcc_url}/x", links=base.links,
                         label="virtual MPS3 over USB", evidence="test")
        eng = engine_for(vb)
        try:
            with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
                H = headers()
                assert c.post("/api/v1/boards", json={"candidate": jsonable(cand)},
                              headers=H).status_code == 200
                B = bid_path(cand.board_id)
                ctl = eng.session(cand.board_id).controller
                real = ctl.temperatures

                def slow_temps():                    # the MCC at 60-100 ms a character
                    time.sleep(2.0)
                    return real()

                ctl.temperatures = slow_temps
                reader = threading.Thread(
                    target=lambda: c.get(f"{B}/controller/temps", headers=H))
                if busy:
                    reader.start()
                    time.sleep(0.3)                  # the Details panel's read is in flight
                t0 = time.monotonic()
                r = c.post(f"{B}/controller/reboot", json={}, headers=H)
                accepted_s = time.monotonic() - t0
                assert r.status_code == 202, r.text
                assert accepted_s < 1.0              # was ~1.7 s here, ~6.5 s on the UI
                job = r.json()["job"]
                _wait(lambda: c.get(f"/api/v1/jobs/{job}", headers=H).json()["state"]
                      != "running", timeout=60, what="the reboot job")
                j = c.get(f"/api/v1/jobs/{job}", headers=H).json()
                assert j["state"] == "done", j
                if busy:
                    reader.join(10)
                    assert j["phases"] == [WAITING, "sent", "down", "up"]   # said so
                else:                                                        # twin
                    assert j["phases"] == ["sent", "down", "up"]
        finally:
            eng.close_all()
