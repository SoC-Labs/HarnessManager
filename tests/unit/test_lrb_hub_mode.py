"""LR-B: hub mode (T8). The lease service hears fpgahub's event stream (CCR T8-4) and honours
the REST client's limits (no note store; force needs an admin token). Fakes only.

The ``hub.event`` payloads below are shaped exactly as ``tests/fakes/t8_hub_rest.py``
emits them for a revoke (``lease.revoked`` with ``holder`` and ``reason``, then
``lease.admin_revoked`` with ``by``, ``reason``, ``prior_holder``), as
``transports/hub_events.attach_bus`` wraps them: ``{type, ts, target, board, data}``.
"""

from __future__ import annotations

import pytest

from harness_manager.core.errors import UnavailableError
from harness_manager.core.events import Event
from harness_manager.services.lease import ForceRefusedError, force_reason
from tests.fakes.lrb_fake_hub import TARGET, iso
from tests.fakes.lrb_rig import BID, BOB, CAROL, DAVID, World


@pytest.fixture(autouse=True)
def _no_real_hub(monkeypatch):
    from harness_manager_mps3 import hub as hubmod

    def refuse(host, group):
        raise AssertionError(f"a test tried to reach the real hub {host}")

    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", refuse)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def hub_event(etype: str, ts: str, **data) -> Event:
    return Event("hub.event", BID, {"type": etype, "ts": ts, "target": TARGET, "board": "mps3_01",
                                    "data": data})


def revoke_events(prior: str, reason: str, actor: str, ts: str) -> list[Event]:
    full = f"{reason} (by {actor})"
    return [hub_event("lease.revoked", ts, board=TARGET, holder=prior, user=prior.split("@")[0],
                      tier="interactive", reason=full),
            hub_event("lease.released", ts, board=TARGET, holder=prior, chassis="mps3_01"),
            hub_event("lease.admin_revoked", ts, by=actor, reason=full, members=[TARGET],
                      prior_holder=prior, prior_user=prior.split("@")[0], chassis="mps3_01")]


# --- CCR T8-4: the event stream ------------------------------------------------------------------


def test_a_streamed_revoke_settles_the_lease_at_once_and_says_who(world):
    a = world.holding()
    reason = force_reason(BOB, iso(world.clock.t - 200))
    ts = "2026-09-24T12:00:00.123456+00:00"
    for ev in revoke_events(DAVID, reason, "unix:bob", ts):
        a.svc.on_hub_event(ev)
    assert a.states()[-1] == "lost" and a.svc.store.get(a.hub.host, TARGET) is None
    assert a.svc.tracked() == []
    assert a.of("lease.taken") == [{"by": BOB, "reason": f"{reason} (by unix:bob)",
                                    "at": "2026-09-24T12:00:00+00:00"}]     # once, D8 form
    assert world.calls(DAVID, "lease_heartbeat") == []          # no heartbeat needed
    assert a.svc.view(a.hub)["taken"]["by"] == BOB


def test_an_admin_kick_names_the_admin(world):
    a = world.holding()
    for ev in revoke_events(DAVID, "maintenance", "token:ops", "2026-09-24T12:00:00Z"):
        a.svc.on_hub_event(ev)
    (taken,) = a.of("lease.taken")
    assert taken["by"] == "token:ops" and taken["reason"] == "maintenance (by token:ops)"


def test_negative_twin_a_streamed_revoke_of_someone_elses_lease_changes_nothing(world):
    a = world.holding()
    n = len(world.calls(DAVID, "lease_status"))
    a.svc.view(a.hub)
    for ev in revoke_events(CAROL, "x", "unix:bob", "2026-09-24T12:00:00Z"):
        a.svc.on_hub_event(ev)
    assert a.of("lease.taken") == [] and "lost" not in a.states()
    assert a.svc.store.get(a.hub.host, TARGET) is not None
    a.svc.view(a.hub)                                           # but the cached view was dropped
    assert len(world.calls(DAVID, "lease_status")) == n + 2
    b = world.session(BOB)                                      # an untracked board: ignored
    b.svc.on_hub_event(revoke_events(BOB, "x", "unix:x", "2026-09-24T12:00:00Z")[0])
    assert b.events == []


def test_a_streamed_expiry_heartbeats_now(world):
    a = world.holding()
    world.hub.expire()
    a.svc.on_hub_event(hub_event("lease.expired", "2026-09-24T12:00:00Z", board=TARGET,
                                 holder=DAVID, user="david"))
    assert a.states()[-1] == "expired" and world.calls(DAVID, "lease_heartbeat")
    assert a.of("lease.taken") == []                            # twin: expired is not taken


def test_the_stream_fills_in_a_reason_the_history_could_not_give(world):
    a = world.holding()
    a.svc.store.put_taken(a.hub.host, TARGET, {"by": BOB, "reason": "", "at": iso(world.clock.t)})
    a.svc.store.drop(a.hub.host, TARGET)                        # the heartbeat settled it first
    ev = revoke_events(DAVID, "because", "unix:bob", iso(world.clock.t))[2]
    a.svc.on_hub_event(ev)
    assert a.svc.view(a.hub)["taken"]["reason"] == "because (by unix:bob)"
    a.svc.on_hub_event(ev)                                      # twin: nothing more to add
    assert len(a.of("lease.taken")) == 1


