"""LR-B against LR-A's REAL ``HubClient`` over the fake fpgahub 0.3.0 (tests/fakes/lr_hub.py).

The lease service drives the real client (ssh argv, fpgahub's text and JSON, the note
script's ops, revoke notes); only the runner is fake. The module refuses the default
runner factory, so no test can reach the real hub. Each check has a negative twin.
"""

from __future__ import annotations

import threading
import time

import pytest

from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    ExitCode,
    RefusedError,
)
from harness_manager.core.events import EventBus
from harness_manager.services.lease import (
    ForceTooEarlyError,
    LeaseService,
    force_reason,
)
from harness_manager_mps3 import hub as hubmod
from tests.fakes.lr_hub import TARGET, LrFakeHub
from tests.fakes.lrb_fake_hub import FakeClock, iso
from tests.fakes.lrb_rig import BID, Sess

HOST = "mapstone-dev.ecs.soton.ac.uk"
ALICE, BOB, CAROL = "alice@mapstone-dev", "bob@mapstone-dev", "carol@mapstone-dev"


@pytest.fixture(autouse=True)
def _no_real_hub(monkeypatch):
    def refuse(host, group):
        raise AssertionError(f"a test tried to reach the real hub {host}")

    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", refuse)


class Ref:
    def __init__(self, client: hubmod.HubClient) -> None:
        self.host, self.target, self.client = HOST, TARGET, client


class Lab:
    def __init__(self, tmp_path) -> None:
        self.tmp = tmp_path
        self.clock = FakeClock(t0=time.time())          # the fake hub's audit log runs on real time
        self.hub = LrFakeHub()
        self.sessions: list[Sess] = []

    def session(self, name: str, *, state: str = "") -> Sess:
        """``name`` is ``person`` or ``person-where`` (two sessions of one person)."""
        person = name.split("-")[0]
        bus = EventBus()
        svc = LeaseService(self.tmp / (state or name), bus, clock=self.clock.monotonic,
                           tick_s=3600.0, wall_clock=self.clock.wall(), sleep=self.clock.advance)
        client = hubmod.HubClient(HOST, TARGET, runner=self.hub.as_user(person))
        s = Sess(name, f"{person}@mapstone-dev", svc, Ref(client))
        bus.subscribe("lease.*", lambda ev: s.events.append((ev.topic, ev.board_id, dict(ev.data))))
        self.sessions.append(s)
        return s

    def holding(self, name: str, **kw) -> Sess:
        s = self.session(name, **kw)
        s.svc.acquire(s.hub, board_id=BID, holder="david-hm", heartbeat=False)
        s.svc.track(BID, s.hub, announced=True)
        return s

    def requests(self) -> list[str]:
        return [n for n in self.hub.notes() if n.startswith("req-")]

    def waiting(self) -> list[str]:
        return [w["holder"] for w in self.hub.waiters]

    def revoke_calls(self) -> list[list[str]]:
        return [c for c in self.hub.calls if c[:4] == ["fpgahub", "board", "lease", "revoke"]]

    def close(self) -> None:
        for s in self.sessions:
            s.svc.close()
        self.hub.close()


@pytest.fixture
def lab(tmp_path):
    lb = Lab(tmp_path)
    yield lb
    lb.close()


def test_real_client_the_holder_is_mine_by_principal(lab):
    alice = lab.holding("alice")
    v = alice.svc.view(alice.hub)
    assert v["lease"]["holder"] == ALICE and v["lease"]["mine"]
    assert "david-hm" in lab.hub.ignored_holders             # the name we asked for was ignored
    bob = lab.session("bob")                                  # twin: not bob's
    assert not bob.svc.view(bob.hub)["lease"]["mine"]


def test_real_client_request_then_release_then_granted(lab):
    alice, bob = lab.holding("alice"), lab.session("bob")

    def answer():
        alice.svc.watch_due(force=True)
        (wanted,) = alice.of("lease.wanted")
        assert wanted["by"] == BOB and wanted["message"] == "need the MCC"
        alice.svc.respond(BID, alice.hub, wanted["id"], "release", message="go ahead")

    lab.clock.after(15, answer)
    out = bob.svc.request(BID, bob.hub, message="need the MCC")
    assert out["lease"]["holder"] == BOB and out["lease"]["mine"]
    assert lab.hub.current["holder"] == BOB and lab.waiting() == []
    assert lab.requests() == []                               # withdrawn once held
    assert any(n.startswith("ans-") for n in lab.hub.notes())
    assert lab.revoke_calls() == []                           # twin: no force was needed


def test_real_client_keep_answer_round_trips(lab):
    alice, bob = lab.holding("alice"), lab.session("bob")
    got: dict = {}

    def keep():
        alice.svc.watch_due(force=True)
        alice.svc.respond(BID, alice.hub, alice.of("lease.wanted")[0]["id"], "keep", minutes=15,
                          message="demo at 3, then it's yours")

    def still_waiting():
        got["answered"] = list(bob.of("lease.answered"))
        got["queue"], got["notes"] = lab.waiting(), lab.requests()
        with pytest.raises(RefusedError) as exc:
            bob.svc.force(BID, bob.hub, confirm=True)
        got["err"] = exc.value
        (inc,) = alice.svc.view(alice.hub)["incoming"]
        got["incoming"] = inc
        bob.svc.leave(BID, bob.hub)

    lab.clock.after(15, keep)
    lab.clock.after(35, still_waiting)
    assert bob.svc.request(BID, bob.hub) == {"left": True}
    (ans,) = got["answered"]
    assert ans["minutes"] == 15 and ans["message"] == "demo at 3, then it's yours"
    assert got["queue"] == [BOB] and len(got["notes"]) == 1         # the place and note stayed
    assert got["err"].code == ExitCode.REFUSED and lab.revoke_calls() == []
    assert got["incoming"]["answer"]["minutes"] == 15              # D5, read from the hub
    assert lab.waiting() == [] and lab.requests() == []


