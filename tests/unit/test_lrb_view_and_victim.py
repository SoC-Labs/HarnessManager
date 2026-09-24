"""LR-B: whose lease it is (the "mine" fix), the extended view, the holder's watch, the victim,
and the force rule as a pure function. Fakes only; each check has a negative twin."""

from __future__ import annotations

import pytest

from harness_manager.core.errors import AbsentError, RefusedError, UsageError
from harness_manager.services.lease import (
    AnswerNote,
    RequestNote,
    StoredLease,
    force_check,
    force_reason,
    parse_utc,
)
from tests.fakes.lrb_fake_hub import HOST, TARGET, iso
from tests.fakes.lrb_rig import BID, BOB, CAROL, DAVID, World

VIEW_KEYS = {"lease", "hub", "queue", "request", "incoming", "taken"}


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


# --- the "mine" fix ------------------------------------------------------------------------------


def test_mine_uses_the_principal_the_hub_recorded_not_the_holder_we_asked_for(world):
    """Regression (L1): we asked for ``david-hm``, the hub recorded ``david@mapstone-dev``,
    and our own lease showed as someone else's."""
    a = world.session(DAVID)
    out = a.svc.acquire(a.hub, board_id=BID, holder="david-hm", heartbeat=False)
    assert out["lease"] == {"target": TARGET, "holder": DAVID, "expires_at": out["lease"]["expires_at"],
                            "mine": True}
    stored = a.svc.store.get(HOST, TARGET)
    assert stored.principal == DAVID and stored.holder == "david-hm"
    v = a.svc.view(a.hub)["lease"]
    assert v["holder"] == DAVID and v["mine"]
    assert v["holder"] != stored.holder                       # the comparison L1 got wrong
    b = world.session(BOB)                                     # twin: someone else's lease
    assert not b.svc.view(b.hub)["lease"]["mine"]
    again = a.svc.acquire(a.hub, board_id=BID, holder="david-hm", heartbeat=False)
    assert again.get("already")                               # not a second queue entry
    assert world.hub.queue == []


def test_a_lease_stored_before_the_fix_is_still_ours_once_the_hub_says_who_we_are(world):
    a = world.session(DAVID)
    token = world.hub.grant_to(DAVID)
    a.svc.store.put(StoredLease(HOST, TARGET, "david-hm", token, 3600))   # no principal on it
    assert a.svc.view(a.hub)["lease"]["mine"]
    b = world.session(BOB)                                     # twin: bob's copy of that file
    b.svc.store.put(StoredLease(HOST, TARGET, "david-hm", "tok-bob", 3600))
    v = b.svc.view(b.hub)
    assert v["lease"] is not None and not v["lease"]["mine"]


def test_is_this_queue_entry_me(world):
    world.holding()
    b = world.session(BOB)
    world.queued_by_hand(CAROL, age_s=0)
    world.queued_by_hand(BOB, age_s=0)
    q = b.svc.view(b.hub)["queue"]
    assert [(e["position"], e["holder"], e["mine"]) for e in q] == [(1, CAROL, False), (2, BOB, True)]
    c = world.session(CAROL)
    q = c.svc.view(c.hub)["queue"]
    assert [e["mine"] for e in q] == [True, False]


# --- the view --------------------------------------------------------------------------------------


def test_the_view_has_exactly_the_api_shape(world):
    a, b = world.holding(), world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=30, message="hi")
    va, vb = a.svc.view(a.hub), b.svc.view(b.hub)
    assert set(va) == VIEW_KEYS == set(vb)
    assert set(va["lease"]) == {"target", "holder", "user", "expires_at", "mine"}
    assert set(va["queue"][0]) == {"position", "holder", "user", "mine"}
    assert va["request"] is None and va["taken"] is None and va["hub"] == HOST
    assert va["incoming"] == [{"id": note.id, "by": BOB, "user": "bob", "host": "lab-pc",
                               "message": "hi", "created_at": note.created_at,
                               "deadline_at": note.deadline_at}]
    req = vb["request"]
    assert set(req) == {"id", "message", "created_at", "deadline_at", "position", "answer",
                        "force_available", "force_reason"}
    assert (req["id"], req["message"], req["position"], req["answer"]) == (note.id, "hi", 1, None)
    assert not req["force_available"] and "90 s left" in req["force_reason"]
    assert vb["incoming"] == []                                # twin: not the holder, nothing incoming
    a.svc.respond(BID, a.hub, note.id, "keep", minutes=5, message="soon")
    world.clock.advance(11)
    answer = b.svc.view(b.hub)["request"]["answer"]
    assert answer == {"answer": "keep", "minutes": 5, "message": "soon", "at": iso(world.clock.t - 11)}
    assert a.svc.view(None) == {"lease": None, "hub": None, "queue": [], "request": None,
                                "incoming": [], "taken": None}


