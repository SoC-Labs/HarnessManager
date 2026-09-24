"""LR-A: the HubClient additions against the fake fpgahub 0.3.0 (tests/fakes/lr_hub.py).

Each check has a twin. Nothing here reaches a hub: every client gets a fake runner, and the
module refuses the default runner factory (which would ssh to the real hub).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from harness_manager.core.errors import (
    AbsentError,
    ExitCode,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager_mps3 import hub as hubmod
from tests.fakes.lr_hub import BOARD, TARGET, LrFakeHub, two_sessions

HOST = "mapstone-dev.ecs.soton.ac.uk"
ALICE, BOB, CAROL = "alice@mapstone-dev", "bob@mapstone-dev", "carol@mapstone-dev"
REASON = "force-released by bob@mapstone-dev via Harness Manager: no answer to a request made at X"


@pytest.fixture(autouse=True)
def _no_real_hub(monkeypatch):
    def refuse(host, group):
        raise AssertionError(f"a test tried to reach the real hub {host}")

    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", refuse)


@pytest.fixture
def s():
    sessions = two_sessions()
    yield sessions
    sessions.hub.close()


def fpgahub_calls(hub: LrFakeHub, *words: str) -> list[list[str]]:
    return [c for c in hub.calls if c[:1] == ["fpgahub"] and c[1:1 + len(words)] == list(words)]


# -- principal ---------------------------------------------------------------------------------


def test_principal_is_the_hubs_name_for_us_asked_once(s):
    assert s.holder.principal() == ALICE and s.requester.principal() == BOB
    assert s.holder.principal() == ALICE
    assert len(fpgahub_calls(s.hub, "whoami")) == 2                  # one per client, then cached
    assert s.requester.whoami()["audit_id"] == "unix:bob"


def test_negative_twin_the_holder_we_ask_for_is_not_what_the_hub_records(s):
    """Today's bug: HM saved 'harness-manager-bob@srv03335'; the hub records bob@mapstone-dev."""
    s.hub.expire()
    lease, _ = s.requester.lease_acquire("harness-manager-bob@srv03335", ttl=600)
    assert s.requester.lease_status().holder == BOB != lease.holder
    assert s.hub.ignored_holders == ["harness-manager-bob@srv03335"]


def test_principal_fails_loudly_on_a_bad_whoami(s):
    s.hub.override(("whoami",), stdout="name    bob\n")
    with pytest.raises(UnreachableError):
        s.requester.principal()
    s.hub.override(("whoami",), rc=2, stderr="Error: No such command 'whoami'.\n")
    with pytest.raises(HarnessError):
        s.requester.principal()
    assert s.requester.principal() == BOB                           # nothing bad was cached


# -- lease_status ------------------------------------------------------------------------------


def test_lease_status_shows_the_holder_and_the_queue_in_one_call(s):
    s.hub.as_user("bob")(["fpgahub", "lease", "acquire", TARGET, "--ttl", "600"])
    s.hub.as_user("carol")(["fpgahub", "lease", "acquire", TARGET, "--ttl", "600"])
    before = len(s.hub.calls)
    st = s.requester.lease_status()
    assert len(s.hub.calls) == before + 1
    assert (st.held, st.holder, st.user) == (True, ALICE, "alice")
    assert [(q.position, q.holder, q.user) for q in st.queue] == [(1, BOB, "bob"), (2, CAROL, "carol")]
    assert st.head.holder == BOB and st.expires_at.endswith("Z")


def test_negative_twin_lease_status_free_and_ascii_box_and_garbage():
    hub = LrFakeHub(ascii_box=True)
    c = hubmod.HubClient(HOST, TARGET, runner=hub)
    assert c.lease_status() == hubmod.LeaseStatus(False, "", "", "", ())
    hub.grant("alice")
    hub.as_user("bob")(["fpgahub", "lease", "acquire", TARGET])
    assert c.lease_status().queue == (hubmod.QueueEntry(1, BOB, "bob"),)
    hub.override(("lease", "show"), stdout="")
    with pytest.raises(UnreachableError):
        c.lease_status()
    hub.override(("lease", "show"), rc=1, stderr="GET /targets/mps3_01_pl/lease → HTTP 404: no such board\n")
    with pytest.raises(AbsentError):
        c.lease_status()


