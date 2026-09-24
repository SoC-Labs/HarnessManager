"""W5 / D12: force-releasing a holder no Harness Manager session is known to be needs the
board's name typed (docs/LEASE_REQUESTS.md D12). Scripts (the B1 runner, soaks, proof
scripts) hold leases through pyverify and never answer a request, so after 2 minutes
Harness Manager's force would kick them.

The one evidence that a Harness Manager session holds the lease is an answer to our
request: ``respond()`` writes one only from the session holding the lease's token.
Fakes only; each rule with a negative twin. Nothing here reaches a hub.
"""

from __future__ import annotations

import pytest

from harness_manager.core.errors import ExitCode, RefusedError, UsageError
from harness_manager.services.lease import (
    HOLDER_HM,
    HOLDER_UNKNOWN,
    AnswerNote,
    confirm_board_error,
    holder_kind,
    typed_names,
    view_confirm_error,
)
from tests.fakes.lrb_rig import BID, BOB, CAROL, DAVID, World

NAME = "mps3-01"
TARGET = "mps3_01_pl"
NAMES = typed_names(NAME, "mps3_01", TARGET, "192.168.10.101:6900")


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


# --- the rule, as functions --------------------------------------------------------------------


def test_only_an_answer_to_our_request_or_holding_it_here_says_hm():
    keep = AnswerNote(id="r1", answer="keep", minutes=15, message="", at="2026-09-24T10:00:00Z")
    assert holder_kind(keep) == (HOLDER_HM, "the holder answered your request from Harness "
                                            "Manager (keep 15 min, at 2026-09-24T10:00:00+00:00)")
    assert holder_kind({"answer": "release", "minutes": 0, "at": ""})[0] == HOLDER_HM   # view dict
    assert holder_kind(None, here=True)[0] == HOLDER_HM
    # twins: no answer, no request, notes off, or merely our principal: nobody can say
    kind, why = holder_kind(None)
    assert kind == HOLDER_UNKNOWN and "a script (a soak or runner)" in why
    assert "you have not asked" in holder_kind(None, asked=False)[1]
    assert "no request notes" in holder_kind(None, notes_ok=False)[1]
    kind, why = holder_kind(keep, mine=True)
    assert kind == HOLDER_UNKNOWN and "a script you run" in why, \
        "our own principal is no evidence: scripts lease under their owner's name@host"
    assert holder_kind({"answer": "maybe"})[0] == HOLDER_UNKNOWN


def test_the_names_to_type_put_the_one_to_ask_for_first_and_dedupe_by_case():
    assert NAMES == ["mps3-01", "mps3_01", "192.168.10.101:6900", TARGET]
    assert typed_names("", "mps3_01", TARGET)[0] == "mps3-01"      # no N1 name: the hub's board
    assert typed_names("", None, TARGET) == [TARGET]
    assert typed_names("MPS3-01", "mps3_01", TARGET)[:2] == ["MPS3-01", "mps3_01"]


def test_an_unknown_holder_needs_the_name_missing_is_usage_wrong_is_refused():
    err = confirm_board_error(HOLDER_UNKNOWN, "why", None, NAMES, TARGET)
    assert isinstance(err, UsageError) and err.code == ExitCode.USAGE          # HTTP 400
    assert err.data == {"holder_kind": "unknown", "holder_kind_reason": "why",
                        "confirm_board": NAME}
    assert "--confirm-board mps3-01" in err.hint
    assert isinstance(confirm_board_error(HOLDER_UNKNOWN, "why", "  ", NAMES, TARGET), UsageError)
    assert isinstance(confirm_board_error(HOLDER_UNKNOWN, "why", 1, NAMES, TARGET), UsageError)
    err = confirm_board_error(HOLDER_UNKNOWN, "why", "mps3-02", NAMES, TARGET)
    assert isinstance(err, RefusedError) and err.code == ExitCode.REFUSED       # HTTP 409
    # twins: any of the board's names, in any case, with spaces around it
    for ok in ("mps3-01", " MPS3-01 ", "mps3_01", "192.168.10.101:6900", TARGET):
        assert confirm_board_error(HOLDER_UNKNOWN, "why", ok, NAMES, TARGET) is None, ok


def test_an_hm_holder_needs_no_name_but_a_wrong_one_is_still_refused():
    assert confirm_board_error(HOLDER_HM, "", None, NAMES, TARGET) is None
    assert confirm_board_error(HOLDER_HM, "", "", NAMES, TARGET) is None
    assert confirm_board_error(HOLDER_HM, "", NAME, NAMES, TARGET) is None
    assert isinstance(confirm_board_error(HOLDER_HM, "", "mps3-02", NAMES, TARGET), RefusedError)


