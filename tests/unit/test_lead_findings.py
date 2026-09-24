"""Lead: Harness Manager changes driven by the ILA mint session's silicon findings
(platform feat/rm-ila-mint docs/planning/FINDINGS_FOR_LINUX_HARNESS_2026-09-24.md)."""

from __future__ import annotations

from harness_manager_mps3 import mcc


def test_the_mcc_over_a_hub_share_is_paced_at_100_ms():
    # Finding #2: the remote REBOOT over the share was proven at one char per 100 ms.
    for url in ("hub://mapstone-dev/mps3_01_pl/dev/mps3_01_pl/tty_00", "tcp://127.0.0.1:4000"):
        assert mcc.timing_for(url).pace_s == mcc.SHARE_PACE_S == 0.1


def test_negative_twin_the_debug_usb_keeps_the_proven_60_ms():
    # Slower pacing on USB would hold the board longer for every MCC read.
    t = mcc.timing_for("serial:///dev/ttyUSB10")
    assert t is mcc.DEFAULT_TIMING and 0.05 <= t.pace_s < 0.1
