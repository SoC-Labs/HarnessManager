"""LR-B: asking the holder for the board (docs/LEASE_REQUESTS.md). Fakes only: no hub is touched.

Every transition of the request state machine, each check with a negative twin. The
sessions run on one fake clock; ``clock.after(s, fn)`` is what the other side does while
a requester's ``request()`` waits (its sleeps advance the clock).
"""

from __future__ import annotations

import time

import pytest

from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    ExitCode,
    UnavailableError,
    UsageError,
)
from harness_manager.services.lease import (
    ForceRefusedError,
    ForceTooEarlyError,
    LeaseService,
    force_reason,
)
from tests.fakes.lrb_fake_hub import HOST, TARGET, iso
from tests.fakes.lrb_rig import BID, BOB, CAROL, DAVID, World


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


def phases_into(out: list[str]):
    return lambda phase, _done, _total: out.append(phase)


# --- request -> release -> granted ------------------------------------------------------------


def test_request_then_release_then_granted(world):
    a, b = world.holding(), world.session(BOB)
    phases: list[str] = []

    def midway():                                   # t0+10: asked, not answered, not granted
        v = b.svc.view(b.hub)
        assert v["lease"]["holder"] == DAVID and not v["lease"]["mine"]
        assert v["request"]["answer"] is None and v["request"]["position"] == 1
        assert not v["request"]["force_available"]

    def holder_answers():                           # t0+30: david's poll, then "release"
        a.svc.watch_due(force=True)
        (wanted,) = a.of("lease.wanted")
        assert wanted["by"] == BOB and wanted["message"] == "need the MCC"
        a.svc.respond(BID, a.hub, wanted["id"], "release", message="all yours")

    world.clock.after(5, midway)
    world.clock.after(25, holder_answers)
    out = b.svc.request(BID, b.hub, message="need the MCC", progress=phases_into(phases))

    assert out["lease"]["holder"] == BOB and out["lease"]["mine"]
    assert world.hub.current["holder"] == BOB
    assert phases == ["queued", "notified", "held"]
    assert world.hub.notes == {}                              # the request is withdrawn once held
    assert [n.answer for n in world.hub.answers.values()] == ["release"]
    stored = b.svc.store.get(HOST, TARGET)
    assert stored.principal == BOB and stored.token == world.hub.current["token"]
    assert a.svc.store.get(HOST, TARGET) is None and a.states()[-1] == "released"
    assert a.svc.tracked() == []                              # david stops heartbeating it
    assert b.states() == ["queued", "held"]


def test_a_request_for_a_free_board_is_granted_at_once_without_a_note(world):
    b = world.session(BOB)
    out = b.svc.request(BID, b.hub)
    assert out["lease"]["holder"] == BOB and world.hub.notes == {}
    # twin: a board someone holds queues and writes a note
    c = world.session(CAROL)
    world.clock.after(5, lambda: c.svc.leave(BID, c.hub))
    with pytest.raises(ActionFailedError):
        c.svc.request(BID, c.hub)
    assert c.of("lease.left") == [{}] and [p for p, v in world.hub.calls if v == "put_request"] == [CAROL]


# --- request -> keep -> the keep runs out -> force available ----------------------------------