def test_a_view_without_holder_kind_counts_as_unknown():
    """An older service (before D12) never lets a nameless force through."""
    assert isinstance(view_confirm_error({"lease": {"holder": "x"}}, None, NAMES, TARGET), UsageError)
    assert view_confirm_error({"lease": {"holder_kind": "hm"}}, None, NAMES, TARGET) is None


# --- the service ---------------------------------------------------------------------------------


def test_force_of_a_holder_that_never_answered_needs_the_name(world):
    """david's lease, and nobody answers bob (the soak case): no name, a wrong name, then the
    right one."""
    world.holding()
    b = world.session(BOB)
    world.queued_by_hand(BOB, age_s=200)                      # the deadline has passed
    lease = b.svc.view(b.hub)["lease"]
    assert (lease["holder"], lease["holder_kind"]) == (DAVID, "unknown")
    with pytest.raises(UsageError) as exc:
        b.svc.force(BID, b.hub, confirm=True)
    assert exc.value.data["confirm_board"] == NAME and "a script" in exc.value.message
    with pytest.raises(RefusedError) as exc:
        b.svc.force(BID, b.hub, confirm=True, confirm_board="mps3-02")
    assert "is not this board's name" in exc.value.message
    assert world.hub.revokes == [] and world.hub.current["holder"] == DAVID
    out = b.svc.force(BID, b.hub, confirm=True, confirm_board=NAME)             # twin
    assert out["lease"]["holder"] == BOB and len(world.hub.revokes) == 1


def test_the_caller_names_come_first(world):
    """The daemon passes the N1 name and the address; the service adds the hub's board."""
    world.holding()
    b = world.session(BOB)
    world.queued_by_hand(BOB, age_s=200)
    with pytest.raises(UsageError) as exc:
        b.svc.force(BID, b.hub, confirm=True, board_names=("lab-left", "192.168.10.101:6900"))
    assert exc.value.data["confirm_board"] == "lab-left"
    out = b.svc.force(BID, b.hub, confirm=True, confirm_board="192.168.10.101:6900",
                      board_names=("lab-left", "192.168.10.101:6900"))
    assert out["lease"]["holder"] == BOB


def test_negative_twin_a_holder_that_answered_is_forced_with_the_plain_confirm(world):
    a, b = world.holding(), world.session(BOB)
    note = world.queued_by_hand(BOB, age_s=200)
    a.svc.respond(BID, a.hub, note.id, "keep", minutes=5, message="one more run")
    world.clock.advance(300)                                  # the keep runs out
    world.clock.advance(11)                                   # past the view cache
    lease = b.svc.view(b.hub)["lease"]
    assert lease["holder_kind"] == "hm" and "keep 5 min" in lease["holder_kind_reason"]
    out = b.svc.force(BID, b.hub, confirm=True)
    assert out["lease"]["holder"] == BOB and len(world.hub.revokes) == 1


def test_a_new_holder_is_unknown_again_until_it_answers(world):
    """D9: the board passed to carol, who was never asked: david's answer says nothing about
    her, so her lease is unknown and forcing it (after her deadline) needs the name."""
    a, b, c = world.holding(), world.session(BOB), world.session(CAROL)
    carol = world.queued_by_hand(CAROL, age_s=300)
    note = world.queued_by_hand(BOB, age_s=200)
    a.svc.respond(BID, a.hub, note.id, "keep", minutes=5)
    a.svc.respond(BID, a.hub, carol.id, "release")            # carol (the head) gets it
    c.svc.acquire(c.hub, board_id=BID, heartbeat=False)
    world.hub.notes.pop(note.id, None)
    fresh = world.queued_by_hand(BOB, age_s=130)              # the request, re-sent to carol
    world.clock.advance(11)
    lease = b.svc.view(b.hub)["lease"]
    assert lease["holder"] == CAROL and lease["holder_kind"] == "unknown"
    with pytest.raises(UsageError):
        b.svc.force(BID, b.hub, confirm=True)
    assert world.hub.revokes == []
    c.svc.track(BID, c.hub, announced=True)                   # twin: carol answers keep ...
    c.svc.respond(BID, c.hub, fresh.id, "keep", minutes=5)
    world.clock.advance(311)                                  # ... and it runs out
    assert b.svc.view(b.hub)["lease"]["holder_kind"] == "hm"
    assert b.svc.force(BID, b.hub, confirm=True)["lease"]["holder"] == BOB


def test_the_holders_own_session_is_hm_another_of_its_sessions_is_unknown(world):
    a = world.holding()
    assert a.svc.view(a.hub)["lease"]["holder_kind"] == "hm"
    other = world.session(DAVID, name="david-laptop")          # same principal, no token here
    lease = other.svc.view(other.hub)["lease"]
    assert lease["mine"] and lease["holder_kind"] == "unknown"
    assert "under your hub name" in lease["holder_kind_reason"]
