"""Lane L2: ``screen`` on the demo boards (``harness-manager daemon start --demo``).

The demo engine's console service has ``subscribe`` but no PTYs or rates of its
own, so ``consoles_api`` serves them through its fallback. Thursday's plan has
david trying ``screen`` on the demo boards. Every check has a negative twin.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import ExitCode
from harness_manager.daemon.app import create_app
from harness_manager.demo import DemoEngine
from harness_manager.services import pty as ptymod
from tests.fakes.l2_rig import PtyClient, pty_dir, wait_for
from tests.fakes.t13_daemon import TOKEN, bid_path, headers

pytestmark = pytest.mark.skipif(not ptymod.supported(), reason="PTYs need a POSIX system")

H = headers()


@pytest.fixture
def demo(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ptymod.PTY_DIR_ENV, str(pty_dir(tmp_path)))
    eng = DemoEngine(speed=0)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            cand = c.post("/api/v1/probe", json={}, headers=H).json()["candidates"][0]
            bid = c.post("/api/v1/boards", json={"candidate": cand}, headers=H).json()["board_id"]
            yield c, bid
    finally:
        eng.close_all()


def test_a_demo_console_gets_a_pty_that_echoes(demo):
    client, bid = demo
    B = bid_path(bid)
    info = client.post(f"{B}/consoles/uart0/pty", headers=H).json()
    assert info["ok"] and info["command"] == f"screen {info['path']}"
    with PtyClient(info["path"]) as term:
        term.type("hello")
        term.read_until(b"hello\n")                     # the demo DUT echoes
        wait_for(lambda: client.get(f"{B}/consoles/uart0/pty", headers=H).json()["pty"]
                 ["clients"] == 1, what="one client")
    rows = {r["name"]: r for r in client.get(f"{B}/consoles", headers=H).json()["consoles"]}
    assert rows["uart0"]["pty"] == info["path"] and rows["uart0"]["source"] == "unknown"
    # Closing the board closes the PTY.
    client.delete(B, headers=H)
    assert not os.path.lexists(info["path"])


def test_negative_twin_demo_rates_are_unknown_and_not_settable(demo):
    client, bid = demo
    B = bid_path(bid)
    row = client.get(f"{B}/consoles/uart0/baud", headers=H).json()
    assert row["baud"] is None and row["settable"] is False and row["reason"]
    r = client.post(f"{B}/consoles/uart0/baud", json={"baud": 115200}, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == ExitCode.UNAVAILABLE
    r = client.post(f"{B}/consoles/nope/pty", headers=H)
    assert r.status_code == 404