def test_request_keep_then_the_keep_runs_out_then_force_is_available(world):
    a, b = world.holding(), world.session(BOB)
    t0 = world.clock.t
    phases: list[str] = []

    def holder_keeps():                             # t0+30
        a.svc.watch_due(force=True)
        (wanted,) = a.of("lease.wanted")
        a.svc.respond(BID, a.hub, wanted["id"], "keep", minutes=5, message="finishing a run")

    world.clock.after(25, holder_keeps)
    out = b.svc.request(BID, b.hub, progress=phases_into(phases))

    rid = next(iter(world.hub.notes))
    assert out == {"answered": {"answer": "keep", "minutes": 5, "message": "finishing a run",
                                "at": iso(t0 + 30)}}
    assert b.of("lease.answered") == [{"id": rid, "answer": "keep", "minutes": 5,
                                       "message": "finishing a run"}]
    assert phases == ["queued", "notified", "answered"]
    assert world.hub.position(BOB) == 1 and list(world.hub.notes) == [rid]   # place and note kept
    assert world.hub.current["holder"] == DAVID
    req = b.svc.view(b.hub)["request"]
    assert req["answer"]["answer"] == "keep" and not req["force_available"]
    assert "keep" in req["force_reason"]
    assert a.svc.view(a.hub)["incoming"] == []              # answered: no prompt while it runs

    # One requester poll is two hub calls: the acquire (grant or place) and the answer note.
    n = len(world.calls(BOB))
    b.svc.watch_due(force=True)
    assert world.calls(BOB)[n:] == ["lease_acquire", "get_answer"]

    world.clock.advance(t0 + 330 - world.clock.t - 1)       # one second before the keep ends
    b.svc.watch_due(force=True)
    assert b.of("lease.force_available") == []
    with pytest.raises(ForceRefusedError) as exc:
        b.svc.force(BID, b.hub, confirm=True)
    assert exc.value.code == ExitCode.REFUSED and exc.value.time_left_s == 1
    assert world.hub.revokes == []

    world.clock.advance(2)                                    # the keep has run out
    b.svc.watch_due(force=True)
    b.svc.watch_due(force=True)
    assert b.of("lease.force_available") == [{"id": rid}]   # announced once
    world.clock.advance(11)                                   # past the view cache
    req = b.svc.view(b.hub)["request"]
    assert req["force_available"] and req["force_reason"] == ""
    assert [i["id"] for i in a.svc.view(a.hub)["incoming"]] == [rid]   # the prompt is back
    out = b.svc.force(BID, b.hub, confirm=True)
    assert out["lease"]["holder"] == BOB and world.hub.current["holder"] == BOB
    assert world.hub.revokes[0]["reason"] == force_reason(BOB, iso(t0))


# --- request -> no answer -> force -> the victim is told ---------------------------------------


def test_no_answer_then_force_and_the_holder_is_told_who_took_it(world):
    a, b = world.holding(), world.session(BOB)
    t0 = world.clock.t
    phases: list[str] = []
    forced: dict = {}
    world.clock.after(125, lambda: forced.update(b.svc.force(BID, b.hub, confirm=True)))
    out = b.svc.request(BID, b.hub, progress=phases_into(phases))

    reason = force_reason(BOB, iso(t0))
    assert reason == (f"force-released by {BOB} via Harness Manager: no answer to a request "
                      f"made at {iso(t0)}")
    assert phases == ["queued", "notified", "force-available", "held"]
    assert len(b.of("lease.force_available")) == 1
    assert world.hub.revokes == [{"reason": reason, "by": BOB, "prior_holder": DAVID}]
    assert forced["forced"]["holder"] == DAVID and forced["lease"]["holder"] == BOB
    assert out["lease"]["holder"] == BOB and world.hub.notes == {}

    a.svc.beat_due(force=True)                                # the victim's next heartbeat
    assert a.states()[-1] == "lost"
    (taken,) = a.of("lease.taken")
    assert taken["by"] == BOB and taken["reason"].startswith(reason)
    assert taken["at"] == iso(t0 + 130)
    assert a.svc.view(a.hub)["taken"] == taken                # kept for the view ...
    assert a.svc.dismiss_taken(a.hub) and a.svc.view(a.hub)["taken"] is None   # ... until dismissed
    assert not a.svc.dismiss_taken(a.hub)


def test_negative_twin_a_lease_that_expired_or_went_to_someone_unasked_is_not_taken(world):
    a = world.holding()
    world.hub.expire()
    a.svc.beat_due(force=True)
    assert a.states()[-1] == "expired" and a.of("lease.taken") == []
    assert world.calls(DAVID, "lease_history") == []
    a.svc.acquire(a.hub, board_id=BID, heartbeat=False)
    a.svc.track(BID, a.hub, announced=True)
    world.hub.expire()
    world.hub.grant_to(CAROL)                                 # carol just took the free board
    a.svc.beat_due(force=True)
    assert a.states()[-1] == "lost" and a.of("lease.taken") == []
    assert a.svc.view(a.hub)["taken"] is None


# --- force: refused before any revoke ---------------------------------------------------------


