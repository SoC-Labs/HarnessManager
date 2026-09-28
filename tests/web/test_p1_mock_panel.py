"""Lane P1: the T14 mock daemon's front-panel routes, which lane P3 builds the UI against.

The mock must answer like the daemon (docs/API.md "Front panel"): a demo board is bare metal
until ``PanelSim.linux`` gives it the Linux harness's panel verbs. Every check has a twin.
"""

from __future__ import annotations

import warnings
from urllib.parse import quote

import pytest

from harness_manager.demo import BOARD_FIELDED, DemoEngine
from tests.fakes.t14_mock_api import create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "p1-mock-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BID = quote(BOARD_FIELDED, safe="")


@pytest.fixture
def mock():
    engine = DemoEngine(speed=0.02)
    app = create_app(engine, token=TOKEN, serve_ui=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        cands = client.post("/api/v1/probe", json={}, headers=AUTH).json()["candidates"]
        cand = next(c for c in cands if c["board_id"] == BOARD_FIELDED)
        assert client.post("/api/v1/boards", json={"candidate": cand}, headers=AUTH).status_code == 200
        yield client, app.state.panel, engine
    engine.close_all()


def test_a_demo_board_is_bare_metal_rebuilt_with_identify_greyed(mock):
    client, _sim, _engine = mock
    body = client.get(f"/api/v1/boards/{BID}/panel", headers=AUTH).json()
    assert body["panel"]["source"] == "rebuilt" and body["panel"]["owner"] == "harness"
    assert body["identify"]["available"] is False and "harness feature 'locate'" in body["identify"]["reason"]
    assert body["presence"]["active"] is False
    r = client.post(f"/api/v1/boards/{BID}/identify", json={"seconds": 5}, headers=AUTH)
    assert r.status_code == 422 and r.json()["error"]["capability"] == "locate"
    frame = client.get(f"/api/v1/boards/{BID}/panel/frame", headers=AUTH).json()
    assert frame["source"] == "rebuilt" and len(frame["rows"]) == 15


def test_the_linux_knob_gives_the_panel_sessions_taps_and_identify(mock):
    client, sim, engine = mock
    sim.linux(BOARD_FIELDED)
    sim.watcher(BOARD_FIELDED)
    events = []
    engine.bus.subscribe("panel.*", events.append)
    sim.tap(BOARD_FIELDED, "request")
    body = client.get(f"/api/v1/boards/{BID}/panel", headers=AUTH).json()
    panel = body["panel"]
    assert panel["source"] == "panel" and [s["role"] for s in panel["sessions"]] == ["owner", "watch"]
    assert [e["on"] for e in panel["events"]] == ["request"] and body["presence"]["active"]
    r = client.post(f"/api/v1/boards/{BID}/identify", json={"seconds": 5}, headers=AUTH)
    assert r.status_code == 200 and r.json()["seconds"] == 5
    assert [e.topic for e in events] == ["panel.tap", "panel.locate"]
    assert client.get(f"/api/v1/boards/{BID}/panel", headers=AUTH).json()["identify"]["until"]
    frame = client.get(f"/api/v1/boards/{BID}/panel/frame", headers=AUTH).json()
    assert frame["source"] == "panel" and len(frame["roles"]) == 600


def test_the_mock_checks_seconds_and_holds_like_the_daemon(mock):
    client, sim, _engine = mock
    sim.linux(BOARD_FIELDED)
    r = client.post(f"/api/v1/boards/{BID}/identify", json={"seconds": 99}, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"]["name"] == "USAGE"
    other = quote("mps3@192.168.10.199:6900", safe="")
    assert client.get(f"/api/v1/boards/{other}/panel", headers=AUTH).status_code == 404
