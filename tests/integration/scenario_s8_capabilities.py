"""S8: the capability view is honest about which link or firmware each feature needs.

The same board viewed three ways:
- Ethernet only: the standalone "no USB cable" mode david asked for.
- USB only.
- Both.
"""

from __future__ import annotations

from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import negotiate
from tests.fakes.virtual_board import VirtualMps3


def view(pack, cand, features):
    return negotiate(pack.capability_specs(), [lk.kind for lk in cand.links], features)


def test_s8_ethernet_only(tmp_path, mps3_pack):
    with VirtualMps3(tmp_path, usb=True) as vb:
        features = mps3_pack.open(vb.candidate(usb=False)).identity().features
        avail, unavail = view(mps3_pack, vb.candidate(usb=False), features)
        # Works over the one Ethernet cable on today's firmware:
        assert {C.DEPLOY_PARTIAL, C.CONSOLE_DUT, C.DEBUG_DUT, C.RESET_DUT, C.CLOCK_DUT} <= avail
        # Needs USB, newer firmware, or add-on hardware; each says which:
        assert "Debug USB" in unavail[C.REBOOT_BOARD] and "'mcc'" in unavail[C.REBOOT_BOARD]
        assert "SSH" in unavail[C.CONSOLE_SHELL] or "Debug USB" in unavail[C.CONSOLE_SHELL]
        assert "no power sensor" in unavail[C.TELEMETRY_POWER]


def test_s8_usb_only(tmp_path, mps3_pack):
    with VirtualMps3(tmp_path, usb=True) as vb:
        avail, unavail = view(mps3_pack, vb.candidate(ethernet=False), ())
        assert {C.REBOOT_BOARD, C.CONSOLE_CONTROLLER, C.STORAGE_BACKUP} <= avail
        assert C.DEPLOY_PARTIAL in unavail   # partitions go over Ethernet


def test_s8_both_links(tmp_path, mps3_pack):
    with VirtualMps3(tmp_path, usb=True) as vb:
        avail, _ = view(mps3_pack, vb.candidate(), ("windowed",))
        assert {C.DEPLOY_PARTIAL, C.REBOOT_BOARD, C.STORAGE_INSTALL, C.CONSOLE_SHELL} <= avail
