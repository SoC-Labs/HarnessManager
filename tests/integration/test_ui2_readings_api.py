"""UI2-API-BUILD G4: the readings the service keeps (``daemon/readings_api.py``,
``services/history.py``) on the real daemon over the demo, the same in the T14 mock, and the
MPS3 telemetry adapter's seam. None of it contacts a board beyond the reads that feed it.
Each route has a negative twin."""

from __future__ import annotations

import time
import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind, Reading
from harness_manager.daemon import readings_api
from harness_manager.daemon.app import create_app
from harness_manager.demo import BOARD_FIELDED, BOARD_USB, DemoEngine
from tests.fakes.t13_daemon import TOKEN, bid_path, headers

H = headers()
KEYS = {"answer_ms", "uptime_s", "os_uptime_s", "readings_at", "readings_source", "stats"}


@pytest.fixture
def demo():
    eng = DemoEngine(speed=0)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            for bid in (BOARD_USB, BOARD_FIELDED):
                cand = next(x for x in c.post("/api/v1/probe", json={}, headers=H)
                            .json()["candidates"] if x["board_id"] == bid)
                assert c.post("/api/v1/boards", json={"candidate": cand},
                              headers=H).status_code == 200
            yield eng, c
    finally:
        eng.close_all()


def test_the_history_is_seeded_by_the_engine_and_queried(demo):
    eng, c = demo
    body = c.get(bid_path(BOARD_USB) + "/readings/history", headers=H).json()
    (temp,) = [s for s in body["series"] if s["name"] == "mcc_temp"]
    assert temp["unit"] == "degC" and len(temp["points"]) == 30
    assert temp["points"][-1][1] == 38.5                           # ends at today's value
    assert body["spacing_s"] == 30.0 and body["capacity"] == 720
    assert body["oldest"] == temp["points"][0][0]
    last = c.get(bid_path(BOARD_USB) + "/readings/history", params={"name": "mcc_temp",
                                                                    "limit": "5"}, headers=H)
    assert len(last.json()["series"][0]["points"]) == 5
    # twins: a bad query is 400; a board with no temperature has no series; no board is empty
    for q in ({"limit": "0"}, {"limit": "x"}, {"since": "later"}):
        r = c.get(bid_path(BOARD_USB) + "/readings/history", params=q, headers=H)
        assert r.status_code == 400 and r.json()["error"]["name"] == "USAGE", q
    fielded = c.get(bid_path(BOARD_FIELDED) + "/readings/history", headers=H).json()["series"]
    assert [s["name"] for s in fielded if s["name"] != "answer_ms"] == []    # no temperature
    assert c.get(bid_path("mps3@10.9.9.9:6900") + "/readings/history",
                 headers=H).json()["series"] == []
    assert c.get(bid_path(BOARD_USB) + "/readings/history").status_code == 401


def test_the_history_never_waits_for_a_job_on_the_board(demo):
    eng, c = demo
    eng.delays["deploy.deploy"] = 1.0
    job = c.post(bid_path(BOARD_USB) + "/deploy", json={"overlay": "led"}, headers=H).json()["job"]
    assert c.get(bid_path(BOARD_USB), headers=H).status_code == 409          # the board is held
    r = c.get(bid_path(BOARD_USB) + "/readings/history", headers=H)
    assert r.status_code == 200 and r.json()["series"]                        # memory only
    end = time.monotonic() + 10
    while c.get(f"/api/v1/jobs/{job}", headers=H).json()["state"] == "running":
        assert time.monotonic() < end
        time.sleep(0.05)


def test_the_hooks_keep_the_answer_and_telemetry_and_give_the_info_keys(demo):
    eng, c = demo
    d = c.app.state.daemon
    readings_api.history_of(d).forget(BOARD_USB)     # (with CCR UI2-G4-1, the open kept one)
    info = eng.info(BOARD_USB)
    extra = readings_api.info_extra(d, BOARD_USB, info, 12.34)
    assert set(extra) == KEYS and extra["answer_ms"] == 12.3
    assert extra["uptime_s"] > 18000 and extra["stats"]["swap_n"] == 11
    assert extra["os_uptime_s"] is None and extra["readings_source"] == "demo"   # bare metal
    readings_api.note_telemetry(d, BOARD_USB, [
        Reading("fpga_die_temp", 45.5, "degC", "sysmon (harness stats)")])
    body = c.get(bid_path(BOARD_USB) + "/readings/history",
                 params={"name": "answer_ms,fpga_die_temp"}, headers=H).json()
    got = {s["name"]: s["points"][-1][1] for s in body["series"]}
    assert got == {"answer_ms": 12.3, "fpga_die_temp": 45.5}
    # twins: a board that is not open still answers the keys (all but answer_ms null), and a
    # broken readings list is ignored, never raised
    closed = readings_api.info_extra(d, "mps3@10.9.9.9:6900", None, None)
    assert set(closed) == KEYS and set(closed.values()) == {None}
    readings_api.note_telemetry(d, BOARD_USB, None)


