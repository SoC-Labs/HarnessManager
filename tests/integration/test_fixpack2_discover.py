"""FIX-PACK-2 item 1: "Find boards on the network" (``discover_network``) is gated on an Ethernet
link and a board that answers identify, never on a feature no harness lists.

The capability was gated on the harness feature ``identify``; no image lists it (identify is
a service every Linux harness since v0.11, and bare metal since FOLD A-v0.12, serves on UDP
6899), so it was unavailable everywhere. Now an Ethernet link allows it and a recent identify
answer is the proof (``identify.DiscoverWitness``, through the engine's
``session.capability_reasons`` seam). Board-free: a ``VirtualMps3`` and its UDP responder on
127.0.0.1. Each check has a negative twin.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import negotiate
from harness_manager.core.errors import UnreachableError
from harness_manager.core.model import BoardIdentity, Link, LinkKind
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3 import identify as ident
from harness_manager_mps3.capabilities import NEEDS_ETHERNET_LAN, SPECS
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t1_fakes import FakePack, candidate
from tests.fakes.virtual_board import FIELDED_3F1A560F, LINUX_HARNESSD, VirtualMps3


def engine_for(vb: VirtualMps3, tmp_path) -> Engine:
    return Engine(EngineConfig(state_dir=tmp_path / "state"),
                  packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})


# --- the spec: an Ethernet link, no feature -------------------------------------------------------


def test_the_spec_needs_only_an_ethernet_link_and_no_feature():
    avail, _ = negotiate(SPECS, [LinkKind.ETHERNET], ())
    assert C.DISCOVER_NETWORK in avail                  # no 'identify' feature needed
    for spec in SPECS:
        if spec.name == C.DISCOVER_NETWORK:
            assert all(not r.features for r in spec.routes)


def test_negative_twin_without_an_ethernet_link_it_says_so():
    avail, why = negotiate(SPECS, [LinkKind.USB_SERIAL, LinkKind.USB_MSD], ())
    assert C.DISCOVER_NETWORK not in avail
    assert why[C.DISCOVER_NETWORK] == NEEDS_ETHERNET_LAN
    assert "firmware" not in why[C.DISCOVER_NETWORK]


# --- a real board session: identify is the proof --------------------------------------------------


def test_a_linux_board_that_answers_identify_can_find_boards(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        monkeypatch.setenv(ident.IDENTIFY_PORT_ENV, str(vb.identify_port))
        eng = engine_for(vb, tmp_path)
        try:
            cand = vb.candidate()
            session = eng.open(cand)
            info = eng.info(cand.board_id)
            assert "identify" not in info.identity.features     # what the old gate wanted
            assert C.DISCOVER_NETWORK in info.capabilities
            assert C.DISCOVER_NETWORK not in info.unavailable
            eng.info(cand.board_id)                              # within the TTL: no re-ask
            assert session._discover.probes == 1
        finally:
            eng.close_all()


def test_negative_twin_a_board_that_does_not_answer_identify_says_why(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, FIELDED_3F1A560F) as vb:     # no UDP responder
        assert vb.identify_port == 0
        with VirtualMps3(tmp_path / "other", LINUX_HARNESSD) as quiet:
            port = quiet.identify_port
        monkeypatch.setenv(ident.IDENTIFY_PORT_ENV, str(port))   # nothing listens there now
        eng = engine_for(vb, tmp_path)
        try:
            cand = vb.candidate()
            eng.open(cand)
            info = eng.info(cand.board_id)
            assert C.DISCOVER_NETWORK not in info.capabilities
            why = info.unavailable[C.DISCOVER_NETWORK]
            assert why.startswith("the board did not answer identify from here")
            assert "UDP 6899" in why and "'identify'" not in why
        finally:
            eng.close_all()


def test_through_a_tunnel_it_is_unavailable_without_a_probe(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        monkeypatch.setenv(ident.IDENTIFY_PORT_ENV, str(vb.identify_port))
        eng = engine_for(vb, tmp_path)
        try:
            cand = vb.candidate(tunnel=True)                     # a TCP-only tunnel
            session = eng.open(cand)
            info = eng.info(cand.board_id)
            assert info.unavailable[C.DISCOVER_NETWORK] == ident.NOT_THROUGH_TUNNEL
            assert session._discover.probes == 0                 # UDP cannot cross it: not asked
        finally:
            eng.close_all()


# --- the witness: a hub says "not through a hub" ---------------------------------------------------


def _hub_session(**links_kw):
    """The shape of the 09-28 HIL board 1: Ethernet ``via ssh:HUB`` plus the hub's MCC link."""
    links = (Link(LinkKind.ETHERNET, "192.168.10.101:6900",
                  "shell control channel, via ssh:mapstone-dev.ecs.soton.ac.uk", via="ssh"),
             Link(LinkKind.HUB, "hub-mcc://mapstone-dev.ecs.soton.ac.uk/mps3_01_pl/tty_00",
                  "the MCC console", via="hub"))
    return SimpleNamespace(candidate=SimpleNamespace(links=links), reach=None,
                           shell=SimpleNamespace(host="127.0.0.1"))


