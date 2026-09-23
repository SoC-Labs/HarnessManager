"""T12: the control-port failure mapping and health states, on both harness engines.

Each wire behaviour comes from tests/fakes/t12_raw_servers.py (one behaviour per
server), and each check has a negative twin: the neighbouring behaviour that
must NOT map the same way.
"""

from __future__ import annotations

import socket

import pytest

from harness_manager.core.errors import ExitCode, HeldError, UnreachableError
from harness_manager.core.model import Check
from harness_manager_mps3 import shell as shellmod
from harness_manager_mps3.identify import IdentifyReply
from harness_manager_mps3.shell import (
    Mps3Shell,
    ShellProbes,
    ShellRefusedError,
    ShellRescueError,
    ShellSilentError,
    ShellWedgedError,
)
from tests.fakes.fake_identify import FakeIdentifyResponder, canonical_reply
from tests.fakes.t12_raw_servers import PING, VERSION_BARE, VERSION_LINUX, RawShell


def dead_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Probes:
    """Recording diagnosis probes: nothing touches the network."""

    def __init__(self, reply=None, tcp=False, icmp=False) -> None:
        self.reply, self.tcp_answer, self.icmp_answer = reply, tcp, icmp
        self.calls: list[str] = []

    def as_probes(self) -> ShellProbes:
        def ident(host, timeout):
            self.calls.append("identify")
            return self.reply

        def tcp(host, port, timeout):
            self.calls.append(f"tcp{port}")
            return self.tcp_answer

        def icmp(host, timeout):
            self.calls.append("icmp")
            return self.icmp_answer

        return ShellProbes(identify=ident, tcp=tcp, icmp=icmp)


def reply(**over) -> IdentifyReply:
    return IdentifyReply(raw=canonical_reply("00112233", **over), source=("10.0.0.5", 6899))


# -- connect refused -> offline ------------------------------------------------------------


def test_refused_is_unreachable_and_offline():
    sh = Mps3Shell("127.0.0.1", dead_port(), timeout=1, probes=Probes().as_probes())
    with pytest.raises(ShellRefusedError) as exc:
        sh.identity()
    assert exc.value.code == ExitCode.UNREACHABLE and "refused" in exc.value.message
    h = sh.health()
    assert (h.reachable, h.control_channel) == (False, "offline")


def test_negative_twin_a_listening_shell_is_not_offline():
    with RawShell(replies={"ping": PING, "version": VERSION_BARE, "diag": {"ok": True}}) as srv:
        h = Mps3Shell("127.0.0.1", srv.port, timeout=1).health()
        assert (h.reachable, h.control_channel) == (True, "idle")


def test_loopback_diagnosis_never_trusts_the_local_machine():
    """127.0.0.1 is a fake or a tunnel end: the local sshd or ICMP says nothing about a board."""
    probes = Probes(tcp=True, icmp=True)
    Mps3Shell("127.0.0.1", dead_port(), timeout=1, probes=probes.as_probes()).health()
    assert probes.calls == ["identify"]


def test_refused_but_identify_answers_is_os_up_harness_down():
    probes = Probes(reply=reply())
    sh = Mps3Shell("10.0.0.5", 6900, probes=probes.as_probes())
    d = sh.diagnose(ShellRefusedError("x"))
    assert d.state == "service_down" and "identify answers (linux)" in d.notes[0]
    assert d.health().control_channel == "offline" and d.health().reachable


def test_refused_but_ssh_answers_is_os_up_harness_down():
    probes = Probes(tcp=True)
    d = Mps3Shell("10.0.0.5", 6900, probes=probes.as_probes()).diagnose(ShellRefusedError("x"))
    assert d.state == "service_down" and "SSH answers" in d.notes[0]
    assert probes.calls == ["identify", "tcp22"]


def test_refused_but_icmp_answers_is_harness_down():
    probes = Probes(icmp=True)
    d = Mps3Shell("10.0.0.5", 6900, probes=probes.as_probes()).diagnose(ShellRefusedError("x"))
    assert d.state == "service_down" and "ICMP" in d.notes[0]


def test_negative_twin_nothing_answers_is_plain_offline():
    probes = Probes()
    d = Mps3Shell("10.0.0.5", 6900, probes=probes.as_probes()).diagnose(ShellRefusedError("x"))
    assert d.state == "offline" and not d.health().reachable
    assert probes.calls == ["identify", "tcp22", "icmp"]


def test_silent_connect_only_asks_ssh_when_refused():
    """A connect that TIMES OUT is not "harness down": SSH is not asked; ICMP still is."""
    probes = Probes(tcp=True, icmp=True)
    d = Mps3Shell("10.0.0.5", 6900, probes=probes.as_probes()).diagnose(ShellSilentError("x"))
    assert "tcp22" not in probes.calls and d.state == "service_down"
    assert "does not accept" in d.notes[0]


