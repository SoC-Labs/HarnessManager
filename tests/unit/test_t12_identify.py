"""T12: the UDP 6899 identify client and ``probe_identify`` (the pack.py hook).

Responders are tests/fakes/fake_identify.py on 127.0.0.1 ephemeral ports; the
environment seams point the client at them, so nothing leaves loopback.
"""

from __future__ import annotations

import json
import socket
import time

import pytest

from harness_manager.core.errors import UnreachableError, UsageError
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.pack import ProbeHints
from harness_manager_mps3 import identify as ident
from tests.fakes.fake_identify import FakeIdentifyResponder, canonical_reply


def closed_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- the wire ---------------------------------------------------------------------------


def test_request_is_the_agreed_shape():
    assert json.loads(ident.encode_request("0123456789abcdef")) == \
        {"op": "identify", "v": 1, "nonce": "0123456789abcdef"}
    assert 8 <= len(ident.new_nonce()) <= 32


@pytest.mark.parametrize("nonce", ["1234567", "x" * 16, "0" * 33])
def test_negative_twin_a_bad_nonce_is_refused_before_sending(nonce):
    with pytest.raises(UsageError):
        ident.encode_request(nonce)


def test_parse_reply_checks_op_nonce_and_size():
    good = json.dumps(canonical_reply("aabbccdd")).encode()
    assert ident.parse_reply(good, "aabbccdd", ("10.0.0.5", 6899)) is not None
    assert ident.parse_reply(good, "aabbccdd".upper(), ("10.0.0.5", 6899)) is not None
    assert ident.parse_reply(good, "00000000", ("10.0.0.5", 6899)) is None      # someone else's
    other = json.dumps({**canonical_reply("aabbccdd"), "op": "ping"}).encode()
    assert ident.parse_reply(other, "aabbccdd", ("10.0.0.5", 6899)) is None
    assert ident.parse_reply(b"{not json", "aabbccdd", ("10.0.0.5", 6899)) is None
    big = json.dumps(canonical_reply("aabbccdd", pad="x" * 1300)).encode()
    assert ident.parse_reply(big, "aabbccdd", ("10.0.0.5", 6899)) is None


def test_reply_properties_linux_and_bare_metal():
    linux = ident.IdentifyReply(canonical_reply("n"), ("10.0.0.5", 6899))
    assert (linux.impl, linux.mode, linux.os_up_ms, linux.control_port) == ("linux", "run", 60000, 6900)
    bare = ident.IdentifyReply(canonical_reply("n", impl=None, os_up_ms=None, ssh=None),
                               ("10.0.0.6", 6899))
    assert bare.impl == "bare-metal" and bare.os_up_ms is None and bare.ssh == {}


def test_board_id_prefers_unit_then_the_answering_address():
    with_unit = ident.IdentifyReply(canonical_reply("n", unit="dna-00ff"), ("10.0.0.5", 6899))
    assert with_unit.board_id == "mps3@dna-00ff"
    no_unit = ident.IdentifyReply(canonical_reply("n", ip="192.168.10.101"), ("10.0.0.5", 6899))
    assert no_unit.board_id == "mps3@10.0.0.5:6900"          # the address that answered us


def test_rescue_reply_is_stage0_not_a_harness():
    r = ident.IdentifyReply(canonical_reply("n", mode="rescue", reason="no card", impl=None,
                                            ports={"tftp": 69}), ("192.168.10.101", 6899))
    assert r.is_rescue and r.impl == "" and r.harness == "" and r.control_port == 6900
    cand = r.candidate()
    assert "RESCUE" in cand.evidence and "no card" in cand.evidence
    assert "rescue" in cand.links[0].detail and cand.identity.harness_impl == ""
    assert cand.identity.shell_id == "0x3f1a560f"


def test_negative_twin_run_mode_candidate_is_not_rescue():
    cand = ident.IdentifyReply(canonical_reply("n"), ("10.0.0.5", 6899)).candidate()
    assert "RESCUE" not in cand.evidence and cand.identity.harness_impl == "linux"
    assert cand.links == (Link(LinkKind.ETHERNET, "10.0.0.5:6900",
                               "shell control channel (found by identify)"),)


# -- unicast ----------------------------------------------------------------------------


def test_unicast_identify_against_a_responder():
    with FakeIdentifyResponder(lambda n: canonical_reply(n)) as r:
        got = ident.identify("127.0.0.1", r.port, timeout=1.0)
    assert got.ok and got.source == ("127.0.0.1", r.port) and got.probe_port == r.port
    assert r.requests[0][1]["op"] == "identify" and r.replies_sent == 1


def test_negative_twin_a_closed_loopback_port_fails_fast():
    t0 = time.monotonic()
    with pytest.raises(UnreachableError):
        ident.identify("127.0.0.1", closed_udp_port(), timeout=2.0)
    assert time.monotonic() - t0 < 1.0           # connected socket: ECONNREFUSED at once


def test_a_silent_responder_times_out():
    with FakeIdentifyResponder(lambda n: None) as r:
        with pytest.raises(UnreachableError, match="nothing answered"):
            ident.identify("127.0.0.1", r.port, timeout=0.2, retries=1)
        assert len(r.requests) == 2 and r.replies_sent == 0      # re-sent once, both silent