def test_force_is_refused_before_the_deadline_and_allowed_after(world):
    world.holding()
    b = world.session(BOB)
    world.queued_by_hand(BOB, age_s=30)                      # bob's note, from another process
    with pytest.raises(ForceTooEarlyError) as exc:
        b.svc.force(BID, b.hub, confirm=True)
    assert exc.value.code == ExitCode.UNAVAILABLE and exc.value.time_left_s == 90   # 422
    assert "90 s left" in exc.value.message and exc.value.data["time_left_s"] == 90
    assert world.hub.revokes == [] and world.hub.current["holder"] == DAVID
    world.clock.advance(90)
    out = b.svc.force(BID, b.hub, confirm=True)
    assert out["lease"]["holder"] == BOB and len(world.hub.revokes) == 1


def test_force_is_refused_after_an_answer(world):
    a, b = world.holding(), world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=200)              # the deadline has passed
    a.svc.respond(BID, a.hub, note.id, "keep", minutes=15, message="demo at 3")
    with pytest.raises(ForceRefusedError) as exc:
        b.svc.force(BID, b.hub, confirm=True)
    assert exc.value.code == ExitCode.REFUSED and exc.value.time_left_s == 900
    assert "keep for 15 min" in exc.value.message
    assert world.hub.revokes == []
    world.clock.advance(900)                                  # twin: the keep has run out
    assert b.svc.force(BID, b.hub, confirm=True)["lease"]["holder"] == BOB


def test_force_is_refused_after_a_release_answer(world):
    a, b = world.holding(), world.session(BOB)
    world.queued_by_hand(CAROL, age_s=300)
    note = world.queued_by_hand(BOB, age_s=200)
    a.svc.respond(BID, a.hub, note.id, "release")             # the hub hands it to carol (head)
    assert world.hub.current["holder"] == CAROL and world.hub.position(BOB) == 1
    with pytest.raises(ForceRefusedError) as exc:
        b.svc.force(BID, b.hub, confirm=True)
    assert "answered release" in exc.value.message and world.hub.revokes == []


def test_force_is_refused_when_not_at_the_head_of_the_queue(world):
    world.holding()
    b, c = world.session(BOB), world.session(CAROL)
    world.queued_by_hand(CAROL, age_s=300)
    world.queued_by_hand(BOB, age_s=200)
    with pytest.raises(ForceRefusedError) as exc:
        b.svc.force(BID, b.hub, confirm=True)
    assert "position 2" in exc.value.message and world.hub.revokes == []
    req = b.svc.view(b.hub)["request"]
    assert req["position"] == 2 and not req["force_available"] and "position 2" in req["force_reason"]
    out = c.svc.force(BID, c.hub, confirm=True)               # twin: the head may
    assert out["lease"]["holder"] == CAROL and world.hub.revokes[0]["by"] == CAROL


def test_force_needs_confirm_and_a_request_of_ours(world):
    world.holding()
    b, c = world.session(BOB), world.session(CAROL)
    world.queued_by_hand(BOB, age_s=200)
    for bad in (False, None, "yes", 1):
        with pytest.raises(UsageError) as exc:
            b.svc.force(BID, b.hub, confirm=bad)
        assert "confirm" in exc.value.message and not isinstance(exc.value, ForceTooEarlyError)
    assert world.calls(BOB) == []                            # refused before touching the hub
    with pytest.raises(ForceRefusedError) as exc:
        c.svc.force(BID, c.hub, confirm=True)                 # carol never asked
    assert "no request" in exc.value.message and world.hub.revokes == []
    assert b.svc.force(BID, b.hub, confirm=True)["lease"]["holder"] == BOB   # twin


def test_force_rechecks_the_hub_not_a_cached_view(world):
    a, b = world.holding(), world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=200)
    assert b.svc.view(b.hub)["request"]["force_available"]    # the view says yes ...
    a.svc.respond(BID, a.hub, note.id, "keep", minutes=5)     # ... then david answers
    with pytest.raises(ForceRefusedError):
        b.svc.force(BID, b.hub, confirm=True)                 # within the view's 10 s cache
    assert world.hub.revokes == []


# --- leave -------------------------------------------------------------------------------------


