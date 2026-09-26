"""L1: the daemon's hub routes (docs/API.md "Week-plan additions: the hub"). Each check has a twin.

The app runs a real Engine; the lab rig fakes the network (ssh, the hub). Only
127.0.0.1 is reached.
"""

from __future__ import annotations

import os
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import ExitCode
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from tests.fakes.l1_rig import BOARD_IP, HUB, lab
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import VirtualMps3

H = headers()


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


@pytest.fixture
def rig(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as r:
        yield r


@pytest.fixture
def client(rig) -> Iterator[TestClient]:
    eng = Engine(EngineConfig(state_dir=state_dir()))
    with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
        yield c
    eng.close_all()


def open_lab(client: TestClient) -> str:
    r = client.post("/api/v1/boards", json={"target": BOARD_IP, "note": "l1"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["info"]["identity"]["shell_id"] == "0x3f1a560f"
    return r.json()["board_id"]


def wait_job(client: TestClient, job: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/jobs/{job}", headers=H).json()
        if state["state"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError(f"job {job} still running")


def test_the_tunnel_route_shows_the_forwards(client, rig):
    bid = open_lab(client)
    r = client.get(f"{bid_path(bid)}/tunnel", headers=H)
    assert r.status_code == 200
    t = r.json()["tunnel"]
    assert t["state"] == "up" and t["via"] == f"ssh:{HUB}" and t["host"] == HUB
    assert set(t["ports"]) >= {"6900", "6910", "6921", "6930"}
    assert all(isinstance(v, int) and v != 2542 for v in t["ports"].values())


def test_lease_acquire_is_a_job_then_the_lease_is_mine_until_released(client, rig):
    bid = open_lab(client)
    seen: list[dict] = []
    client.app.state.daemon.bus.subscribe("lease.*", lambda ev: seen.append(ev.data))
    # LR-B adds queue/request/incoming/taken to the view; L1's keys are unchanged.
    got = client.get(f"{bid_path(bid)}/lease", headers=H).json()
    assert {k: got[k] for k in ("ok", "lease", "hub")} == {"ok": True, "lease": None, "hub": HUB}
    r = client.post(f"{bid_path(bid)}/lease", json={"ttl_s": 600}, headers=H)
    assert r.status_code == 202
    done = wait_job(client, r.json()["job"])
    assert done["state"] == "done" and done["kind"] == "lease"
    assert done["result"]["lease"]["mine"] and "token" not in str(done["result"])
    view = client.get(f"{bid_path(bid)}/lease", headers=H).json()
    assert view["lease"]["mine"] and view["lease"]["target"] == "mps3_01_pl"
    assert seen[-1]["state"] == "held"
    # The service heartbeats it while the board is open.
    leases = client.app.state.daemon.leases
    assert bid in leases.tracked()
    leases.beat_due(force=True)
    assert rig.hub.heartbeats == 1
    r = client.delete(f"{bid_path(bid)}/lease", headers=H)
    assert r.status_code == 200 and r.json()["released"]["holder"]
    assert rig.hub.current is None and seen[-1]["state"] == "released"
    assert client.get(f"{bid_path(bid)}/lease", headers=H).json()["lease"] is None


def test_negative_twin_a_queued_lease_holds_the_board_until_cancelled(client, rig):
    bid = open_lab(client)
    rig.hub.queue_first = 10**6
    leases = client.app.state.daemon.leases
    r = client.post(f"{bid_path(bid)}/lease", json={"ttl_s": 600}, headers=H)
    job = r.json()["job"]
    deadline = time.monotonic() + 5
    while not rig.hub.queue and time.monotonic() < deadline:
        time.sleep(0.02)
    busy = client.get(bid_path(bid), headers=H)                   # the board is not ours yet
    assert busy.status_code == 409 and busy.json()["error"]["data"]["kind"] == "lease"
    assert client.get(f"{bid_path(bid)}/lease", headers=H).status_code == 200   # still readable
    assert client.delete(f"{bid_path(bid)}/lease", headers=H).json()["cancelled"] is True
    state = wait_job(client, job)
    assert state["state"] == "failed" and "cancelled" in state["error"]["message"]
    assert rig.hub.queue == []                                    # the queue entry went with it
    assert leases.store.get(HUB, "mps3_01_pl") is None


def test_the_lease_routes_refuse_bad_input(client, rig):
    bid = open_lab(client)
    for bad in ({"ttl_s": "long"}, {"ttl_s": 5}, {"ttl_s": True}):
        r = client.post(f"{bid_path(bid)}/lease", json=bad, headers=H)
        assert r.status_code == 400 and r.json()["error"]["code"] == ExitCode.USAGE
    r = client.delete(f"{bid_path(bid)}/lease", headers=H)        # nothing of ours to release
    assert r.status_code == 404 and r.json()["error"]["code"] == ExitCode.ABSENT


def test_a_lost_lease_is_an_event_and_is_no_longer_heartbeated(client, rig):
    bid = open_lab(client)
    seen: list[dict] = []
    client.app.state.daemon.bus.subscribe("lease.*", lambda ev: seen.append(ev.data))
    wait_job(client, client.post(f"{bid_path(bid)}/lease", json={}, headers=H).json()["job"])
    rig.hub.steal("b0-linux")
    leases = client.app.state.daemon.leases
    leases.beat_due(force=True)
    assert seen[-1]["state"] == "lost" and bid not in leases.tracked()
    view = client.get(f"{bid_path(bid)}/lease", headers=H).json()["lease"]
    assert view["holder"] == "b0-linux" and not view["mine"]


def test_closing_the_board_stops_the_heartbeat_and_the_tunnel(client, rig):
    bid = open_lab(client)
    wait_job(client, client.post(f"{bid_path(bid)}/lease", json={}, headers=H).json()["job"])
    leases = client.app.state.daemon.leases
    assert bid in leases.tracked()
    assert client.delete(bid_path(bid), headers=H).status_code == 200
    assert bid not in leases.tracked() and not rig.ssh.live()
    assert leases.store.get(HUB, "mps3_01_pl") is not None        # the lease itself is kept


def test_tunnel_state_events_follow_a_drop(client, rig, monkeypatch):
    bid = open_lab(client)
    seen: list[str] = []
    client.app.state.daemon.bus.subscribe("tunnel.*", lambda ev: seen.append(ev.data["state"]))
    tunnel = client.app.state.daemon.engine.session(bid).reach.tunnel
    monkeypatch.setattr(tunnel, "_backoff", (0.05,))
    rig.ssh.procs[0].drop()
    deadline = time.monotonic() + 10
    while (tunnel.restarts < 1 or tunnel.state != "up") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "down" in seen and seen[-1] == "up"


def test_negative_twin_a_direct_board_has_no_tunnel_and_no_hub(tmp_path):
    with VirtualMps3(tmp_path) as vb:
        eng = engine_for(vb)
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            r = c.post("/api/v1/boards", json={"target": vb.shell_endpoint}, headers=H)
            bid = r.json()["board_id"]
            assert c.get(f"{bid_path(bid)}/tunnel", headers=H).json() == {"ok": True, "tunnel": None}
            got = c.get(f"{bid_path(bid)}/lease", headers=H).json()
            assert {k: got[k] for k in ("ok", "lease", "hub")} == {"ok": True, "lease": None,
                                                                   "hub": None}
            r = c.post(f"{bid_path(bid)}/lease", json={}, headers=H)
            assert r.status_code == 422 and r.json()["error"]["code"] == ExitCode.UNAVAILABLE
        eng.close_all()


def test_a_probed_candidate_keeps_its_route_through_json(client, rig):
    """The UI probes, then opens the candidate it got back: via and the hub link must survive."""
    r = client.post("/api/v1/probe", json={"hosts": [BOARD_IP], "scan_usb": False,
                                           "scan_network": False}, headers=H)
    (cand,) = r.json()["candidates"]
    eth = next(lk for lk in cand["links"] if lk["kind"] == "ethernet")
    assert eth["via"] == "ssh" and f"ssh:{HUB}" in eth["detail"]
    # MCC-FIX: the MCC is a HUB link (reached on the hub), never a hub:// share link
    assert any(lk["address"].startswith("hub-mcc://") and lk["via"] == "hub"
               and lk["kind"] == "hub" for lk in cand["links"])
    assert not any(lk["address"].startswith("hub://") for lk in cand["links"])
    opened = client.post("/api/v1/boards", json={"candidate": cand, "note": "ui"}, headers=H)
    assert opened.status_code == 200 and opened.json()["info"]["identity"]["shell_id"] == "0x3f1a560f"
    bid = opened.json()["board_id"]
    assert client.get(f"{bid_path(bid)}/tunnel", headers=H).json()["tunnel"]["state"] == "up"
