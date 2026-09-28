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


def test_loopback_route_reboot_and_osc_are_pending_v018(open_board):
    fake, eng, session, bid = open_board(("mcc_local",), route="loopback")
    info = eng.info(bid)
    assert C.REBOOT_BOARD not in info.capabilities and C.CLOCK_BOARD not in info.capabilities
    assert "pending v0.18" in info.unavailable[C.REBOOT_BOARD]
    assert "(loopback)" in info.unavailable[C.REBOOT_BOARD]
    assert "pending v0.18" in info.unavailable[C.CLOCK_BOARD]
    eng.info(bid)                                            # inside the TTL: not asked again
    assert fake.mcc_requests == [ACT_STATUS]                 # status only: nothing else is sent
    assert session.harness_mcc.route() == "loopback"


def test_scc_route_cannot_reboot_the_board_and_says_which_route_can(open_board):
    fake, eng, session, bid = open_board(("mccif",), route="scc")
    info = eng.info(bid)
    why = info.unavailable[C.REBOOT_BOARD]
    assert "USB loopback route (feature 'mcc_local')" in why and "route scc" in why
    assert "pending v0.18" in info.unavailable[C.CLOCK_BOARD]   # osc: scc serves it, HM not yet
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


# --- the acts: status is read, reboot and osc_set are pending --------------------------------------


def test_mcc_status_reads_the_route_from_the_fake(open_board):
    fake, eng, session, bid = open_board(("mccif",), route="scc")
    st = session.harness_mcc.status(refresh=True)
    assert st.route == "scc" and st.serves("osc") and not st.serves("reboot")
    assert st.raw["op"] == "mcc" and fake.mcc_requests == [ACT_STATUS]


@pytest.mark.parametrize(("call", "what"), [("reboot", "rebooting"), ("set_osc", "setting"),
                                            ("oscillators", "reading the board osc"),
                                            ("temperatures", "temperature")])
def test_reboot_and_osc_set_over_the_harness_are_pending_and_send_nothing(open_board, call, what):
    fake, eng, session, bid = open_board(("mcc_local",), route="loopback")
    with pytest.raises(UnavailableError) as exc:
        getattr(session.harness_mcc, call)()
    assert "pending v0.18" in exc.value.reason and what in exc.value.reason
    assert fake.mcc_requests == []


def test_the_cli_and_api_require_says_pending_not_no_adapter(open_board):
    fake, eng, session, bid = open_board(("mcc_local",), route="loopback")
    assert session.controller is None                         # no Debug USB, no hub
    ctx = Ctx(argparse.Namespace(), eng, "json")
    with pytest.raises(UnavailableError) as exc:
        ctx.require(session, "controller", C.REBOOT_BOARD)
    assert "pending v0.18" in exc.value.reason