def test_leave_while_queued_withdraws_the_request(world):
    a, b = world.holding(), world.session(BOB)
    left: dict = {}
    world.clock.after(15, lambda: left.update(b.svc.leave(BID, b.hub)))
    with pytest.raises(ActionFailedError) as exc:
        b.svc.request(BID, b.hub, message="x")
    assert "left the queue" in exc.value.message
    assert left == {"left": True}
    assert world.hub.queue == [] and world.hub.notes == {}
    assert b.of("lease.left") == [{}]
    assert world.calls(BOB, "lease_cancel") == ["lease_cancel"]   # once: the request did not repeat it
    assert b.svc.leave(BID, b.hub) == {"left": False}         # twin: nothing left to leave
    assert b.of("lease.left") == [{}]
    a.svc.watch_due(force=True)
    assert a.of("lease.wanted") == []


def test_leave_after_an_answer(world):
    a, b = world.holding(), world.session(BOB)

    def holder_keeps():
        a.svc.watch_due(force=True)
        a.svc.respond(BID, a.hub, a.of("lease.wanted")[0]["id"], "keep", minutes=30)

    world.clock.after(5, holder_keeps)
    assert "answered" in b.svc.request(BID, b.hub)
    n = len(world.calls(BOB))
    b.svc.watch_due(force=True)
    assert len(world.calls(BOB)) > n                          # twin: still watched until it leaves
    assert b.svc.leave(BID, b.hub) == {"left": True}
    assert world.hub.queue == [] and world.hub.notes == {} and b.of("lease.left") == [{}]
    n = len(world.calls(BOB))
    b.svc.watch_due(force=True)
    assert len(world.calls(BOB)) == n                         # nothing watched any more
    assert b.svc.view(b.hub)["request"] is None


def test_closing_the_board_while_queued_leaves_the_queue(world):
    a, b = world.holding(), world.session(BOB)
    world.clock.after(15, lambda: b.svc.cancel_acquire(BID))   # the daemon's session.closed
    with pytest.raises(ActionFailedError):
        b.svc.request(BID, b.hub)
    assert world.hub.queue == [] and world.hub.notes == {} and b.of("lease.left") == [{}]
    # twin: an answered request the service is watching leaves too
    world.clock.after(5, lambda: a.svc.respond(BID, a.hub, next(iter(world.hub.notes)), "keep",
                                               minutes=5))
    assert "answered" in b.svc.request(BID, b.hub)
    assert world.hub.position(BOB) == 1
    assert b.svc.cancel_acquire(BID)
    deadline = time.monotonic() + 5
    while world.hub.queue and time.monotonic() < deadline:
        time.sleep(0.01)
    assert world.hub.queue == [] and world.hub.notes == {}


def test_closing_the_service_leaves_a_watched_queue_place(world):
    a, b = world.holding(), world.session(BOB)
    world.clock.after(5, lambda: a.svc.respond(BID, a.hub, next(iter(world.hub.notes)), "keep",
                                               minutes=5))
    assert "answered" in b.svc.request(BID, b.hub)
    a.svc.close()                                             # twin: a holder's close leaves nothing
    assert world.hub.position(BOB) == 1 and len(world.hub.notes) == 1
    b.svc.close()                                             # the daemon shuts down
    assert world.hub.queue == [] and world.hub.notes == {} and b.of("lease.left") == [{}]


# --- two requesters ------------------------------------------------------------------------------


def test_two_requesters_are_served_in_queue_order(world):
    a, b, c = world.holding(), world.session(BOB), world.session(CAROL)
    world.clock.after(5, lambda: world.queued_by_hand(BOB, age_s=0, message="after carol"))

    def holder_answers():
        a.svc.watch_due(force=True)
        a.svc.watch_due(force=True)
        wanted = a.of("lease.wanted")
        assert [w["by"] for w in wanted] == [CAROL, BOB]         # one each, once each
        v = a.svc.view(a.hub)
        assert [(q["position"], q["holder"], q["mine"]) for q in v["queue"]] == [
            (1, CAROL, False), (2, BOB, False)]
        assert [i["by"] for i in v["incoming"]] == [CAROL, BOB]
        a.svc.respond(BID, a.hub, wanted[0]["id"], "release")

    world.clock.after(25, holder_answers)
    out = c.svc.request(BID, c.hub, message="carol first")
    assert out["lease"]["holder"] == CAROL
    assert world.hub.current["holder"] == CAROL and world.hub.queue == [(BOB, "bob")]
    v = b.svc.view(b.hub)
    assert v["queue"] == [{"position": 1, "holder": BOB, "user": "bob", "mine": True}]
    assert v["request"]["position"] == 1