def test_connect_timeout_is_silent_not_wedged(monkeypatch):
    def slow(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(shellmod, "SocketTransport", slow)
    sh = Mps3Shell("10.0.0.5", 6900, timeout=0.1, probes=Probes().as_probes())
    with pytest.raises(ShellSilentError, match="did not answer"):
        sh.live()
    assert sh.health().control_channel == "offline"


# -- rescue ---------------------------------------------------------------------------


def test_rescue_board_health_and_identity():
    probes = Probes(reply=reply(mode="rescue", reason="slot A CRC failed", impl=None,
                                harness="", ssh=None, ports={"tftp": 69}))
    sh = Mps3Shell("127.0.0.1", dead_port(), timeout=1, probes=probes.as_probes())
    h = sh.health()
    assert (h.reachable, h.control_channel) == (True, "rescue")
    assert any("RESCUE" in n for n in h.notes) and any("slot A CRC failed" in n for n in h.notes)
    with pytest.raises(ShellRescueError) as exc:  # no harness to identify: an error...
        sh.identity()
    assert exc.value.code == ExitCode.UNREACHABLE and "slot A CRC failed" in exc.value.message
    ident = exc.value.identity                   # ...that carries what stage0 said
    assert ident.shell_id == "0x3f1a560f" and ident.harness_impl == "" and ident.features == ()


def test_negative_twin_run_mode_identify_does_not_hide_a_refused_port():
    probes = Probes(reply=reply())
    sh = Mps3Shell("127.0.0.1", dead_port(), timeout=1, probes=probes.as_probes())
    with pytest.raises(ShellRefusedError) as exc:
        sh.identity()
    assert "harness service" in exc.value.hint and not isinstance(exc.value, ShellRescueError)


# -- accepted, never replied -> wedged --------------------------------------------------


def test_silent_after_accept_is_wedged():
    with RawShell("silent") as srv:
        sh = Mps3Shell("127.0.0.1", srv.port, timeout=0.3, probes=Probes().as_probes())
        with pytest.raises(ShellWedgedError) as exc:
            sh.identity()
        assert exc.value.code == ExitCode.UNREACHABLE and "did not reply" in exc.value.message
        assert "hung" in exc.value.hint
        h = sh.health()
        assert (h.reachable, h.control_channel) == (True, "wedged")


def test_negative_twin_a_reply_is_not_wedged():
    with RawShell(replies={"ping": PING, "version": VERSION_BARE}) as srv:
        assert Mps3Shell("127.0.0.1", srv.port, timeout=0.3).identity().shell_id == "0x1a102610"


def test_wedged_does_not_run_the_network_diagnosis():
    probes = Probes(reply=reply())
    with RawShell("silent") as srv:
        Mps3Shell("127.0.0.1", srv.port, timeout=0.3, probes=probes.as_probes()).health()
    assert probes.calls == []


# -- held: EOF, RST, EBUSY --------------------------------------------------------------


@pytest.mark.parametrize("behaviour", ["eof", "rst"])
def test_accept_then_eof_or_rst_is_held(behaviour):
    with RawShell(behaviour) as srv:
        sh = Mps3Shell("127.0.0.1", srv.port, timeout=1, probes=Probes().as_probes())
        with pytest.raises(HeldError) as exc:
            sh.live()
        assert exc.value.code == ExitCode.HELD
        assert sh.health().control_channel == "busy"


def test_ebusy_is_held_with_the_holder():
    with RawShell("ebusy") as srv:
        sh = Mps3Shell("127.0.0.1", srv.port, timeout=1, probes=Probes().as_probes())
        with pytest.raises(HeldError) as exc:
            sh.live()
        assert "EBUSY" in exc.value.message and exc.value.holder == "10.1.2.3:40000"
        h = sh.health()
        assert h.control_channel == "busy" and h.counters == {}   # not "idle with zero counters"
        assert any("10.1.2.3:40000" in n for n in h.notes)


def test_negative_twin_an_ordinary_failure_is_not_ebusy():
    with RawShell(replies={"ping": {"ok": False, "err": "bad args"}}) as srv:
        with pytest.raises(Exception) as exc:
            Mps3Shell("127.0.0.1", srv.port, timeout=1).live()
        assert not isinstance(exc.value, HeldError) and "bad args" in str(exc.value)


def test_held_identity_falls_back_to_identify(monkeypatch):
    """identity() does not fail on EBUSY: identify (UDP) is independent of 6900."""
    with RawShell("ebusy") as srv, FakeIdentifyResponder(
            lambda n: canonical_reply(n, unit="dna-0123456789abcdef")) as ident:
        monkeypatch.setenv("HARNESS_MANAGER_MPS3_IDENTIFY_PORT", str(ident.port))
        got = Mps3Shell("127.0.0.1", srv.port, timeout=1).identity()
    assert got.shell_id == "0x3f1a560f" and got.harness_impl == "linux"
    assert got.unit_id == "dna-0123456789abcdef" and got.build_check == Check.UNCHECKED


def test_negative_twin_held_without_identify_still_raises_held(monkeypatch):
    with RawShell("ebusy") as srv:
        monkeypatch.setenv("HARNESS_MANAGER_MPS3_IDENTIFY_PORT", str(dead_port()))
        with pytest.raises(HeldError):
            Mps3Shell("127.0.0.1", srv.port, timeout=1).identity()


def test_version_ebusy_after_a_good_ping_keeps_the_ping():
    with RawShell(replies={"ping": PING, "version": {"ok": False, "err": "EBUSY"}}) as srv:
        live = Mps3Shell("127.0.0.1", srv.port, timeout=1).live()
        ident = Mps3Shell("127.0.0.1", srv.port, timeout=1).identity()
    assert live.version_busy and not live.version_ok
    assert ident.shell_id == "0x1a102610" and ident.harness_impl == ""


# -- identity: impl / proto / usercode ----------------------------------------------------


def test_identity_reads_impl_linux():
    with RawShell(replies={"ping": PING, "version": VERSION_LINUX}) as srv:
        ident = Mps3Shell("127.0.0.1", srv.port, timeout=1).identity()
    assert ident.harness_impl == "linux" and "windowed" not in ident.features


def test_negative_twin_impl_absent_is_bare_metal_and_no_version_is_unknown():
    with RawShell(replies={"ping": PING, "version": VERSION_BARE}) as srv:
        assert Mps3Shell("127.0.0.1", srv.port, timeout=1).identity().harness_impl == "bare-metal"
    with RawShell(replies={"ping": PING}) as srv:          # v0.7: "unknown op"
        ident = Mps3Shell("127.0.0.1", srv.port, timeout=1).identity()
    assert ident.harness_impl == "" and ident.harness_version == "" and ident.proto == ""


def test_identity_reads_proto_and_usercode_when_the_harness_sends_them():
    version = {**VERSION_BARE, "proto": "0.12", "usercode": "0xD46FCDCB"}
    with RawShell(replies={"ping": PING, "version": version}) as srv:
        ident = Mps3Shell("127.0.0.1", srv.port, timeout=1).identity()
    assert (ident.proto, ident.usercode) == ("0.12", "0xd46fcdcb")
    with RawShell(replies={"ping": PING, "version": VERSION_BARE}) as srv:
        ident = Mps3Shell("127.0.0.1", srv.port, timeout=1).identity()
    assert (ident.proto, ident.usercode) == ("", "")


def test_identity_skew_is_a_mismatch_not_a_pass():
    version = {**VERSION_BARE, "usr_access": "0x01000100", "skew": True}
    with RawShell(replies={"ping": PING, "version": version}) as srv:
        assert Mps3Shell("127.0.0.1", srv.port, timeout=1).identity().build_check == Check.MISMATCH


# -- health counters: only the keys that were sent ------------------------------------------


def test_health_counters_only_for_keys_present():
    diag = {"ok": True, "rx_drops": 3, "icap_bytes": 886432, "svc_skipped": 0}
    with RawShell(replies={"diag": diag}) as srv:
        h = Mps3Shell("127.0.0.1", srv.port, timeout=1).health()
    assert h.counters == {"rx_drops": 3, "icap_bytes": 886432, "svc_skipped": 0}
    assert "pbuf_free" not in h.counters        # pyverify would have said 0


def test_negative_twin_a_full_diag_keeps_every_counter():
    from pyverify.testing.fakeshell import DIAG_COUNTERS

    diag = {"ok": True, **{k: 1 for k in DIAG_COUNTERS}}
    with RawShell(replies={"diag": diag}) as srv:
        h = Mps3Shell("127.0.0.1", srv.port, timeout=1).health()
    assert set(h.counters) == set(DIAG_COUNTERS)


def test_health_notes_a_skipped_service():
    with RawShell(replies={"diag": {"ok": True, "svc_skipped": 4}}) as srv:
        h = Mps3Shell("127.0.0.1", srv.port, timeout=1).health()
    assert any("skipped" in n for n in h.notes)


def test_unreachable_subclasses_keep_exit_code_7():
    for cls in (ShellRefusedError, ShellSilentError, ShellWedgedError):
        assert issubclass(cls, UnreachableError) and cls("x").code == ExitCode.UNREACHABLE