def test_a_board_reached_through_a_hub_says_not_through_a_hub():
    asked: list[str] = []
    w = ident.DiscoverWitness(probe=lambda host, **kw: asked.append(host))
    why = w.reason(_hub_session())
    assert why == ident.NOT_THROUGH_HUB and why.startswith("not through a hub")
    assert asked == [] and w.probes == 0


def test_negative_twin_the_same_board_on_its_own_lan_is_asked_and_answers():
    ok = SimpleNamespace(ok=True, raw={"ok": True})
    asked: list[str] = []
    w = ident.DiscoverWitness(probe=lambda host, **kw: asked.append(host) or ok)
    lan = SimpleNamespace(
        candidate=SimpleNamespace(links=(Link(LinkKind.ETHERNET, "192.168.10.101:6900",
                                              "shell control channel"),)),
        reach=None, shell=SimpleNamespace(host="192.168.10.101"))
    assert w.reason(lan) == "" and asked == ["192.168.10.101"]


def test_a_silence_is_remembered_for_a_while_then_asked_again():
    now = [100.0]
    calls: list[str] = []

    def silent(host, **kw):
        calls.append(host)
        raise UnreachableError(f"nothing answered identify at {host}:6899")

    w = ident.DiscoverWitness(probe=silent, clock=lambda: now[0])
    lan = SimpleNamespace(candidate=SimpleNamespace(links=()), reach=None,
                          shell=SimpleNamespace(host="10.0.0.9"))
    assert "did not answer identify" in w.reason(lan)
    now[0] += ident.DISCOVER_FAIL_TTL_S - 1
    w.reason(lan)
    assert len(calls) == 1                                 # remembered
    now[0] += 2
    w.reason(lan)
    assert len(calls) == 2                                 # twin: past the TTL, asked again


# --- the engine seam: it only narrows, and never fails info --------------------------------------


@pytest.fixture
def fake_engine(tmp_path):
    pack = FakePack(identity=BoardIdentity(board_type="fake", shell_id="0x1",
                                           features=("reboot",)))
    eng = Engine(EngineConfig(state_dir=tmp_path / "state"), packs={"fake": pack})
    yield eng, pack
    eng.close_all()


def test_the_engine_takes_a_sessions_reason_for_an_available_capability(fake_engine):
    eng, pack = fake_engine
    session = eng.open(candidate("fake@1"))
    session.capability_reasons = lambda avail, identity: {C.RESET_SHELL: "not now: testing"}
    info = eng.info("fake@1")
    assert C.RESET_SHELL not in info.capabilities
    assert info.unavailable[C.RESET_SHELL] == "not now: testing"


def test_negative_twin_a_session_can_never_widen_and_a_failing_hook_is_ignored(fake_engine):
    eng, pack = fake_engine
    session = eng.open(candidate("fake@1"))
    before = eng.info("fake@1")
    assert C.REBOOT_BOARD not in before.capabilities          # no USB link
    session.capability_reasons = lambda avail, identity: {C.REBOOT_BOARD: "",
                                                           C.IDENTIFY: ""}
    info = eng.info("fake@1")
    assert info.capabilities == before.capabilities
    assert info.unavailable[C.REBOOT_BOARD] == before.unavailable[C.REBOOT_BOARD]

    def boom(avail, identity):
        raise RuntimeError("a bug in a pack")

    session.capability_reasons = boom
    assert eng.info("fake@1").capabilities == before.capabilities