def test_a_new_holder_is_asked_before_it_can_be_forced(world):
    """Carol gets the board after bob's deadline passed; bob may not kick her at once."""
    a, b, c = world.holding(), world.session(BOB), world.session(CAROL)
    t0 = world.clock.t
    carol = world.queued_by_hand(CAROL, age_s=0)
    got: dict = {}

    def carol_gets_it():                                      # t0+130: bob's first deadline is past
        a.svc.respond(BID, a.hub, carol.id, "release")
        c.svc.acquire(c.hub, board_id=BID, heartbeat=False)
        c.svc.track(BID, c.hub, announced=True)
        with pytest.raises(ForceRefusedError) as exc:
            b.svc.force(BID, b.hub, confirm=True)             # before bob's poll notices
        assert "was not asked" in exc.value.message
        got["early"] = exc.value

    def carol_is_asked():                                     # t0+140
        c.svc.watch_due(force=True)
        (wanted,) = c.of("lease.wanted")
        assert wanted["by"] == BOB and wanted["deadline_at"] == iso(t0 + 250)
        with pytest.raises(ForceTooEarlyError) as exc:
            b.svc.force(BID, b.hub, confirm=True)
        assert exc.value.time_left_s == 110

    def bob_forces():                                         # t0+260
        got["forced"] = b.svc.force(BID, b.hub, confirm=True)

    world.clock.after(125, carol_gets_it)
    world.clock.after(135, carol_is_asked)
    world.clock.after(255, bob_forces)
    out = b.svc.request(BID, b.hub)
    assert out["lease"]["holder"] == BOB and got["forced"]["forced"]["holder"] == CAROL
    assert [r["prior_holder"] for r in world.hub.revokes] == [CAROL]
    assert len(b.of("lease.force_available")) == 1            # for the new note only
    c.svc.beat_due(force=True)
    assert c.of("lease.taken")[0]["by"] == BOB


def test_the_wait_re_asks_a_new_holder_itself(world):
    """No force call at all: the waiting request notices the new holder on its own poll,
    so the UI is never offered a force-release against someone who was not asked."""
    a, b, c = world.holding(), world.session(BOB), world.session(CAROL)
    t0 = world.clock.t
    carol = world.queued_by_hand(CAROL, age_s=0)
    phases: list[str] = []

    def carol_gets_it():                                      # t0+130: the first deadline is past
        a.svc.respond(BID, a.hub, carol.id, "release")
        c.svc.acquire(c.hub, board_id=BID, heartbeat=False)
        c.svc.track(BID, c.hub, announced=True)

    def check():                                              # t0+140
        c.svc.watch_due(force=True)
        assert [w["deadline_at"] for w in c.of("lease.wanted")] == [iso(t0 + 250)]
        assert b.of("lease.force_available") == []            # not against carol, not yet
        assert not b.svc.view(b.hub)["request"]["force_available"]
        b.svc.leave(BID, b.hub)

    world.clock.after(125, carol_gets_it)
    world.clock.after(135, check)
    with pytest.raises(ActionFailedError):
        b.svc.request(BID, b.hub, progress=phases_into(phases))
    assert phases == ["queued", "notified", "notified"]       # asked david, then carol
    assert world.hub.current["holder"] == CAROL and world.hub.revokes == []


# --- the holder's session is absent ---------------------------------------------------------------


def test_the_holder_session_absent_no_answer_so_force_works(world):
    a = world.holding()
    a.svc.close()                                             # david's Harness Manager is not running
    b = world.session(BOB)
    world.clock.after(125, lambda: b.svc.force(BID, b.hub, confirm=True))
    out = b.svc.request(BID, b.hub)
    assert out["lease"]["holder"] == BOB and world.hub.answers == {}
    assert world.calls(DAVID, "list_requests") == []
    # david's service starts again (same state dir): the lease is gone, and it says who took it
    a2 = world.session(DAVID, name="david-again", state="david")
    v = a2.svc.view(a2.hub)
    assert v["lease"]["holder"] == BOB and not v["lease"]["mine"]
    assert v["taken"]["by"] == BOB and a2.of("lease.taken") == [v["taken"]]
    assert a2.states() == ["lost"] and a2.svc.store.get(HOST, TARGET) is None
    # twin: the next view does not announce it again
    world.clock.advance(11)
    a2.svc.view(a2.hub)
    assert len(a2.of("lease.taken")) == 1


