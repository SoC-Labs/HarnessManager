"""Lane XVC-CORE (X3): the ``/boards/{bid}/xvc`` routes on the real daemon (``xvc_api.py``).

The app wraps the real Engine and MPS3 pack over a ``VirtualMps3``; the harness's XVC is
the fake server on 127.0.0.1 (``HARNESS_MANAGER_MPS3_XVC_PORT`` points a direct board at
it); behind a hub, the L1 lab rig fakes ssh and fpgahub. Each check has a negative twin.
"""

from __future__ import annotations

import os
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.core.errors import ExitCode
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager.services import xvc as X
from harness_manager_mps3 import xvc as MX
from tests.fakes.l1_rig import BOARD_IP, lab
from tests.fakes.l4_service import H, hold_board, wait_job
from tests.fakes.t2_overlays import FIELDED_USERCODE, make_overlay, use_overlay_dirs
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for
from tests.fakes.virtual_board import FIELDED_3F1A560F, FIELDED_ILA_V011, VirtualMps3
from tests.fakes.xvc_server import FakeXvcServer
from tests.integration.test_xvc_mps3 import NANOSOC_ILA, with_ltx
from tests.unit.test_xvc_service import IDCODE, read_idcode, wait_for


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


@pytest.fixture
def fake(monkeypatch):
    with FakeXvcServer() as srv:
        monkeypatch.setenv(MX.XVC_PORT_ENV, str(srv.port))
        yield srv


