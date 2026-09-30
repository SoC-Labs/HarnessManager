"""UI2-API-BUILD G6: the OS-slot rollback and the card commit/clear from the app, on the real
daemon (``daemon/card_api.py``) over pyverify's FakeShell (a Linux ``SlotBoard``) through the
real engine and MPS3 pack; the T14 mock and the demo take the same changes. Each route has a
negative twin that sends nothing to the board.

No real slot write runs anywhere here: a rollback reads a slot back and flips the default,
and the card commit re-pushes an overlay into the fake's store (docs/API.md "OS slots and the
card: roll back, commit, clear").
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterator

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from tests.fakes.lxslots_board import LINUX_SID, slot_board
from tests.fakes.t2_overlays import SYNTH_RM_ID, make_overlay, use_overlay_dirs
from tests.fakes.t13_daemon import TOKEN, bid_path, headers, state_dir

H = headers()
A_RECORDED = {"state": "valid", "hdr_crc": 0x3E5E9C2C, "len": 24354312, "sid": LINUX_SID}
B_RECORDED = {"state": "valid", "hdr_crc": 0x1D0C55A1, "len": 24100864, "sid": LINUX_SID}
BARE = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed")


@pytest.fixture
def api(request, monkeypatch, tmp_path) -> Iterator[tuple]:
    kw = dict(request.param)
    if kw.pop("overlay", False):
        root = tmp_path / "ovl"
        make_overlay(root, "synth", rm_id=SYNTH_RM_ID, static_id=LINUX_SID)
        use_overlay_dirs(monkeypatch, root)
    fake = slot_board(**kw)
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    eng = Engine(EngineConfig(state_dir=state_dir(), pack_overrides={"mps3": {
        "console_ports": fake.console_ports, "push_port": fake.raw_tcp_port,
        "tftp_port": fake.tftp_port}}))
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as client:
            r = client.post("/api/v1/boards", json={"target": f"{fake.host}:{fake.control_port}",
                                                    "note": "ui2-g6"}, headers=H)
            assert r.status_code == 200, r.text
            bid = r.json()["board_id"]
            slots = eng.session(bid).os_slots
            if slots is not None:                     # a fast fake: poll it fast
                slots.poll_s = slots.poll_max_s = slots.reboot_poll_s = 0.02
            yield fake, client, bid
    finally:
        eng.close_all()
        fake.stop()


def wait(client: TestClient, job: str, timeout: float = 30.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = client.get(f"/api/v1/jobs/{job}", headers=H).json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job} still running")


BOOTED_B = {"slots": {"running": "B", "default": "B", "a": A_RECORDED, "b": B_RECORDED}}


@pytest.mark.parametrize("api", [BOOTED_B], indirect=True)
def test_rollback_reads_the_other_slot_back_makes_it_the_default_and_boots_it(api):
    fake, client, bid = api
    r = client.post(bid_path(bid) + "/slots/rollback", json={"confirm": True, "wait_s": 20},
                    headers=H)
    assert r.status_code == 202, r.text
    job = wait(client, r.json()["job"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["act"] == "rollback" and res["slot"] == "A" and res["rebooted"] is True
    assert res["slots"]["running"] == "A" and res["slots"]["default"] == "A"
    assert res["note"] == "slot A runs again (rebooted)" and res["evidence"]
    assert fake.boots == ["A"] and fake.slots.deflt == "A"
    assert "rollback" in job["phases"]


@pytest.mark.parametrize("api", [BOOTED_B], indirect=True)
def test_twin_rollback_without_confirm_or_with_a_bad_wait_sends_nothing(api):
    fake, client, bid = api
    for body in ({}, {"confirm": "yes"}, {"confirm": True, "wait_s": 0},
                 {"confirm": True, "reboot": "no"}):
        r = client.post(bid_path(bid) + "/slots/rollback", json=body, headers=H)
        assert r.status_code == 400 and r.json()["error"]["name"] == "USAGE", body
    assert "confirm: true" in client.post(bid_path(bid) + "/slots/rollback", json={},
                                          headers=H).json()["error"]["message"]
    assert fake.boots == [] and fake.slots.deflt == "B" and fake.slots.running == "B"


@pytest.mark.parametrize("api", [BOOTED_B], indirect=True)
def test_rollback_without_the_reboot_only_flips_the_default(api):
    fake, client, bid = api
    r = client.post(bid_path(bid) + "/slots/rollback", json={"confirm": True, "reboot": False},
                    headers=H)
    job = wait(client, r.json()["job"])
    assert job["state"] == "done", job
    assert job["result"]["rebooted"] is False and job["result"]["slot"] == "A"
    assert job["result"]["note"] == "slot A boots at the next reboot"
    assert fake.boots == [] and fake.slots.deflt == "A" and fake.slots.running == "B"


@pytest.mark.parametrize("api", [{"slots": {"a": A_RECORDED}}], indirect=True)
def test_twin_rollback_with_no_valid_slot_to_go_back_to_fails_the_job_refused(api):
    fake, client, bid = api
    r = client.post(bid_path(bid) + "/slots/rollback", json={"confirm": True}, headers=H)
    assert r.status_code == 202
    job = wait(client, r.json()["job"])
    assert job["state"] == "failed" and job["error"]["name"] == "REFUSED"
    assert "slot B holds no valid image" in job["error"]["message"]
    assert fake.boots == [] and fake.slots.deflt == "A"


@pytest.mark.parametrize("api", [{"profile": "bare-metal", "slots": None, "features": BARE}],
                         indirect=True)
def test_twin_bare_metal_has_no_slots_or_card_store_422_before_any_job(api):
    fake, client, bid = api
    for route in ("/slots/rollback", "/card/commit", "/card/clear"):
        r = client.post(bid_path(bid) + route, json={"confirm": True}, headers=H)
        assert r.status_code == 422, (route, r.text)
        err = r.json()["error"]
        assert err["name"] == "UNAVAILABLE" and err["reason"], route
    assert client.get("/api/v1/jobs", headers=H).json()["jobs"] == []
    assert fake.commits == [] and fake.slots is None


@pytest.mark.parametrize("api", [{"usd_card": "da", "boot_rm_id": SYNTH_RM_ID,
                                  "slots": {"a": A_RECORDED}, "overlay": True}], indirect=True)
def test_card_commit_then_clear(api):
    fake, client, bid = api
    r = client.post(bid_path(bid) + "/card/commit", json={"confirm": True}, headers=H)
    assert r.status_code == 202, r.text
    job = wait(client, r.json()["job"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["committed"]["rm_name"] == "synth" and res["card"]["default"]["rm_name"] == "synth"
    assert "default synth" in res["line"] and "power-on default" in res["note"]
    assert fake.commits and fake.commits[0][0] == "synth"
    card = client.get(bid_path(bid) + "/card", headers=H).json()
    assert card["card"]["default"]["rm_name"] == "synth"
    r = client.post(bid_path(bid) + "/card/clear", json={"confirm": True}, headers=H)
    job = wait(client, r.json()["job"])
    assert job["state"] == "done", job
    assert job["result"]["card"]["default"] is None
    assert "greybox loads" in job["result"]["note"]


@pytest.mark.parametrize("api", [{"slots": {"a": A_RECORDED}}], indirect=True)
def test_twin_card_changes_with_no_card_are_refused_before_any_job(api):
    fake, client, bid = api
    for route in ("/card/commit", "/card/clear"):
        assert client.post(bid_path(bid) + route, json={}, headers=H).status_code == 400
        r = client.post(bid_path(bid) + route, json={"confirm": True}, headers=H)
        assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED", r.text
        assert "no card" in r.json()["error"]["message"]
    assert client.get("/api/v1/jobs", headers=H).json()["jobs"] == []
    assert fake.commits == [] and fake.usd_card is None


def test_twin_a_board_that_is_not_open_is_404():
    from harness_manager.demo import DemoEngine

    eng = DemoEngine(speed=0)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            for route in ("/slots/rollback", "/card/commit", "/card/clear"):
                r = c.post(bid_path("mps3@192.168.10.101:6900") + route, json={"confirm": True},
                           headers=H)
                assert r.status_code == 404 and r.json()["error"]["name"] == "ABSENT", route
    finally:
        eng.close_all()


# --- the lease: another holder is 409 HELD before any job ----------------------------------------


def test_the_lease_is_checked_before_the_job(monkeypatch):
    from harness_manager.core.errors import HeldError
    from harness_manager.daemon import card_api
    from harness_manager.demo import DemoEngine
    from harness_manager.demo_showcase import BOARD_LINUX

    def not_yours(engine, leases=None):
        def check(session, what):
            raise HeldError(f"cannot {what}: it is for the lease holder only",
                            holder="alice@lab")
        return check

    monkeypatch.setattr(card_api, "lease_check_for", not_yours)
    eng = DemoEngine(speed=0, showcase=True)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            cand = next(x for x in c.post("/api/v1/probe", json={}, headers=H).json()["candidates"]
                        if x["board_id"] == BOARD_LINUX)
            assert c.post("/api/v1/boards", json={"candidate": cand}, headers=H).status_code == 200
            for route in ("/slots/rollback", "/card/commit", "/card/clear"):
                r = c.post(bid_path(BOARD_LINUX) + route, json={"confirm": True}, headers=H)
                assert r.status_code == 409 and r.json()["error"]["holder"] == "alice@lab", route
            assert c.get("/api/v1/jobs", headers=H).json()["jobs"] == []
    finally:
        eng.close_all()


# --- the demo and the mock take the same changes -----------------------------------------------------


def test_the_demo_linux_board_takes_the_changes_in_memory():
    from harness_manager.demo import DemoEngine
    from harness_manager.demo_showcase import BOARD_LINUX

    eng = DemoEngine(speed=0, showcase=True)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            cand = next(x for x in c.post("/api/v1/probe", json={}, headers=H).json()["candidates"]
                        if x["board_id"] == BOARD_LINUX)
            c.post("/api/v1/boards", json={"candidate": cand}, headers=H)
            job = wait(c, c.post(bid_path(BOARD_LINUX) + "/slots/rollback",
                                 json={"confirm": True}, headers=H).json()["job"])
            assert job["state"] == "done" and job["result"]["slots"]["running"] == "B"
            assert c.get(bid_path(BOARD_LINUX) + "/slots", headers=H).json()["slots"]["default"] \
                == "B"
            job = wait(c, c.post(bid_path(BOARD_LINUX) + "/card/clear", json={"confirm": True},
                                 headers=H).json()["job"])
            assert job["state"] == "done" and job["result"]["card"]["default"] is None
            job = wait(c, c.post(bid_path(BOARD_LINUX) + "/card/commit", json={"confirm": True},
                                 headers=H).json()["job"])
            assert job["state"] == "done" and job["result"]["committed"]["rm_name"]
            assert c.get(bid_path(BOARD_LINUX) + "/card", headers=H).json()["card"]["default"]
    finally:
        eng.close_all()


def test_the_mock_serves_the_changes_over_its_card_sim():
    from harness_manager.demo import BOARD_USB, DemoEngine
    from tests.fakes.t14_mock_api import create_app as mock_app

    eng = DemoEngine(speed=0.02)
    try:
        app = mock_app(eng, token="t14", serve_ui=False)
        h = {"Authorization": "Bearer t14"}
        with TestClient(app) as c:
            cand = next(x for x in c.post("/api/v1/probe", json={}, headers=h).json()["candidates"]
                        if x["board_id"] == BOARD_USB)
            c.post("/api/v1/boards", json={"candidate": cand}, headers=h)
            p = bid_path(BOARD_USB)
            # no card yet: no OS slots (422) and nothing to commit (409); no confirm: 400
            assert c.post(p + "/slots/rollback", json={"confirm": True}, headers=h).status_code \
                == 422
            assert c.post(p + "/card/commit", json={"confirm": True}, headers=h).status_code == 409
            assert c.post(p + "/card/clear", json={}, headers=h).status_code == 400
            app.state.card.insert(BOARD_USB)
            r = c.post(p + "/card/commit", json={"confirm": True}, headers=h)
            assert r.status_code == 202
            job = wait_mock(c, h, r.json()["job"])
            assert job["state"] == "done" and job["result"]["committed"]["rm_name"] == "nanosoc"
            r = c.post(p + "/slots/rollback", json={"confirm": True}, headers=h)
            job = wait_mock(c, h, r.json()["job"])
            assert job["state"] == "failed" and job["error"]["name"] == "REFUSED"   # B is empty
    finally:
        eng.close_all()


def wait_mock(c: TestClient, h: dict, job: str) -> dict:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        body = c.get(f"/api/v1/jobs/{job}", headers=h).json()
        if body["state"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("mock job still running")