# --- clock skew: the notes' times win ---------------------------------------------------------------


def test_clock_skew_the_requests_deadline_is_the_notes(world):
    a = world.holding(skew=+90.0)                             # david's clock is 90 s ahead
    b = world.session(BOB)
    t0 = world.clock.t
    cli: dict = {}

    def at_100():                                             # bob's t0+100 is david's t0+190
        a.svc.watch_due(force=True)
        (wanted,) = a.of("lease.wanted")
        assert wanted["deadline_at"] == iso(t0 + 120)          # not david's now + 120
        assert a.svc.view(a.hub)["incoming"][0]["deadline_at"] == iso(t0 + 120)
        with pytest.raises(ForceTooEarlyError) as exc:
            b.svc.force(BID, b.hub, confirm=True)
        assert exc.value.time_left_s == 20
        cli["b2"] = b2 = world.session(BOB, name="bob-cli")   # a process that never saw the wait
        with pytest.raises(ForceTooEarlyError) as exc:
            b2.svc.force(BID, b2.hub, confirm=True)
        assert exc.value.time_left_s == 20

    def cli_forces():                                         # t0+130: past the note's deadline
        cli["forced"] = cli["b2"].svc.force(BID, cli["b2"].hub, confirm=True)

    world.clock.after(100, at_100)
    world.clock.after(125, cli_forces)
    out = b.svc.request(BID, b.hub)
    assert out["lease"]["holder"] == BOB and cli["forced"]["lease"]["holder"] == BOB
    assert world.hub.revokes[0]["reason"] == force_reason(BOB, iso(t0))


def test_clock_skew_a_keep_runs_from_the_answer_notes_time(world):
    a = world.holding(skew=+90.0)
    b = world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=200)
    a.svc.respond(BID, a.hub, note.id, "keep", minutes=5)
    assert world.hub.answers[note.id].at == iso(world.clock.t + 90)
    with pytest.raises(ForceRefusedError) as exc:
        b.svc.force(BID, b.hub, confirm=True)
    assert exc.value.time_left_s == 390                       # at + 5 min, on bob's clock
    world.clock.advance(389)
    with pytest.raises(ForceRefusedError):
        b.svc.force(BID, b.hub, confirm=True)
    world.clock.advance(1)
    assert b.svc.force(BID, b.hub, confirm=True)["lease"]["holder"] == BOB


# --- request: arguments and prerequisites ---------------------------------------------------------


def test_request_checks_its_arguments(world):
    world.holding()
    b = world.session(BOB)
    with pytest.raises(UsageError):
        b.svc.request(BID, b.hub, message="x" * 1001)
    for bad in (0, -5, 1.5, True, "600"):
        with pytest.raises(UsageError):
            b.svc.request(BID, b.hub, ttl_s=bad)
    assert world.calls(BOB) == []
    world.clock.after(5, lambda: b.svc.leave(BID, b.hub))    # twin: a 1000-character message is fine
    with pytest.raises(ActionFailedError):
        b.svc.request(BID, b.hub, message="x" * 1000, ttl_s=600)
    assert world.calls(BOB, "put_request") == ["put_request"]


def test_a_second_request_while_one_waits_is_refused(world):
    world.holding()
    b = world.session(BOB)
    seen: dict = {}

    def again():
        with pytest.raises(AlreadyError):
            b.svc.request(BID, b.hub)
        seen["refused"] = True
        b.svc.leave(BID, b.hub)

    world.clock.after(5, again)
    with pytest.raises(ActionFailedError):
        b.svc.request(BID, b.hub)
    assert seen["refused"] and world.calls(BOB, "put_request") == ["put_request"]