def test_real_client_no_answer_then_force_and_the_victim_is_told_via_the_revoke_note(lab):
    alice, bob = lab.holding("alice"), lab.session("bob")
    t0 = lab.clock.t
    forced: dict = {}
    lab.clock.after(125, lambda: forced.update(bob.svc.force(BID, bob.hub, confirm=True,
                                                             confirm_board="mps3-01")))
    out = bob.svc.request(BID, bob.hub)
    reason = force_reason(BOB, iso(t0))
    assert out["lease"]["holder"] == BOB and forced["forced"]["holder"] == ALICE
    (rev,) = lab.hub.revocations
    assert rev["reason_arg"] == reason and rev["by"] == "unix:bob"
    # fpgahub 0.3.0's own target history never carries admin_revoked ...
    assert not any(e.get("event") == "lease.admin_revoked" and e.get("board") == TARGET
                   for e in lab.hub.audit)
    alice.svc.beat_due(force=True)                            # ... so the revoke note tells alice
    assert alice.states()[-1] == "lost"
    (taken,) = alice.of("lease.taken")
    assert taken["by"] == BOB and taken["reason"] == reason and taken["at"]
    assert alice.svc.view(alice.hub)["taken"] == taken


def test_real_client_negative_twin_an_expired_lease_promoted_to_a_requester_is_not_taken(lab):
    alice, bob = lab.holding("alice"), lab.session("bob")
    lab.clock.after(15, lab.hub.expire)                       # the TTL ran out; bob is promoted
    out = bob.svc.request(BID, bob.hub)
    assert out["lease"]["holder"] == BOB
    alice.svc.beat_due(force=True)
    assert alice.states()[-1] == "lost" and alice.of("lease.taken") == []


def test_real_client_force_refused_before_the_deadline_revokes_nothing(lab):
    lab.holding("alice")
    bob = lab.session("bob")
    got: dict = {}

    def too_early():
        with pytest.raises(ForceTooEarlyError) as exc:
            bob.svc.force(BID, bob.hub, confirm=True)
        got["err"] = exc.value
        bob.svc.leave(BID, bob.hub)

    lab.clock.after(35, too_early)                            # runs at t0+40
    assert bob.svc.request(BID, bob.hub) == {"left": True}
    assert got["err"].code == ExitCode.UNAVAILABLE and got["err"].time_left_s == 80
    assert got["err"].data["deadline_at"].endswith("+00:00")    # D3/D8
    assert lab.revoke_calls() == [] and lab.hub.current["holder"] == ALICE


def test_real_client_leave_cancels_by_principal_and_withdraws_the_note(lab):
    lab.holding("alice")
    bob = lab.session("bob")
    left: dict = {}
    lab.clock.after(15, lambda: left.update(bob.svc.leave(BID, bob.hub)))
    assert bob.svc.request(BID, bob.hub) == {"left": True}
    assert left == {"left": True} and lab.waiting() == [] and lab.requests() == []
    assert ["fpgahub", "lease", "cancel", TARGET, "--holder", BOB] in lab.hub.calls
    assert bob.of("lease.left") == [{}]
    assert bob.svc.leave(BID, bob.hub) == {"left": False}    # twin


def test_real_client_same_principal_in_two_sessions_is_refused(lab):
    lab.holding("bob-desk")
    laptop = lab.session("bob-laptop")
    with pytest.raises(AlreadyError) as exc:
        laptop.svc.request(BID, laptop.hub)
    assert "another session" in exc.value.message
    assert lab.waiting() == [] and lab.requests() == []
    carol = lab.session("carol")                              # twin: another person queues
    lab.clock.after(15, lambda: carol.svc.leave(BID, carol.hub))
    assert carol.svc.request(BID, carol.hub) == {"left": True}
    cancels = [c for c in lab.hub.calls if c[:3] == ["fpgahub", "lease", "cancel"]]
    assert cancels[-1][-1] == CAROL


def test_real_client_l1_acquire_abort_cancels_by_principal(lab):
    """CCR-A3: L1's cancel-on-abort passed the holder it ASKED for; over the hub's unix
    socket we are an admin, whose --holder is taken literally, so it cancelled nothing."""
    lab.holding("alice")
    bob = lab.session("bob")
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    with pytest.raises(ActionFailedError) as exc:
        bob.svc.acquire(bob.hub, board_id=BID, holder="david-hm", poll_s=0.02, cancel=cancel,
                        heartbeat=False)
    assert "queue entry was removed" in exc.value.message
    assert lab.waiting() == []
    assert ["fpgahub", "lease", "cancel", TARGET, "--holder", BOB] in lab.hub.calls
    # twin: what L1 used to send removes nothing
    lab.hub.waiters.append({"holder": BOB, "user": "bob", "ttl": 3600})
    assert bob.hub.client.lease_cancel("david-hm") is False and lab.waiting() == [BOB]