def test_incoming_leaves_out_stale_notes_whose_requester_is_not_queued(world):
    a = world.holding()
    note = world.queued_by_hand(BOB, age_s=10)
    world.hub.queue.clear()                                    # bob's session died after queueing
    assert a.svc.view(a.hub)["incoming"] == []
    world.hub.queue.append((BOB, "bob"))                       # twin: queued again
    world.clock.advance(11)
    assert [i["id"] for i in a.svc.view(a.hub)["incoming"]] == [note.id]


# --- the holder's watch ------------------------------------------------------------------------------


def test_the_holder_is_told_once_per_request_one_call_per_poll(world):
    a = world.holding()
    note = world.queued_by_hand(BOB, age_s=0, message="please")
    a.svc.watch_due(force=True)
    a.svc.watch_due(force=True)
    assert a.of("lease.wanted") == [{"id": note.id, "by": BOB, "user": "bob", "host": "lab-pc",
                                     "message": "please", "deadline_at": note.deadline_at}]
    assert world.calls(DAVID, "list_requests") == ["list_requests"] * 2
    n = len(world.calls(DAVID))
    a.svc.watch_due()                                          # not due again within 10 s
    assert len(world.calls(DAVID)) == n
    world.clock.advance(10)
    a.svc.watch_due()
    assert world.calls(DAVID)[n:] == ["list_requests"]         # one ssh call per poll
    c_note = world.queued_by_hand(CAROL, age_s=0)
    world.clock.advance(10)
    a.svc.watch_due()
    assert [w["id"] for w in a.of("lease.wanted")] == [note.id, c_note.id]


def test_negative_twin_a_session_that_does_not_hold_it_does_not_poll_requests(world):
    world.holding()
    world.queued_by_hand(CAROL, age_s=0)
    b = world.session(BOB)
    b.svc.track(BID, b.hub)                                    # bob has the board open, no lease
    b.svc.watch_due(force=True)
    assert world.calls(BOB, "list_requests") == [] and b.of("lease.wanted") == []


# --- the victim, when fpgahub's history has no admin_revoked entry -----------------------------------


def test_victim_without_admin_revoked_in_history_names_the_requester_it_was_sent(world):
    world.hub.history_has_revoke = False                       # fpgahub 0.3.0's target history
    a, b = world.holding(), world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=200)
    a.svc.watch_due(force=True)                                # david's session saw the request
    b.svc.force(BID, b.hub, confirm=True)
    a.svc.beat_due(force=True)
    (taken,) = a.of("lease.taken")
    assert taken["by"] == BOB and taken["reason"] == force_reason(BOB, note.created_at)


def test_negative_twin_no_revoke_entry_and_no_request_is_lost_not_taken(world):
    world.hub.history_has_revoke = False
    a = world.holding()
    world.hub.expire()
    world.hub.grant_to(CAROL)
    a.svc.beat_due(force=True)
    assert a.states()[-1] == "lost" and a.of("lease.taken") == []


def test_an_old_revoke_in_the_history_is_not_this_lease(world):
    a = world.holding()
    world.hub.history.append({"ts": iso(world.clock.t - 7200), "event": "lease.admin_revoked",
                              "by": "unix:x", "reason": "old", "prior_holder": DAVID})
    world.hub.expire()
    world.hub.grant_to(CAROL)
    a.svc.beat_due(force=True)
    assert a.states()[-1] == "lost" and a.of("lease.taken") == []
    world.hub.current = None                                   # twin: a revoke of THIS lease
    a.svc.acquire(a.hub, board_id=BID, heartbeat=False)
    a.svc.track(BID, a.hub, announced=True)
    world.hub.history.append({"ts": iso(world.clock.t), "event": "lease.admin_revoked",
                              "by": "unix:x", "reason": "new", "prior_holder": DAVID})
    world.hub.expire()
    world.hub.grant_to(CAROL)
    a.svc.beat_due(force=True)
    assert [t["reason"] for t in a.of("lease.taken")] == ["new"]


