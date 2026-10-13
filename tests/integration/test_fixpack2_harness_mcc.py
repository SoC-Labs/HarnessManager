"""FIX-PACK-2 item 2: the MCC gates for net-protocol v0.18 (mint 4), against a fake harness.

``reboot_board``/``clock_board`` keep today's Debug-USB (and hub, and plug) routes, and accept
the harness features ``mccif`` (in-fabric SCC) or ``mcc_local`` (the USB loopback), with the
route read from ``mcc status``. Only the gate, the feature-name seam and the ``mcc status``
read are implemented; ``reboot`` and ``osc_set`` (and the ``osc``/``temp`` reads) over the
harness are PENDING v0.18 and say so. Each check has a negative twin.
"""

from __future__ import annotations

import argparse

import pytest

from harness_manager.cli.context import Ctx
from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import negotiate
from harness_manager.core.errors import UnavailableError
from harness_manager.core.model import Link, LinkKind
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3 import harness_mcc as HM
from harness_manager_mps3.capabilities import SPECS
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.fp2_mcc_board import mcc_board

ACT_STATUS = {"op": "mcc", "act": "status"}


# --- the seam: feature names --------------------------------------------------------------------


def test_the_seam_recognises_both_v018_names_and_the_routes_they_promise():
    assert HM.recognise(["clcd", "mccif"]).routes == ("scc",)
    assert HM.recognise(["mcc_local"]).routes == ("loopback",)
    both = HM.recognise(["mcc_local", "stats", "mccif"])
    assert both.features == ("mcc_local", "mccif") and set(both.routes) == {"scc", "loopback"}
    assert HM.recognise(["mcc"]).legacy                       # the pre-v0.18 alias


def test_negative_twin_an_image_without_them_is_not_recognised():
    assert HM.recognise(["clcd", "stats", "xvc_dbgbr", "mccx", "local"]) is None
    assert HM.recognise([]) is None


# --- the specs: the gate accepts the features, the Debug USB path stays --------------------------


@pytest.mark.parametrize("feature", ["mccif", "mcc_local"])
def test_the_specs_accept_mccif_or_mcc_local_over_ethernet(feature):
    avail, _ = negotiate(SPECS, [LinkKind.ETHERNET], ["stats", feature])
    assert {C.REBOOT_BOARD, C.CLOCK_BOARD} <= avail


def test_negative_twin_ethernet_without_the_features_names_them_and_usb_still_works():
    avail, why = negotiate(SPECS, [LinkKind.ETHERNET], ["stats", "reboot"])
    assert C.REBOOT_BOARD not in avail and C.CLOCK_BOARD not in avail
    assert "'mccif' or 'mcc_local'" in why[C.REBOOT_BOARD] and "J7" not in why[C.REBOOT_BOARD]
    assert "Debug USB" in why[C.CLOCK_BOARD]
    usb, _ = negotiate(SPECS, [LinkKind.USB_SERIAL], ())
    assert {C.REBOOT_BOARD, C.CLOCK_BOARD} <= usb             # today's path, unchanged


# --- a session: the route from `mcc status` decides -------------------------------------------------


@pytest.fixture
def open_board(tmp_path):
    made: list = []

    def opener(extra=("mccif",), *, route="scc", verb=True, links=()):
        fake = mcc_board(extra, mcc_route=route, mcc_verb=verb)
        pack = Mps3Pack(console_ports=fake.console_ports, push_port=fake.raw_tcp_port)
        eng = Engine(EngineConfig(state_dir=tmp_path / "state"), packs={"mps3": pack})
        made.append((fake, eng))
        cand = pack.candidate_for_host(f"{fake.host}:{fake.control_port}")
        if links:
            from dataclasses import replace
            cand = replace(cand, links=cand.links + tuple(links))
        session = eng.open(cand)
        return fake, eng, session, cand.board_id

    yield opener
    for fake, eng in made:
        eng.close_all()
        fake.stop()


