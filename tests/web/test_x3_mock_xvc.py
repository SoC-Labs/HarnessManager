"""Lane XVC-CORE: the T14 mock daemon's XVC routes (``tests/fakes/x3_mock_xvc.py``) follow
docs/API.md "Fabric debug over XVC", so the web card (X4) can be built against them. Each
check has a negative twin.
"""

from __future__ import annotations

import time
import warnings
from urllib.parse import quote

import pytest

from harness_manager.demo import BOARD_FIELDED, DemoEngine
from harness_manager.services.xvc import PARTITION_SCOPE, UNAUTHENTICATED_WARNING
from tests.fakes.t14_mock_api import create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "x3-mock-token"
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


def open_board(client, board_id: str, features=None) -> str:
    cands = client.post("/api/v1/probe", json={}, headers=AUTH).json()["candidates"]
    cand = next(c for c in cands if c["board_id"] == board_id)
    assert client.post("/api/v1/boards", json={"candidate": cand}, headers=AUTH).status_code == 200
    if features is not None:
        client.app.state.daemon.engine.set_features(board_id, features)
    return f"/api/v1/boards/{quote(board_id, safe='')}"


def wait_job(client, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()
        if body["state"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("job still running")


def test_the_mock_opens_reports_and_closes_an_xvc_session(client):
    base = open_board(client, BOARD_FIELDED, features=("xvc_dbgbr",))
    down = client.get(f"{base}/xvc", headers=AUTH).json()
    assert down["state"] == "down" and down["scope"] == PARTITION_SCOPE and down["reason"] == ""
    r = client.post(f"{base}/xvc/open", json={}, headers=AUTH)
    assert r.status_code == 202
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done" and job["result"]["state"] == "ready"
    st = client.get(f"{base}/xvc", headers=AUTH).json()
    assert st["open"] and st["mode"] == "m1" and st["url"] == f"localhost:{st['hw_server_port']}"
    assert UNAUTHENTICATED_WARNING in st["warnings"]
    tcl = client.get(f"{base}/xvc/tcl", headers=AUTH).json()
    assert f"connect_hw_server -url {st['url']}" in tcl["tcl"]
    assert client.post(f"{base}/xvc/close", headers=AUTH).json()["state"] == "down"


def test_negative_twin_the_mock_refuses_like_the_daemon(client):
    base = open_board(client, BOARD_FIELDED)                   # the fielded image: no xvc_dbgbr
    r = client.post(f"{base}/xvc/open", json={}, headers=AUTH)
    assert r.status_code == 422 and "xvc_dbgbr" in r.json()["error"]["message"]
    client.app.state.daemon.engine.set_features(BOARD_FIELDED, ("xvc_dbgbr",))
    client.app.state.sim.behind_hub(BOARD_FIELDED, lease="other")
    r = client.post(f"{base}/xvc/open", json={}, headers=AUTH)
    assert r.status_code == 409 and "lease holder only" in r.json()["error"]["message"]
    r = client.get(f"{base}/xvc/ltx?which=everything", headers=AUTH)
    assert r.status_code == 400