def test_a_revoke_entry_names_the_admin_when_the_reason_is_not_ours(world):
    a = world.holding()
    world.clock.advance(5)
    world.hub.expire()
    world.hub.grant_to(CAROL)
    world.hub.history.append({"ts": iso(world.clock.t), "event": "lease.admin_revoked",
                              "by": "unix:root", "reason": "maintenance (by unix:root)",
                              "prior_holder": DAVID})
    a.svc.beat_due(force=True)
    (taken,) = a.of("lease.taken")
    assert taken == {"by": "unix:root", "reason": "maintenance (by unix:root)", "at": iso(world.clock.t)}
    world.hub.current = None
    b = world.holding(BOB, name="bob-holds")                   # twin: someone else's revoke
    world.hub.history.append({"ts": iso(world.clock.t), "event": "lease.admin_revoked",
                              "by": "unix:root", "reason": "x", "prior_holder": CAROL})
    world.hub.expire()
    world.hub.grant_to(CAROL)
    b.svc.beat_due(force=True)
    assert b.of("lease.taken") == []


# --- respond ----------------------------------------------------------------------------------------


def test_respond_checks_its_arguments_and_who_answers(world):
    a, b = world.holding(), world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=10)
    bad = [("maybe", {}), ("keep", {"minutes": 7}), ("keep", {"minutes": 0}),
           ("keep", {"minutes": True}), ("keep", {"minutes": 5, "message": "x" * 1001})]
    for answer, kw in bad:
        with pytest.raises(UsageError):
            a.svc.respond(BID, a.hub, note.id, answer, **kw)
    with pytest.raises(UsageError):
        a.svc.respond(BID, a.hub, "../etc/passwd", "release")
    with pytest.raises(AbsentError):
        a.svc.respond(BID, a.hub, "no-such-request", "keep", minutes=5)
    with pytest.raises(RefusedError):
        b.svc.respond(BID, b.hub, note.id, "release")          # bob does not hold it
    assert world.hub.answers == {} and world.hub.current["holder"] == DAVID
    out = a.svc.respond(BID, a.hub, note.id, "keep", minutes=15, message="m")   # twin
    assert out == {"ok": True, "answer": {"answer": "keep", "minutes": 15, "message": "m",
                                          "at": iso(world.clock.t)}}
    assert world.hub.current["holder"] == DAVID and a.svc.store.get(HOST, TARGET) is not None
    assert world.hub.answers[note.id].minutes == 15


def test_respond_release_releases_and_the_hub_promotes_the_head(world):
    a = world.holding()
    world.queued_by_hand(CAROL, age_s=10)
    note = world.queued_by_hand(BOB, age_s=5)
    out = a.svc.respond(BID, a.hub, note.id, "release", minutes=30, message="go")
    assert out["answer"] == {"answer": "release", "minutes": 0, "message": "go", "at": iso(world.clock.t)}
    assert world.hub.current["holder"] == CAROL                # the head, not the asker
    assert a.svc.store.get(HOST, TARGET) is None and a.states()[-1] == "released"


# --- the rule as a function ---------------------------------------------------------------------------


def _note(created: float) -> RequestNote:
    return RequestNote("r1", BOB, "bob", "lab-pc", "", iso(created), iso(created + 120))


def test_force_check_rule_table():
    t = 1_790_000_000.0
    note = _note(t)
    keep = AnswerNote("r1", "keep", 5, "", iso(t + 30))
    release = AnswerNote("r1", "release", 0, "", iso(t + 30))
    cases = [
        (note, None, 1, t + 119, False, "early", 1),
        (note, None, 1, t + 120, True, "", 0),
        (note, None, 2, t + 500, False, "refused", 0),
        (note, None, 0, t + 500, False, "refused", 0),
        (note, keep, 1, t + 329, False, "refused", 1),
        (note, keep, 1, t + 330, True, "", 0),
        (note, release, 1, t + 500, False, "refused", 0),
        (None, None, 1, t + 500, False, "refused", 0),
        (RequestNote("r1", BOB, "", "", "", "", "soon"), None, 1, t, False, "refused", 0),
        (note, AnswerNote("r1", "keep", 5, "", "garbled"), 1, t + 999, False, "refused", 0),
    ]
    for n, ans, pos, now, ok, kind, left in cases:
        got = force_check(n, ans, pos, now)
        assert (got.available, got.kind, got.time_left_s) == (ok, kind, left), (ans, pos, now - t)
        assert bool(got.reason) is not ok


def test_parse_utc_reads_the_notes_timestamps():
    assert parse_utc("2026-09-24T10:00:00+00:00") == parse_utc("2026-09-24T10:00:00Z")
    assert parse_utc("2026-09-24T11:00:00+01:00") == parse_utc("2026-09-24T10:00:00+00:00")
    assert parse_utc("2026-09-24T10:00:00") == parse_utc("2026-09-24T10:00:00+00:00")   # naive = UTC
    for bad in ("", "soon", None, 12, "2026-13-01T00:00:00Z"):
        assert parse_utc(bad) is None
