"""HARNESS-CAT: the harness versions routes in the T14 mock daemon (what the H9 card is built
against). The mock runs the real ``harness_api`` routes and the real catalogue over a
simulated update service (``tests/fakes/hcat_mock_harness.py``); these check it behaves as
the daemon does: verdicts for a demo board, an install that moves the running mark, the
lease refusal, pins and history. Each with its negative twin.
"""

from __future__ import annotations

import time
import warnings
from urllib.parse import quote

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_USB, DemoEngine
from tests.fakes.t14_mock_api import create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "hcat-mock-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
Q = quote(BOARD_USB, safe="")          # the demo board with the Debug USB


@pytest.fixture
def client():
    eng = DemoEngine(speed=0.02)
    app = create_app(eng, token=TOKEN)
    with TestClient(app, raise_server_exceptions=False) as c:
        cands = c.post("/api/v1/probe", json={}, headers=AUTH).json()["candidates"]
        for bid in (BOARD_USB, BOARD_FIELDED):
            cand = next(x for x in cands if x["board_id"] == bid)
            assert c.post("/api/v1/boards", json={"candidate": cand},
                          headers=AUTH).status_code == 200
        c.app_state = app.state
        yield c
    eng.close_all()


def wait(c, r) -> dict:
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    for _ in range(500):
        body = c.get(f"/api/v1/jobs/{job}", headers=AUTH).json()
        if body["state"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("job still running")


def refresh(c, bid: str = BOARD_USB) -> dict:
    body = wait(c, c.post("/api/v1/harness/catalog/refresh",
                          json={"board_id": bid, "all": True}, headers=AUTH))
    assert body["state"] == "done", body
    return body["result"]


def test_the_mock_lists_verdicts_for_a_demo_board_and_installs_a_release(client):
    c = client
    assert c.get("/api/v1/harness/catalog", params={"board_id": BOARD_USB},
                 headers=AUTH).status_code == 409
    # the twin first: a board with no Debug USB cannot take a new base at all
    over_eth = {r["version"]: r["verdict"] for r in refresh(c, BOARD_FIELDED)["releases"]}
    assert over_eth["1.1.1"] == "needs-door" and over_eth["1.0.0"] == "fits"
    result = refresh(c)
    rows = {r["version"]: r for r in result["releases"]}
    assert {v: r["verdict"] for v, r in rows.items()} == {
        "2.0.0": "needs-door", "1.1.1": "re-key", "1.1.0": "re-key", "1.0.0": "fits"}
    assert rows["1.0.0"]["marks"] == ["running"] and result["board"]["running_release"] == "1.0.0"
    shown = c.get("/api/v1/harness/releases/1.1.1", params={"board_id": BOARD_USB},
                  headers=AUTH).json()
    fp = shown["plan"]["fingerprint"]
    r = c.post(f"/api/v1/boards/{Q}/harness/install", json={"fingerprint": fp}, headers=AUTH)
    assert r.status_code == 409 and "RE-KEYS" in r.json()["error"]["message"]     # the twin
    body = wait(c, c.post(f"/api/v1/boards/{Q}/harness/install",
                          json={"fingerprint": fp, "rekey_phrase": "REKEY 0x72bb0a36"},
                          headers=AUTH))
    assert body["state"] == "done" and body["result"]["result"] == "installed"
    rows = {r["version"]: r for r in refresh(c)["releases"]}
    assert "running" in rows["1.1.1"]["marks"] and "installed" in rows["1.1.1"]["marks"]
    hist = c.get(f"/api/v1/boards/{Q}/harness/history", headers=AUTH).json()
    assert [h["version"] for h in hist["history"]] == ["1.1.1"]
    assert hist["rollback"][0]["version"] == "1.0.0"


def test_the_mock_refuses_an_install_without_the_lease_and_pins_per_board(client):
    c = client
    refresh(c)
    fp = c.get("/api/v1/harness/releases/1.1.1", params={"board_id": BOARD_USB},
               headers=AUTH).json()["plan"]["fingerprint"]
    c.app_state.sim.behind_hub(BOARD_USB, lease="other")
    r = c.post(f"/api/v1/boards/{Q}/harness/install",
               json={"fingerprint": fp, "rekey_phrase": "REKEY 0x72bb0a36"}, headers=AUTH)
    assert r.status_code == 409 and r.json()["error"]["name"] == "HELD"
    assert r.json()["error"]["holder"] == "alice@lab-pc-07"
    r = c.put(f"/api/v1/boards/{Q}/harness/pin", json={"version": "1.1.0"}, headers=AUTH)
    assert r.json()["pinned"] == "1.1.0" and refresh(c)["offer"] == "1.1.0"
    assert c.delete(f"/api/v1/boards/{Q}/harness/pin", headers=AUTH).json()["previous"] == "1.1.0"
    assert refresh(c)["offer"] == "1.1.1"
