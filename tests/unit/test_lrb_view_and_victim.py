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
    parse_utc,
)
from tests.fakes.lrb_fake_hub import HOST, TARGET, iso
from tests.fakes.lrb_rig import BID, BOB, CAROL, DAVID, World

VIEW_KEYS = {"lease", "hub", "board", "queue", "request", "incoming", "taken",
             "notes_supported", "notes_reason", "can_revoke", "revoke_reason"}


@pytest.fixture(autouse=True)
def _no_real_hub(monkeypatch):
    """Nothing here may reach the real hub: a revoke kicks a real person."""
    from harness_manager_mps3 import hub as hubmod

    def refuse(host, group):
        raise AssertionError(f"a test tried to reach the real hub {host}")

    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", refuse)


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


def test_mine_is_by_principal_even_from_another_session_but_incoming_needs_the_token(world):
    world.holding(BOB, name="bob-desk")                        # bob's other session holds it
    world.queued_by_hand(CAROL, age_s=0)
    laptop = world.session(BOB, name="bob-laptop")
    v = laptop.svc.view(laptop.hub)
    assert v["lease"]["holder"] == BOB and v["lease"]["mine"]   # the spec: mine by principal
    assert v["incoming"] == []                                  # it cannot answer: no token here
    desk = world.sessions[0]                                    # twin: the session with the token
    assert [i["by"] for i in desk.svc.view(desk.hub)["incoming"]] == [CAROL]


# --- the view --------------------------------------------------------------------------------------


def test_the_view_has_exactly_the_api_shape(world):
    a, b = world.holding(), world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=30, message="hi")
    va, vb = a.svc.view(a.hub), b.svc.view(b.hub)
    assert set(va) == VIEW_KEYS == set(vb)
    assert set(va["lease"]) == {"target", "holder", "user", "expires_at", "mine", "here",
                                "holder_kind", "holder_kind_reason"}  # D12; here: REVIEW-W5
    assert va["lease"]["holder_kind"] == "hm"                     # this session holds it
    assert va["lease"]["here"] is True and vb["lease"]["here"] is False   # the token is a's
    assert vb["lease"]["holder_kind"] == "unknown"                # not answered: maybe a script
    assert set(va["queue"][0]) == {"position", "holder", "user", "mine"}
    assert va["request"] is None and va["taken"] is None and va["hub"] == HOST
    assert va["board"] == vb["board"] == "mps3_01"                  # D4
    assert va["incoming"] == [{"id": note.id, "by": BOB, "user": "bob", "host": "lab-pc",
                               "message": "hi", "created_at": note.created_at,
                               "deadline_at": note.deadline_at, "answer": None,
                               "tapped_at": None}]                            # PANEL-1
    req = vb["request"]
    assert set(req) == {"id", "message", "created_at", "deadline_at", "position", "answer",
                        "force_available", "force_reason", "reasked", "reasked_at",
                        "tapped_at"}                                       # PANEL-1
    assert (req["reasked"], req["reasked_at"]) == (False, None)
    assert (va["notes_supported"], va["notes_reason"], va["can_revoke"], va["revoke_reason"]) == (
        True, "", True, "")                                        # SSH: all of it works
    assert (req["id"], req["message"], req["position"], req["answer"]) == (note.id, "hi", 1, None)
    assert not req["force_available"] and "90 s left" in req["force_reason"]
    assert vb["incoming"] == []                                # twin: not the holder, nothing incoming
    a.svc.respond(BID, a.hub, note.id, "keep", minutes=5, message="soon")
    world.clock.advance(11)
    answer = b.svc.view(b.hub)["request"]["answer"]
    assert answer == {"answer": "keep", "minutes": 5, "message": "soon", "at": iso(world.clock.t - 11)}
    lease = b.svc.view(b.hub)["lease"]                             # D12: the answer shows HM
    assert lease["holder_kind"] == "hm" and "answered your request" in lease["holder_kind_reason"]
    assert a.svc.view(a.hub)["incoming"][0]["answer"] == answer    # D5: the holder sees it too
    assert a.svc.view(None) == {"lease": None, "hub": None, "board": None, "queue": [],
                                "request": None, "incoming": [], "taken": None,
                                "notes_supported": False,
                                "notes_reason": "this board is not behind a hub",
                                "can_revoke": False,
                                "revoke_reason": "this board is not behind a hub"}


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


