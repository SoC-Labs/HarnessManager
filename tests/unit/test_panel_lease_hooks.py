"""The lease service's two hooks for the front panel's presence service (lane PANEL-CORE).

- **PANEL-1** ``notify_holder(board_id, hub, *, seq, at)``: someone at the board tapped the
  lease-request banner on the panel. It records ``tapped_at`` on the open request (``view()``:
  ``incoming[]`` for the holder, ``request`` for the requester) and publishes
  ``lease.tapped {id, by, at}``. It NEVER releases, answers, forces or leaves (decision P2).
- **PANEL-2** ``view(hub, cached_only=True)``: the last view, with no hub call; None when
  there is none (the presence beat then reads it the ordinary way).

Fakes only, each with a negative twin. Nothing here reaches a hub.
"""

from __future__ import annotations

import pytest

from harness_manager.services.lease import AnswerNote, LeaseService, iso_utc
from tests.fakes.lrb_rig import BID, BOB, CAROL, DAVID, World

#: The hub verbs that change anything: a tap may call none of them.
HUB_WRITES = {"lease_acquire", "lease_heartbeat", "lease_release", "lease_cancel", "lease_revoke",
              "put_request", "delete_request", "put_answer"}


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


@pytest.fixture
def no_handover(monkeypatch):
    """A spy, armed once the scene is set: anything that could hand the board over (release,
    respond, force, leave, and what they call) fails the test."""
    def arm() -> None:
        def boom(name):
            def fail(*_a, **_k):
                raise AssertionError(f"a panel tap called LeaseService.{name}")
            return fail

        for name in ("release", "respond", "force", "leave", "request", "acquire",
                     "_release_stored", "_leave_hub", "_granted"):
            monkeypatch.setattr(LeaseService, name, boom(name))
    return arm


def writes(world, principal):
    return [v for p, v in world.hub.calls if p == principal and v in HUB_WRITES]


# --- PANEL-1: a tap notifies; it never hands the board over ------------------------------------------


def test_a_tap_in_the_holders_session_notifies_and_records_tapped_at(world, no_handover):
    a = world.holding()
    note = world.queued_by_hand(BOB, age_s=30)
    no_handover()
    before = writes(world, DAVID)
    at = world.clock.t - 2
    out = a.svc.notify_holder(BID, a.hub, seq=7, at=at)
    assert out == {"notified": True, "request": {"id": note.id, "by": BOB}}
    assert a.of("lease.tapped") == [{"id": note.id, "by": BOB, "at": iso_utc(at)}]
    (inc,) = a.svc.view(a.hub)["incoming"]
    assert inc["id"] == note.id and inc["tapped_at"] == iso_utc(at)
    assert writes(world, DAVID) == before and world.hub.revokes == []
    assert world.hub.current["holder"] == DAVID and list(world.hub.notes) == [note.id]
    assert world.hub.answers == {}


def test_negative_twin_the_same_tap_twice_is_one_event_and_a_new_tap_another(world, no_handover):
    a = world.holding()
    world.queued_by_hand(BOB, age_s=30)
    no_handover()
    a.svc.notify_holder(BID, a.hub, seq=7, at=world.clock.t)
    a.svc.notify_holder(BID, a.hub, seq=7, at=world.clock.t)
    assert len(a.of("lease.tapped")) == 1
    a.svc.notify_holder(BID, a.hub, seq=8, at=world.clock.t + 5)
    assert len(a.of("lease.tapped")) == 2
    assert a.svc.view(a.hub)["incoming"][0]["tapped_at"] == iso_utc(world.clock.t + 5)


def test_negative_twin_no_open_request_notifies_nobody(world, no_handover):
    a = world.holding()
    no_handover()
    assert a.svc.notify_holder(BID, a.hub, seq=1, at=world.clock.t) == {"notified": False,
                                                                        "request": None}
    note = world.queued_by_hand(BOB, age_s=30)
    world.hub.answers[note.id] = AnswerNote(id=note.id, answer="keep", minutes=5, message="",
                                            at=iso_utc(world.clock.t))   # answered elsewhere
    world.clock.advance(11)                         # past the notes' cache
    assert a.svc.notify_holder(BID, a.hub, seq=2, at=world.clock.t)["notified"] is False, \
        "an answered request is not the banner's open request"
    assert a.of("lease.tapped") == [] and world.hub.revokes == []
    assert a.svc.notify_holder(BID, None, seq=3, at=world.clock.t) == {"notified": False,
                                                                       "request": None}


def test_the_banner_is_the_oldest_unanswered_request(world, no_handover):
    a = world.holding()
    older = world.queued_by_hand(CAROL, age_s=60)
    world.queued_by_hand(BOB, age_s=30)
    no_handover()
    assert a.svc.notify_holder(BID, a.hub, seq=1, at=world.clock.t)["request"]["id"] == older.id


