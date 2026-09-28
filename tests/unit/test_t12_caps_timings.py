"""T12: capability routes for the two harness generations, and the reboot budget per engine."""

from __future__ import annotations

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import negotiate
from harness_manager.core.model import BoardIdentity, LinkKind
from harness_manager_mps3.capabilities import HARNESS_STATES, SPECS
from harness_manager_mps3.constants import (
    REBOOT_WAIT_S_BARE_METAL,
    REBOOT_WAIT_S_LINUX,
    reboot_wait_s,
)

V011 = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed", "dut_egress", "jtag_server",
        "xvc_dbgbr", "stats", "log", "reboot", "touch_cal")


def view(links, features=()):
    return negotiate(SPECS, links, features)


# -- the shell console ----------------------------------------------------------------


def test_shell_console_over_ssh_on_a_linux_harness():
    avail, _ = view([LinkKind.ETHERNET, LinkKind.SSH])
    assert C.CONSOLE_SHELL in avail


def test_negative_twin_the_dead_ethernet_shell_console_route_is_gone():
    """A12 (TCP 6939) will never ship: even a board claiming 'shell_console' gets no route."""
    avail, unavail = view([LinkKind.ETHERNET], ["shell_console"])
    assert C.CONSOLE_SHELL not in avail
    assert "SSH" in unavail[C.CONSOLE_SHELL] and "Debug USB" in unavail[C.CONSOLE_SHELL]


def test_shell_console_still_via_usb_and_hub():
    assert C.CONSOLE_SHELL in view([LinkKind.USB_SERIAL])[0]
    assert C.CONSOLE_SHELL in view([LinkKind.HUB])[0]


def test_no_spec_routes_through_shell_console_any_more():
    for spec in SPECS:
        for route in spec.routes:
            assert "shell_console" not in route.features, spec.name


# -- v0.11 features light up what they should -------------------------------------------


def test_v011_reboot_feature_enables_restart_the_shell():
    assert C.RESET_SHELL in view([LinkKind.ETHERNET], V011)[0]
    assert C.RESET_SHELL not in view([LinkKind.ETHERNET], V011[:5])[0]   # v0.10: no `reboot`


# -- harness states -------------------------------------------------------------------


def test_every_health_state_the_pack_reports_is_explained():
    for state in ("idle", "busy", "wedged", "offline", "service_down", "rescue"):
        assert HARNESS_STATES[f"harness.{state}"]
    assert "TFTP" in HARNESS_STATES["harness.rescue"]
    assert "6900" in HARNESS_STATES["harness.rescue"]


# -- the reboot witness budget -----------------------------------------------------------


@pytest.mark.parametrize(("impl", "want"), [
    ("linux", REBOOT_WAIT_S_LINUX),
    ("bare-metal", REBOOT_WAIT_S_BARE_METAL),
    ("", REBOOT_WAIT_S_BARE_METAL),          # no `version`: an old bare-metal harness
])
def test_reboot_wait_by_engine(impl, want):
    assert reboot_wait_s(BoardIdentity(board_type="mps3", harness_impl=impl)) == want


def test_reboot_wait_numbers_are_the_agreed_ones():
    # FIX-PACK-2 item 7: Linux 180 -> 300 s (stage0's DDR settle: a cold boot is ~190 s)
    assert (REBOOT_WAIT_S_BARE_METAL, REBOOT_WAIT_S_LINUX) == (120.0, 300.0)
    assert reboot_wait_s(None) == 120.0
