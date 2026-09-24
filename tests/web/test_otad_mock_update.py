"""Lane OTA-D: the T14 mock daemon's app-update routes (``tests/fakes/l3_week_plan.py``) follow
docs/API.md "App self-update", so the UI lane (OTA-U) can build against them. Each check has
a negative twin.
"""

from __future__ import annotations

import json
import time
import warnings
from urllib.parse import quote

import pytest

from harness_manager.demo import BOARD_FIELDED, DemoEngine
from tests.fakes.t14_mock_api import create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "otad-mock-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def engine():
    eng = DemoEngine(speed=0.02)
    yield eng
    eng.close_all()


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine, token=TOKEN, serve_ui=False),
                    raise_server_exceptions=False) as c:
        yield c


def events_until(ws, topic: str) -> list[dict]:
    frames = []
    for _ in range(500):
        frame = json.loads(ws.receive_text())
        frames.append(frame)
        if frame["topic"] == topic:
            return frames
    raise AssertionError(f"no {topic}")


def test_the_mock_applies_and_reports_the_restart(client):
    body = client.get("/api/v1/update/app", headers=AUTH).json()
    assert body["apply"]["state"] == "idle" and body["effective"]["auto"] == "stage"
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=update.*") as ws:
        r = client.post("/api/v1/update/app/apply", json={"version": "0.1.0"}, headers=AUTH)
        assert r.status_code == 202 and r.json()["apply"]["to"] == "0.1.0"
        frames = events_until(ws, "update.applied")
    assert [f["data"].get("phase") for f in frames if f["topic"] == "update.applying"] == \
        ["draining", "restarting"]
    deadline = time.monotonic() + 5
    while client.get("/api/v1/update/app", headers=AUTH).json()["running"] != "0.1.0":
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_negative_twin_the_mock_rolls_back_and_marks_the_version_bad(client):
    sim = client.app.state.sim
    sim.apply_outcome = "rolled-back"
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=update.*") as ws:
        assert client.post("/api/v1/update/app/apply", json={"version": "0.1.0"},
                           headers=AUTH).status_code == 202
        events_until(ws, "update.rolled_back")
    r = client.post("/api/v1/update/app/apply", json={"version": "0.1.0"}, headers=AUTH)
    assert (r.status_code, r.json()["error"]["name"]) == (409, "REFUSED")


def test_the_mock_needs_confirm_for_an_attached_screen(client, engine):
    cands = client.post("/api/v1/probe", json={}, headers=AUTH).json()["candidates"]
    cand = next(c for c in cands if c["board_id"] == BOARD_FIELDED)
    client.post("/api/v1/boards", json={"candidate": cand}, headers=AUTH)
    B = f"/api/v1/boards/{quote(BOARD_FIELDED, safe='')}"
    names = client.get(f"{B}/consoles", headers=AUTH).json()["names"]
    pty = client.post(f"{B}/consoles/{names[0]}/pty", headers=AUTH)
    assert pty.status_code == 200, pty.text
    sim = client.app.state.sim
    sim.attach_screen(BOARD_FIELDED, names[0])
    r = client.post("/api/v1/update/app/apply", json={"version": "0.1.0"}, headers=AUTH)
    err = r.json()["error"]
    assert (r.status_code, err["data"]["reason"]) == (409, "SOFT_BUSY")
    assert err["data"]["soft_busy"][0]["kind"] == "screen"
    r = client.post("/api/v1/update/app/apply", json={"version": "0.1.0", "confirm": True},
                    headers=AUTH)
    assert r.status_code == 202                                           # the twin


def test_the_mock_settings_and_cancel(client):
    r = client.put("/api/v1/update/settings", json={"auto": "notify"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["effective"]["auto"] == "notify"
    r = client.put("/api/v1/update/settings", json={"auto": "always"}, headers=AUTH)
    assert (r.status_code, r.json()["error"]["name"]) == (400, "USAGE")
    r = client.post("/api/v1/update/app/cancel", headers=AUTH)
    assert (r.status_code, r.json()["error"]["name"]) == (409, "ALREADY")