def test_in_the_requesters_session_the_tap_is_recorded_but_the_holder_is_told_elsewhere(
        world, no_handover):
    world.holding()
    b = world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=30)
    no_handover()
    out = b.svc.notify_holder(BID, b.hub, seq=3, at=world.clock.t)
    assert out == {"notified": False, "request": {"id": note.id, "by": BOB}}
    assert b.of("lease.tapped") == [{"id": note.id, "by": BOB, "at": iso_utc(world.clock.t)}]
    req = b.svc.view(b.hub)["request"]
    assert req["id"] == note.id and req["tapped_at"] == iso_utc(world.clock.t)
    assert writes(world, BOB) == [] and world.hub.current["holder"] == DAVID


def test_a_withdrawn_request_takes_its_tap_with_it(world, no_handover):
    a = world.holding()
    note = world.queued_by_hand(BOB, age_s=30)
    no_handover()
    a.svc.notify_holder(BID, a.hub, seq=1, at=world.clock.t)
    world.hub.notes.pop(note.id)
    world.clock.advance(11)
    assert a.svc.view(a.hub)["incoming"] == []
    again = world.queued_by_hand(BOB, age_s=0)                 # a new request: not tapped yet
    world.clock.advance(11)
    (inc,) = a.svc.view(a.hub)["incoming"]
    assert inc["id"] == again.id and inc["tapped_at"] is None


# --- PANEL-2: the beat's view costs no hub call ------------------------------------------------------


def test_cached_only_returns_the_last_view_without_a_hub_call(world):
    a = world.holding()
    world.queued_by_hand(BOB, age_s=30)
    n = len(world.hub.calls)
    assert a.svc.view(a.hub, cached_only=True) is None          # nothing built yet ...
    assert len(world.hub.calls) == n                            # ... and nothing was asked
    full = a.svc.view(a.hub)
    n = len(world.hub.calls)
    world.clock.advance(3600)                                    # far past the 10 s cache
    cached = a.svc.view(a.hub, cached_only=True)
    assert cached == full and len(world.hub.calls) == n          # no ssh on a beat
    cached["lease"]["holder"] = "someone@else"                   # a copy: the cache is intact
    assert a.svc.view(a.hub, cached_only=True)["lease"]["holder"] == DAVID
    # twin: the ordinary view reads the hub again once the 10 s have passed
    a.svc.view(a.hub)
    assert len(world.hub.calls) > n


def test_negative_twin_a_lease_change_or_age_drops_the_cached_view(world):
    a = world.holding()
    a.svc.view(a.hub)
    assert a.svc.view(a.hub, cached_only=True) is not None
    assert a.svc.view(a.hub, cached_only=True, max_age_s=5) is not None
    world.clock.advance(6)
    assert a.svc.view(a.hub, cached_only=True, max_age_s=5) is None      # too old for the caller
    a.svc.release(a.hub, board_id=BID)                                   # lease.state released
    assert a.svc.view(a.hub, cached_only=True) is None, "never a view from before the change"


def test_cached_only_without_a_hub_is_the_empty_view(world):
    a = world.session(DAVID)
    assert a.svc.view(None, cached_only=True) == a.svc.view(None)
    assert a.svc.view(None, cached_only=True)["hub"] is None


def test_a_tap_shows_in_the_cached_view_at_once(world, no_handover):
    a = world.holding()
    note = world.queued_by_hand(BOB, age_s=30)
    a.svc.view(a.hub)
    no_handover()
    a.svc.notify_holder(BID, a.hub, seq=4, at=world.clock.t)
    (inc,) = a.svc.view(a.hub, cached_only=True)["incoming"]
    assert inc["id"] == note.id and inc["tapped_at"] == iso_utc(world.clock.t)


def test_notify_holder_has_the_signature_presence_calls():
    """services/presence.py calls ``hook(board_id, hub, seq=..., at=...)`` through getattr."""
    import inspect

    params = inspect.signature(LeaseService.notify_holder).parameters
    assert list(params) == ["self", "board_id", "hub", "seq", "at"]
    assert params["seq"].kind is params["at"].kind is inspect.Parameter.KEYWORD_ONLY
    view = inspect.signature(LeaseService.view).parameters
    assert view["cached_only"].default is False and view["max_age_s"].default is None


def test_a_presence_beat_reads_the_hub_only_when_the_cached_view_is_missing_or_old(world):
    """daemon/panel_api.py's beat: the cached view while it is recent; else one ordinary read."""
    from harness_manager.daemon.panel_api import LEASE_VIEW_MAX_AGE_S, beat_lease_view

    a = world.holding()
    first = beat_lease_view(a.svc, a.hub)                       # nothing cached: one real read
    assert first["lease"]["holder"] == DAVID
    n = len(world.hub.calls)
    world.clock.advance(LEASE_VIEW_MAX_AGE_S - 1)
    assert beat_lease_view(a.svc, a.hub) == first and len(world.hub.calls) == n   # no ssh
    world.clock.advance(2)                                      # twin: too old, read again
    beat_lease_view(a.svc, a.hub)
    assert len(world.hub.calls) > n
    assert beat_lease_view(a.svc, None) is None and beat_lease_view(None, a.hub) is None
