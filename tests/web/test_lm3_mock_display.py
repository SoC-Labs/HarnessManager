"""Lane LM3: the T14 mock daemon serves the Live display (docs/API.md "Live display") with the
REAL routes over a FakeLcdMirror per demo board (``tests/fakes/lm3_mock_display.py``), so
lane LM4 builds its canvas against exactly what the daemon sends. Each check has a twin.
"""

from __future__ import annotations

import json
import warnings
from urllib.parse import quote

import pytest

from harness_manager.core import display_wire as w
from harness_manager.core.errors import ExitCode
from harness_manager.demo import BOARD_FIELDED, BOARD_USB, DemoEngine
from tests.fakes.lm1_fake_lcd_mirror import ViewerModel
from tests.fakes.t14_mock_api import create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "lm3-mock-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def enc(board_id: str) -> str:
    return quote(board_id, safe="")


@pytest.fixture
def mock():
    engine = DemoEngine(speed=0.02)
    app = create_app(engine, token=TOKEN, serve_ui=False)
    try:
        with TestClient(app) as client:
            yield client, app
    finally:
        app.state.display.close()
        engine.close_all()


def open_board(client: TestClient, board_id: str) -> None:
    cands = client.post("/api/v1/probe", json={}, headers=AUTH).json()["candidates"]
    cand = next(c for c in cands if c["board_id"] == board_id)
    r = client.post("/api/v1/boards", json={"candidate": cand, "note": "lm3"}, headers=AUTH)
    assert r.status_code == 200, r.text


def ws_url(board_id: str, **query: str) -> str:
    q = "&".join(f"{k}={v}" for k, v in {"token": TOKEN, **query}.items())
    return f"/api/v1/boards/{enc(board_id)}/display/ws?{q}"


def refused(client: TestClient, url: str) -> tuple[int, dict]:
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect(url) as ws:
        first = json.loads(ws.receive_text())
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code, first


def test_a_live_demo_board_streams_a_picture_the_png_matches(mock):
    client, app = mock
    open_board(client, BOARD_FIELDED)
    app.state.display.freeze(BOARD_FIELDED)
    vm = ViewerModel()
    with client.websocket_connect(ws_url(BOARD_FIELDED)) as ws:
        status = json.loads(ws.receive_text())
        assert {"state", "hello", "reason", "badges"} <= set(status)
        while True:
            msg = ws.receive()
            if msg.get("bytes") is not None:
                vm.apply(msg["bytes"])
                break
        ws.send_text(json.dumps({"ack": vm.seq}))
    frame, valid = app.state.display.mirror(BOARD_FIELDED).picture()
    assert vm.keys == 1 and vm.matches(frame, valid)
    raw = client.get(f"/api/v1/boards/{enc(BOARD_FIELDED)}/display.png?format=raw", headers=AUTH)
    assert raw.status_code == 200 and raw.content == frame and len(raw.content) == w.FRAME_BYTES
    st = client.get(f"/api/v1/boards/{enc(BOARD_FIELDED)}/display", headers=AUTH).json()
    assert st["available"] is True and st["state"] == "live" and st["mode"] == "hw"
    # the twin: no token is 401, as on the daemon
    assert client.get(f"/api/v1/boards/{enc(BOARD_FIELDED)}/display").status_code == 401


def test_the_lease_rule_and_a_board_without_a_display_are_refused_as_the_daemon_refuses(mock):
    client, app = mock
    open_board(client, BOARD_FIELDED)
    app.state.sim.behind_hub(BOARD_FIELDED, lease="other", holder="alice@lab-pc-07")
    code, first = refused(client, ws_url(BOARD_FIELDED))
    assert code == 4000 + ExitCode.UNAVAILABLE and first["state"] == "refused"
    assert first["reason"] == ("the live display is for the lease holder only: "
                               "alice@lab-pc-07 holds mps3_01_pl")
    r = client.get(f"/api/v1/boards/{enc(BOARD_FIELDED)}/display.png", headers=AUTH)
    assert r.status_code == 422 and r.json()["error"]["reason"] == first["reason"]
    # the twin: the lease is mine
    app.state.sim.behind_hub(BOARD_FIELDED, lease="mine")
    assert client.get(f"/api/v1/boards/{enc(BOARD_FIELDED)}/display",
                      headers=AUTH).json()["available"] is True
    open_board(client, BOARD_USB)
    app.state.display.no_display(BOARD_USB)
    code, first = refused(client, ws_url(BOARD_USB))
    assert code == 4012 and "has no live display" in first["reason"]
    app.state.display.allow(BOARD_USB)                      # and the twin
    assert client.get(f"/api/v1/boards/{enc(BOARD_USB)}/display",
                      headers=AUTH).json()["available"] is True
