"""Lane L4: ``GET /boards/{bid}/power`` and ``POST /boards/{bid}/power/cycle`` on the real daemon.

The app wraps the real Engine and MPS3 pack over ``VirtualMps3``; the meter is T9's
``FakeShelly`` on 127.0.0.1, configured in ``boards.toml`` like a real plug. The power
cycle runs on the plug's fake clock, so it costs no wall time. Every check has a
negative twin.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.core.errors import ExitCode, UnavailableError
from harness_manager.daemon.app import create_app
from harness_manager.power import ina260
from tests.fakes.l4_service import H, events_until, hold_board, share_clock, write_boards
from tests.fakes.t9_plugs import FakeClock, FakeShelly
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for
from tests.fakes.virtual_board import VirtualMps3

ROWS = ["board_power", "supply_voltage", "supply_current"]


@pytest.fixture
def engine(vboard: VirtualMps3):
    eng = engine_for(vboard)
    yield eng
    eng.close_all()


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    with TestClient(create_app(engine, token=TOKEN, static_dir=None)) as c:
        yield c


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def plug(clock) -> Iterator[FakeShelly]:
    with FakeShelly(clock=clock) as dev:
        yield dev


def shelly_board(vb: VirtualMps3, plug: FakeShelly, **extra: str) -> str:
    board_id = f"mps3@{vb.shell_endpoint}"
    lines = [f'[boards."{board_id}".power]', 'kind = "shelly_gen2"', f'url = "{plug.url}"']
    lines += [f"{k} = {v}" for k, v in extra.items()]
    write_boards("\n".join(lines) + "\n")
    return board_id


def open_board(client: TestClient, vb: VirtualMps3) -> str:
    r = client.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "l4"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def open_with_plug(client, engine, vb, plug, clock, **extra) -> str:
    shelly_board(vb, plug, **extra)
    bid = open_board(client, vb)
    share_clock(engine.session(bid), clock)
    return bid


# --- GET /power ---------------------------------------------------------------------------------


def test_power_reads_the_configured_plug_and_says_it_can_cycle(client, engine, vboard, plug, clock):
    bid = open_with_plug(client, engine, vboard, plug, clock)
    r = client.get(f"{bid_path(bid)}/power", headers=H)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["board_id"] == bid
    rows = {x["name"]: x for x in body["readings"]}
    assert list(rows) == ROWS
    assert (rows["board_power"]["value"], rows["board_power"]["unit"]) == (11.4, "W")
    assert rows["supply_voltage"]["value"] == 239.1 and rows["supply_current"]["value"] == 0.071
    assert all(x["available"] and "age_s" in x for x in body["readings"])
    assert body["device"] == f"shelly_gen2 {plug.url} outlet 0"
    assert rows["board_power"]["source"] == body["device"]
    assert body["cycle_reason"] == ""
    # Negative twin: the same plug hanging answers unavailable rows with the reason, never 0.
    plug.hang = True
    engine.session(bid).power.driver.http.timeout_s = 0.3
    body = client.get(f"{bid_path(bid)}/power", headers=H).json()
    assert [x["value"] for x in body["readings"]] == [None, None, None]
    assert all(not x["available"] and "did not answer" in x["reason"] for x in body["readings"])


def test_a_board_without_a_meter_answers_unavailable_rows_with_the_packs_reason(client, vboard):
    bid = open_board(client, vboard)
    body = client.get(f"{bid_path(bid)}/power", headers=H).json()
    assert [x["name"] for x in body["readings"]] == ROWS
    assert all(x["value"] is None and not x["available"] for x in body["readings"])
    assert all("no power sensor" in x["reason"] and x["source"] == "power-meter"
               for x in body["readings"])
    assert body["device"] is None
    assert "networked power plug" in body["cycle_reason"]
    # ... and the cycle is refused with that same reason, before any job.
    r = client.post(f"{bid_path(bid)}/power/cycle", json={}, headers=H)
    err = r.json()["error"]
    assert (r.status_code, err["name"], err["capability"]) == (422, "UNAVAILABLE", "power_cycle")
    assert err["reason"] == body["cycle_reason"]
    assert client.get("/api/v1/jobs", headers=H).json()["jobs"] == []


# --- POST /power/cycle ----------------------------------------------------------------------------


def test_power_cycle_is_a_job_with_power_events_and_the_devices_evidence(client, engine, vboard,
                                                                         plug, clock):
    bid = open_with_plug(client, engine, vboard, plug, clock)
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=job.*,power.*") as ws:
        r = client.post(f"{bid_path(bid)}/power/cycle", json={"off_s": 5}, headers=H)
        assert r.status_code == 202, r.text
        job = r.json()["job"]
        frames = events_until(ws, "job.done", job)
    topics = [f["topic"] for f in frames]
    assert topics[0] == "job.started" and frames[0]["data"]["kind"] == "power_cycle"
    cycle = [f["data"] for f in frames if f["topic"] == "power.cycle"]
    device = f"shelly_gen2 {plug.url} outlet 0"
    assert cycle == [{"phase": "off", "off_s": 5.0, "device": device},
                     {"phase": "on", "off_s": 5.0, "device": device}]
    assert all(f["board_id"] == bid for f in frames)
    assert [f["data"]["phase"] for f in frames if f["topic"] == "job.progress"] == ["off", "on"]
    # the result is the device's evidence: it switched off, and reported on again
    ev = frames[-1]["data"]["result"]
    assert ev["board_id"] == bid and ev["meter"] == device and ev["off_s"] == 5.0
    assert (ev["was_on"], ev["confirmed_off"], ev["confirmed_on"]) == (True, True, True)
    assert ev["seconds"] >= 5.0
    assert plug.sets == [{"id": "0", "on": "false", "toggle_after": "5"}] and plug.output is True
    state = client.get(f"/api/v1/jobs/{job}", headers=H).json()
    assert state["state"] == "done" and state["phases"] == ["off", "on"]
    assert state["result"] == ev


def test_negative_twin_an_outlet_that_never_switches_fails_the_job_with_no_power_events(
        client, engine, vboard, plug, clock):
    bid = open_with_plug(client, engine, vboard, plug, clock)
    plug.ignore_set = True
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=job.*,power.*") as ws:
        job = client.post(f"{bid_path(bid)}/power/cycle", json={}, headers=H).json()["job"]
        frames = events_until(ws, "job.failed", job)
    err = frames[-1]["data"]["error"]
    assert err["code"] == ExitCode.ACTION_FAILED and "still reports ON" in err["message"]
    assert not [f for f in frames if f["topic"] == "power.cycle"]       # nothing claimed
    assert client.get(f"/api/v1/jobs/{job}", headers=H).json()["state"] == "failed"


def test_a_meter_only_ina260_measures_but_the_cycle_is_refused_with_its_reason(
        client, vboard, monkeypatch):
    class NoBridge:                   # never reach for a real USB-I2C bridge on the test machine
        def __init__(self, device: int = 0) -> None:
            raise UnavailableError("ina260_mcp2221", "no MCP2221A here (test stub)")

    monkeypatch.setattr(ina260, "Mcp2221Bus", NoBridge)
    write_boards('[boards.lab]\nmatch = ["127.0.0.1"]\n'
                 'power = { kind = "ina260_mcp2221", i2c_address = 0x41, device = 1 }\n')
    bid = open_board(client, vboard)
    body = client.get(f"{bid_path(bid)}/power", headers=H).json()
    assert all(x["value"] is None and "no MCP2221A here" in x["reason"] for x in body["readings"])
    assert "only measures" in body["cycle_reason"] and body["device"].startswith("ina260")
    r = client.post(f"{bid_path(bid)}/power/cycle", json={"off_s": 5}, headers=H)
    err = r.json()["error"]
    assert (r.status_code, err["name"], err["capability"]) == (422, "UNAVAILABLE", "power_cycle")
    assert err["reason"] == body["cycle_reason"]
    assert client.get("/api/v1/jobs", headers=H).json()["jobs"] == []


def test_a_plug_with_cycling_disabled_is_refused_and_never_switched(client, engine, vboard, plug,
                                                                    clock):
    bid = open_with_plug(client, engine, vboard, plug, clock, cycle="false")
    body = client.get(f"{bid_path(bid)}/power", headers=H).json()
    assert body["readings"][0]["value"] == 11.4                   # it still measures
    assert "power.cycle = false" in body["cycle_reason"]
    r = client.post(f"{bid_path(bid)}/power/cycle", json={}, headers=H)
    assert r.status_code == 422 and r.json()["error"]["reason"] == body["cycle_reason"]
    assert plug.sets == []


@pytest.mark.parametrize("body", [{"off_s": 0.5}, {"off_s": 301}, {"off_s": "5"}, {"off_s": True},
                                  [5]])
def test_bad_off_times_are_usage_errors_before_any_job(client, engine, vboard, plug, clock, body):
    bid = open_with_plug(client, engine, vboard, plug, clock)
    r = client.post(f"{bid_path(bid)}/power/cycle", json=body, headers=H)
    assert (r.status_code, r.json()["ok"], r.json()["error"]["name"]) == (400, False, "USAGE")
    assert plug.sets == [] and client.get("/api/v1/jobs", headers=H).json()["jobs"] == []


# --- the board gate, the envelope -------------------------------------------------------------------


def test_power_routes_are_held_while_another_job_runs_on_the_board(client, engine, vboard, plug,
                                                                   clock):
    bid = open_with_plug(client, engine, vboard, plug, clock)
    job, release = hold_board(client.app.state.daemon, bid)
    try:
        for r in (client.get(f"{bid_path(bid)}/power", headers=H),
                  client.post(f"{bid_path(bid)}/power/cycle", json={}, headers=H)):
            err = r.json()["error"]
            assert (r.status_code, err["name"]) == (409, "HELD")
            assert err["data"] == {"job": job.id, "kind": "deploy", "board_id": bid}
        assert plug.sets == []
    finally:
        release.set()
    assert job.finished.wait(10)
    # Negative twin: once the job ends, the same requests are served.
    assert client.get(f"{bid_path(bid)}/power", headers=H).status_code == 200
    r = client.post(f"{bid_path(bid)}/power/cycle", json={}, headers=H)
    assert r.status_code == 202, r.text


def test_power_routes_need_an_open_board_and_the_token(client, vboard):
    B = bid_path(f"mps3@{vboard.shell_endpoint}")
    for r in (client.get(f"{B}/power", headers=H), client.post(f"{B}/power/cycle", headers=H)):
        assert (r.status_code, r.json()["error"]["name"]) == (404, "ABSENT")
    open_board(client, vboard)
    r = client.get(f"{B}/power")
    assert r.status_code == 401 and r.json()["error"]["code"] == ExitCode.REFUSED
    assert client.get(f"{B}/power", headers=H).status_code == 200       # the twin: with it
