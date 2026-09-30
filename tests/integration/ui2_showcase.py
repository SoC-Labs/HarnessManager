"""Shared by the UI v2 drop B tests (lane UI2-API-HUB): the real daemon over the showcase demo, whose
hub is in memory (nothing reaches a real hub or board)."""

from __future__ import annotations

import time
import warnings
from urllib.parse import quote

import pytest

from harness_manager import demo_showcase as show
from harness_manager.daemon.app import create_app
from harness_manager.demo import DemoEngine

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "ui2-api-hub-b"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
API = "/api/v1"
LEASED, SPARE, LINUX = show.BOARD_LEASED, show.BOARD_SPARE, show.BOARD_LINUX


def enc(bid: str) -> str:
    return quote(bid, safe="")


@pytest.fixture
def showcase(tmp_path):
    eng = DemoEngine(showcase=True, state_dir=tmp_path / "demo", speed=0)
    app = create_app(eng, token=TOKEN, static_dir=None)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(f"{API}/probe", json={}, headers=AUTH)
        yield client, eng, app.state.daemon
    eng.close_all()


def open_board(client, bid: str) -> None:
    rows = {r["board_id"]: r for r in client.get(f"{API}/boards", headers=AUTH).json()["boards"]}
    r = client.post(f"{API}/boards", json={"candidate": rows[bid]["candidate"]}, headers=AUTH)
    assert r.status_code == 200, r.text


def wait_job(client, job_id: str, timeout: float = 10.0, headers: dict | None = None) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{API}/jobs/{job_id}", headers=headers or AUTH).json()
        if body["state"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} still running")