def test_the_real_daemons_board_read_carries_the_readings_keys(demo):
    eng, c = demo
    info = c.get(bid_path(BOARD_USB), headers=H).json()
    assert KEYS <= set(info) and info["answer_ms"] is not None
    c.get(bid_path(BOARD_USB) + "/telemetry", headers=H)
    names = {s["name"] for s in c.get(bid_path(BOARD_USB) + "/readings/history",
                                      headers=H).json()["series"]}
    assert {"answer_ms", "dut_clk"} <= names


def test_the_linux_showcase_board_says_its_os_uptime():
    from harness_manager.demo_showcase import BOARD_LINUX

    eng = DemoEngine(speed=0, showcase=True)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            cand = next(x for x in c.post("/api/v1/probe", json={}, headers=H).json()["candidates"]
                        if x["board_id"] == BOARD_LINUX)
            c.post("/api/v1/boards", json={"candidate": cand}, headers=H)
            extra = readings_api.info_extra(c.app.state.daemon, BOARD_LINUX,
                                            eng.info(BOARD_LINUX), 3.0)
            assert extra["os_uptime_s"] > extra["uptime_s"] > 100000
            assert "svc_max_us" not in extra["stats"]          # harnessd has no superloop
    finally:
        eng.close_all()


# --- the T14 mock serves the same -------------------------------------------------------------


def test_the_mock_board_read_carries_the_keys_and_telemetry_feeds_its_history():
    from tests.fakes.t14_mock_api import create_app as mock_app

    eng = DemoEngine(speed=0.02)
    h = {"Authorization": "Bearer t14"}
    try:
        with TestClient(mock_app(eng, token="t14", serve_ui=False)) as c:
            cand = next(x for x in c.post("/api/v1/probe", json={}, headers=h).json()["candidates"]
                        if x["board_id"] == BOARD_USB)
            c.post("/api/v1/boards", json={"candidate": cand}, headers=h)
            info = c.get(bid_path(BOARD_USB), headers=h).json()
            assert KEYS <= set(info) and info["answer_ms"] is not None
            assert info["stats"]["swap_n"] == 11
            c.get(bid_path(BOARD_USB) + "/telemetry", headers=h)
            names = {s["name"] for s in c.get(bid_path(BOARD_USB) + "/readings/history",
                                              headers=h).json()["series"]}
            assert {"mcc_temp", "dut_clk", "answer_ms"} <= names
            r = c.get(bid_path(BOARD_USB) + "/readings/history", params={"limit": "-1"},
                      headers=h)
            assert r.status_code == 400
    finally:
        eng.close_all()


# --- the MPS3 pack's seam: the last stats reply its telemetry read -----------------------------


class _Tap:
    last: dict = {}


class _Resp:
    def __init__(self, raw):
        self.ok, self.raw = bool(raw.get("ok")), raw


class _Client:
    def __init__(self, replies, tap):
        self.replies, self.tap = replies, tap

    def telemetry(self):
        self.tap.last = dict(self.replies["telemetry"])
        return _Resp(self.tap.last)

    def stats(self):
        self.tap.last = dict(self.replies["stats"])
        return _Resp(self.tap.last)


class _Shell:
    def __init__(self, replies=None, error=None):
        self.tap, self.replies, self.error = _Tap(), replies or {}, error

    def call_raw(self, fn):
        if self.error is not None:
            raise self.error
        return fn(_Client(self.replies, self.tap), self.tap)


class _Session:
    def __init__(self, shell, features):
        self.ident = BoardIdentity("mps3", shell_id="0x72bb0a36", features=tuple(features))
        self.candidate = Candidate("mps3", "mps3@x", (Link(LinkKind.ETHERNET, "x:6900"),))
        self.shell = shell

    def identity(self):
        return self.ident


def test_the_mps3_telemetry_adapter_offers_the_stats_it_read_and_nothing_else():
    from harness_manager.core.errors import UnreachableError
    from harness_manager.services.history import facts_of
    from harness_manager_mps3.telemetry import Mps3Telemetry
    from tests.fakes.t9_shell import STATS_SYSMON

    stats = {"ok": True, "up_ms": 7_200_000, "swap_n": 4, "icap": 1_648_640, "rxdrop": 0,
             "txerr": 0, "link": True, "spd": 100, "sysmon": STATS_SYSMON, "mac": "x"}
    shell = _Shell({"telemetry": {"ok": True}, "stats": stats})
    t = Mps3Telemetry(_Session(shell, ("sysmon",)))
    assert t.readings_facts() is None                        # nothing read yet: nothing asked
    t.readings()
    f = facts_of(type("S", (), {"telemetry": t})())
    assert f.uptime_s == 7200.0 and f.source == "stats (6900)"
    assert f.stats == {"up_ms": 7_200_000, "swap_n": 4, "icap": 1_648_640, "rxdrop": 0,
                       "txerr": 0, "link": True, "spd": 100}
    # twins: a harness without sysmon is never asked for stats; a failed read offers nothing
    quiet = Mps3Telemetry(_Session(_Shell({"telemetry": {"ok": True}, "stats": stats}), ()))
    quiet.readings()
    assert quiet.readings_facts() is None
    broken = Mps3Telemetry(_Session(_Shell(error=UnreachableError("no answer")), ("sysmon",)))
    broken.readings()
    assert broken.readings_facts() is None
