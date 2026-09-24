"""LR-C: the lease-request routes through the real ``create_app`` (docs/LEASE_REQUESTS.md "API").

A real Engine opens the lab board through L1's faked lab (fake ssh, fake hub, a
VirtualMps3); the lease service is ``tests.fakes.lrc_lease.FakeLeaseService`` over
an in-memory hub. Nothing reaches a real hub: a "revoke" is a row in
``world.revoked``. Every check has a negative twin.
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import ExitCode, UnreachableError
from harness_manager.core.events import Event
from harness_manager.core.services import EngineConfig
from harness_manager.daemon import hub_api
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from tests.fakes.l1_rig import BOARD_IP, HUB, TARGET, lab
from tests.fakes.lrc_lease import ALICE, BOB, ME, LeaseWorld, factory, iso
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import VirtualMps3

H = headers()


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


@pytest.fixture
def world() -> LeaseWorld:
    return LeaseWorld()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as r:
        yield r


@pytest.fixture
def client(rig, world, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setattr(hub_api, "LeaseService", factory(world))
    eng = Engine(EngineConfig(state_dir=state_dir()))
    with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
        yield c
    eng.close_all()


@pytest.fixture
def bid(client) -> str:
    r = client.post("/api/v1/boards", json={"target": BOARD_IP, "note": "lrc"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def lease(bid: str, suffix: str = "") -> str:
    return f"{bid_path(bid)}/lease{suffix}"


def wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def job_state(client: TestClient, job: str) -> dict:
    return client.get(f"/api/v1/jobs/{job}", headers=H).json()


def wait_job(client: TestClient, job: str, timeout: float = 10.0) -> dict:
    state: dict = {}
    wait_for(lambda: (state.update(job_state(client, job)) or state["state"] != "running"), timeout)
    return state


def start_request(client: TestClient, bid: str, world: LeaseWorld, **body) -> str:
    r = client.post(lease(bid, "/request"), json=body, headers=H)
    assert r.status_code == 202, r.text
    wait_for(lambda: world.my_request() is not None)
    return r.json()["job"]


@pytest.fixture
def blocker(client):
    """A stand-in board job of another kind (a deploy), running until released."""
    gate = threading.Event()
    jobs: list = []

    def start(bid: str, kind: str = "deploy"):
        job = client.app.state.daemon.jobs.submit(kind, bid, lambda _p: gate.wait(10))
        jobs.append(job)
        return job

    yield start
    gate.set()
    for job in jobs:
        job.finished.wait(5)


# --- POST /lease/request ----------------------------------------------------------------------


def test_request_is_a_job_that_holds_the_board_until_the_holder_releases(client, bid, world):
    job = start_request(client, bid, world, message="need it for B1", ttl_s=900)
    assert ("request", bid, "need it for B1", 900) in world.calls
    busy = client.get(bid_path(bid), headers=H)                    # the board is not ours yet
    assert busy.status_code == 409 and busy.json()["error"]["code"] == ExitCode.HELD
    assert busy.json()["error"]["data"]["kind"] == "lease_request"
    view = client.get(lease(bid), headers=H)                        # the lease stays readable
    assert view.status_code == 200
    v = view.json()
    assert v["lease"]["holder"] == ALICE and not v["lease"]["mine"]
    assert v["queue"] == [{"position": 1, "holder": ME, "user": "david", "mine": True}]
    assert v["request"]["message"] == "need it for B1" and v["request"]["answer"] is None
    assert v["request"]["force_available"] is False and v["request"]["position"] == 1
    world.answer(v["request"]["id"], "release")
    done = wait_job(client, job)
    assert done["state"] == "done" and done["kind"] == "lease_request"
    assert done["result"]["lease"]["mine"] and done["result"]["lease"]["holder"] == ME
    assert done["phases"][:2] == ["queued", "notified"] and done["phases"][-1] == "held"
    assert client.get(bid_path(bid), headers=H).status_code == 200    # free again


def test_negative_twin_a_second_request_or_an_acquire_while_one_waits_is_held(client, bid, world):
    job = start_request(client, bid, world)
    for path, body in ((lease(bid, "/request"), {}), (lease(bid), {"ttl_s": 600})):
        r = client.post(path, json=body, headers=H)
        assert r.status_code == 409 and r.json()["error"]["data"] == {
            "job": job, "kind": "lease_request", "board_id": bid}
    assert sum(1 for c in world.calls if c[0] == "request") == 1


def test_a_keep_answer_does_not_end_the_request_job(client, bid, world):
    """D1: the job goes on after a keep, still queued and holding the board."""
    job = start_request(client, bid, world)
    rid = world.my_request()["id"]
    world.answer(rid, "keep", 15, "running B1, 15 min")
    wait_for(lambda: "answered" in job_state(client, job)["phases"])
    time.sleep(0.1)
    assert job_state(client, job)["state"] == "running"
    assert client.get(bid_path(bid), headers=H).json()["error"]["data"]["kind"] == "lease_request"
    req = client.get(lease(bid), headers=H).json()["request"]
    assert req["answer"]["answer"] == "keep" and req["answer"]["minutes"] == 15
    assert req["force_available"] is False and ME in world.queue
    world.release()                                                   # later, they let go
    done = wait_job(client, job)
    assert done["state"] == "done" and done["result"]["lease"]["mine"]
    assert done["phases"][-2:] == ["answered", "held"]


def test_negative_twin_a_free_board_is_granted_at_once(client, bid, world):
    world.holder = None
    done = wait_job(client, client.post(lease(bid, "/request"), json={}, headers=H).json()["job"])
    assert done["state"] == "done" and done["result"]["lease"]["mine"]
    assert world.my_request() is None and world.queue == []          # no note, no queue entry


def test_a_request_for_a_lease_this_principal_holds_is_409_already_with_no_job(client, bid, world):
    world.holder = ME                       # this session, or another of the same person's
    r = client.post(lease(bid, "/request"), json={}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["code"] == ExitCode.ALREADY
    assert "already hold" in r.json()["error"]["message"]
    assert not any(c[0] == "request" for c in world.calls) and world.queue == []
    assert client.get("/api/v1/jobs", headers=H).json()["jobs"] == []


def test_the_request_route_refuses_bad_input(client, bid, world):
    for bad in ({"ttl_s": 5}, {"ttl_s": "long"}, {"message": 7}, {"message": "x" * 501}, [1]):
        r = client.post(lease(bid, "/request"), json=bad, headers=H)
        assert r.status_code == 400 and r.json()["error"]["code"] == ExitCode.USAGE, bad
    assert not any(c[0] == "request" for c in world.calls)
    r = client.post(lease("mps3@10.9.9.9:6900", "/request"), json={}, headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == ExitCode.ABSENT


def test_negative_twin_a_pasted_newline_is_cleaned_not_refused(client, bid, world):
    start_request(client, bid, world, message="line one\nline two\t ")
    assert world.my_request()["message"] == "line one line two"


def test_a_board_without_a_hub_cannot_request(tmp_path, world, monkeypatch):
    monkeypatch.setattr(hub_api, "LeaseService", factory(world))
    with VirtualMps3(tmp_path) as vb:
        eng = engine_for(vb)
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            b = c.post("/api/v1/boards", json={"target": vb.shell_endpoint}, headers=H).json()
            bid = b["board_id"]
            for method, suffix, body in (("post", "/request", {}), ("post", "/force",
                                                                    {"confirm": True}),
                                         ("post", "/respond", {"id": "r1", "answer": "release"}),
                                         ("delete", "/queue", None)):
                kw = {"json": body} if body is not None else {}
                r = getattr(c, method)(lease(bid, suffix), headers=H, **kw)
                assert r.status_code == 422 and r.json()["error"]["code"] == ExitCode.UNAVAILABLE
            assert c.get(lease(bid), headers=H).json() == {"ok": True, "lease": None, "hub": None}
        eng.close_all()
    assert world.calls == [("view",)]


# --- POST /lease/respond ------------------------------------------------------------------------


def test_the_holder_answers_keep_then_release(client, bid, world):
    world.holder = ME
    rid = world.add_request(ALICE, "please, B1 at 3")
    v = client.get(lease(bid), headers=H).json()
    assert [i["id"] for i in v["incoming"]] == [rid] and v["incoming"][0]["by"] == ALICE
    r = client.post(lease(bid, "/respond"), headers=H,
                    json={"id": rid, "answer": "keep", "minutes": 15, "message": "5 more min"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert world.answers[rid]["minutes"] == 15 and world.holder == ME
    rid2 = world.add_request(BOB)
    r = client.post(lease(bid, "/respond"), json={"id": rid2, "answer": "release"}, headers=H)
    assert r.status_code == 200 and world.holder == ALICE             # the head was promoted


def test_negative_twin_respond_refuses_bad_answers_and_unknown_requests(client, bid, world):
    world.holder = ME
    rid = world.add_request(ALICE)
    for bad in ({"id": rid, "answer": "maybe"}, {"id": rid, "answer": "keep"},
                {"id": rid, "answer": "keep", "minutes": 7},
                {"id": rid, "answer": "keep", "minutes": True},
                {"id": rid, "answer": "release", "minutes": 5},
                {"id": "../etc", "answer": "release"}, {"answer": "release"},
                {"id": rid, "answer": "keep", "minutes": 5, "message": ["x"]}):
        r = client.post(lease(bid, "/respond"), json=bad, headers=H)
        assert r.status_code == 400 and r.json()["error"]["code"] == ExitCode.USAGE, bad
    assert not any(c[0] == "respond" for c in world.calls)
    r = client.post(lease(bid, "/respond"), json={"id": "r999", "answer": "release"}, headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == ExitCode.ABSENT
    world.holder = ALICE                                              # not ours to answer
    r = client.post(lease(bid, "/respond"), json={"id": rid, "answer": "release"}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["code"] == ExitCode.REFUSED
    assert world.answers == {}


def test_release_waits_for_a_running_deploy_but_keep_does_not(client, bid, world, blocker):
    world.holder = ME
    rid = world.add_request(ALICE)
    job = blocker(bid)
    r = client.post(lease(bid, "/respond"), json={"id": rid, "answer": "release"}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["data"]["kind"] == "deploy"
    assert world.holder == ME and rid not in world.answers
    r = client.post(lease(bid, "/respond"), json={"id": rid, "answer": "keep", "minutes": 5},
                    headers=H)
    assert r.status_code == 200 and world.answers[rid]["answer"] == "keep"
    assert job.state == "running"


# --- POST /lease/force --------------------------------------------------------------------------


def test_force_after_the_deadline_runs_beside_the_request_job(client, bid, world):
    req_job = start_request(client, bid, world)
    world.expire_deadline()
    wait_for(lambda: "force-available" in job_state(client, req_job)["phases"])
    created = world.my_request()["created_at"]
    assert client.get(lease(bid), headers=H).json()["request"]["force_available"] is True
    r = client.post(lease(bid, "/force"), json={"confirm": True}, headers=H)
    assert r.status_code == 202, r.text
    force_job = r.json()["job"]
    done = wait_job(client, force_job)
    assert done["kind"] == "lease_force" and done["state"] == "done"
    assert done["result"]["lease"]["mine"] and done["phases"] == ["revoke", "held"]
    assert wait_job(client, req_job)["result"]["lease"]["mine"]      # the request got it
    (rev,) = world.revoked
    assert rev["prior_holder"] == ALICE
    assert rev["reason"] == (f"force-released by {ME} via Harness Manager: no answer to a "
                             f"request made at {created}")
    kinds = [j["kind"] for j in client.get("/api/v1/jobs", headers=H).json()["jobs"]]
    assert kinds[-2:] == ["lease_request", "lease_force"]


def test_negative_twin_force_before_the_deadline_is_422_with_the_time_left(client, bid, world):
    start_request(client, bid, world)
    r = client.post(lease(bid, "/force"), json={"confirm": True}, headers=H)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == ExitCode.UNAVAILABLE and "left to answer" in err["message"]
    assert 100 <= err["data"]["time_left_s"] <= 120 and err["data"]["deadline_at"]
    assert world.revoked == [] and not any(c[0] == "force" for c in world.calls)


def test_force_without_confirm_true_is_400_usage(client, bid, world):
    start_request(client, bid, world)
    world.expire_deadline()
    for body in ({}, {"confirm": False}, {"confirm": "yes"}, {"confirm": 1}):
        r = client.post(lease(bid, "/force"), json=body, headers=H)            # D3: 400
        assert r.status_code == 400 and r.json()["error"]["code"] == ExitCode.USAGE, body
        assert "confirm" in r.json()["error"]["message"]
    assert world.revoked == [] and not any(c[0] == "force" for c in world.calls)


@pytest.mark.parametrize("setup, code, words", [
    ("no-request", ExitCode.REFUSED, "no request"),
    ("kept", ExitCode.REFUSED, "keep"),
    ("not-head", ExitCode.REFUSED, "position 2"),
    ("mine", ExitCode.ALREADY, "already yours"),
    ("released", ExitCode.REFUSED, "released"),
])
def test_force_is_refused_with_the_reason_before_any_revoke(client, bid, world, setup, code, words):
    if setup == "mine":
        world.holder = ME
    elif setup != "no-request":
        start_request(client, bid, world)
        rid = world.my_request()["id"]
        world.expire_deadline()
        if setup == "kept":
            world.answer(rid, "keep", 15, "B1 running")
        elif setup == "not-head":
            with world.mu:
                world.queue.insert(0, BOB)
        elif setup == "released":
            with world.mu:                  # answered, but the lease not moved yet
                world.answers[rid] = {"id": rid, "answer": "release", "minutes": 0,
                                      "message": "", "at": world.my_request()["created_at"]}
    r = client.post(lease(bid, "/force"), json={"confirm": True}, headers=H)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == code and words in r.json()["error"]["message"]
    assert world.revoked == [] and not any(c[0] == "force" for c in world.calls)


def test_negative_twin_a_keep_that_ran_out_opens_force_again(client, bid, world):
    job = start_request(client, bid, world)
    rid = world.my_request()["id"]
    world.expire_deadline()
    world.answer(rid, "keep", 15, age_s=16 * 60)                    # 15 min kept, 16 gone
    wait_for(lambda: "force-available" in job_state(client, job)["phases"])
    assert job_state(client, job)["state"] == "running"             # D1: still waiting
    r = client.post(lease(bid, "/force"), json={"confirm": True}, headers=H)
    assert r.status_code == 202, r.text
    assert wait_job(client, r.json()["job"])["result"]["lease"]["mine"] and len(world.revoked) == 1
    assert wait_job(client, job)["result"]["lease"]["mine"]


def _queued_by_the_cli(world: LeaseWorld) -> None:
    """A request made elsewhere (the CLI): a note and a queue place, no job in this daemon."""
    with world.mu:
        world.queue.append(ME)
        world.notes["r77"] = {"id": "r77", "by": ME, "user": "david", "host": "mapstone-dev",
                              "message": "", "created_at": iso(time.time() - 300),
                              "deadline_at": iso(time.time() - 180)}


def test_force_with_no_request_job_is_an_ordinary_board_job(client, bid, world):
    _queued_by_the_cli(world)
    r = client.post(lease(bid, "/force"), json={"confirm": True}, headers=H)
    assert r.status_code == 202
    done = wait_job(client, r.json()["job"])
    assert done["state"] == "done" and done["board_id"] == bid and world.holder == ME


def test_negative_twin_force_is_held_while_another_job_runs(client, bid, world, blocker):
    _queued_by_the_cli(world)
    blocker(bid)
    r = client.post(lease(bid, "/force"), json={"confirm": True}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["data"]["kind"] == "deploy"
    assert world.revoked == [] and world.holder == ALICE


# --- DELETE /lease/queue ------------------------------------------------------------------------


def test_leave_withdraws_the_request_and_ends_the_job(client, bid, world):
    job = start_request(client, bid, world)
    r = client.delete(lease(bid, "/queue"), headers=H)
    assert r.status_code == 200 and r.json() == {"ok": True, "left": True}
    state = wait_job(client, job)
    assert state["state"] == "done" and state["result"] == {"left": True}      # D7
    assert ME not in world.queue and world.my_request() is None
    assert client.get(bid_path(bid), headers=H).status_code == 200


def test_negative_twin_a_service_that_raises_on_cancel_still_ends_with_left(client, bid, world):
    world.cancel_raises = True
    job = start_request(client, bid, world)
    client.delete(lease(bid, "/queue"), headers=H)
    state = wait_job(client, job)
    assert state["state"] == "done" and state["result"] == {"left": True}
    # ... but a failure we did not cause is still a failure
    world.cancel_raises = False
    job = start_request(client, bid, world)
    boom = client.app.state.daemon.leases

    def broken(*_a, **_k):
        raise UnreachableError("the hub stopped answering")

    boom.request = broken
    job2 = client.post(lease(bid, "/request"), json={}, headers=H)
    assert job2.status_code == 409                                    # the first still holds
    client.delete(lease(bid, "/queue"), headers=H)
    wait_job(client, job)
    state = wait_job(client, client.post(lease(bid, "/request"), json={}, headers=H).json()["job"])
    assert state["state"] == "failed" and state["error"]["code"] == ExitCode.UNREACHABLE


def test_negative_twin_leave_with_nothing_queued_says_so(client, bid, world):
    r = client.delete(lease(bid, "/queue"), headers=H)
    assert r.status_code == 200 and r.json() == {"ok": True, "left": False}
    assert world.holder == ALICE


def test_delete_lease_cancels_a_queued_request_like_a_queued_acquire(client, bid, world):
    job = start_request(client, bid, world)
    r = client.delete(lease(bid), headers=H)
    assert r.status_code == 200 and r.json()["cancelled"] is True
    assert wait_job(client, job)["result"] == {"left": True} and ME not in world.queue


def test_closing_the_board_leaves_the_queue(client, bid, world):
    job = start_request(client, bid, world)
    client.app.state.daemon.engine.close(bid)                       # session.closed
    assert wait_job(client, job)["result"] == {"left": True}
    wait_for(lambda: ME not in world.queue)


# --- events on the WebSocket -----------------------------------------------------------------


def frames_until(ws, topic: str, limit: int = 20) -> dict:
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["topic"] == topic:
            return frame
    raise AssertionError(f"no {topic} frame")


def sentinel(client: TestClient, bid: str) -> None:
    client.app.state.daemon.bus.publish(Event("lease.state", bid, {"state": "sentinel"}))


def test_the_requester_gets_force_available_answered_and_left(client, bid, world):
    url = f"/api/v1/events?token={TOKEN}&topics=lease.*"
    with client.websocket_connect(url) as ws:
        first = start_request(client, bid, world)
        rid = world.my_request()["id"]
        world.expire_deadline()
        f = frames_until(ws, "lease.force_available")
        assert f["board_id"] == bid and f["data"] == {"id": rid}
        client.delete(lease(bid, "/queue"), headers=H)
        f = frames_until(ws, "lease.left")
        assert f["board_id"] == bid and f["data"] == {}
        # lease.left comes from leave(); the job frees the board on its next poll tick.
        assert wait_job(client, first)["result"] == {"left": True}
        start_request(client, bid, world)
        rid = world.my_request()["id"]
        world.answer(rid, "keep", 5, "two more runs")
        f = frames_until(ws, "lease.answered")
        assert f["data"] == {"id": rid, "answer": "keep", "minutes": 5, "message": "two more runs"}


def test_the_holder_gets_wanted_and_the_victim_gets_taken(client, bid, world):
    leases = client.app.state.daemon.leases
    hub = client.app.state.daemon.engine.session(bid).hub
    world.holder = ME
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=lease.*") as ws:
        rid = world.add_request(ALICE, "B1 at 3?")
        leases.poll_holder_side(bid, hub)
        f = frames_until(ws, "lease.wanted")
        assert f["board_id"] == bid and f["data"]["id"] == rid and f["data"]["by"] == ALICE
        assert f["data"]["message"] == "B1 at 3?" and f["data"]["deadline_at"]
        world.force_me_off(ALICE)
        leases.poll_holder_side(bid, hub)
        f = frames_until(ws, "lease.taken")
        assert f["data"]["by"] == ALICE and f["data"]["reason"].startswith("force-released by")
        v = client.get(lease(bid), headers=H).json()
        assert v["taken"]["by"] == ALICE and v["lease"]["holder"] == ALICE


def test_negative_twin_a_topic_filter_and_an_empty_leave_send_nothing(client, bid, world):
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=lease.state") as ws:
        world.holder = ME
        world.add_request(ALICE)
        client.app.state.daemon.leases.poll_holder_side(
            bid, client.app.state.daemon.engine.session(bid).hub)       # lease.wanted: filtered
        sentinel(client, bid)
        assert ws.receive_json()["data"] == {"state": "sentinel"}
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=lease.*") as ws:
        world.holder = ALICE
        client.delete(lease(bid, "/queue"), headers=H)                # nothing queued: no lease.left
        sentinel(client, bid)
        assert ws.receive_json()["topic"] == "lease.state"


def test_get_lease_passes_the_extended_view_through(client, bid, world):
    v = client.get(lease(bid), headers=H).json()
    assert set(v) >= {"ok", "lease", "hub", "queue", "request", "incoming", "taken", "board"}
    assert v["hub"] == HUB and v["lease"]["target"] == TARGET and v["board"] == "mps3_01"
    assert v["queue"] == [] and v["request"] is None and v["incoming"] == [] and v["taken"] is None
    world.holder = ME                                                 # D5: my answers survive
    rid = world.add_request(ALICE)
    world.answer(rid, "keep", 30, "long run")
    (inc,) = client.get(lease(bid), headers=H).json()["incoming"]
    assert inc["answer"]["answer"] == "keep" and inc["answer"]["minutes"] == 30
    assert inc["answer"]["at"].endswith("+00:00")                     # D8


# --- CCR T8-2: fpgahub's event stream follows a REST-mode board ---------------------------------


class _Stream:
    def __init__(self, board_id: str, client, on_resync) -> None:
        self.board_id, self.client, self.on_resync = board_id, client, on_resync
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def streams(monkeypatch):
    """A recording ``hub_events.attach_bus``: never a socket (the T8 fake hub covers that)."""
    from harness_manager.transports import hub_events

    made: list[_Stream] = []

    def attach_bus(bus, board_id, client, *, on_resync=None, **_kw):
        made.append(_Stream(board_id, client, on_resync))
        return made[-1]

    monkeypatch.setattr(hub_events, "attach_bus", attach_bus)
    return made


def _as_rest(client: TestClient, bid: str, *, events: bool = True):
    """Make the open board's hub a REST-mode one (T8-1 is not on main yet), then re-announce it."""
    from types import SimpleNamespace

    session = client.app.state.daemon.engine.session(bid)
    real = session.hub
    session.hub = SimpleNamespace(host=real.host, target=real.target, close=real.close,
                                  client=SimpleNamespace(transport="rest", target=real.target,
                                                         config=SimpleNamespace(events=events)))
    client.app.state.daemon.bus.publish(Event("session.opened", bid, {}))
    return session.hub