@pytest.fixture
def ila_overlay(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "ov"
    ila = make_overlay(root, "nanosoc_ila", rm_id=NANOSOC_ILA,
                       static_id=FIELDED_ILA_V011.static_id, static_usercode=FIELDED_USERCODE)
    ltx = with_ltx(ila, "nanosoc_ila", '{"probes": ["ila_0"]}')
    use_overlay_dirs(monkeypatch, root)
    return ltx


def client_for(vb: VirtualMps3) -> Iterator[tuple[TestClient, Engine]]:
    eng = engine_for(vb)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            yield c, eng
    finally:
        eng.close_all()


@pytest.fixture
def v011(tmp_path, fake, ila_overlay):
    with VirtualMps3(tmp_path / "vb", FIELDED_ILA_V011, boot_rm_id=NANOSOC_ILA) as vb:
        yield from ((c, eng, vb) for c, eng in client_for(vb))


def open_board(client: TestClient, vb: VirtualMps3) -> str:
    r = client.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "xvc"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def open_xvc(client: TestClient, bid: str, **body) -> dict:
    r = client.post(f"{bid_path(bid)}/xvc/open", json=body, headers=H)
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done", job
    return job["result"]


# --- status, open, tcl, ltx, close -----------------------------------------------------------------------


def test_status_open_tcl_ltx_close_round_trip(v011, fake, ila_overlay):
    client, eng, vb = v011
    bid = open_board(client, vb)
    down = client.get(f"{bid_path(bid)}/xvc", headers=H).json()
    assert down["ok"] and down["state"] == "down" and down["open"] is False
    assert "never whole-device JTAG" in down["scope"] and down["reason"] == ""
    assert X.UNAUTHENTICATED_WARNING in down["warnings"]           # X6, even before open
    result = open_xvc(client, bid, byo=True)
    assert result["state"] == "ready" and result["mode"] == "byo"
    st = client.get(f"{bid_path(bid)}/xvc", headers=H).json()
    assert st["state"] == "ready" and st["board_slot"] == "ours" and st["reach"] == "direct"
    with X.XvcClient("127.0.0.1", st["relay_port"]) as c:
        assert read_idcode(c) == IDCODE
    tcl = client.get(f"{bid_path(bid)}/xvc/tcl", headers=H).json()
    assert f"open_hw_target -xvc_url 127.0.0.1:{st['relay_port']}" in tcl["tcl"]
    assert f"PROBES.FILE {{{ila_overlay}}}" in tcl["tcl"] and tcl["which"] == "rm"
    meta = client.get(f"{bid_path(bid)}/xvc/ltx?which=rm&format=json", headers=H).json()
    assert meta["name"] == "nanosoc_ila.ltx" and meta["crc_ok"] is True
    got = client.get(f"{bid_path(bid)}/xvc/ltx", headers=H)
    assert got.status_code == 200 and got.content == ila_overlay.read_bytes()
    assert 'filename="nanosoc_ila.ltx"' in got.headers["content-disposition"]
    closed = client.post(f"{bid_path(bid)}/xvc/close", headers=H).json()
    assert closed["state"] == "down"
    wait_for(lambda: not fake.attached, what="the board's slot to free")


def test_negative_twin_missing_probes_bad_which_and_a_closed_board(v011):
    client, eng, vb = v011
    bid = open_board(client, vb)
    r = client.get(f"{bid_path(bid)}/xvc/ltx?which=static", headers=H)     # bare-metal: none
    assert r.status_code == 404 and r.json()["error"]["code"] == ExitCode.ABSENT
    r = client.get(f"{bid_path(bid)}/xvc/ltx?which=everything", headers=H)
    assert r.status_code == 400
    r = client.get(f"{bid_path(bid)}/xvc/tcl?byo=maybe", headers=H)
    assert r.status_code == 400
    r = client.get(f"{bid_path('mps3@10.9.9.9:6900')}/xvc", headers=H)
    assert r.status_code == 404


def test_open_is_refused_before_any_job_on_a_harness_without_xvc_dbgbr(tmp_path, fake):
    with VirtualMps3(tmp_path / "vb", FIELDED_3F1A560F) as vb:
        for client, _eng in client_for(vb):
            bid = open_board(client, vb)
            r = client.post(f"{bid_path(bid)}/xvc/open", json={"byo": True}, headers=H)
            assert r.status_code == 422 and "xvc_dbgbr" in r.json()["error"]["message"]
            assert client.get("/api/v1/jobs", headers=H).json()["jobs"] == []
            assert fake.stats.connections == 0


def test_an_engine_without_an_xvc_service_is_unavailable_not_an_internal_error():
    """XVC-UI: the demo engine (the GUI's, and the web tests' real daemon) has no XVC
    service. Every XVC route says so with 422 UNAVAILABLE, which the card shows as a reason,
    instead of 500 "this is a bug". The twin: the real engine above answers 200."""
    from harness_manager.demo import BOARD_FIELDED, DemoEngine

    eng = DemoEngine(speed=0.02)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as client:
            cands = client.post("/api/v1/probe", json={}, headers=H).json()["candidates"]
            cand = next(c for c in cands if c["board_id"] == BOARD_FIELDED)
            assert client.post("/api/v1/boards", json={"candidate": cand}, headers=H).status_code == 200
            for method, suffix in (("get", ""), ("get", "/tcl"), ("get", "/ltx"), ("post", "/open"),
                                   ("post", "/close")):
                r = getattr(client, method)(f"{bid_path(BOARD_FIELDED)}/xvc{suffix}", headers=H)
                err = r.json()["error"]
                assert r.status_code == 422 and err["code"] == ExitCode.UNAVAILABLE, (suffix, r.text)
                assert "no XVC service" in err["message"]
    finally:
        eng.close_all()


def test_negative_twin_open_is_held_while_a_job_runs_and_already_when_open(v011):
    client, eng, vb = v011
    bid = open_board(client, vb)
    job, release = hold_board(client.app.state.daemon, bid)
    try:
        r = client.post(f"{bid_path(bid)}/xvc/open", json={"byo": True}, headers=H)
        assert r.status_code == 409 and r.json()["error"]["name"] == "HELD"
    finally:
        release.set()
    wait_job(client, job.id)
    open_xvc(client, bid, byo=True)
    r = client.post(f"{bid_path(bid)}/xvc/open", json={"byo": True}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["name"] == "ALREADY"


def test_closing_the_board_closes_its_xvc_session(v011, fake):
    client, eng, vb = v011
    bid = open_board(client, vb)
    open_xvc(client, bid, byo=True)
    assert fake.attached
    assert client.delete(bid_path(bid), headers=H).json()["closed"] is True
    wait_for(lambda: not fake.attached, what="the board's slot to free")


# --- behind a hub: the lease holder only (X6) --------------------------------------------------------------


def test_behind_the_hub_open_needs_the_lease_and_uses_the_daemons_lease_service(
        tmp_path, monkeypatch, fake):
    with VirtualMps3(tmp_path / "vb", FIELDED_ILA_V011) as vb, \
            lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        rig.ssh.routes[(BOARD_IP, 2542)] = ("127.0.0.1", fake.port)
        eng = Engine(EngineConfig(state_dir=state_dir()))
        try:
            with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as client:
                r = client.post("/api/v1/boards", json={"target": BOARD_IP}, headers=H)
                bid = r.json()["board_id"]
                r = client.post(f"{bid_path(bid)}/xvc/open", json={"byo": True}, headers=H)
                assert r.status_code == 409 and "lease holder only" in r.json()["error"]["message"]
                assert fake.stats.connections == 0                 # twin: not even probed
                r = client.post(f"{bid_path(bid)}/lease", json={"ttl_s": 600}, headers=H)
                assert wait_job(client, r.json()["job"])["state"] == "done"
                result = open_xvc(client, bid, byo=True)
                assert result["reach"] == "hub-tunnel"
                assert X.UNAUTHENTICATED_WARNING in result["warnings"]
                assert eng.xvc.leases is client.app.state.daemon.leases   # one lease view
                # Releasing the lease kicks XVC at once and closes it.
                client.delete(f"{bid_path(bid)}/lease", headers=H)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and eng.xvc.open_boards():
                    time.sleep(0.05)
                assert eng.xvc.open_boards() == []
                st = client.get(f"{bid_path(bid)}/xvc", headers=H).json()
                assert st["state"] == "down" and "lease was released" in st["detail"]
        finally:
            eng.close_all()
