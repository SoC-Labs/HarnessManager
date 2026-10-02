"""UI2-POLISH item 5: the Linux harness's DUT clock is FIXED by the shell, so the MPS3 clock
adapter reports the pin model's dut_clk rate ("fixed by the shell"), never "cannot read it back";
the bare-metal shell keeps its set-it-to-know-it answer. Each with its twin."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from harness_manager.core.errors import UnavailableError
from harness_manager_mps3.clock import FIXED, FIXED_WHY, Mps3Clocks, fixed_dut_mhz


def session(impl: str, shell_id: str) -> NS:
    return NS(candidate=NS(identity=NS(harness_impl=impl, shell_id=shell_id)), shell=object())


@pytest.mark.parametrize("sid", ["0x44ee76d5", "0x44EE76D5", "0x72bb0a36"])
def test_the_pin_model_gives_each_shells_dut_clk_rate(sid):
    assert fixed_dut_mhz(sid) == 50.0                      # dut_clk: 20 ns in both shells


@pytest.mark.parametrize("sid", ["0x4c1a0003", "", "not-hex"])
def test_twin_a_shell_the_model_lacks_has_no_rate(sid):
    assert fixed_dut_mhz(sid) is None


def test_a_linux_board_reports_its_fixed_rate_and_refuses_a_set():
    (r,) = Mps3Clocks(session("linux", "0x44ee76d5")).clocks()
    # FIX-PACK-9: the whole fact, from the pin model (the tile still shows "fixed by the shell")
    assert (r.name, r.value, r.unit, r.reason) == ("dut", 50.0, "MHz", FIXED_WHY)
    assert r.reason.startswith(FIXED) and r.source == "pin-model"
    with pytest.raises(UnavailableError, match="fixed by the shell"):
        Mps3Clocks(session("linux", "0x44ee76d5")).set_clock("dut", 25.0)


def test_twin_a_linux_board_on_an_unknown_shell_says_why_not_cannot_read():
    (r,) = Mps3Clocks(session("linux", "0x4c1a0003")).clocks()
    assert r.value is None and r.reason.startswith(FIXED) and "cannot read" not in r.reason


def test_twin_bare_metal_still_cannot_read_its_clock_back():
    (r,) = Mps3Clocks(session("bare-metal", "0x72bb0a36")).clocks()
    assert r.value is None and "cannot read the DUT clock back" in r.reason


# --- FIX-PACK-9: a candidate with no identity (the CLI's) asks the session, once ---------------


def bare_session(impl: str, shell_id: str, calls: list) -> NS:
    def identity():
        calls.append(1)
        return NS(harness_impl=impl, shell_id=shell_id)

    return NS(candidate=NS(identity=None), shell=object(), identity=identity)


def test_a_cli_candidate_asks_the_session_and_a_linux_board_is_fixed():
    calls: list = []
    clocks = Mps3Clocks(bare_session("linux", "0x44ee76d5", calls))
    (r,) = clocks.clocks()
    assert (r.value, r.source, r.reason) == (50.0, "pin-model", FIXED_WHY)
    clocks.clocks()
    with pytest.raises(UnavailableError):
        clocks.set_clock("dut", 25.0)
    assert calls == [1]                                    # asked once per adapter


def test_twin_a_cli_candidate_on_bare_metal_still_cannot_read_it_back():
    calls: list = []
    (r,) = Mps3Clocks(bare_session("bare-metal", "0x72bb0a36", calls)).clocks()
    assert r.value is None and "cannot read the DUT clock back" in r.reason and calls == [1]


def test_twin_a_probed_candidate_is_not_asked_again():
    calls: list = []
    s = session("linux", "0x44ee76d5")
    s.identity = lambda: calls.append(1)
    Mps3Clocks(s).clocks()
    assert calls == []