def test_a_reply_with_the_wrong_nonce_is_ignored():
    with FakeIdentifyResponder(lambda n: canonical_reply("ffffffffffffffff")) as r:
        with pytest.raises(UnreachableError):
            ident.identify("127.0.0.1", r.port, timeout=0.2, retries=0)


def test_the_fake_is_silent_on_a_malformed_request():
    with FakeIdentifyResponder(lambda n: canonical_reply(n)) as r:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.sendto(b'{"op":"identify","v":1,"nonce":"zz"}', ("127.0.0.1", r.port))
            with pytest.raises(TimeoutError):
                s.recvfrom(2048)
        assert r.requests[0][1] is None


# -- broadcast discovery ----------------------------------------------------------------


def test_discover_collects_every_board_once():
    with FakeIdentifyResponder(lambda n: canonical_reply(n, unit="dna-a")) as a, \
            FakeIdentifyResponder(lambda n: canonical_reply(n, unit="dna-b")) as b:
        replies = ident.discover([("127.0.0.1", a.port), ("127.0.0.1", b.port),
                                  ("127.0.0.1", a.port)], timeout=0.4)
    assert sorted(r.unit for r in replies) == ["dna-a", "dna-b"]


def test_negative_twin_discover_with_nobody_listening_is_empty():
    assert ident.discover([("127.0.0.1", closed_udp_port())], timeout=0.2) == []


def test_broadcast_env_replaces_the_default_targets(monkeypatch):
    monkeypatch.setenv(ident.IDENTIFY_PORT_ENV, "7000")
    assert ident.broadcast_targets() == [("255.255.255.255", 7000)]
    monkeypatch.setenv(ident.IDENTIFY_BROADCAST_ENV, "127.0.0.1:1234, 127.0.0.2")
    assert ident.broadcast_targets() == [("127.0.0.1", 1234), ("127.0.0.2", 7000)]


def test_bad_identify_port_env_is_usage(monkeypatch):
    monkeypatch.setenv(ident.IDENTIFY_PORT_ENV, "70000")
    with pytest.raises(UsageError):
        ident.identify_port()


# -- probe_identify: the pack hook ------------------------------------------------------------


def test_probe_without_hosts_broadcasts_and_builds_candidates(monkeypatch):
    ports_a = {**canonical_reply()["ports"], "ctrl": 16900}      # two boards, two endpoints
    with FakeIdentifyResponder(lambda n: canonical_reply(n, unit="dna-a", ports=ports_a)) as a, \
            FakeIdentifyResponder(lambda n: canonical_reply(
                n, mode="rescue", reason="blank card", impl=None, ports={"tftp": 69})) as b:
        monkeypatch.setenv(ident.IDENTIFY_BROADCAST_ENV,
                           f"127.0.0.1:{a.port},127.0.0.1:{b.port}")
        cands = ident.probe_identify(ProbeHints(timeout_s=0.4), [])
    by_id = {c.board_id: c for c in cands}
    assert set(by_id) == {"mps3@dna-a", "mps3@127.0.0.1:6900"}
    assert by_id["mps3@dna-a"].identity.harness_impl == "linux"
    assert by_id["mps3@dna-a"].links[0].address == "127.0.0.1:16900"
    assert "RESCUE" in by_id["mps3@127.0.0.1:6900"].evidence


def test_probe_with_hosts_skips_boards_the_6900_ping_found(monkeypatch):
    with FakeIdentifyResponder(lambda n: canonical_reply(n)) as r:
        monkeypatch.setenv(ident.IDENTIFY_PORT_ENV, str(r.port))
        found = [Candidate("mps3", "mps3@127.0.0.1:6900",
                           (Link(LinkKind.ETHERNET, "127.0.0.1:6900"),))]
        assert ident.probe_identify(ProbeHints(hosts=("127.0.0.1",)), found) == []
        assert r.requests == []                                  # not even asked


def test_negative_twin_probe_with_hosts_finds_a_board_the_ping_missed(monkeypatch):
    with FakeIdentifyResponder(lambda n: canonical_reply(
            n, mode="rescue", impl=None, ports={"tftp": 69})) as r:
        monkeypatch.setenv(ident.IDENTIFY_PORT_ENV, str(r.port))
        (cand,) = ident.probe_identify(ProbeHints(hosts=("127.0.0.1",), timeout_s=0.5), [])
    assert cand.board_id == "mps3@127.0.0.1:6900" and "RESCUE" in cand.evidence


def test_probe_dedupes_against_found_by_board_id(monkeypatch):
    with FakeIdentifyResponder(lambda n: canonical_reply(n, unit="dna-a")) as r:
        monkeypatch.setenv(ident.IDENTIFY_BROADCAST_ENV, f"127.0.0.1:{r.port}")
        found = [Candidate("mps3", "mps3@dna-a", (Link(LinkKind.USB_SERIAL, "/dev/ttyUSB0"),))]
        assert ident.probe_identify(ProbeHints(timeout_s=0.3), found) == []


def test_probe_ignores_a_not_ok_reply(monkeypatch):
    with FakeIdentifyResponder(lambda n: canonical_reply(n, ok=False)) as r:
        monkeypatch.setenv(ident.IDENTIFY_BROADCAST_ENV, f"127.0.0.1:{r.port}")
        assert ident.probe_identify(ProbeHints(timeout_s=0.3), []) == []