def test_loopback_route_offers_reboot_and_osc_through_the_harness(open_board):
    fake, eng, session, bid = open_board(("mcc_local",), route="loopback")
    info = eng.info(bid)
    assert {C.REBOOT_BOARD, C.CLOCK_BOARD} <= info.capabilities       # L5b: both are driven now
    assert C.REBOOT_BOARD not in info.unavailable and C.CLOCK_BOARD not in info.unavailable
    eng.info(bid)                                            # inside the TTL: not asked again
    assert fake.mcc_requests == [ACT_STATUS]                 # status only: nothing else is sent
    assert session.harness_mcc.route() == "loopback"


def test_scc_route_cannot_reboot_the_board_and_says_which_route_can(open_board):
    fake, eng, session, bid = open_board(("mccif",), route="scc")
    info = eng.info(bid)
    why = info.unavailable[C.REBOOT_BOARD]
    assert "USB loopback route (feature 'mcc_local')" in why and "route scc" in why
    assert C.CLOCK_BOARD in info.capabilities                # osc is served by the scc route
    assert fake.mcc_requests == [ACT_STATUS]


def test_route_none_says_there_is_no_route(open_board):
    fake, eng, session, bid = open_board(("mccif", "mcc_local"), route="none")
    info = eng.info(bid)
    assert "no route to the MCC" in info.unavailable[C.REBOOT_BOARD]
    assert "no route to the MCC" in info.unavailable[C.CLOCK_BOARD]


def test_an_image_that_lists_the_feature_but_not_the_verb_says_so(open_board):
    fake, eng, session, bid = open_board(("mccif",), verb=False)
    info = eng.info(bid)
    assert "does not answer `mcc status`" in info.unavailable[C.REBOOT_BOARD]


def test_negative_twin_an_image_without_the_features_is_never_asked(open_board):
    fake, eng, session, bid = open_board(())
    info = eng.info(bid)
    assert C.REBOOT_BOARD not in info.capabilities
    assert "'mccif' or 'mcc_local'" in info.unavailable[C.REBOOT_BOARD]
    assert fake.mcc_requests == []
    with pytest.raises(UnavailableError, match="does not reach the MCC"):
        session.harness_mcc.status()                          # behind the feature
    assert fake.mcc_requests == []


def test_negative_twin_the_debug_usb_route_wins_and_the_harness_is_not_asked(open_board):
    usb = Link(LinkKind.USB_SERIAL, "fake://fp2-mcc-not-opened", "MCC console (never opened)")
    fake, eng, session, bid = open_board(("mcc_local",), route="loopback", links=(usb,))
    info = eng.info(bid)
    assert {C.REBOOT_BOARD, C.CLOCK_BOARD} <= info.capabilities
    assert fake.mcc_requests == []


# --- the acts: status is read; the rest are driven (L5b) ---------------------------------------


def test_mcc_status_reads_the_route_from_the_fake(open_board):
    fake, eng, session, bid = open_board(("mccif",), route="scc")
    st = session.harness_mcc.status(refresh=True)
    assert st.route == "scc" and st.serves("osc") and not st.serves("reboot")
    assert st.raw["op"] == "mcc" and fake.mcc_requests == [ACT_STATUS]


def test_oscillator_reads_go_over_the_harness_and_a_protected_set_is_never_sent(open_board):
    from harness_manager.core.errors import RefusedError

    fake, eng, session, bid = open_board(("mccif",), route="scc")
    readings = session.harness_mcc.oscillators()
    # this 1.1-era fake models only `status`, so every osc read comes back unavailable with
    # its reason; what matters is the eight `osc` requests, one per device 0..7
    assert [r.available for r in readings] == [False] * 8
    assert [(m["act"], m["dev"]) for m in fake.mcc_requests] == [("osc", d) for d in range(8)]
    n = len(fake.mcc_requests)
    with pytest.raises(RefusedError):
        session.harness_mcc.set_osc(1, 40_000_000)           # the harness's own clock
    assert len(fake.mcc_requests) == n


def test_the_cli_and_api_require_gets_the_routed_controller_with_no_usb(open_board):
    fake, eng, session, bid = open_board(("mcc_local",), route="loopback")
    session.identity()          # opened by address: the first read names mcc_local (no read at open)
    assert session.controller is not None and not session.controller.has_fallback
    ctx = Ctx(argparse.Namespace(), eng, "json")
    assert ctx.require(session, "controller", C.REBOOT_BOARD) is session.controller