def test_leaving_the_queue_needs_the_principal_not_the_stored_holder(s):
    s.hub.as_user("bob")(["fpgahub", "lease", "acquire", TARGET])
    assert s.requester.lease_cancel("harness-manager-bob@srv03335") is False     # the twin
    assert s.requester.lease_status().position_of(BOB) == 1
    assert s.requester.lease_cancel(s.requester.principal()) is True
    assert s.requester.lease_status().queue == ()


# -- board_id ------------------------------------------------------------------------------------


def test_board_id_comes_from_fpgahub_and_is_cached(s):
    assert s.requester.board_id() == BOARD and s.requester.board_id() == BOARD
    assert len(fpgahub_calls(s.hub, "board", "list")) == 1


def test_negative_twin_board_id_from_boards_toml_asks_nobody(s, monkeypatch):
    cfg = hubmod.parse_hub_table({"host": HOST, "target": TARGET, "board": "mps3_07"})
    assert cfg.board == "mps3_07"
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", lambda host, group: s.hub)
    client = hubmod.Mps3Hub(cfg).client             # the pack's own construction passes it on
    assert client.board == "mps3_07" and client.board_id() == "mps3_07"
    assert fpgahub_calls(s.hub, "board", "list") == []


@pytest.mark.parametrize("bad", ["../mps3_01", "mps3 01", "", 7])
def test_boards_toml_board_must_be_a_board_id(bad):
    if bad == "":
        assert hubmod.parse_hub_table({"host": HOST, "board": bad}).board == ""
        return
    with pytest.raises(UsageError):
        hubmod.parse_hub_table({"host": HOST, "board": bad})


def test_board_id_for_a_target_no_board_owns_is_absent():
    hub = LrFakeHub(target="mps3_02_pl", board="mps3_01", members=["mps3_01_pl"])
    hub.members.remove("mps3_02_pl")
    with pytest.raises(AbsentError) as exc:
        hubmod.HubClient(HOST, "mps3_02_pl", runner=hub).board_id()
    assert "hub.board" in exc.value.hint
    hub.override(("board", "list"), stdout="Boards\n")
    with pytest.raises(UnreachableError):
        hubmod.HubClient(HOST, "mps3_02_pl", runner=hub).board_id()


# -- lease_revoke ---------------------------------------------------------------------------------


def test_revoke_kicks_the_holder_promotes_the_head_and_leaves_a_note(s):
    s.hub.as_user("bob")(["fpgahub", "lease", "acquire", TARGET, "--ttl", "900"])
    out = s.requester.lease_revoke(REASON)
    assert out["revoked"] == [TARGET] and out["by"] == "unix:bob" and out["board"] == BOARD
    assert out["prior_holder"] == ALICE and out["principal"] == BOB
    assert ["fpgahub", "board", "lease", "revoke", BOARD, "--reason", REASON, "--yes"] in s.hub.calls
    assert s.requester.lease_status().holder == BOB                   # the head got it
    assert s.hub.revocations[0]["reason"] == f"{REASON} (by unix:bob)"
    rev = [n for n in s.hub.notes() if n.startswith("rev-")]
    assert len(rev) == 1
    # the victim finds out who and why
    taken = hubmod.taken_from_history(s.holder.lease_history(), ALICE)
    assert taken is not None and taken["by"] == BOB and taken["reason"] == REASON


def test_negative_twin_revoke_with_nobody_holding_it_does_nothing(s):
    s.hub.expire()
    out = s.requester.lease_revoke(REASON)
    assert out["revoked"] == [] and out["prior_holder"] == ""
    assert fpgahub_calls(s.hub, "board", "lease", "revoke") == []
    assert s.hub.notes() == {}


def test_revoke_refuses_our_own_lease(s):
    with pytest.raises(RefusedError):
        s.holder.lease_revoke(REASON)
    assert fpgahub_calls(s.hub, "board", "lease", "revoke") == []


def test_revoke_refuses_when_it_would_kick_someone_on_another_member():
    hub = LrFakeHub(members=[TARGET, "mps3_01_mcc"])
    hub.grant("alice")
    hub.others["mps3_01_mcc"] = {"holder": CAROL, "user": "carol", "expires_at": "x"}
    bob = hubmod.HubClient(HOST, TARGET, runner=hub.as_user("bob"))
    with pytest.raises(RefusedError) as exc:
        bob.lease_revoke(REASON)
    assert "mps3_01_mcc" in str(exc.value) and hub.revocations == []
    hub.others["mps3_01_mcc"] = {"holder": ALICE, "user": "alice", "expires_at": "x"}    # the twin
    assert sorted(bob.lease_revoke(REASON)["revoked"]) == ["mps3_01_mcc", TARGET]