def test_forget_drops_the_cached_view(world):
    a = world.holding()
    a.svc.view(a.hub)
    n = len(world.calls(DAVID, "lease_status"))
    a.svc.view(a.hub)
    assert len(world.calls(DAVID, "lease_status")) == n         # twin: cached
    a.svc.forget(a.hub)
    a.svc.view(a.hub)
    assert len(world.calls(DAVID, "lease_status")) == n + 1


def test_a_broken_event_never_breaks_the_bus(world):
    a = world.holding()
    a.svc.on_hub_event(Event("hub.event", BID, {"type": "lease.revoked", "data": "not a dict"}))
    a.svc.on_hub_event(Event("hub.event", BID, {}))
    assert a.svc.store.get(a.hub.host, TARGET) is not None


# --- the REST client's limits -------------------------------------------------------------------


def _rest(sess, *, admin: bool) -> None:
    """Make a session's client look like T8's RestHubClient: no notes; revoke if admin."""
    c = sess.hub.client
    c.notes_supported = False
    c.notes_reason = "this hub is reached over its REST API, which has no note store"
    c.can_revoke = (lambda: (True, "")) if admin else (
        lambda: (False, "force-release needs an admin credential on hub:8000; this token is write"))


def test_rest_keep_is_unavailable_and_release_writes_no_note(world):
    a = world.holding()
    _rest(a, admin=False)
    note = world.queued_by_hand(BOB, age_s=10)
    with pytest.raises(UnavailableError) as exc:
        a.svc.respond(BID, a.hub, note.id, "keep", minutes=5)
    assert "REST" in exc.value.reason and world.calls(DAVID, "put_answer") == []
    out = a.svc.respond(BID, a.hub, note.id, "release")        # twin: release still works
    assert out["released"] and world.hub.current["holder"] == BOB
    assert world.calls(DAVID, "put_answer") == []


def test_rest_force_needs_an_admin_token(world):
    world.holding()
    b = world.session(BOB)
    _rest(b, admin=False)
    world.queued_by_hand(BOB, age_s=200)                       # deadline passed, at the head
    req = b.svc.view(b.hub)["request"]
    assert not req["force_available"] and "admin credential" in req["force_reason"]
    with pytest.raises(ForceRefusedError) as exc:
        b.svc.force(BID, b.hub, confirm=True)
    assert "admin credential" in exc.value.message and world.hub.revokes == []
    _rest(b, admin=True)                                       # twin: an admin token may
    world.clock.advance(11)
    assert b.svc.view(b.hub)["request"]["force_available"]
    assert b.svc.force(BID, b.hub, confirm=True)["lease"]["holder"] == BOB


def test_rest_without_admin_the_wait_never_offers_force(world):
    world.holding()
    b = world.session(BOB)
    _rest(b, admin=False)
    world.clock.after(145, lambda: b.svc.leave(BID, b.hub))
    assert b.svc.request(BID, b.hub) == {"left": True}
    assert b.of("lease.force_available") == []


# --- CCR-1: what the hub connection can do, in the view -------------------------------------------


def test_the_view_says_what_the_hub_connection_can_do(world):
    a = world.holding()
    v = a.svc.view(a.hub)                                      # twin: SSH, everything works
    assert (v["notes_supported"], v["notes_reason"], v["can_revoke"], v["revoke_reason"]) == (
        True, "", True, "")
    _rest(a, admin=False)
    world.clock.advance(11)
    v = a.svc.view(a.hub)
    assert v["notes_supported"] is False and "no note store" in v["notes_reason"]
    assert v["can_revoke"] is False and "admin credential" in v["revoke_reason"]
    assert v["request"] is None                                # said before any request or deadline
    _rest(a, admin=True)
    v = a.svc.view(a.hub)
    assert v["can_revoke"] is True and v["revoke_reason"] == "" and v["notes_supported"] is False


def test_a_hub_that_cannot_say_its_role_cannot_revoke(world):
    from harness_manager.core.errors import UnreachableError

    a = world.holding()

    def fails():
        raise UnreachableError("GET /whoami: connection refused")

    a.hub.client.can_revoke = fails
    v = a.svc.view(a.hub)
    assert v["can_revoke"] is False and "connection refused" in v["revoke_reason"]


# --- CCR-1 over T8's REST fake (the real RestHubClient; 127.0.0.1 only) -------------------------


def test_rest_client_capabilities_reach_the_view(tmp_path):
    from harness_manager.services.lease import LeaseService
    from tests.fakes.t8_hub_rest import FakeFpgahub, client_for

    class Ref:
        def __init__(self, client):
            self.host, self.target, self.client = "hub.test", "mps3_01_pl", client

    hub = FakeFpgahub().start()
    svc = LeaseService(tmp_path / "s", tick_s=3600.0)
    try:
        writer = Ref(client_for(hub, hub.add_token("alice", "write")))
        v = svc.view(writer)
        assert v["notes_supported"] is False and v["notes_reason"]
        assert v["can_revoke"] is False and "admin" in v["revoke_reason"]
        assert v["board"] == "mps3_01"
        admin = Ref(client_for(hub, hub.add_token("david", "admin")))     # twin: an admin token
        svc2 = LeaseService(tmp_path / "s2", tick_s=3600.0)
        try:
            v = svc2.view(admin)
            assert v["can_revoke"] is True and v["revoke_reason"] == ""
            assert v["notes_supported"] is False                         # REST all the same
        finally:
            svc2.close()
    finally:
        svc.close()
        hub.close()
