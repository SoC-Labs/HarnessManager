"""T10: the T14 mock daemon serves the XDC routes the way ``daemon/xdc_api.py`` does.

The mock runs the real xdc service (``tests/fakes/t10_mock_xdc.py``); these tests pin the
answers the UI relies on, each with a negative twin, so the browser tests over the mock
see what they would see over the real daemon.
"""

from __future__ import annotations

import io
import warnings
import zipfile
from urllib.parse import quote

import pytest

from harness_manager.demo import BOARD_USB, DemoEngine
from tests.fakes.t14_mock_api import create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "t10-mock"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client():
    eng = DemoEngine(speed=0.02)
    try:
        with TestClient(create_app(eng, token=TOKEN, serve_ui=False),
                        raise_server_exceptions=False) as c:
            cands = c.post("/api/v1/probe", json={}, headers=H).json()["candidates"]
            cand = next(x for x in cands if x["board_id"] == BOARD_USB)
            assert c.post("/api/v1/boards", json={"candidate": cand}, headers=H).status_code == 200
            yield c
    finally:
        eng.close_all()


def path(tail: str) -> str:
    return f"/api/v1/boards/{quote(BOARD_USB, safe='')}{tail}"


def test_the_catalogue_and_the_board_static(client):
    assert client.get("/api/v1/xdc", headers=H).json()["model"]["default_shell"] == "0x72BB0A36"
    b = client.get(path("/xdc"), headers=H).json()["board"]
    assert b["matches"] is False and b["static_id"].lower() == "0x3f1a560f"   # the demo shell


def test_preview_passes_or_fails_with_the_files_either_way(client):
    good = client.post(path("/xdc/export"), json={"kit": "board", "design": "blinky",
                                                  "preview": True}, headers=H).json()
    assert good["ok"] and good["passed"] and "blinky_pins.xdc" in good["files"]
    rm = client.post(path("/xdc/export"), json={"kit": "rm-kit", "design": "nanosoc",
                                                "preview": True}, headers=H).json()
    assert rm["ok"] and rm["passed"] is False            # the demo board runs another static
    assert [c["code"] for c in rm["checks"] if c["severity"] == "error"] == ["static_id"]


def test_a_refused_export_is_409_with_the_checks_and_zip_is_a_zip(client):
    r = client.post(path("/xdc/export"), json={"kit": "rm-kit", "design": "nanosoc"}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    assert r.json()["error"]["data"]["checks"]
    z = client.post(path("/xdc/export"), json={"kit": "board", "design": "blinky",
                                               "format": "zip"}, headers=H)
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    assert "blinky/blinky_pins.xdc" in zipfile.ZipFile(io.BytesIO(z.content)).namelist()


def test_bad_requests_answer_like_the_daemon(client):
    assert client.post(path("/xdc/export"), json={"kit": "nope"}, headers=H).status_code == 400
    assert client.post(path("/xdc/export"), json={"kit": "board", "design": "/etc/passwd"},
                       headers=H).status_code == 400
    assert client.post(path("/xdc/export"), json={"kit": "board", "design": "no_such"},
                       headers=H).status_code == 404
    assert client.get("/api/v1/boards/mps3%40203.0.113.9%3A6900/xdc", headers=H).status_code == 404
    assert client.get("/api/v1/xdc").status_code == 401
