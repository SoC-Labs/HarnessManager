"""T12: the deploy transport rule, tunnel detection and the shell-refusal mapping (pure)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyverify.swap import SwapError

from harness_manager.core.errors import ActionFailedError, HeldError, IncompatibleError
from harness_manager.core.model import Candidate, Check, Link, LinkKind
from harness_manager.core.pack import OverlayRef
from harness_manager_mps3 import deploy as dep
from harness_manager_mps3.deploy import (
    TRANSPORT_TCP,
    TRANSPORT_TFTP,
    TRANSPORT_WINDOWED,
    _check_transport,
    _Live,
    _refusal_error,
    _swap_error,
    is_tunnelled,
)

BARE_V011 = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed", "stats", "reboot")
LINUX = tuple(f for f in BARE_V011 if f != "windowed")
NON_WINDOWED = ("clcd", "clcd_kvm", "touch", "hwicap_fifo")
OV = OverlayRef(name="nanosoc", rm_id="0x01000001", static_id="0x1a102610")


def live(features=(), impl="bare-metal", version_ok=True) -> _Live:
    return _Live("0x1a102610", "0x0", version_ok, tuple(features), impl=impl)


# -- the transport rule, in order -------------------------------------------------------


def test_windowed_wins_over_everything():
    assert _check_transport(live(BARE_V011), tunnelled=True)[1] == TRANSPORT_WINDOWED
    assert _check_transport(live(BARE_V011, impl="linux"))[1] == TRANSPORT_WINDOWED


def test_linux_gets_plain_tcp_even_on_a_direct_link():
    item, transport = _check_transport(live(LINUX, impl="linux"), tunnelled=False)
    assert transport == TRANSPORT_TCP and item.check == Check.OK and "mps3-harnessd" in item.detail


def test_a_tunnel_gets_plain_tcp_never_tftp():
    item, transport = _check_transport(live(NON_WINDOWED), tunnelled=True)
    assert transport == TRANSPORT_TCP and "tunnel" in item.detail


def test_negative_twin_bare_metal_direct_non_windowed_is_tftp():
    item, transport = _check_transport(live(NON_WINDOWED), tunnelled=False)
    assert transport == TRANSPORT_TFTP and item.detail.startswith("tftp")


def test_no_version_verb_stays_unchecked_and_tcp_through_a_tunnel():
    item, transport = _check_transport(live(version_ok=False, impl=""), tunnelled=True)
    assert (transport, item.check) == (TRANSPORT_TCP, Check.UNCHECKED)
    item, transport = _check_transport(live(version_ok=False, impl=""), tunnelled=False)
    assert (transport, item.check) == (TRANSPORT_TFTP, Check.UNCHECKED)


# -- tunnel detection -----------------------------------------------------------------


def session(*links: Link):
    return SimpleNamespace(candidate=Candidate("mps3", "mps3@x", links))


ETH = Link(LinkKind.ETHERNET, "127.0.0.1:16900", "shell control channel")


def test_loopback_alone_is_not_a_tunnel(monkeypatch):
    monkeypatch.delenv(dep.TUNNEL_ENV, raising=False)
    assert not is_tunnelled(session(ETH))


def test_tunnel_signals(monkeypatch):
    monkeypatch.delenv(dep.TUNNEL_ENV, raising=False)
    assert is_tunnelled(session(Link(LinkKind.ETHERNET, "127.0.0.1:16900", "via SSH Tunnel")))
    assert is_tunnelled(session(ETH, Link(LinkKind.HUB, "hub://mapstone-dev/mps3_01")))
    monkeypatch.setenv(dep.TUNNEL_ENV, "1")
    assert is_tunnelled(session(ETH))


def test_negative_twin_the_env_can_force_direct(monkeypatch):
    monkeypatch.setenv(dep.TUNNEL_ENV, "0")
    assert not is_tunnelled(session(Link(LinkKind.ETHERNET, "x", "tunnel")))


# -- the shell's own refusals -----------------------------------------------------------


@pytest.mark.parametrize("err", ["fabric mismatch: card 0x11c30003, fabric 0x1a102610",
                                 "ESKEW", "static mismatch"])
def test_a_fabric_mismatch_refusal_is_incompatible(err):
    exc = _refusal_error({"ok": False, "err": err}, OV)
    assert isinstance(exc, IncompatibleError) and err in exc.message


def test_negative_twin_other_refusals_are_action_failed_or_held():
    assert isinstance(_refusal_error({"ok": False, "err": "bad args"}, OV), ActionFailedError)
    assert isinstance(_refusal_error({"ok": False, "err": "EBUSY"}, OV), HeldError)


def test_swap_error_reads_the_err_pyverify_dropped():
    exc = SwapError("swap RPC rejected for rm='nanosoc': SwapResponse(ok=False)")
    assert isinstance(_swap_error(exc, OV, {"ok": False, "err": "fabric mismatch"}),
                      IncompatibleError)
    plain = _swap_error(exc, OV, {"ok": False, "err": "swap failed (at VERIFY)"})
    assert isinstance(plain, ActionFailedError) and "swap failed (at VERIFY)" in plain.message
    assert "refused the swap" in _swap_error(exc, OV, None).message
