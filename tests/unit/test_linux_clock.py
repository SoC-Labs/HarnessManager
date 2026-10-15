"""LINUX-CLOCK (HM 1.2): the Linux harness answers ``set_clk`` (presets 25/50/100 MHz) and reports
``stats.dut_mhz`` (read back from clk_wiz_dut), so the MPS3 clock adapter sets and reads the DUT
clock on a Linux board the way it does on bare metal. Each case has its negative twin."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from harness_manager.core.errors import (
    ActionFailedError,
    ClaimLockedError,
    HarnessError,
    UsageError,
)
from harness_manager_mps3 import clock as clock_mod
from harness_manager_mps3.clock import LINUX_WHY, STATS_SOURCE, Mps3Clocks

WHY_TEXT = "set with presets 25, 50 or 100 MHz; goes back to 50 MHz when the harness restarts"


class Board:
    """A shell whose ``call`` runs the function on a client; ``stats`` tracks the last preset."""

    def __init__(self, mhz=50, stats_ok=True, locked_attr=False, set_ok=True, stats_raises=False):
        self.mhz, self.stats_ok, self.locked_attr = mhz, stats_ok, locked_attr
        self.set_ok, self.stats_raises = set_ok, stats_raises
        self.sent: list[str] = []
        self.client = NS(set_clk=self._set_clk, stats=self._stats)

    def _set_clk(self, preset, presets=None):
        self.sent.append(preset)
        if self.set_ok:
            self.mhz = int(preset[:-3])
        # the client reads a missing ``locked`` as False: the Linux reply may lack it
        return NS(ok=self.set_ok, locked=self.locked_attr)

    def _stats(self):
        if self.stats_raises:
            raise HarnessError("no answer")
        return NS(ok=self.stats_ok, dut_mhz=self.mhz)

    def call(self, fn):
        return fn(self.client)


def session(impl: str, board: Board, shell_id="0x44ee76d5") -> NS:
    return NS(candidate=NS(identity=NS(harness_impl=impl, shell_id=shell_id)), shell=board)


@pytest.fixture
def routed(monkeypatch):
    calls: list[str] = []

    def fake(sess, what, direct, fn):
        calls.append(what)
        return fn(direct.client)

    monkeypatch.setattr(clock_mod.claim_route, "routed_call", fake)
    return calls


def test_linux_set_100_goes_through_the_claim_route_and_reads_100_back(routed):
    b = Board()
    r = Mps3Clocks(session("linux", b)).set_clock("dut", 100.0)
    assert b.sent == ["100mhz"] and routed == ["setting the DUT clock to 100mhz"]
    assert (r.value, r.unit, r.source, r.reason) == (100.0, "MHz", STATS_SOURCE, LINUX_WHY)
    assert LINUX_WHY == WHY_TEXT


def test_twin_linux_without_a_locked_key_is_not_reported_as_unlocked(routed):
    b = Board(locked_attr=False)                             # the client's default for a missing key
    assert Mps3Clocks(session("linux", b)).set_clock("dut", 25.0).value == 25.0
    twin = Board(locked_attr=False)
    with pytest.raises(ActionFailedError, match="did not lock"):
        Mps3Clocks(session("bare-metal", twin)).set_clock("dut", 25.0)


def test_linux_clock_that_reads_back_differently_is_a_failure(routed):
    b = Board()
    b.client.set_clk = lambda preset, presets=None: NS(ok=True, locked=False)   # board ignores it
    with pytest.raises(ActionFailedError, match="board reads 50 MHz after set_clk 100mhz"):
        Mps3Clocks(session("linux", b)).set_clock("dut", 100.0)


def test_twin_the_board_refusing_set_clk_is_a_failure(routed):
    with pytest.raises(ActionFailedError, match="refused set_clk"):
        Mps3Clocks(session("linux", Board(set_ok=False))).set_clock("dut", 100.0)


def test_a_non_preset_is_a_usage_error_and_nothing_is_sent(routed):
    b = Board()
    with pytest.raises(UsageError, match="33.0 MHz is not a preset") as ei:
        Mps3Clocks(session("linux", b)).set_clock("dut", 33.0)
    assert "25, 50, 100" in str(ei.value.hint) and b.sent == [] and routed == []
    assert Mps3Clocks(session("linux", Board())).set_clock("dut", 50.0).value == 50.0   # twin


@pytest.mark.parametrize("impl", ["linux", "bare-metal"])
def test_a_board_claimed_by_someone_else_refuses_the_set_the_same_way(monkeypatch, impl):
    b = Board()

    def refuse(*_a, **_k):
        raise ClaimLockedError("set_clk locked: board claimed (use ssh)")

    monkeypatch.setattr(clock_mod.claim_route, "routed_call", refuse)
    with pytest.raises(ClaimLockedError):
        Mps3Clocks(session(impl, b)).set_clock("dut", 100.0)
    assert b.sent == []


def test_linux_clocks_reads_dut_mhz_from_the_board_stats():
    (r,) = Mps3Clocks(session("linux", Board(mhz=100))).clocks()
    assert (r.value, r.source, r.reason) == (100.0, STATS_SOURCE, WHY_TEXT)
    assert STATS_SOURCE == "board stats (clk_wiz_dut read-back)"


@pytest.mark.parametrize("board", [Board(stats_ok=False), Board(stats_raises=True), Board(mhz=0)])
def test_unreadable_stats_fall_back_to_the_pin_model_and_say_so(board):
    (r,) = Mps3Clocks(session("linux", board)).clocks()
    assert (r.value, r.source) == (50.0, "pin-model")
    assert "stats could not be read" in r.reason and r.reason.endswith(WHY_TEXT)


def test_twin_unreadable_stats_on_a_shell_the_model_lacks_is_unavailable_with_the_reason():
    (r,) = Mps3Clocks(session("linux", Board(stats_ok=False), shell_id="0x4c1a0003")).clocks()
    assert r.value is None and "stats could not be read" in r.reason and "0x4c1a0003" in r.reason


def test_linux_set_with_unreadable_stats_reports_the_value_set(routed):
    r = Mps3Clocks(session("linux", Board(stats_ok=False))).set_clock("dut", 25.0)
    assert (r.value, r.source) == (25.0, "shell set_clk (last set value)")


def test_bare_metal_is_unchanged(routed):
    b = Board(locked_attr=True)
    clocks = Mps3Clocks(session("bare-metal", b, shell_id="0x72bb0a36"))
    (r,) = clocks.clocks()
    assert r.value is None and "cannot read the DUT clock back" in r.reason
    r = clocks.set_clock("dut", 100.0)
    assert (r.value, r.source, r.reason) == (100.0, "shell set_clk (last set value)", "")
    assert clocks.clocks()[0].value == 100.0