def test_a_rest_board_streams_hub_events_until_it_closes(client, bid, world, streams):
    d = client.app.state.daemon
    hub = _as_rest(client, bid)
    (s,) = streams
    assert d.hub_streams == {bid: s} and s.board_id == bid and s.client is hub.client
    assert callable(s.on_resync)
    s.on_resync()                                                     # drops the cached view
    d.bus.publish(Event("session.opened", bid, {}))                   # a second open: no second
    assert len(streams) == 1
    assert client.delete(bid_path(bid), headers=H).status_code == 200
    assert d.hub_streams == {} and s.closed == 1


def test_negative_twin_ssh_or_events_off_starts_no_stream(client, bid, world, streams):
    d = client.app.state.daemon
    assert d.hub_streams == {} and streams == []                     # the SSH lab rig (L1)
    _as_rest(client, bid, events=False)
    assert d.hub_streams == {} and streams == []


def test_stopping_the_daemon_closes_the_streams(rig, world, streams, monkeypatch):
    monkeypatch.setattr(hub_api, "LeaseService", factory(world))
    eng = Engine(EngineConfig(state_dir=state_dir()))
    with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
        bid = c.post("/api/v1/boards", json={"target": BOARD_IP}, headers=H).json()["board_id"]
        _as_rest(c, bid)
        assert len(c.app.state.daemon.hub_streams) == 1
    eng.close_all()
    assert [s.closed for s in streams] == [1]
