"""L1: the SSH tunnel, against a fake ssh that runs real local forwarders. Each check has a twin."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from harness_manager.core.errors import ExitCode, UnreachableError, UsageError
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager_mps3 import tunnel as T
from tests.fakes.l1_fake_ssh import FakeSsh

HUB = "mapstone-dev.ecs.soton.ac.uk"


class Upstream:
    """A TCP server standing in for a board port: echoes with a prefix."""

    def __init__(self, prefix: bytes = b"echo:") -> None:
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.prefix = prefix
        self.connections = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._echo, args=(c,), daemon=True).start()

    def _echo(self, c: socket.socket) -> None:
        with c:
            while True:
                try:
                    data = c.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                c.sendall(self.prefix + data)

    def close(self) -> None:
        self.srv.close()


def roundtrip(port: int, payload: bytes = b"ping") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(payload)
        s.settimeout(3)
        return s.recv(4096)


@pytest.fixture
def board():
    up = Upstream()
    yield up
    up.close()


def make_tunnel(fake: FakeSsh, board: Upstream, **kw) -> T.SshTunnel:
    fake.routes[("192.168.10.101", 6900)] = ("127.0.0.1", board.port)
    return T.SshTunnel(HUB, [T.Forward("control", "192.168.10.101", 6900)], launcher=fake,
                       ssh_g=fake.ssh_g, backoff_s=(0.05,), **kw)


# --- via ---------------------------------------------------------------------------------------


def test_with_via_marks_the_ethernet_link_and_keeps_the_address():
    cand = Candidate("mps3", "mps3@192.168.10.101:6900",
                     (Link(LinkKind.ETHERNET, "192.168.10.101:6900", "shell control channel"),
                      Link(LinkKind.USB_MSD, "/media/sd", "V2M-MPS3")))
    routed = T.with_via(cand, f"ssh:{HUB}")
    eth = routed.links[0]
    assert eth.via == "ssh" and eth.address == "192.168.10.101:6900"
    assert T.via_host(eth) == HUB and T.candidate_via(routed) == f"ssh:{HUB}"
    assert routed.links[1] == cand.links[1]                     # other links untouched
    assert T.with_via(routed, f"ssh:{HUB}").links[0].detail == eth.detail   # idempotent
    # The twin: no via changes nothing, and a direct link names no host.
    assert T.with_via(cand, "") is cand and T.candidate_via(cand) == ""


def test_a_via_that_is_not_ssh_host_is_a_usage_error():
    for bad in ("mapstone-dev", "hub:mapstone-dev", "ssh:", "ssh:two words"):
        with pytest.raises(UsageError):
            T.parse_via(bad)
    assert T.parse_via(f"ssh:{HUB}") == HUB


def test_a_hand_made_forward_marked_via_ssh_without_a_host_is_opened_as_it_is():
    cand = Candidate("mps3", "mps3@127.0.0.1:16900",
                     (Link(LinkKind.ETHERNET, "127.0.0.1:16900", "shell control channel", via="ssh"),))
    assert T.open_reach(cand, {}) is None


# --- the tunnel --------------------------------------------------------------------------------


def test_the_tunnel_forwards_bytes_and_uses_the_safe_ssh_options(board):
    fake = FakeSsh()
    with make_tunnel(fake, board) as t:
        local = t.local_port("control")
        assert roundtrip(local) == b"echo:ping"
        argv = fake.launches[0]
        for opt in ("ControlPath=none", "ExitOnForwardFailure=yes", "BatchMode=yes",
                    "ServerAliveInterval=15"):
            assert opt in argv
        assert "-N" in argv and argv[-1] == HUB
        spec = argv[argv.index("-L") + 1]
        assert spec == f"127.0.0.1:{local}:192.168.10.101:6900"      # loopback bind only
        assert t.status()["state"] == "up" and t.ports() == {6900: local}
    assert not T.listening(local)                                   # closed with ssh
    assert fake.procs[0].poll() is not None


def test_negative_twin_an_ssh_that_cannot_log_in_is_unreachable_with_its_reason(board):
    fake = FakeSsh(fail="auth")
    t = make_tunnel(fake, board)
    with pytest.raises(UnreachableError) as exc:
        t.start()
    assert exc.value.code == ExitCode.UNREACHABLE
    assert "Permission denied" in exc.value.message and "BatchMode" in exc.value.hint
    assert t.state == "down"


def test_a_taken_local_port_fails_the_start_like_exit_on_forward_failure(board):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    taken = blocker.getsockname()[1]
    try:
        fake = FakeSsh({("192.168.10.101", 6900): ("127.0.0.1", board.port)})
        t = T.SshTunnel(HUB, [T.Forward("control", "192.168.10.101", 6900, local_port=taken)],
                        launcher=fake, ssh_g=fake.ssh_g)
        with pytest.raises(UnreachableError) as exc:
            t.start()
        assert "Address already in use" in exc.value.message
    finally:
        blocker.close()


def test_a_dropped_tunnel_restarts_on_the_same_local_port(board):
    fake = FakeSsh()
    states: list[str] = []
    t = make_tunnel(fake, board)
    t.watch(lambda st: states.append(st["state"]))
    with t:
        local = t.local_port("control")
        assert roundtrip(local) == b"echo:ping"
        fake.current.drop()
        deadline = time.monotonic() + 10
        while (t.restarts < 1 or t.state != "up") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert t.restarts == 1 and len(fake.launches) == 2
        assert fake.launches[1] == fake.launches[0]                 # same argv, same ports
        assert roundtrip(local, b"again") == b"echo:again"
        assert "down" in states and states[-1] == "up"
    assert states[-1] == "down" and t.detail == "closed with the board"


def test_negative_twin_a_closed_tunnel_does_not_restart(board):
    fake = FakeSsh()
    t = make_tunnel(fake, board).start()
    t.close()
    time.sleep(0.3)
    assert len(fake.launches) == 1 and not fake.live()


def test_a_forward_to_a_dead_board_port_accepts_then_closes_and_ssh_says_why(board):
    """What a client sees through ssh -L when the board refuses (the ambiguity in shell.py)."""
    fake = FakeSsh()
    t = T.SshTunnel(HUB, [T.Forward("rbb", "192.168.10.101", 6921)], launcher=fake,
                    ssh_g=fake.ssh_g)
    with t:
        with socket.create_connection(("127.0.0.1", t.local_port("rbb")), timeout=2) as s:
            s.settimeout(2)
            assert s.recv(10) == b""                                # accepted, then EOF
        assert "open failed: connect failed" in fake.current.stderr_tail


def test_the_local_port_is_never_2542(monkeypatch):
    real = socket.socket

    class First2542(real):  # type: ignore[misc, valid-type]
        handed = False

        def getsockname(self):
            if not First2542.handed:
                First2542.handed = True
                return ("127.0.0.1", 2542)
            return super().getsockname()

    monkeypatch.setattr(T.socket, "socket", First2542)
    assert T.free_local_port() != 2542
    with pytest.raises(UsageError):
        T.SshTunnel(HUB, [T.Forward("xvc", "192.168.10.101", 2542, local_port=2542)])


def test_listening_reads_the_kernel_table_without_connecting():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    assert not T.listening(port)                                    # bound is not listening
    s.listen(1)
    assert T.listening(port)
    s.close()


# --- the user's ssh config -------------------------------------------------------------------

DAVIDS_BLOCK = """\
Host mapstone-dev mapstone-dev.ecs.soton.ac.uk
    HostName mapstone-dev.ecs.soton.ac.uk
    LocalForward 18081 localhost:8081
    LocalForward 18082 localhost:8082
    ServerAliveInterval 30
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 60

