"""UI v2 G2 (lane UI2-API-HUB): an open board's Debug USB route from its session, never a contact
(``daemon/mcc_route.of_session``). Each check has a negative twin."""

from __future__ import annotations

from types import SimpleNamespace

from harness_manager.core.errors import HarnessError
from harness_manager.core.model import Link, LinkKind
from harness_manager.daemon import mcc_route

ETH = Link(LinkKind.ETHERNET, "10.0.0.5:6900", "shell")
MCC = Link(LinkKind.USB_SERIAL, "/dev/ttyUSB0", "FT4232H X if00 (MCC)")
HUB_MCC = Link(LinkKind.HUB, "hub-mcc://hubhost/t/dev/t/tty_00", "on the hub", via="hub")


def session(links, *, controller=None, harness_route=None, hook=None):
    s = SimpleNamespace(candidate=SimpleNamespace(links=tuple(links)), controller=controller)
    if harness_route is not None:
        status = SimpleNamespace(route=harness_route)
        s.harness_mcc = SimpleNamespace(_last=(status, "", 0.0))
    if hook is not None:
        s.mcc_route = hook
    return s


def ident(*features):
    return SimpleNamespace(features=features)


def test_the_hub_and_this_pc():
    assert mcc_route.of_session(session((ETH, HUB_MCC), controller=object()))[0] == "hub"
    route, why = mcc_route.of_session(session((ETH, MCC), controller=object()))
    assert route == "pc" and "/dev/ttyUSB0" in why


def test_the_harness_route_as_last_read():
    loop = mcc_route.of_session(session((ETH,), harness_route="loopback"), ident("mcc_local"))
    assert loop[0] == "self" and "loops back" in loop[1]
    unread = mcc_route.of_session(session((ETH,)), ident("mccif"))
    assert unread[0] == "self" and "read when needed" in unread[1]
    # twin: the harness says it has no route
    none = mcc_route.of_session(session((ETH,), harness_route="none"), ident("mccif"))
    assert none[0] == "none"


def test_twin_ethernet_only_is_none():
    route, why = mcc_route.of_session(session((ETH,)), ident("reboot"))
    assert route == "none" and "Ethernet only" in why


def test_a_pack_may_answer_for_itself_and_a_bad_answer_is_ignored():
    assert mcc_route.of_session(session((ETH,), hook=lambda: ("pc", "said so"))) == \
        ("pc", "said so")
    assert mcc_route.of_session(session((ETH,), hook=lambda: ("sideways", "")))[0] == "none"

    def broken():
        raise HarnessError("no")

    assert mcc_route.of_session(session((ETH, HUB_MCC), hook=broken))[0] == "hub"
