"""Lane LR-D: the mock daemon's lease requests (docs/LEASE_REQUESTS.md) over HTTP.

The browser tests (test_lrd_*.py) run the page against these routes, so the routes must
be the frozen contract: the extended ``GET /lease``, request / respond / force / leave, the
``lease_request`` and ``lease_force`` jobs, and the ``lease.*`` events. Every check has its
negative twin. Nothing here reaches a hub.
"""

from __future__ import annotations

import time
import warnings
from urllib.parse import quote

import pytest

from harness_manager.demo import BOARD_USB, DemoEngine
from tests.fakes.t14_mock_api import create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "lrd-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
B = quote(BOARD_USB, safe="")
LEASE = f"/api/v1/boards/{B}/lease"


@pytest.fixture
def engine():
    eng = DemoEngine(speed=0.02)
    yield eng
    eng.close_all()


@pytest.fixture
def app(engine):
    return create_app(engine, token=TOKEN, serve_ui=False)


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=False) as c:
        cands = c.post("/api/v1/probe", json={}, headers=AUTH).json()["candidates"]
        cand = next(x for x in cands if x["board_id"] == BOARD_USB)
        assert c.post("/api/v1/boards", json={"candidate": cand}, headers=AUTH).status_code == 200
        yield c


@pytest.fixture
def sim(app):
    return app.state.sim


@pytest.fixture
def events(engine):
    seen = []
    unsub = engine.bus.subscribe("lease.*", seen.append)
    yield seen
    unsub()


def view(client):
    body = client.get(LEASE, headers=AUTH).json()
    assert body["ok"] is True
    return body