def test_requests_need_a_hub_client_that_can_carry_them(tmp_path):
    from harness_manager.core.errors import HarnessError
    from tests.fakes.l1_fake_hub import FakeHub
    from tests.unit.test_l1_lease import Hub

    class L1OnlyClient:                                       # show/acquire/heartbeat/release only
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            if name.startswith("lease_") and name not in ("lease_status", "lease_revoke",
                                                          "lease_history"):
                return lambda *a, **k: self.calls.append(name)
            raise AttributeError(name)

    class Ref:
        host, target = HOST, TARGET
        client = L1OnlyClient()

    svc = LeaseService(tmp_path / "s", tick_s=3600.0)
    fake = FakeHub()                                          # an fpgahub without whoami
    try:
        with pytest.raises(UnavailableError) as exc:
            svc.request(BID, Ref())
        assert "lease_status" in exc.value.message and Ref.client.calls == []
        with pytest.raises(UnavailableError):
            svc.request(BID, None)                             # not behind a hub at all
        with pytest.raises(HarnessError):                      # the hub cannot say who we are
            svc.request(BID, Hub(fake))
        assert [c[1] for c in fake.calls] == ["whoami"]        # nothing queued, no note
    finally:
        svc.close()
        fake.close()


# --- the same person in two sessions (CCR-A2) ---------------------------------------------------


def test_a_request_is_refused_when_our_principal_already_holds_it(world):
    world.holding(BOB, name="bob-desk")                       # bob's other session holds it
    b = world.session(BOB, name="bob-laptop")
    n = len(world.hub.calls)
    with pytest.raises(AlreadyError) as exc:
        b.svc.request(BID, b.hub)
    assert "another session" in exc.value.message
    assert [v for _p, v in world.hub.calls[n:]] == ["principal", "lease_status"]   # no queue, no note
    assert world.hub.queue == [] and world.hub.notes == {}
    same = world.sessions[0]                                  # this session holds it
    with pytest.raises(AlreadyError) as exc:
        same.svc.request(BID, same.hub)
    assert "another session" not in exc.value.message
    c = world.session(CAROL)                                  # twin: another principal queues
    world.clock.after(5, lambda: c.svc.leave(BID, c.hub))
    with pytest.raises(ActionFailedError):
        c.svc.request(BID, c.hub)
    assert c.of("lease.left") == [{}]


def test_force_is_refused_when_our_principal_already_holds_it(world):
    world.holding(BOB, name="bob-desk")
    b = world.session(BOB, name="bob-laptop")
    with pytest.raises(AlreadyError):
        b.svc.force(BID, b.hub, confirm=True)
    assert world.hub.revokes == []


# --- as the daemon runs it: the request in a job thread, force or leave from another ----------


def _threaded(world, principal):
    """A session whose request() really waits (cancel.wait), polling every 20 ms."""
    from harness_manager.core.events import EventBus
    from tests.fakes.lrb_fake_hub import LrbHubRef
    from tests.fakes.lrb_rig import Sess

    bus = EventBus()
    svc = LeaseService(world.tmp / f"t-{principal}", bus, tick_s=3600.0,
                       wall_clock=world.clock.wall(), request_poll_s=0.02)
    s = Sess(principal, principal, svc, LrbHubRef(world.hub.client(principal)))
    bus.subscribe("lease.*", lambda ev: s.events.append((ev.topic, ev.board_id, dict(ev.data))))
    world.sessions.append(s)
    return s


def _until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.005)
    return cond()


def test_threaded_force_while_the_request_job_waits(world):
    import threading

    world.holding()
    b = _threaded(world, BOB)
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(b.svc.request(BID, b.hub)), daemon=True)
    t.start()
    assert _until(lambda: world.hub.notes)
    with pytest.raises(ForceTooEarlyError):                  # twin: not before the deadline
        b.svc.force(BID, b.hub, confirm=True)
    world.clock.t += 121                                      # the note's deadline passes
    assert _until(lambda: b.of("lease.force_available"))
    out = b.svc.force(BID, b.hub, confirm=True)
    t.join(timeout=5)
    assert not t.is_alive() and result["lease"]["holder"] == BOB == out["lease"]["holder"]
    assert world.hub.queue == [] and world.hub.notes == {}


def test_threaded_leave_while_the_request_job_waits(world):
    import threading

    world.holding()
    b = _threaded(world, BOB)
    errors: list = []

    def job():
        try:
            b.svc.request(BID, b.hub)
        except ActionFailedError as exc:
            errors.append(exc)

    t = threading.Thread(target=job, daemon=True)
    t.start()
    assert _until(lambda: world.hub.notes)
    assert b.svc.leave(BID, b.hub) == {"left": True}
    t.join(timeout=5)
    assert not t.is_alive() and "left the queue" in errors[0].message
    assert world.hub.queue == [] and world.hub.notes == {} and b.of("lease.left") == [{}]
