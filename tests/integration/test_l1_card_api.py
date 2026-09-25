"""L1-CARD: "Keep on the card" over the API (docs/API.md), on the real daemon and the T14 mock.

The real daemon wraps the real Engine and the MPS3 pack over a virtual Linux board with the
D13 store (pyverify's FakeShell, ``usd`` feature, a card put in the slot). The mock runs
over DemoEngine with its card knobs. Each behaviour has a negative twin.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import ExitCode
from harness_manager.daemon.app import create_app
from harness_manager.demo import BOARD_USB, DemoEngine
from harness_manager.services.deploy import NO_CARD, NO_STORE
from harness_manager_mps3 import shell as sh
from tests.fakes.t2_overlays import make_overlay, use_overlay_dirs
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.t14_mock_api import create_app as mock_app
from tests.fakes.virtual_board import FIELDED_ILA_V011, LINUX_HARNESSD, VirtualMps3

H = headers()
LINUX_USD = replace(LINUX_HARNESSD, name="linux-harnessd-usd",
                    features=(*LINUX_HARNESSD.features, "usd"))


@pytest.fixture(autouse=True)
def _no_failed_pushes():
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()
    yield
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()


def served(tmp_path: Path, monkeypatch, profile=LINUX_USD, *, card: str | None = "da"):
    """(virtual board, engine, TestClient, board path) with ``synth`` built for the board."""
    root = tmp_path / "ov"
    make_overlay(root, "synth", static_id=profile.static_id)
    use_overlay_dirs(monkeypatch, root)
    vb = VirtualMps3(tmp_path, profile).__enter__()
    if card is not None:
        vb.shell.usd_insert(card)
    eng = engine_for(vb)
    client = TestClient(create_app(eng, token=TOKEN, static_dir=None)).__enter__()
    r = client.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "l1"},
                    headers=H)
    assert r.status_code == 200, r.text
    return vb, eng, client, bid_path(r.json()["board_id"])


@pytest.fixture
def rig(tmp_path, monkeypatch, request) -> Iterator:
    profile, card = getattr(request, "param", (LINUX_USD, "da"))
    vb, eng, client, path = served(tmp_path, monkeypatch, profile, card=card)
    try:
        yield vb, client, path
    finally:
        client.__exit__(None, None, None)
        eng.close_all()
        vb.__exit__(None, None, None)


def wait_job(client: TestClient, job: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/jobs/{job}", headers=H).json()
        if state["state"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError(f"job {job} still running after {timeout} s")


def deploy(client, path, **body) -> dict:
    r = client.post(f"{path}/deploy", json={"overlay": "synth", **body}, headers=H)
    assert r.status_code == 202, r.text
    return wait_job(client, r.json()["job"])


# -- the real daemon ------------------------------------------------------------------------


def test_get_card_reads_a_linux_board_with_a_card(rig):
    vb, client, path = rig
    r = client.get(f"{path}/card", headers=H)
    assert r.status_code == 200
    card = r.json()["card"]
    assert (card["store"], card["present"], card["state"], card["reason"]) == \
        (True, True, "empty", "")
    assert vb.shell.commits == []


@pytest.mark.parametrize("rig, reason, store", [
    ((LINUX_USD, None), NO_CARD, True),
    ((FIELDED_ILA_V011, None), NO_STORE, False),
], indirect=["rig"])
def test_negative_twin_get_card_says_why_it_cannot_keep(rig, reason, store):
    _, client, path = rig
    card = client.get(f"{path}/card", headers=H).json()["card"]
    assert (card["store"], card["present"], card["reason"]) == (store, False, reason)


def test_the_default_deploy_writes_no_card(rig):
    vb, client, path = rig
    state = deploy(client, path)
    assert state["state"] == "done" and state["result"]["verified"] is True
    assert state["result"]["card"] is None and vb.shell.commits == []
    assert "card" not in state["phases"]


def test_negative_twin_keep_on_card_true_keeps_it_and_the_result_says_where(rig):
    vb, client, path = rig
    state = deploy(client, path, keep_on_card=True)
    assert state["state"] == "done"
    assert state["result"]["card"] == {"kept": True, "slot": "A", "why": ""}
    assert vb.shell.commits == [("synth", "A")]
    assert "card" in state["phases"]
    assert client.get(f"{path}/card", headers=H).json()["card"]["state"] == "valid"


@pytest.mark.parametrize("rig, reason", [
    ((LINUX_USD, None), NO_CARD),
    ((FIELDED_ILA_V011, None), NO_STORE),
], indirect=["rig"])
def test_keep_on_card_is_refused_before_any_job(rig, reason):
    vb, client, path = rig
    r = client.post(f"{path}/deploy", json={"overlay": "synth", "keep_on_card": True},
                    headers=H)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == ExitCode.UNAVAILABLE and err["name"] == "UNAVAILABLE"
    assert err["message"] == f"keep_on_card is unavailable: {reason}"
    assert err["data"]["card"]["reason"] == reason and err["data"]["overlay"]["name"] == "synth"
    assert vb.shell.accepted_pushes == [] and vb.shell.current_rm_id == 0
    assert client.get("/api/v1/jobs", headers=H).json()["jobs"] == []


@pytest.mark.parametrize("rig", [(LINUX_USD, None)], indirect=True)
def test_negative_twin_the_same_board_deploys_without_keep(rig):
    vb, client, path = rig
    assert deploy(client, path)["result"]["card"] is None
    assert vb.shell.current_rm_id != 0


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_keep_on_card_must_be_a_boolean(rig, value):
    vb, client, path = rig
    r = client.post(f"{path}/deploy", json={"overlay": "synth", "keep_on_card": value},
                    headers=H)
    assert r.status_code == 400 and r.json()["error"]["code"] == ExitCode.USAGE
    assert vb.shell.accepted_pushes == []


def test_negative_twin_false_is_the_default(rig):
    vb, client, path = rig
    assert deploy(client, path, keep_on_card=False)["result"]["card"] is None
    assert vb.shell.commits == []


# -- the T14 mock over DemoEngine: the same contract ------------------------------------------


@pytest.fixture
def demo() -> Iterator[DemoEngine]:
    eng = DemoEngine(speed=0.0)
    yield eng
    eng.close_all()


@pytest.fixture(params=["mock", "daemon"])
def demo_client(request, demo, tmp_path) -> Iterator[TestClient]:
    app = (mock_app(demo, token=TOKEN) if request.param == "mock"
           else create_app(demo, token=TOKEN, static_dir=None, state_dir=tmp_path / "d"))
    with TestClient(app, raise_server_exceptions=False) as c:
        cands = c.post("/api/v1/probe", json={}, headers=H).json()["candidates"]
        cand = next(x for x in cands if x["board_id"] == BOARD_USB)
        assert c.post("/api/v1/boards", json={"candidate": cand, "note": "l1"},
                      headers=H).status_code == 200
        yield c


def demo_card(demo: DemoEngine, *, usd: bool, card: str | None) -> None:
    feats = tuple(f for f in demo._board(BOARD_USB).identity.features if f != "usd")
    demo.set_features(BOARD_USB, (*feats, "usd") if usd else feats)
    demo.set_card(BOARD_USB, card)


def test_demo_keep_on_card_is_kept_over_both_servers(demo, demo_client):
    demo_card(demo, usd=True, card="empty")
    path = bid_path(BOARD_USB)
    assert demo_client.get(f"{path}/card", headers=H).json()["card"]["reason"] == ""
    r = demo_client.post(f"{path}/deploy", json={"overlay": "led", "keep_on_card": True},
                         headers=H)
    assert r.status_code == 202, r.text
    state = wait_job(demo_client, r.json()["job"])
    assert state["result"]["card"]["kept"] is True
    assert demo.called("deploy.deploy")[-1][-1] is True


@pytest.mark.parametrize("usd, card, reason", [(False, "empty", NO_STORE), (True, None, NO_CARD)])
def test_negative_twin_demo_refuses_over_both_servers(demo, demo_client, usd, card, reason):
    demo_card(demo, usd=usd, card=card)
    path = bid_path(BOARD_USB)
    got = demo_client.get(f"{path}/card", headers=H).json()["card"]
    assert got["reason"] == reason
    r = demo_client.post(f"{path}/deploy", json={"overlay": "led", "keep_on_card": True},
                         headers=H)
    assert r.status_code == 422 and r.json()["error"]["data"]["card"]["reason"] == reason
    assert demo.called("deploy.deploy") == []