def wait_for(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    return fn()


def job(client, job_id):
    return client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()


def done(client, job_id):
    return wait_for(lambda: job(client, job_id)["state"] != "running" and job(client, job_id))


def request(client, message="need it for the demo"):
    r = client.post(f"{LEASE}/request", json={"message": message}, headers=AUTH)
    assert r.status_code == 202, r.text
    return r.json()["job"]


# --- GET /lease --------------------------------------------------------------------------------


def test_get_lease_adds_the_new_keys_even_off_a_hub(client):
    body = view(client)
    assert body["lease"] is None and body["hub"] is None and body["board"] is None
    assert (body["queue"], body["request"], body["incoming"], body["taken"]) == ([], None, [], None)
    assert body["notes_supported"] is True and body["can_revoke"] is True


def test_get_lease_behind_a_hub_names_the_holder_and_the_physical_board(client, sim):
    sim.behind_hub(BOARD_USB, lease="other")
    body = view(client)
    assert body["lease"]["holder"] == "alice@lab-pc-07" and body["lease"]["mine"] is False
    assert body["request"] is None and body["queue"] == []
    assert body["board"] == "mps3_01" and body["lease"]["target"] == "mps3_01_pl"      # D4
    sim.requests.set_board(BOARD_USB, "mps3_07")
    assert view(client)["board"] == "mps3_07"


def test_every_lease_request_time_is_iso_utc(client, sim):
    # D8: ISO 8601 UTC with +00:00, never epoch seconds
    sim.behind_hub(BOARD_USB, lease="other")
    request(client)
    req = wait_for(lambda: view(client)["request"])
    sim.requests.answer(BOARD_USB, "keep", minutes=5)
    req = wait_for(lambda: view(client)["request"]["answer"] and view(client)["request"])
    for at in (req["created_at"], req["deadline_at"], req["answer"]["at"]):
        assert isinstance(at, str) and at.endswith("+00:00") and "T" in at, at


# --- the requester -------------------------------------------------------------------------------


def test_a_request_queues_notifies_and_counts_down_from_the_note(client, sim, events):
    sim.behind_hub(BOARD_USB, lease="other")
    jid = request(client)
    req = wait_for(lambda: view(client)["request"])
    assert req["message"] == "need it for the demo" and req["position"] == 1
    assert req["answer"] is None and req["force_available"] is False
    assert "left to answer" in req["force_reason"]
    made = time.mktime(time.strptime(req["created_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    due = time.mktime(time.strptime(req["deadline_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    assert due - made == 120
    (entry,) = view(client)["queue"]
    assert entry["position"] == 1 and entry["mine"] is True and "@" in entry["holder"]
    assert wait_for(lambda: "notified" in job(client, jid)["phases"])
    assert job(client, jid)["kind"] == "lease_request" and job(client, jid)["state"] == "running"
    assert [e.topic for e in events][:1] == ["lease.state"]


def test_a_keep_answer_is_a_phase_and_the_job_waits_on(client, sim, events):
    # D1: the request job keeps running after a keep; force opens again when it runs out.
    sim.behind_hub(BOARD_USB, lease="other")
    jid = request(client)
    wait_for(lambda: view(client)["request"])
    sim.requests.answer(BOARD_USB, "keep", minutes=15, message="finishing a run")
    assert wait_for(lambda: "answered" in job(client, jid)["phases"])
    assert job(client, jid)["state"] == "running"                # not ended by the answer
    req = view(client)["request"]
    assert req["answer"]["answer"] == "keep" and req["answer"]["message"] == "finishing a run"
    assert req["force_available"] is False                       # the twin: kept, not forceable
    sim.requests.advance(BOARD_USB, 120 + 15 * 60)             # the keep runs out
    assert view(client)["request"]["force_available"] is True
    assert wait_for(lambda: job(client, jid)["phases"][-1] == "force-available")
    assert any(e.topic == "lease.answered" for e in events)
    r = client.post(f"{LEASE}/force", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 202
    assert done(client, jid)["result"]["lease"]["target"] == "mps3_01_pl"    # D2: ends held


def test_a_release_answer_promotes_us(client, sim):
    sim.behind_hub(BOARD_USB, lease="other")
    jid = request(client)
    wait_for(lambda: view(client)["request"])
    sim.requests.answer(BOARD_USB, "release")
    body = done(client, jid)
    assert body["result"]["lease"]["target"] == "mps3_01_pl"
    v = view(client)
    assert v["lease"]["mine"] is True and v["request"] is None


def test_force_after_the_deadline_revokes_and_promotes(client, sim, events):
    sim.behind_hub(BOARD_USB, lease="other")
    jid = request(client)
    wait_for(lambda: view(client)["request"])
    sim.requests.advance(BOARD_USB, 121)
    assert wait_for(lambda: any(e.topic == "lease.force_available" for e in events))
    assert view(client)["request"]["force_available"] is True
    r = client.post(f"{LEASE}/force", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 202, r.text
    fjob = done(client, r.json()["job"])
    assert fjob["kind"] == "lease_force" and fjob["result"]["lease"]["holder"].endswith("@harness-manager")
    assert done(client, jid)["result"]["lease"]
    (revoke,) = sim.requests.revokes
    assert revoke["prior_holder"] == "alice@lab-pc-07"
    assert revoke["reason"].startswith("force-released by ") and "no answer to a request made at" in revoke["reason"]


def test_force_before_the_deadline_is_refused_with_the_time_left(client, sim):
    sim.behind_hub(BOARD_USB, lease="other")
    request(client)
    wait_for(lambda: view(client)["request"])
    r = client.post(f"{LEASE}/force", json={"confirm": True}, headers=AUTH)
    err = r.json()["error"]
    assert r.status_code == 422 and err["name"] == "UNAVAILABLE" and "left to answer" in err["message"]
    assert 110 <= err["data"]["time_left_s"] <= 120                             # D3
    assert err["data"]["deadline_at"] == view(client)["request"]["deadline_at"]
    assert sim.requests.revokes == []


def test_force_without_confirm_is_usage_and_revokes_nothing(client, sim):
    sim.behind_hub(BOARD_USB, lease="other")
    request(client)
    wait_for(lambda: view(client)["request"])
    sim.requests.advance(BOARD_USB, 121)
    for body in ({}, {"confirm": "yes"}, {"confirm": 1}):
        r = client.post(f"{LEASE}/force", json=body, headers=AUTH)
        assert r.status_code == 400 and r.json()["error"]["name"] == "USAGE", body
    assert sim.requests.revokes == []


def test_force_when_not_at_the_head_is_refused(client, sim):
    sim.behind_hub(BOARD_USB, lease="other")
    sim.requests.queue_ahead(BOARD_USB, "carol@lab-pc-09")
    request(client)
    req = wait_for(lambda: view(client)["request"])
    assert req["position"] == 2
    sim.requests.advance(BOARD_USB, 121)
    req = view(client)["request"]
    assert req["force_available"] is False and "carol@lab-pc-09 would get the board" in req["force_reason"]
    r = client.post(f"{LEASE}/force", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"


def test_leave_withdraws_the_request_and_ends_its_job(client, sim, events):
    sim.behind_hub(BOARD_USB, lease="other")
    jid = request(client)
    wait_for(lambda: view(client)["request"])
    r = client.delete(f"{LEASE}/queue", headers=AUTH)
    assert r.json() == {"ok": True, "left": True}
    body = done(client, jid)
    assert body["state"] == "done" and body["result"] == {"left": True}          # D7
    assert view(client)["request"] is None
    assert any(e.topic == "lease.left" for e in events)
    # the twin: leaving again, with nothing queued, says so
    assert client.delete(f"{LEASE}/queue", headers=AUTH).json()["left"] is False


# --- the holder -------------------------------------------------------------------------------


def test_incoming_lists_the_request_and_a_keep_answer_is_recorded(client, sim, events):
    sim.behind_hub(BOARD_USB, lease="mine")
    rid = sim.requests.incoming(BOARD_USB, by="bob@lab-pc-02", message="demo at 3")
    (inc,) = view(client)["incoming"]
    assert inc["id"] == rid and inc["by"] == "bob@lab-pc-02" and inc["user"] == "bob"
    assert inc["answer"] is None                                 # D5: not answered yet
    assert view(client)["queue"][0]["holder"] == "bob@lab-pc-02"
    r = client.post(f"{LEASE}/respond", json={"id": rid, "answer": "keep", "minutes": 15,
                                              "message": "ten more minutes"}, headers=AUTH)
    assert r.json() == {"ok": True}
    assert sim.requests.answers[BOARD_USB][rid]["minutes"] == 15
    assert view(client)["lease"]["mine"] is True                 # kept
    (inc,) = view(client)["incoming"]                            # D5: the answer is on the note
    assert inc["answer"]["answer"] == "keep" and inc["answer"]["message"] == "ten more minutes"
    assert inc["answer"]["at"].endswith("+00:00")
    assert any(e.topic == "lease.wanted" for e in events)


def test_a_release_answer_hands_the_board_over(client, sim):
    sim.behind_hub(BOARD_USB, lease="mine")
    rid = sim.requests.incoming(BOARD_USB)
    assert client.post(f"{LEASE}/respond", json={"id": rid, "answer": "release"},
                       headers=AUTH).status_code == 200
    v = view(client)
    assert v["lease"]["holder"] == "bob@lab-pc-02" and v["incoming"] == []


@pytest.mark.parametrize("body", [
    {"answer": "keep", "minutes": 15},                      # no id
    {"id": "X", "answer": "maybe"},                         # no such answer
    {"id": "X", "answer": "keep", "minutes": 7},            # not 5/15/30/60
    {"id": "X", "answer": "keep"},                          # keep without minutes
])
def test_respond_refuses_a_malformed_answer(client, sim, body):
    sim.behind_hub(BOARD_USB, lease="mine")
    rid = sim.requests.incoming(BOARD_USB)
    body = {**body, "id": rid} if body.get("id") == "X" else body
    r = client.post(f"{LEASE}/respond", json=body, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"]["name"] == "USAGE"
    assert BOARD_USB not in sim.requests.answers


def test_respond_to_an_unknown_request_is_absent(client, sim):
    sim.behind_hub(BOARD_USB, lease="mine")
    r = client.post(f"{LEASE}/respond", json={"id": "gone", "answer": "release"}, headers=AUTH)
    assert r.status_code == 404


# --- the victim -------------------------------------------------------------------------------


def test_taken_is_reported_on_the_lease_and_as_an_event(client, sim, events):
    sim.behind_hub(BOARD_USB, lease="mine")
    assert view(client)["taken"] is None                         # the twin: nothing taken yet
    sim.requests.taken(BOARD_USB, by="bob@lab-pc-02")
    v = view(client)
    assert v["taken"]["by"] == "bob@lab-pc-02" and "force-released by bob" in v["taken"]["reason"]
    assert v["lease"]["holder"] == "bob@lab-pc-02"
    assert [e.data["by"] for e in events if e.topic == "lease.taken"] == ["bob@lab-pc-02"]


# --- hub mode over fpgahub REST (T8): no notes, no revoke without an admin token ----------------


def test_over_rest_notes_are_off_messages_never_arrive_and_keep_is_refused(client, sim):
    sim.behind_hub(BOARD_USB, lease="mine")
    sim.requests.notes_reason = "the hub is reached over its REST API: request notes need SSH"
    body = view(client)
    assert body["notes_supported"] is False and "REST" in body["notes_reason"]
    rid = sim.requests.incoming(BOARD_USB, by="bob@lab-pc-02", message="demo at 3")
    assert view(client)["incoming"][0]["message"] == ""          # the message never arrives
    r = client.post(f"{LEASE}/respond", json={"id": rid, "answer": "keep", "minutes": 15},
                    headers=AUTH)
    assert r.status_code == 422 and "REST" in r.json()["error"]["message"]
    # the twin: Release still works (it is a lease release)
    assert client.post(f"{LEASE}/respond", json={"id": rid, "answer": "release"},
                       headers=AUTH).status_code == 200


def test_over_rest_a_write_token_cannot_force_whatever_the_clock(client, sim):
    sim.behind_hub(BOARD_USB, lease="other")
    sim.requests.revoke_reason = "force-release needs an admin token; this hub token's role is write"
    request(client)
    wait_for(lambda: view(client)["request"])
    sim.requests.advance(BOARD_USB, 121)
    body = view(client)
    assert body["can_revoke"] is False
    assert body["request"]["force_available"] is False and "admin token" in body["request"]["force_reason"]
    r = client.post(f"{LEASE}/force", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and "admin token" in r.json()["error"]["message"]
    assert sim.requests.revokes == []
    sim.requests.revoke_reason = ""                              # the twin: an admin token
    assert view(client)["request"]["force_available"] is True
