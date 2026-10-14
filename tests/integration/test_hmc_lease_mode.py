"""HM-C: GET/POST /boards/{bid}/lease/mode through the real ``create_app`` (lease_mode_api.py).

The board's lease server is a fake adapter set on the open session (``session.lease``);
nothing reaches a board, a hub or ssh. Every check has a negative twin.
"""

from __future__ import annotations

import os
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import RefusedError
from harness_manager.core.events import Event
from harness_manager.core.services import EngineConfig
from harness_manager.daemon import app as app_mod
from harness_manager.daemon import hub_api
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager.services import lease_backend as lb
from tests.fakes.l1_rig import BOARD_IP, lab
from tests.fakes.lrc_lease import LeaseWorld, factory
from tests.fakes.t13_daemon import TOKEN, bid_path, headers
from tests.fakes.virtual_board import VirtualMps3

H = headers()


class FakeBoardLease:
    """A desk board as its own lease server (what harness_manager_mps3.board_lease will be)."""

    kind = lb.KIND_BOARD
    host = "192.168.10.101"
    target = "mps3-01"
    name = "mps3-01"
    words = lb.WORDS[lb.KIND_BOARD]

    def __init__(self, *, single_user=False, can_change=True, reason="") -> None:
        self._mode = lb.LeaseMode(single_user=single_user, can_change=can_change,
                                  change_reason=reason, by="alice@pc", since_s=12)
        self.calls: list[tuple] = []
        self.client = NS(set_mode=self.set_mode, principal=lambda: "me@pc")

    def mode(self) -> lb.LeaseMode:
        return self._mode

    def set_mode(self, on, take=False, confirm_board=None):
        self.calls.append((on, take, confirm_board))
        self._mode = lb.LeaseMode(single_user=on, can_change=True, by="me@pc")
        return {"lease": {"holder": "me@pc", "mine": True} if take else None}


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    if "lease_mode_api" not in app_mod.EXTENSIONS:        # the lead wires the real line
        monkeypatch.setattr(app_mod, "EXTENSIONS", (*app_mod.EXTENSIONS, "lease_mode_api"))
    monkeypatch.setattr(hub_api, "LeaseService", factory(LeaseWorld()))
    state = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state):
        eng = Engine(EngineConfig(state_dir=state))
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            yield c
        eng.close_all()


@pytest.fixture
def bid(client) -> str:
    r = client.post("/api/v1/boards", json={"target": BOARD_IP, "note": "hmc"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def attach(client, bid, adapter):
    session = client.app.state.daemon.engine.session(bid)
    session.lease = adapter
    session.hub = None                  # the rig's real hub must not stand in for it
    return session


def url(bid):
    return f"{bid_path(bid)}/lease/mode"


def wait_job(client, job_id, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        j = client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
        if j["state"] in ("done", "failed"):
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_get_shape_when_shared(client, bid):
    attach(client, bid, FakeBoardLease())
    body = client.get(url(bid), headers=H).json()
    assert body["available"] is True and body["single_user"] is False
    assert body["label"] == "" and body["courtesy"] is False
    assert body["by"] == "alice@pc" and body["since_s"] == 12 and body["ttl_min"] == 15
    assert {"can_change", "change_reason", "persisted", "local_hold_s", "reason"} <= set(body)


def test_get_label_when_on(client, bid):
    attach(client, bid, FakeBoardLease(single_user=True))
    body = client.get(url(bid), headers=H).json()
    assert body["single_user"] and body["courtesy"] and body["label"] == lb.COURTESY_LABEL


def test_get_unavailable_behind_hub_and_without_feature(client, bid):
    attach(client, bid, NS(kind="hub", host="hub", target="t", client=NS()))
    body = client.get(url(bid), headers=H).json()
    assert body["available"] is False and "hub's lease already does this" in body["reason"]
    attach(client, bid, None)
    body = client.get(url(bid), headers=H).json()
    assert body["available"] is False and "board_lease" in body["reason"]


def test_post_turns_on_takes_lease_and_emits_event(client, bid):
    board = FakeBoardLease()
    attach(client, bid, board)
    seen: list[Event] = []
    client.app.state.daemon.bus.subscribe("lease.mode", seen.append)
    r = client.post(url(bid), json={"single_user": True, "confirm": True,
                                    "confirm_board": "mps3-01"}, headers=H)
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done", job
    assert job["result"]["mode"]["single_user"] is True
    assert job["result"]["lease"]["mine"] is True
    assert board.calls == [(True, True, "mps3-01")]
    assert len(seen) == 1 and seen[0].data["single_user"] is True
    assert seen[0].data["via"] == "hm" and seen[0].data["by"] == "me@pc"


def test_post_turns_off_without_taking(client, bid):
    board = FakeBoardLease(single_user=True)
    attach(client, bid, board)
    r = client.post(url(bid), json={"single_user": False, "confirm": True}, headers=H)
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done" and board.calls == [(False, False, None)]
    assert job["result"]["lease"] is None


@pytest.mark.parametrize("body", [{"single_user": True}, {"single_user": True, "confirm": False},
                                  {"confirm": True}, {"single_user": "yes", "confirm": True}])
def test_post_usage_errors_are_400_and_change_nothing(client, bid, body):
    board = FakeBoardLease()
    attach(client, bid, board)
    r = client.post(url(bid), json=body, headers=H)
    assert r.status_code == 400 and r.json()["error"]["name"] == "USAGE"
    assert board.calls == []


def test_post_refused_when_not_owner_or_holder(client, bid):
    board = FakeBoardLease(can_change=False, reason="only the claim holder can switch this")
    attach(client, bid, board)
    r = client.post(url(bid), json={"single_user": True, "confirm": True}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    assert "claim holder" in r.json()["error"]["message"] and board.calls == []


def test_post_already_in_that_mode(client, bid):
    attach(client, bid, FakeBoardLease(single_user=True))
    r = client.post(url(bid), json={"single_user": True, "confirm": True}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["name"] == "ALREADY"


def test_post_unavailable_behind_hub_and_without_feature(client, bid):
    for adapter, words in ((NS(kind="hub", host="h", target="t", client=NS()), "hub's lease"),
                           (None, "board_lease")):
        attach(client, bid, adapter)
        r = client.post(url(bid), json={"single_user": True, "confirm": True}, headers=H)
        assert r.status_code == 422 and r.json()["error"]["name"] == "UNAVAILABLE"
        assert words in r.json()["error"]["message"]


def test_adapter_refusal_inside_the_job_fails_the_job(client, bid):
    board = FakeBoardLease()

    def refuse(*a, **k):
        raise RefusedError("someone else holds the lease")

    board.client.set_mode = refuse
    attach(client, bid, board)
    r = client.post(url(bid), json={"single_user": True, "confirm": True}, headers=H)
    assert wait_job(client, r.json()["job"])["state"] == "failed"