Host haps-xvc
    LocalForward 2542 haps-sx:2542
    ExitOnForwardFailure yes
"""


def test_config_forwards_for_the_host_are_left_out_with_a_filtered_copy(tmp_path, board):
    cfg = tmp_path / "config"
    cfg.write_text(DAVIDS_BLOCK)
    fake = FakeSsh(config_output="hostname mapstone-dev.ecs.soton.ac.uk\n"
                                 "localforward 18081 [localhost]:8081\n"
                                 "localforward 18082 [localhost]:8082\n")
    argv = T.ssh_base_argv(HUB, ssh_g=fake.ssh_g, user_config=cfg, system_config=None,
                           config_dir=tmp_path / "tunnel")
    assert argv[:2] == ["ssh", "-F"]
    copy = Path(argv[2])
    text = copy.read_text()
    assert "# [harness-manager: forward removed]     LocalForward 18081" in text
    assert "\n    LocalForward" not in text                         # none left live
    assert "ControlMaster auto" in text                               # everything else kept
    assert oct(copy.stat().st_mode & 0o777) == "0o600"
    assert fake.g_calls[-1][:3] == ["ssh", "-F", str(copy)]          # checked before use


def test_negative_twin_no_config_forwards_means_the_plain_config(tmp_path):
    fake = FakeSsh(config_output="hostname h\nuser u\n")
    assert T.ssh_base_argv(HUB, ssh_g=fake.ssh_g, config_dir=tmp_path) == ["ssh"]


def test_forwards_the_copy_cannot_remove_refuse_the_tunnel(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Include extra.conf\n")

    def ssh_g(argv):                      # the forward comes from an Include'd file
        return "localforward 18081 [localhost]:8081\n"

    with pytest.raises(UsageError) as exc:
        T.ssh_base_argv(HUB, ssh_g=ssh_g, user_config=cfg, system_config=None,
                        config_dir=tmp_path / "t")
    assert "Include" in exc.value.message


@pytest.mark.skipif(shutil.which("ssh") is None, reason="no OpenSSH client")
def test_real_ssh_parses_the_filtered_copy_and_sees_no_forwards(tmp_path):
    """OpenSSH itself (ssh -G evaluates config, never connects) accepts the copy."""
    cfg = tmp_path / "config"
    cfg.write_text(DAVIDS_BLOCK)

    def real_g(argv):
        out = subprocess.run([*argv[:-2], "-F", str(cfg), "-G", argv[-1]] if "-F" not in argv
                             else list(argv), capture_output=True, text=True, timeout=10)
        assert out.returncode == 0, out.stderr
        return out.stdout

    assert T.config_forwards(real_g(["ssh", "-G", "mapstone-dev"]))       # the twin: they exist
    argv = T.ssh_base_argv("mapstone-dev", ssh_g=real_g, user_config=cfg, system_config=None,
                           config_dir=tmp_path / "t")
    assert argv[1] == "-F"
    shown = subprocess.run(["ssh", "-F", argv[2], "-G", "mapstone-dev"], capture_output=True,
                           text=True, timeout=10)
    assert shown.returncode == 0 and "controlmaster auto" in shown.stdout
    assert not T.config_forwards(shown.stdout)


@pytest.mark.skipif(shutil.which("ssh") is None, reason="no OpenSSH client")
def test_real_ssh_accepts_the_tunnel_command_line(tmp_path):
    """The exact argv the tunnel runs, checked by OpenSSH with -G (evaluate, never connect):
    every option parses, the forwards are ours on 127.0.0.1, and the config's are gone."""
    cfg = tmp_path / "config"
    cfg.write_text(DAVIDS_BLOCK)

    def real_g(argv):
        argv = list(argv)
        if "-F" not in argv:
            argv[1:1] = ["-F", str(cfg)]
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        assert out.returncode == 0, out.stderr
        return out.stdout

    t = T.SshTunnel("mapstone-dev", [T.Forward("control", "192.168.10.101", 6900),
                                     T.Forward("share", "127.0.0.1", 12000)], ssh_g=real_g,
                    user_config=cfg, system_config=None)
    argv = t.build_argv()
    assert argv[:2] == ["ssh", "-F"]                     # the config gives the host forwards
    shown = subprocess.run(["ssh", "-G", *[a for a in argv[1:] if a not in ("-N", "-T")]],
                           capture_output=True, text=True, timeout=10)
    assert shown.returncode == 0, shown.stderr
    forwards = [ln for ln in shown.stdout.splitlines() if ln.startswith("localforward")]
    assert forwards == [
        f"localforward [127.0.0.1]:{t.local_port('control')} [192.168.10.101]:6900",
        f"localforward [127.0.0.1]:{t.local_port('share')} [127.0.0.1]:12000"]
    for want in ("exitonforwardfailure yes", "batchmode yes", "serveraliveinterval 15",
                 "controlmaster false"):
        assert want in shown.stdout
