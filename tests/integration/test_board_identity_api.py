"""BOARD-ID over the real daemon (``identity_api.py``) and the real MPS3 pack, against HM's model
of the identity verbs. Each route has its negative twin: no phrase, a netbooted board, and bad
values are refused BEFORE the job with nothing sent; the fix itself reboots warm and verifies.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterator

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from tests.fakes.claimed_lock import board_key_fp, pin_claim
from tests.fakes.idn_board import BOARD1_MAC, identity_board
from tests.fakes.lxslots_board import TRUSTED, BoardSsh
from tests.fakes.t13_daemon import TOKEN, bid_path, headers, state_dir

AS_DEFAULT = {"label": "MPS3", "ip": "192.168.10.101/24", "mac": BOARD1_MAC,
              "source": {"label": "default", "ip": "default", "mac": "default"}}
WANT = {"label": "MPS3-02", "ip": "192.168.11.101/24", "mac": "02:00:00:00:02:fe"}


@pytest.fixture
def api(request, monkeypatch) -> Iterator[tuple]:
    fake = identity_board(running=AS_DEFAULT, ssh_claimed=True, slots={"trusted_peer": TRUSTED},
                          ssh_host_key_sha256=board_key_fp(), **getattr(request, "param", {}))
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    ssh = BoardSsh(fake)
    monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
    monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
    eng = Engine(EngineConfig(state_dir=state_dir(), pack_overrides={"mps3": {
        "console_ports": fake.console_ports, "push_port": fake.raw_tcp_port,
        "tftp_port": fake.tftp_port}}))
    client = TestClient(create_app(eng, token=TOKEN, static_dir=None))
    with client:
        r = client.post("/api/v1/boards", json={"target": f"{fake.host}:{fake.control_port}",
                                                "note": "board-id"}, headers=headers())
        assert r.status_code == 200, r.text
        bid = r.json()["board_id"]
        pin_claim(eng.session(bid))
        eng.session(bid).os_slots.reboot_poll_s = 0.02
        yield fake, client, bid
    eng.close_all()
    ssh.close()
    fake.stop()


def wait_job(client, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/jobs/{job_id}", headers=headers()).json()
        if body.get("state") not in ("running", "queued"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


def test_get_reads_the_board_and_info_carries_it(api):
    fake, client, bid = api
    r = client.get(bid_path(bid) + "/identity", headers=headers())
    assert r.status_code == 200, r.text
    st = r.json()["identity"]
    assert st["status"] == "unset" and st["reported"]["label"] == "MPS3"
    assert st["fix"]["refusal"] is None
    info = client.get(bid_path(bid), headers=headers()).json()
    assert info["net_identity"]["status"] == "unset"


def test_the_fix_job_sets_reboots_warm_and_verifies(api):
    fake, client, bid = api
    client.get(bid_path(bid) + "/identity", headers=headers())
    r = client.post(bid_path(bid) + "/identity", json={"confirm": "MPS3-02", **WANT,
                                                       "wait_s": 20}, headers=headers())
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["verified"] is True and res["action"] == "set"
    assert [p for p, _ in fake.identity_sets] == [TRUSTED] and len(fake.reboots) == 1


def test_twin_no_phrase_is_refused_before_the_job(api):
    fake, client, bid = api
    r = client.post(bid_path(bid) + "/identity", json={"from_hub": True}, headers=headers())
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    r = client.post(bid_path(bid) + "/identity", json={"confirm": "MPS3-02", "mac": "ff:ff:ff:"
                                                       "ff:ff:ff"}, headers=headers())
    assert r.status_code == 400 and "unicast" in r.json()["error"]["message"]
    assert fake.identity_sets == [] and fake.reboots == []


def test_twin_the_wrong_phrase_fails_the_job_and_nothing_is_sent(api):
    fake, client, bid = api
    r = client.post(bid_path(bid) + "/identity", json={"confirm": "MPS3-2", **WANT},
                    headers=headers())
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "failed" and job["error"]["name"] == "REFUSED"
    assert fake.identity_sets == [] and fake.reboots == []


@pytest.mark.parametrize("api", [{"persist": False}], indirect=True)
def test_twin_a_netbooted_board_is_refused_before_the_job(api):
    fake, client, bid = api
    client.get(bid_path(bid) + "/identity", headers=headers())      # the tile's read
    r = client.post(bid_path(bid) + "/identity", json={"confirm": "MPS3-02", **WANT},
                    headers=headers())
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["name"] == "REFUSED" and "stage0 bake" in err["message"]
    assert fake.identity_sets == [] and fake.reboots == []