def test_a_non_admin_is_refused_by_the_hub_and_the_note_is_withdrawn():
    hub = LrFakeHub()
    hub.grant("alice")
    tok = hubmod.HubClient(HOST, TARGET, runner=hub.as_user("bob", role="write", kind="token"))
    with pytest.raises(RefusedError) as exc:
        tok.lease_revoke(REASON)
    assert exc.value.code == ExitCode.REFUSED and "admin" in str(exc.value)
    assert hub.notes() == {} and hub.current["holder"] == ALICE


def test_revoke_output_we_cannot_read_keeps_the_note(s):
    s.hub.override(("board", "lease", "revoke"), stdout="something new\n")
    with pytest.raises(UnreachableError):
        s.requester.lease_revoke(REASON)
    assert [n for n in s.hub.notes() if n.startswith("rev-")]         # it may have run: keep it


def test_revoke_goes_ahead_without_a_note_when_the_note_dir_is_unwritable(s):
    s.hub.notes_unwritable = True
    s.hub.as_user("bob")(["fpgahub", "lease", "acquire", TARGET])
    assert s.requester.lease_revoke(REASON)["revoked"] == [TARGET]
    taken = hubmod.taken_from_history(s.holder.lease_history(), ALICE)
    assert taken == {"by": BOB, "reason": "", "at": taken["at"]}      # fpgahub alone: no reason


@pytest.mark.parametrize("reason", ["", "a\nb", "x" * 600, "\x1b]0;pwned\x07"])
def test_revoke_reasons_are_checked_before_anything_runs(s, reason):
    before = len(s.hub.calls)
    with pytest.raises(UsageError):
        s.requester.lease_revoke(reason)
    assert len(s.hub.calls) == before


def test_a_reason_with_shell_syntax_is_one_argv_word(s):
    hostile = "no answer'; fpgahub board lease revoke kr260_01 --yes; echo '$(id) `id`"
    s.requester.lease_revoke(hostile)
    assert s.hub.revocations[0]["reason_arg"] == hostile
    assert [c for c in s.hub.calls if "kr260_01" in c] == []


# -- lease_history ---------------------------------------------------------------------------------


def test_history_is_oldest_first_with_the_revoke_notes_merged(s):
    s.hub.as_user("bob")(["fpgahub", "lease", "acquire", TARGET])
    s.requester.lease_revoke(REASON)
    hist = s.holder.lease_history()
    kinds = [e["event"] for e in hist]
    fpgahubs = [r["event"] for r in s.hub.audit if r.get("board") == TARGET]
    assert [k for k in kinds if k != hubmod.ADMIN_REVOKED] == fpgahubs     # its order, untouched
    assert fpgahubs[:2] == ["lease.acquired", "lease.queued"]
    assert kinds.count(hubmod.ADMIN_REVOKED) == 1
    assert kinds.index(hubmod.ADMIN_REVOKED) > kinds.index("lease.queued")  # stamped after it
    assert all("reason" not in e for e in hist if e["event"] == "lease.revoked")   # 0.3.0 drops it
    assert hist == s.holder.lease_history()                                 # the same each time
    assert hubmod.ADMIN_REVOKED not in [r["event"] for r in s.hub.audit if r.get("board")]
    assert len(s.holder.lease_history(limit=2)) == 2


class _CoarseDatetime(datetime):
    """``datetime`` with Windows' clock: it moves in 15.625 ms steps."""

    @classmethod
    def now(cls, tz=None):
        t = time.time()
        return datetime.fromtimestamp(t - t % 0.015625, tz)