# --- the victim, for a client whose module has no taken_from_history -----------------------------


def test_no_revoke_entry_in_the_history_is_lost_not_taken(world):
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


# --- the heartbeat survives anything (Q2) -------------------------------------------------------


def test_a_heartbeat_round_that_fails_oddly_is_said_and_retried(world):
    import errno

    a = world.holding()
    real_put = a.svc.store.put

    def full(_lease):
        raise OSError(errno.ENOSPC, "No space left on device")

    a.svc.store.put = full
    a.svc.beat_due(force=True)                                 # must not raise
    warned = [d for d in a.of("lease.state") if d.get("warning")]
    assert len(warned) == 1 and warned[0]["state"] == "held" and "OSError" in warned[0]["warning"]
    assert a.svc.tracked() == [BID] and a.svc.store.get(HOST, TARGET) is not None
    a.svc.store.put = real_put
    world.clock.advance(61)                                    # retried within a minute
    n = len(world.calls(DAVID, "lease_heartbeat"))
    a.svc.beat_due()
    assert len(world.calls(DAVID, "lease_heartbeat")) == n + 1
    assert len([d for d in a.of("lease.state") if d.get("warning")]) == 1   # twin: a good round is quiet


def test_a_heartbeat_client_bug_does_not_end_the_heartbeat(world):
    a = world.holding()

    def broken(token, holder):
        raise RuntimeError("parser fell over")

    a.hub.client.lease_heartbeat = broken
    a.svc.beat_due(force=True)
    a.svc.watch_due(force=True)
    assert [d["warning"] for d in a.of("lease.state") if d.get("warning")][0].startswith(
        "the lease heartbeat failed (RuntimeError")
    assert a.svc.tracked() == [BID]


# --- D6: the cached view does not hide a deadline or a keep's end --------------------------------


def test_the_cached_view_is_dropped_at_the_deadline(world):
    world.holding()
    b = world.session(BOB)
    world.queued_by_hand(BOB, age_s=115)                       # 5 s before the deadline
    assert not b.svc.view(b.hub)["request"]["force_available"]
    n = len(world.calls(BOB, "lease_status"))
    world.clock.advance(3)                                     # twin: nothing crossed, cache used
    b.svc.view(b.hub)
    assert len(world.calls(BOB, "lease_status")) == n
    world.clock.advance(3)                                     # the deadline passed: re-read
    assert b.svc.view(b.hub)["request"]["force_available"]
    assert len(world.calls(BOB, "lease_status")) == n + 1


def test_the_cached_view_is_dropped_when_a_keep_runs_out(world):
    a, b = world.holding(), world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=200)
    a.svc.respond(BID, a.hub, note.id, "keep", minutes=5)
    world.clock.advance(295)
    assert not b.svc.view(b.hub)["request"]["force_available"]
    n = len(world.calls(BOB, "get_answer"))
    world.clock.advance(6)
    assert b.svc.view(b.hub)["request"]["force_available"]
    assert len(world.calls(BOB, "get_answer")) == n + 1


# --- D8: one time format -------------------------------------------------------------------------------


def test_times_are_iso_utc_with_offset_even_from_z_forms(world):
    from harness_manager.services.lease import iso_norm

    assert iso_norm("2026-09-24T10:00:00Z") == "2026-09-24T10:00:00+00:00"
    assert iso_norm("2026-09-24T10:00:00.123456Z") == "2026-09-24T10:00:00+00:00"
    assert iso_norm("2026-09-24T11:00:00+01:00") == "2026-09-24T10:00:00+00:00"
    assert iso_norm("soon") == "soon" and iso_norm(None) == ""  # twin: unreadable is passed on
    a = world.holding()
    note = RequestNote("zform", BOB, "bob", "lab-pc", "", "2026-09-24T10:00:00Z",
                       "2026-09-24T10:02:00Z")
    world.hub.queue.append((BOB, "bob"))
    world.hub.notes[note.id] = note
    (inc,) = a.svc.view(a.hub)["incoming"]
    assert (inc["created_at"], inc["deadline_at"]) == ("2026-09-24T10:00:00+00:00",
                                                       "2026-09-24T10:02:00+00:00")
