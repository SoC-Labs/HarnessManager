"""SET-API: the T14 mock daemon serves docs/API.md "Settings" with the real routes over a real
resolver in a temporary directory of its own (``tests/fakes/settings_mock.py``), so the
Settings UI (SET-UI) can be built against it. Each check has a negative twin.
"""

from __future__ import annotations

import json
import os
import tempfile
import warnings
from pathlib import Path

import pytest

from harness_manager.demo import DemoEngine
from tests.fakes.t14_mock_api import create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "set-api-mock-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "tok-MOCK-NEVER-SHOWN-20aa"


@pytest.fixture
def mock():
    eng = DemoEngine(speed=0.02)
    try:
        app = create_app(eng, token=TOKEN, serve_ui=False)
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, app.state.settings
    finally:
        eng.close_all()


def test_the_mocks_settings_live_in_its_own_temporary_directory(mock):
    client, sctx = mock
    body = client.get("/api/v1/settings", headers=AUTH).json()
    root = Path(body["files"]["config_dir"])
    assert root == sctx.config_dir and str(root).startswith(tempfile.gettempdir())
    assert root != Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    assert root != Path.home() / ".config" / "harness-manager"
    assert body["files"]["secrets_backend"]["backend"] == "file"


def test_the_mock_round_trips_a_setting_and_announces_it(mock):
    client, sctx = mock
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=settings.*") as ws:
        r = client.put("/api/v1/settings", json={"general.theme": "dark", "advanced.port": 0},
                       headers=AUTH)
        assert r.status_code == 200 and r.json()["apply"] == "restart"
        frame = json.loads(ws.receive_text())
    assert frame["topic"] == "settings.changed"
    assert frame["data"]["keys"] == ["general.theme", "advanced.port"]
    assert "dark" in (sctx.config_dir / "settings.toml").read_text()
    row = client.get("/api/v1/settings?key=general.theme", headers=AUTH).json()["rows"][0]
    assert (row["value"], row["source"]) == ("dark", "user")


def test_negative_twin_the_mock_refuses_a_locked_key_as_the_daemon_does(mock):
    client, sctx = mock
    sctx.policy_path.write_text('[lock]\ngeneral.window_size = "1280x800"\n')
    r = client.put("/api/v1/settings", json={"general.window_size": "1920x1080"}, headers=AUTH)
    assert r.status_code == 200                       # a user row: the policy cannot lock it
    sctx.policy_path.write_text('[lock]\ntools.vivado = "/tools/vivado"\n')
    r = client.put("/api/v1/settings", json={"tools.vivado": "/mine"}, headers=AUTH)
    assert r.status_code == 409 and str(sctx.policy_path) in r.json()["error"]["message"]


def test_the_mock_never_returns_a_secret(mock):
    client, sctx = mock
    texts = [client.put("/api/v1/settings/secrets/updates.github_token",
                        json={"value": SECRET}, headers=AUTH).text,
             client.get("/api/v1/settings?all=1", headers=AUTH).text,
             client.get("/api/v1/settings/schema", headers=AUTH).text]
    assert all(SECRET not in t for t in texts)
    assert sctx.store().get("updates.github_token") == SECRET          # the twin: it is stored


def test_the_mocks_test_route_and_update_settings_event(mock):
    client, _ = mock
    r = client.post("/api/v1/settings/test", json={"section": "consoles"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["testable"] is False
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=settings.*") as ws:
        r = client.put("/api/v1/update/settings", json={"auto": "notify"}, headers=AUTH)
        assert r.status_code == 200 and set(r.json()) == {"ok", "settings", "effective", "policy"}
        frame = json.loads(ws.receive_text())
    assert frame["data"]["keys"] == ["updates.auto"]