def test_the_merge_is_the_same_on_a_coarse_clock(monkeypatch):
    """CI on Windows: the note sorted before 'lease.queued' (the fake ran its clock ahead)."""
    monkeypatch.setattr(hubmod, "datetime", _CoarseDatetime)
    for _ in range(5):
        hub = LrFakeHub(clock=lambda: _CoarseDatetime.now(timezone.utc))
        alice = hubmod.HubClient(HOST, TARGET, runner=hub.as_user("alice"))
        bob = hubmod.HubClient(HOST, TARGET, runner=hub.as_user("bob"))
        hub.grant("alice")
        hub.as_user("bob")(["fpgahub", "lease", "acquire", TARGET])
        bob.lease_revoke(REASON)
        kinds = [e["event"] for e in alice.lease_history()]
        assert [k for k in kinds if k != hubmod.ADMIN_REVOKED] == [
            r["event"] for r in hub.audit if r.get("board") == TARGET]
        assert kinds.index(hubmod.ADMIN_REVOKED) > kinds.index("lease.queued")
        stamps = [r["ts"] for r in hub.audit]
        assert len(set(stamps)) == len(stamps)                  # the fake never repeats a time
        assert max(hubmod.parse_ts(t) for t in stamps) <= datetime.now(timezone.utc)  # nor runs ahead


def test_negative_twin_a_clock_that_never_moves_still_gets_distinct_times():
    frozen = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    hub = LrFakeHub(clock=lambda: frozen)
    hub.grant("alice")
    for name in ("bob", "carol"):
        hub.as_user(name)(["fpgahub", "lease", "acquire", TARGET])
    stamps = [hubmod.parse_ts(r["ts"]) for r in hub.audit]
    assert stamps == sorted(set(stamps)) and stamps[0] == frozen


def test_negative_twin_history_without_readable_notes_is_fpgahubs_alone(s):
    s.hub.as_user("bob")(["fpgahub", "lease", "acquire", TARGET])
    s.requester.lease_revoke(REASON)
    s.hub.notes_unreadable = True
    hist = s.holder.lease_history()
    assert hubmod.ADMIN_REVOKED not in [e["event"] for e in hist] and hist
    s.hub.override(("target", "lease-history"), stdout="no lease events\n")
    with pytest.raises(UnreachableError):
        s.holder.lease_history()


@pytest.mark.parametrize("limit", [0, -1, 501, True, "50", 2.5])
def test_history_limit_must_be_1_to_500(s, limit):
    with pytest.raises(UsageError):
        s.holder.lease_history(limit)


# -- the frozen interface ----------------------------------------------------------------------------


def test_the_frozen_interface_is_all_there():
    for name in ("principal", "lease_status", "board_id", "lease_revoke", "lease_history",
                 "put_request", "list_requests", "delete_request", "put_answer", "get_answer",
                 "lease_show", "lease_acquire", "lease_heartbeat", "lease_release", "lease_cancel"):
        assert callable(getattr(hubmod.HubClient, name)), name
    assert list(hubmod.QueueEntry.__dataclass_fields__) == ["position", "holder", "user"]
    assert list(hubmod.LeaseStatus.__dataclass_fields__) == ["held", "holder", "user", "expires_at", "queue"]
    assert list(hubmod.RequestNote.__dataclass_fields__) == [
        "id", "by", "user", "host", "message", "created_at", "deadline_at"]
    assert list(hubmod.AnswerNote.__dataclass_fields__) == ["id", "answer", "minutes", "message", "at"]
    assert issubclass(UnavailableError, HarnessError)


# -- the queue, end to end through pyverify's acquire ------------------------------------------------


def test_a_queued_acquire_is_granted_the_promoted_lease_when_the_holder_releases(s):
    import threading

    got: dict = {}

    def acquire() -> None:
        got["lease"], got["expires"] = s.requester.lease_acquire("ignored", ttl=600, poll_s=0.01,
                                                                 timeout_s=10)

    t = threading.Thread(target=acquire, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while s.hub.queue != [BOB] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s.requester.lease_status().position_of(BOB) == 1
    s.holder.lease_release(s.tokens["alice"], "any-holder-string")
    t.join(10)
    assert got["lease"].token == s.hub.current["token"] and s.hub.current["holder"] == BOB
    assert [e["event"] for e in s.holder.lease_history()][-2:] == ["lease.promoted", "lease.acquired"]


def test_negative_twin_a_release_with_the_wrong_token_leaves_the_board_held(s):
    with pytest.raises(HeldError):
        s.holder.lease_release("tok-wrong", "x")
    with pytest.raises(HeldError):                   # bob cannot release alice's lease either
        s.requester.lease_release(s.tokens["alice"], ALICE)
    assert s.holder.lease_status().holder == ALICE
