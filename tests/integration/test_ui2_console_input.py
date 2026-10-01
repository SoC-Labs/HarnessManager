"""UI v2 G1b (lane UI2-API-HUB): a read-only client's keystrokes never reach the board, and the socket
says why. Each check has a negative twin."""

from __future__ import annotations

import json
import time

from tests.integration import ui2_showcase as rig
from tests.integration.ui2_showcase import (
    API,
    AUTH,
    LEASED,
    LINUX,
    TOKEN,
    enc,
    open_board,
    wait_job,
)

showcase = rig.showcase                     # the fixture: the daemon over the showcase

# --- G1b: a read-only client's keystrokes never reach the board -------------------------------------------------


def frames_until(ws, pred, limit: int = 50) -> list:
    got = []
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("text") is not None:
            got.append(json.loads(msg["text"]))
            if pred(got[-1]):
                return got
    raise AssertionError(f"no such frame in {got}")


def test_a_read_only_console_drops_the_input_and_says_why(showcase):
    client, eng, _d = showcase
    open_board(client, LEASED)
    url = f"{API}/boards/{enc(LEASED)}/consoles/uart0?token={TOKEN}"
    with client.websocket_connect(url) as ws:
        first = json.loads(ws.receive_text())
        assert set(first) == {"state", "name", "detail"}           # the first frame is as before
        got = frames_until(ws, lambda f: "input" in f)
        assert got[-1]["input"]["writable"] is False and got[-1]["input"]["role"] == "dut"
        assert "alice@lab-pc-07" in got[-1]["input"]["read_only_reason"]
        ws.send_bytes(b"help()\r\n")
        err = frames_until(ws, lambda f: "error" in f)[-1]["error"]
        assert err["name"] == "HELD" and "not sent" in err["message"]
    time.sleep(0.2)
    assert not [w for w in eng.consoles.writes if w[0] == LEASED]


def test_twin_on_a_board_without_a_hub_typing_reaches_it(showcase):
    client, eng, _d = showcase
    open_board(client, LINUX)
    url = f"{API}/boards/{enc(LINUX)}/consoles/shell?token={TOKEN}"
    with client.websocket_connect(url) as ws:
        json.loads(ws.receive_text())
        ws.send_bytes(b"uname -a\n")
        deadline = time.monotonic() + 5
        while not [w for w in eng.consoles.writes if w[0] == LINUX]:
            assert time.monotonic() < deadline, "the keystrokes never reached the board"
            time.sleep(0.02)




def test_the_socket_turns_writable_when_the_lease_becomes_ours(showcase):
    client, eng, _d = showcase
    open_board(client, LEASED)
    b = enc(LEASED)
    url = f"{API}/boards/{b}/consoles/uart0?token={TOKEN}"
    with client.websocket_connect(url) as ws:
        json.loads(ws.receive_text())
        frames_until(ws, lambda f: f.get("input", {}).get("writable") is False)
        # your request's deadline has passed and alice never answered: force-release it
        r = client.post(f"{API}/boards/{b}/lease/force",
                        json={"confirm": True, "confirm_board": "mps3-02"}, headers=AUTH)
        assert r.status_code == 202, r.text
        assert wait_job(client, r.json()["job"])["state"] == "done"
        got = frames_until(ws, lambda f: f.get("input", {}).get("writable") is True)
        assert got[-1]["input"]["read_only_reason"] == ""
        ws.send_bytes(b"help()\r\n")
        deadline = time.monotonic() + 5
        while not [w for w in eng.consoles.writes if w[0] == LEASED]:
            assert time.monotonic() < deadline, "the keystrokes never reached the board"
            time.sleep(0.02)


def test_the_mock_drops_a_read_only_clients_input_too():
    import warnings

    from harness_manager.demo import BOARD_USB, DemoEngine
    from tests.fakes.t14_mock_api import create_app as mock_app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    eng = DemoEngine(speed=0)
    app = mock_app(eng, token="ui2-mock", serve_ui=False)
    auth = {"Authorization": "Bearer ui2-mock"}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            cands = client.post(f"{API}/probe", json={}, headers=auth).json()["candidates"]
            cand = next(c for c in cands if c["board_id"] == BOARD_USB)
            client.post(f"{API}/boards", json={"candidate": cand}, headers=auth)
            app.state.sim.behind_hub(BOARD_USB, lease="other")
            url = f"{API}/boards/{enc(BOARD_USB)}/consoles/uart0?token=ui2-mock"
            with client.websocket_connect(url) as ws:
                json.loads(ws.receive_text())
                ws.send_bytes(b"x\r\n")
                err = frames_until(ws, lambda f: "error" in f)[-1]["error"]
                assert err["name"] == "HELD"
            assert not [w for w in eng.consoles.writes if w[0] == BOARD_USB]
            app.state.sim.behind_hub(BOARD_USB, lease="mine")       # twin: held here
            with client.websocket_connect(url) as ws:
                json.loads(ws.receive_text())
                ws.send_bytes(b"y\r\n")
                deadline = time.monotonic() + 5
                while not [w for w in eng.consoles.writes if w[0] == BOARD_USB]:
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
    finally:
        eng.close_all()
