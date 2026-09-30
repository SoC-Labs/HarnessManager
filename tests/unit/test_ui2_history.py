"""UI2-API-BUILD G4: the readings a board read carries, and the history ring the Overview draws
(``services/history.py``). Pure: no daemon, no board. Each rule has a negative twin."""

from __future__ import annotations

import pytest

from harness_manager.core.errors import UsageError
from harness_manager.core.model import Reading
from harness_manager.services.history import (
    ANSWER_SERIES,
    BoardFacts,
    ReadingsHistory,
    facts_of,
    info_fields,
    query_args,
    stats_fields,
)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_stats_are_whitelisted_and_only_what_the_harness_sent():
    raw = {"ok": True, "up_ms": 5000, "swap_n": 3, "icap": 99, "rxdrop": 0, "link": True,
           "spd": 100, "svc_max_us": 1200, "svc_skips": 0, "mac": "02:00:00:00:00:01",
           "sid": "0x72bb0a36", "sysmon": {"temp": 1}, "weird": [1, 2]}
    out = stats_fields(raw)
    assert out == {"up_ms": 5000, "swap_n": 3, "icap": 99, "rxdrop": 0, "link": True,
                   "spd": 100, "svc_max_us": 1200, "svc_skips": 0}
    # twins: no reply, not a mapping, a key the whitelist names but whose value is a structure
    assert stats_fields(None) is None and stats_fields("busy") is None      # type: ignore[arg-type]
    assert stats_fields({"swap_n": {"x": 1}}) == {}


def test_facts_from_a_seam_and_their_json():
    f = BoardFacts.from_raw({"stats": {"up_ms": 101_520_000, "swap_n": 76}, "os_up_ms": 101_600_000,
                             "at": 12.5, "source": "stats (6900)"})
    assert (f.uptime_s, f.os_uptime_s, f.at) == (101520.0, 101600.0, 12.5)
    assert f.to_json() == {"uptime_s": 101520.0, "os_uptime_s": 101600.0, "readings_at": 12.5,
                           "readings_source": "stats (6900)",
                           "stats": {"up_ms": 101_520_000, "swap_n": 76}}
    # twins: nothing, a negative or boolean uptime is no uptime
    empty = BoardFacts.from_raw(None).to_json()
    assert set(empty.values()) == {None}
    assert BoardFacts.from_raw({"up_ms": -5}).uptime_s is None
    assert BoardFacts.from_raw({"up_ms": True}).uptime_s is None


class Seam:
    def __init__(self, raw=None, exc=None):
        self.raw, self.exc = raw, exc

    def readings_facts(self):
        if self.exc:
            raise self.exc
        return self.raw


class Session:
    def __init__(self, own=None, telemetry=None):
        if own is not None:
            self.readings_facts = own.readings_facts
        self.telemetry = telemetry


def test_facts_of_asks_the_session_then_its_telemetry_adapter_and_never_fails():
    assert facts_of(Session(Seam({"up_ms": 2000}))).uptime_s == 2.0
    assert facts_of(Session(telemetry=Seam({"stats": {"up_ms": 3000}}))).uptime_s == 3.0
    # twins: no seam, a seam that raises, no session at all
    assert facts_of(Session()).to_json()["uptime_s"] is None
    assert facts_of(Session(Seam(exc=RuntimeError("boom")))).uptime_s is None
    assert facts_of(None).stats is None


def test_info_fields_round_the_answer_and_carry_the_facts():
    out = info_fields(12.345, BoardFacts(uptime_s=5.0))
    assert out["answer_ms"] == 12.3 and out["uptime_s"] == 5.0
    assert set(out) == {"answer_ms", "uptime_s", "os_uptime_s", "readings_at",
                        "readings_source", "stats"}
    assert info_fields(None, BoardFacts())["answer_ms"] is None


def test_the_ring_keeps_one_point_per_spacing_and_at_most_capacity():
    clk = Clock()
    h = ReadingsHistory(spacing_s=30.0, capacity=3, clock=clk)
    assert h.add("b", "mcc_temp", 38.5, unit="degC", source="mcc-console")
    clk.t += 10
    assert not h.add("b", "mcc_temp", 38.6)                      # too soon: not kept
    for _ in range(4):
        clk.t += 30
        assert h.add("b", "mcc_temp", 39.0)
    doc = h.history("b")
    (s,) = doc["series"]
    assert (s["name"], s["unit"], s["source"]) == ("mcc_temp", "degC", "mcc-console")
    assert len(s["points"]) == 3 and doc["capacity"] == 3 and doc["oldest"] == s["points"][0][0]
    # twins: not a number, not finite, a bool, no name: nothing kept
    for bad in (None, float("nan"), float("inf"), True, "39"):
        clk.t += 60
        assert not h.add("b", "mcc_temp", bad)
    assert not h.add("b", "", 1.0)


def test_note_readings_keeps_the_available_numbers_and_the_answer_series():
    clk = Clock()
    h = ReadingsHistory(clock=clk)
    kept = h.note_readings("b", [Reading("mcc_temp", 41.0, "degC", "mcc-console", observed_at=clk.t),
                                 Reading.unavailable("fpga_die_temp", "degC", "needs JTAG"),
                                 Reading("dut_clk", 50.0, "MHz", "shell-6900", observed_at=clk.t)])
    assert kept == 2
    assert h.note_answer("b", 42.0)
    names = [s["name"] for s in h.history("b")["series"]]
    assert names == sorted(["mcc_temp", "dut_clk", ANSWER_SERIES])
    assert h.history("b", names=["mcc_temp"])["series"][0]["points"] == [[clk.t, 41.0]]
    # twins: another board has nothing; forget drops the board
    assert h.history("other")["series"] == [] and h.history("other")["oldest"] is None
    h.forget("b")
    assert h.history("b")["series"] == []


def test_since_and_limit():
    clk = Clock(1000.0)
    h = ReadingsHistory(spacing_s=1.0, clock=clk)
    for i in range(5):
        clk.t = 1000.0 + i * 10
        h.add("b", "t", float(i))
    pts = h.history("b", since=1020.0)["series"][0]["points"]
    assert [p[1] for p in pts] == [2.0, 3.0, 4.0]
    assert [p[1] for p in h.history("b", limit=2)["series"][0]["points"]] == [3.0, 4.0]


def test_a_seed_fills_a_board_the_first_time_it_is_seen_and_a_bad_seed_is_nothing():
    calls = []

    def seed(bid):
        calls.append(bid)
        return [{"name": "mcc_temp", "unit": "degC", "source": "demo",
                 "points": [[10.0, 38.0], [70.0, 38.4]]}]

    h = ReadingsHistory(seed=seed)
    assert h.history("b")["series"][0]["points"] == [[10.0, 38.0], [70.0, 38.4]]
    h.history("b")
    assert calls == ["b"]                                         # once per board

    def broken(bid):
        raise RuntimeError("seed broke")

    assert ReadingsHistory(seed=broken).history("b")["series"] == []


def test_query_args_and_their_twins():
    assert query_args("mcc_temp, fpga_die_temp", "12.5", "30") == (
        ["mcc_temp", "fpga_die_temp"], 12.5, 30)
    assert query_args(None, None, None) == ([], None, None)
    for since, limit in (("yesterday", None), ("nan", None), (None, "0"), (None, "721"),
                         (None, "ten")):
        with pytest.raises(UsageError):
            query_args(None, since, limit)
