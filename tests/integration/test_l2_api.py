"""Lane L2: the consoles routes (docs/API.md "Week-plan additions → Consoles") on the virtual MPS3.

harness-manager-daemon's app through FastAPI's TestClient, wrapping the real Engine
pointed at an ``l2_virtual_board`` (nanosoc loaded; ``uart_baud`` where a test asks).
``consoles_api`` is loaded by ``create_app``'s EXTENSIONS hook, untouched. The pack is
wired with the L2 CCR. Every check has a negative twin. Only 127.0.0.1 is reached.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import ExitCode
from harness_manager.daemon.app import create_app
from harness_manager.daemon.jobs import Job
from harness_manager.services import pty as ptymod
from tests.fakes.l2_rig import (
    PtyClient,
    PtyHolder,
    apply_pack_ccr,
    fast_pty_options,
    in_ring,
    install_uart_baud_codec,
    l2_virtual_board,
    pty_dir,
    wait_for,
)
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers

pytestmark = pytest.mark.skipif(not ptymod.supported(), reason="PTYs need a POSIX system")

H = headers()
BANNER = b"nanosoc boot\n"


@pytest.fixture(autouse=True)
def _wiring(tmp_path: Path, monkeypatch) -> None:
    apply_pack_ccr(monkeypatch)
    install_uart_baud_codec(monkeypatch)
    monkeypatch.setenv(ptymod.PTY_DIR_ENV, str(pty_dir(tmp_path)))


def make_client(vb) -> Iterator[tuple[TestClient, object]]:
    eng = engine_for(vb)
    eng.consoles._pty_options.update(fast_pty_options())
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            yield c, eng
    finally:
        eng.close_all()


@pytest.fixture
def board(tmp_path: Path):
    with l2_virtual_board(tmp_path) as vb:
        yield vb


@pytest.fixture
def fielded(tmp_path: Path):
    with l2_virtual_board(tmp_path, features=()) as vb:
        yield vb


@pytest.fixture
def api(board):
    yield from make_client(board)


@pytest.fixture
def api_fielded(fielded):
    yield from make_client(fielded)


def open_board(client: TestClient, vb) -> str:
    r = client.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "l2"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


# -- PTYs ---------------------------------------------------------------------------------------------


def test_pty_post_get_delete(api, board):
    client, _eng = api
    B = bid_path(open_board(client, board))
    r = client.post(f"{B}/consoles/uart0/pty", headers=H)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert set(body) >= {"path", "device", "command", "clients"}
    assert body["command"] == f"screen {body['path']}" and os.readlink(body["path"]) == body["device"]
    again = client.post(f"{B}/consoles/uart0/pty", headers=H).json()
    assert (again["path"], again["device"]) == (body["path"], body["device"])   # idempotent
    with PtyClient(body["path"]) as term:
        term.read_until(BANNER)
        got = wait_for(lambda: client.get(f"{B}/consoles/uart0/pty", headers=H)
                       .json()["pty"]["clients"] == 1, what="one client")
        assert got
    r = client.delete(f"{B}/consoles/uart0/pty", headers=H)
    assert r.status_code == 200 and r.json()["closed"] is True
    assert client.get(f"{B}/consoles/uart0/pty", headers=H).json()["pty"] is None
    assert not os.path.lexists(body["path"])
    # Negative twin: a second DELETE has nothing to close.
    assert client.delete(f"{B}/consoles/uart0/pty", headers=H).json()["closed"] is False


def test_a_program_that_opens_the_pty_and_leaves_does_not_eat_the_banner(api, board):
    # The 2026-09-24 flake, on purpose: on this host something outside the process opens
    # and closes a new PTY within ~1 ms of its link appearing under /tmpdir. The daemon
    # then reset the line with TCSAFLUSH and the banner queued for screen was gone.
    client, eng = api
    bid = open_board(client, board)
    B = bid_path(bid)
    body = client.post(f"{B}/consoles/uart0/pty", headers=H).json()
    port = eng.consoles._ptys.get(bid, "uart0")
    wait_for(lambda: in_ring(port, BANNER), what="the banner in the PTY's recent output")

    def clients() -> int:
        return client.get(f"{B}/consoles/uart0/pty", headers=H).json()["pty"]["clients"]

    probe = PtyHolder(body["path"])                  # opens, never reads
    wait_for(lambda: clients() == 1, what="the probe counted")
    probe.release()
    wait_for(lambda: clients() == 0, what="the probe gone")
    with PtyClient(body["path"]) as term:
        term.read_until(BANNER, timeout=5)


def test_screen_attaching_after_the_banner_still_shows_it(api, board):
    # GNU screen sets its line up with TCSAFLUSH ~200 ms after opening: output queued
    # before that was never shown. The PTY now replays the recent output after the setup.
    client, eng = api
    bid = open_board(client, board)
    body = client.post(f"{bid_path(bid)}/consoles/uart0/pty", headers=H).json()
    port = eng.consoles._ptys.get(bid, "uart0")
    wait_for(lambda: in_ring(port, BANNER), what="the banner printed before screen came")
    with PtyClient(body["path"], exclusive=True, setup_s=0.2) as term:
        term.read_until(BANNER, timeout=5)
        term.type("print(1)")                                      # then it is live
        term.read_until(b"print(1)\r", timeout=5)


def test_negative_twin_no_pty_for_an_unknown_console_or_a_closed_board(api, board):
    client, _eng = api
    B = bid_path(open_board(client, board))
    r = client.post(f"{B}/consoles/uart9/pty", headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == ExitCode.ABSENT
    r = client.post(f"{bid_path('mps3@10.9.9.9:6900')}/consoles/uart0/pty", headers=H)
    assert r.status_code == 404


def test_closing_the_board_closes_its_pty(api, board):
    client, _eng = api
    bid = open_board(client, board)
    path = client.post(f"{bid_path(bid)}/consoles/uart0/pty", headers=H).json()["path"]
    assert os.path.lexists(path)
    assert client.delete(bid_path(bid), headers=H).json()["closed"] is True
    assert not os.path.lexists(path)


def test_without_ptys_the_api_answers_422_with_the_tcp_export(api, board, monkeypatch):
    client, _eng = api
    B = bid_path(open_board(client, board))
    monkeypatch.setattr(ptymod, "supported", lambda: False)
    r = client.post(f"{B}/consoles/uart0/pty", headers=H)
    err = r.json()["error"]
    assert r.status_code == 422 and err["code"] == ExitCode.UNAVAILABLE and "--export" in err["hint"]


# -- baud -------------------------------------------------------------------------------------------------


def test_baud_get_and_set_through_the_harness_verb(api, board):
    client, _eng = api
    B = bid_path(open_board(client, board))
    row = client.get(f"{B}/consoles/uart0/baud", headers=H).json()
    assert (row["baud"], row["settable"], row["source"]) == (76800, True, "harness")
    assert 115200 in row["choices"] and "reason" in row
    r = client.post(f"{B}/consoles/uart0/baud", json={"baud": 115200}, headers=H)
    assert r.status_code == 200 and (r.json()["baud"], r.json()["source"]) == (115200, "harness")
    assert board.shell.uart["uart0"]["baud"] == 115200


def test_negative_twin_the_fielded_harness_refuses_with_the_designs_rate(api_fielded, fielded):
    client, _eng = api_fielded
    B = bid_path(open_board(client, fielded))
    row = client.get(f"{B}/consoles/uart0/baud", headers=H).json()
    assert (row["baud"], row["settable"], row["source"]) == (76800, False, "design")
    r = client.post(f"{B}/consoles/uart0/baud", json={"baud": 115200}, headers=H)
    err = r.json()["error"]
    assert r.status_code == 422 and err["code"] == ExitCode.UNAVAILABLE
    assert "76800" in err["message"] and "uart_baud" in err["message"]


@pytest.mark.parametrize("body", [{}, {"baud": "fast"}, {"baud": True}, {"baud": 1.5}, [1]])
def test_a_bad_baud_body_is_400(api, board, body):
    client, _eng = api
    B = bid_path(open_board(client, board))
    r = client.post(f"{B}/consoles/uart0/baud", json=body, headers=H)
    assert r.status_code == 400 and r.json()["error"]["code"] == ExitCode.USAGE
    assert not [o for o in board.shell.ops if o.get("op") == "uart_baud" and "baud" in o]


def test_the_console_list_carries_rates_and_ptys(api, board):
    client, _eng = api
    B = bid_path(open_board(client, board))
    client.post(f"{B}/consoles/uart0/pty", headers=H)
    body = client.get(f"{B}/consoles", headers=H).json()
    assert body["names"] == ["swo", "uart0", "uart1"]
    rows = {r["name"]: r for r in body["consoles"]}
    assert rows["uart0"]["kind"] == "ethernet" and rows["uart0"]["baud"] == 76800
    assert rows["uart0"]["pty"].endswith("/uart0") and rows["uart1"]["pty"] is None
    assert {"name", "kind", "baud", "settable", "pty"} <= set(rows["swo"])
    # ?rates=0 lists names without asking the board.
    before = len(board.shell.ops)
    names_only = client.get(f"{B}/consoles?rates=0", headers=H).json()
    assert names_only["names"] == body["names"] and len(board.shell.ops) == before


# -- the job/HELD rules ------------------------------------------------------------------------------------


def test_while_a_job_runs_a_rate_change_is_held_and_reads_use_the_last_report(api, board):
    client, _eng = api
    bid = open_board(client, board)
    B = bid_path(bid)
    client.get(f"{B}/consoles/uart0/baud", headers=H)              # a report to fall back on
    gates = client.app.state.daemon.gates
    job = Job("deploy", bid)
    gates.claim(bid, job)
    try:
        before = len(board.shell.ops)
        r = client.post(f"{B}/consoles/uart0/baud", json={"baud": 115200}, headers=H)
        assert r.status_code == 409 and r.json()["error"]["code"] == ExitCode.HELD
        assert r.json()["error"]["data"]["job"] == job.id
        row = client.get(f"{B}/consoles/uart0/baud", headers=H)
        assert row.status_code == 200 and row.json()["baud"] == 76800
        rows = client.get(f"{B}/consoles", headers=H)
        assert rows.status_code == 200 and len(rows.json()["consoles"]) == 3
        # a PTY does not talk to the control port: it still opens during a job
        assert client.post(f"{B}/consoles/uart1/pty", headers=H).status_code == 200
        assert len(board.shell.ops) == before                      # the board was left alone
    finally:
        gates.release(bid, job)
    # Negative twin: once the job ends, the change goes through.
    r = client.post(f"{B}/consoles/uart0/baud", json={"baud": 115200}, headers=H)
    assert r.status_code == 200


def test_during_a_job_with_no_earlier_report_the_rate_says_why(api, board):
    client, _eng = api
    bid = open_board(client, board)
    gates = client.app.state.daemon.gates
    job = Job("sd_install", bid)
    gates.claim(bid, job)
    try:
        row = client.get(f"{bid_path(bid)}/consoles/uart0/baud", headers=H).json()
    finally:
        gates.release(bid, job)
    assert row["baud"] is None and "sd_install job" in row["reason"]
