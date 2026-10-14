"""HM-A BOARD-CLIENT: the board as its own lease server (Release 3, single-user mode).

Runs ``BoardLeaseClient`` against the claimed-lock lab (real Mps3Pack session, real claim route,
claim forward through the fake SSH) with ``tests.fakes.bl_board_lease`` plugged into the board:
every act, the queue poll, the token on gated verbs, the refusal mapping, the no-key words, and
where each act travels (reads on 6900 direct; writes through the claim route).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.core.errors import ActionFailedError, ClaimLockedError, HeldError
from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind
from harness_manager.services import lease_backend
from harness_manager.services.lease import _QUEUED
from harness_manager.services.lease import AnswerNote as ServiceAnswerNote
from harness_manager.services.lease import RequestNote as ServiceRequestNote
from harness_manager_mps3 import board_lease as BL
from harness_manager_mps3 import claim_route as CR
from harness_manager_mps3 import shell as SH
from harness_manager_mps3.hub import LeaseLostError
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.bl_board_lease import attach
from tests.fakes.claimed_lock import TRUSTED
from tests.integration.test_claimed_lock import lab  # noqa: F401  (the fixture)

HUB_SIDE = "127.0.0.1"          # the hub tunnel / this PC: not the board itself
ME = "harness-manager-david@pc1"
OTHER = "harness-manager-bob@pc2"
NOW = 2_000_000_000.0


@pytest.fixture(autouse=True)
def _no_stale_tokens():
    SH._lease_tokens.clear()                  # process-wide, keyed by the lab's one address
    yield
    SH._lease_tokens.clear()


@pytest.fixture
def rig_of(lab):                                                  # noqa: F811
    """``rig_of(**lab_kwargs)`` -> (rig, server, client): a lab whose board has the lease verb
    and enforces the whole claim lock."""
    def make(**kw: Any):
        rig = lab(**kw)
        rig.fake.claim_gate_all = True
        srv = attach(rig.fake, name="mps3-01",
                     on_lease=lambda r, p: rig.control.append(("lease", str(r.get("act")), str(p))))
        client = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="mps3-01",
                                     who=ME, clock=lambda: NOW, sleep=lambda s: None)
        return rig, srv, client
    return make


def ours(rig_of, **kw):
    """A board this Harness Manager claimed (writes ride the claim forward)."""
    return rig_of(pinned=True, observed=True, **kw)


def reset_routed(rig):
    """A gated verb the way a call site makes it: through the claim route."""
    return CR.routed_call(rig.session, "resetting the DUT", rig.session.shell,
                          lambda c: c.reset("dut"))


def lease_peers(rig, act=None):
    return [peer for op, a, peer in rig.control if op == "lease" and (act is None or a == act)]


# --- the wire names -----------------------------------------------------------------------------


def test_wire_names_are_pyverifys_stub_names():
    assert BL.ACTS == ("show", "history", "acquire", "heartbeat", "release", "request",
                       "respond", "cancel", "revoke", "mode")
    assert BL.CODES == ("leased", "lease_required", "lease_held", "bad_token", "queue_full",
                        "local_hold", "not_holder", "busy_access")
    assert (BL.FEATURE, BL.MODE_OPEN, BL.MODE_SINGLE) == ("board_lease", "open", "single_user")
    assert (BL.VERB, BL.TOKEN_KEY, BL.PANEL_EVENT_BREAK, BL.PANEL_EVENT_ARM) == (
        "lease", "lease", "lease_break", "access_arm")
    assert BL.NO_KEY_WORDS == ("Your key is not on this board. Ask its owner to add it "
                               "(Board > Access > Board keys).")


# --- where each act travels ---------------------------------------------------------------------


def test_reads_go_direct_on_6900_to_a_board_claimed_by_someone_else(rig_of):
    rig, srv, client = rig_of(observed=True)                 # claimed; no pin here
    srv.mode = "single_user"
    assert client.mode().single_user is True
    assert client.lease_show().held is False
    assert client.lease_history() == []
    assert lease_peers(rig) == [HUB_SIDE] * 3 and rig.ssh.board_launches() == []


def test_every_write_act_rides_the_claim_forward_on_a_board_we_claimed(rig_of):
    rig, srv, client = ours(rig_of)
    res = client.set_mode(True, take=True)                                    # mode
    token = res.token
    assert client.lease_heartbeat(token, ME)                                   # heartbeat
    client.put_request(ServiceRequestNote("n1", OTHER, "bob", "pc2", "may I?", "", ""))   # request
    rid = client.list_requests()[0].id
    client.put_answer(ServiceAnswerNote(rid, "keep", 5, "", ""))               # respond
    client.delete_request("n1")                                                # cancel
    client.lease_release(token, ME)                                            # release
    client.lease_acquire(ME, ttl=600)                                          # acquire
    client.put_request(ServiceRequestNote("n2", OTHER, "bob", "pc2", "now?", "", ""))
    srv.tick(121)                                    # the holder let the request window pass
    client.lease_revoke("test")                                                # revoke
    wrote = [(a) for a, _ in srv.acts if a not in ("show", "history")]
    assert set(wrote) == {"mode", "heartbeat", "request", "respond", "cancel", "release",
                          "acquire", "revoke"}
    for act in set(wrote):
        assert set(lease_peers(rig, act)) == {TRUSTED}, (act, rig.control)
    assert HUB_SIDE not in [p for op, a, p in rig.control if op == "lease" and a in wrote]
    assert rig.fake.gated == []


def test_twin_writes_on_an_unclaimed_board_go_direct(rig_of):
    rig, srv, client = rig_of(claimed=False, pinned=True, observed=False)
    srv.mode = "single_user"
    client.lease_acquire(ME, ttl=300)
    assert lease_peers(rig, "acquire") == [HUB_SIDE] and rig.ssh.board_launches() == []


def test_a_stale_decision_is_retried_once_through_the_forward(rig_of):
    rig, srv, client = rig_of(pinned=True, observed=False)   # HM thinks unclaimed; the board is not
    srv.mode = "single_user"
    client.lease_acquire(ME, ttl=300)
    assert lease_peers(rig, "acquire") == [HUB_SIDE, TRUSTED]
    assert rig.fake.gated == [("lease", HUB_SIDE)]


def test_a_keyless_peer_gets_the_no_key_words_and_nothing_is_sent(rig_of):
    rig, srv, client = rig_of(observed=True)                 # claimed by a key that is not ours
    srv.mode = "single_user"
    with pytest.raises(ClaimLockedError) as exc:
        client.lease_acquire(ME, ttl=300)
    assert exc.value.message == BL.NO_KEY_WORDS
    assert lease_peers(rig, "acquire") == [] and rig.ssh.board_launches() == []
    with pytest.raises(ClaimLockedError) as exc2:
        client.set_mode(False)
    assert exc2.value.message == BL.NO_KEY_WORDS


def test_the_unclaimed_board_cannot_switch_single_user_mode_on(rig_of):
    rig, srv, client = rig_of(claimed=False, pinned=True, observed=False)
    with pytest.raises(HeldError) as exc:
        client.set_mode(True, take=True)
    assert "only the owner key" in exc.value.message and exc.value.code_text == "denied"
    assert srv.mode == "open"


# --- the acts -----------------------------------------------------------------------------------


def test_mode_on_takes_the_lease_in_the_same_step_and_off_gives_it_up(rig_of):
    rig, srv, client = ours(rig_of)
    assert client.mode().single_user is False
    res = client.set_mode(True, take=True)
    assert res.granted and re.fullmatch(r"[0-9a-f]{32}", res.token)
    assert res.expires_at == BL.iso(NOW + 3600) and res.mode.single_user
    assert client.current_token() == res.token
    assert client.lease_show().holder == ME or client.lease_show().held
    off = client.set_mode(False)
    assert off.mode.single_user is False and client.current_token() == ""
    assert srv.holder is None


def test_mode_off_by_a_non_holder_needs_the_boards_name(rig_of):
    rig, srv, client = ours(rig_of)
    client.set_mode(True, take=True)
    other = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="mps3-01", who=OTHER,
                                clock=lambda: NOW)          # a second PC: no token
    owner_key = srv._claimed
    srv._claimed = lambda: False                           # off is the owner's or the holder's
    with pytest.raises(HeldError) as exc:
        other.set_mode(False)
    assert exc.value.code_text == "not_holder"
    srv._claimed = owner_key                               # the owner's key: no token needed
    other.set_mode(False)
    assert srv.mode == "open" and srv.holder is None


def test_acquire_heartbeat_release(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    grant, expires = client.lease_acquire(ME, ttl=600)
    assert re.fullmatch(r"[0-9a-f]{32}", grant.token) and expires == BL.iso(NOW + 600)
    srv.tick(300)
    assert client.lease_heartbeat(grant.token, ME) == BL.iso(NOW + 600)
    assert srv.holder["expires"] == srv.now + 600
    client.lease_release(grant.token, ME)
    assert srv.holder is None and client.current_token() == ""


def test_heartbeat_with_a_lost_token_is_lease_lost(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    grant, _ = client.lease_acquire(ME, ttl=300)
    srv.holder["token"] = "f" * 32                           # someone else holds it now
    with pytest.raises(LeaseLostError) as lost:
        client.lease_heartbeat(grant.token, ME)
    assert lost.value.state == "lost"
    srv.holder = None                                        # nobody holds it
    with pytest.raises(LeaseLostError) as gone:
        client.lease_heartbeat(grant.token, ME)
    assert gone.value.state == "expired"


def test_the_lease_lapses_when_the_ttl_passes(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    grant, _ = client.lease_acquire(ME, ttl=300)
    srv.tick(301)
    with pytest.raises(LeaseLostError) as gone:
        client.lease_heartbeat(grant.token, ME)
    assert gone.value.state == "expired"


def test_acquire_polls_while_queued_and_logs_the_position(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    bob = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="mps3-01", clock=lambda: NOW)
    first, _ = bob.lease_acquire(OTHER, ttl=600)
    logs: list[str] = []
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 2:
            bob.lease_release(first.token, OTHER)             # the holder gives the board back

    grant, expires = client.lease_acquire(ME, ttl=600, poll_s=7.0, sleep=nap, log_fn=logs.append)
    assert logs == ["queued at position 1", "queued at position 1"]
    assert all((m := _QUEUED.search(t)) and m.group(1) == "1" for t in logs)
    # the queue head polls inside its 120 s reservation: every POLL_HEAD_S, not every poll_s
    assert naps == [BL.POLL_HEAD_S] * 2 and srv.holder["who"] == ME
    assert grant.token == srv.holder["token"]
    acquires = [r for a, r in srv.acts if a == "acquire" and r.get("who") == ME]
    assert len(acquires) == 3 and all("token" not in r for r in acquires)   # known by who
    assert all(r["app"] == "Harness Manager" and r["ttl_s"] == 600 for r in acquires)


def test_acquire_that_waits_too_long_leaves_the_queue_and_says_so(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    bob = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="mps3-01", clock=lambda: NOW)
    bob.lease_acquire(OTHER, ttl=600)
    clock = iter(range(0, 10_000, 100))
    client._mono = lambda: float(next(clock))
    with pytest.raises(ActionFailedError) as exc:
        client.lease_acquire(ME, ttl=60, timeout_s=250, sleep=lambda s: None)
    assert "queue place was removed" in exc.value.message and srv.queue == []


def test_a_full_queue_and_a_local_hold_are_refused_in_the_boards_words(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW
                        ).lease_acquire(OTHER, ttl=600)
    for i in range(4):
        srv.queue.append({"id": f"q{i}", "who": f"w{i}", "msg": "", "at": srv.now})
    with pytest.raises(HeldError) as full:
        client.lease_acquire(ME, ttl=300)
    assert full.value.code_text == "queue_full" and "queue full" in full.value.message
    srv.local_break()
    with pytest.raises(HeldError) as hold:                    # the hold bars switching it on
        client.set_mode(True, take=True)
    assert hold.value.code_text == "local_hold"


def test_cancel_removes_the_queue_place_by_the_id_acquire_returned(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW
                        ).lease_acquire(OTHER, ttl=300)
    assert client.lease_cancel(ME) is False                  # never queued: nothing to cancel
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        if len(srv.queue) == 1:                              # queued: give up (a UI cancel)
            assert client.lease_cancel(ME) is True
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        client.lease_acquire(ME, ttl=300, sleep=nap)
    assert srv.queue == [] and client.lease_cancel(ME) is False


def test_show_status_and_queue_use_absolute_times_on_our_clock(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    client.lease_acquire(ME, ttl=600)
    srv.tick(100)
    bob = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW)
    bob.put_request(ServiceRequestNote("n9", OTHER, "bob", "pc2", "quick look", "", ""))
    view = client.lease_show()
    assert (view.held, view.holder, view.expires_at) == (True, ME, BL.iso(NOW + 500))
    status = client.lease_status()
    assert status.holder == ME and [(q.position, q.holder) for q in status.queue] == [(1, OTHER)]
    assert client.principal() == ME and client.board_id() == rig.board_id


def test_requests_round_trip_with_the_hub_note_shapes(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    grant, _ = client.lease_acquire(ME, ttl=600)
    bob = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW)
    bob.put_request(ServiceRequestNote("note-1", OTHER, "bob", "pc2", "five minutes?", "", ""))
    srv.tick(30)
    [note] = client.list_requests()
    assert (note.by, note.message) == (OTHER, "five minutes?")
    assert note.created_at == BL.iso(NOW - 30) and note.deadline_at == BL.iso(NOW - 30 + 120)
    assert bob.get_answer("note-1") is None
    client.put_answer(BL.AnswerNote(note.id, "keep", 15, "", ""))
    ans = bob.get_answer("note-1")
    assert (ans.answer, ans.minutes, ans.id) == ("keep", 15, "note-1")
    bob.delete_request("note-1")
    assert client.list_requests() == []
    bob.put_request(ServiceRequestNote("note-2", OTHER, "bob", "pc2", "", "", ""))
    client.put_answer(BL.AnswerNote(client.list_requests()[0].id, "release", 0, "", ""))
    assert srv.holder is None and bob.get_answer("note-2").answer == "release"
    assert grant.token and re.fullmatch(r"[0-9a-f]{32}", grant.token)


def test_a_non_holder_cannot_answer(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    client.lease_acquire(ME, ttl=600)
    bob = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW)
    bob.put_request(ServiceRequestNote("n", OTHER, "bob", "pc2", "", "", ""))
    with pytest.raises(HeldError) as exc:
        bob.put_answer(BL.AnswerNote(bob.list_requests()[0].id, "release", 0, "", ""))
    assert exc.value.code_text in ("bad_token", "not_holder")


def test_force_release_needs_the_boards_label_and_an_unanswered_request(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    other = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW)
    other.lease_acquire(OTHER, ttl=600)
    client.put_request(ServiceRequestNote("n1", ME, "me", "pc1", "force test", "", ""))
    with pytest.raises(HeldError) as busy:                  # the holder still has its window
        client.lease_revoke("force-released by me via Harness Manager: no answer")
    assert busy.value.code_text == "busy"
    srv.tick(121)
    out = client.lease_revoke("force-released by me via Harness Manager: no answer")
    assert out["revoked"] == [rig.board_id] and out["prior_holder"] == OTHER
    assert srv.holder is None
    sent = srv.acts[-1][1]
    assert sent["confirm"] == "mps3-01" and "reason" not in sent and "confirm_board" not in sent
    taken = BL.taken_from_history(client.lease_history(), OTHER)
    assert taken is not None and taken["at"] and taken["by"] == ME   # who = the head that forced it
    ev = [e for e in client.lease_history() if e["ev"] == "revoke"][-1]
    assert ev["prior_holder"] == OTHER and ev["by"] == ME            # info = "from <holder>"
    client.name = "wrong"
    with pytest.raises(HeldError) as exc:
        client.lease_revoke("x")
    assert exc.value.code_text == "invalid"


def test_a_touchscreen_break_is_someone_at_the_board(rig_of):
    rig, srv, client = ours(rig_of)
    client.set_mode(True, take=True)
    srv.tick(60)
    srv.local_break()
    events = client.lease_history()
    assert events[-1]["event"] == "lease.broken_local"
    taken = BL.taken_from_history(events, ME)
    assert taken == {"by": "someone at the board", "at": events[-1]["ts"],
                     "reason": "single-user mode was switched off on the touchscreen"}
    mode = client.mode()
    assert mode.single_user is False and mode.local_hold_s == 900 - 0
    with pytest.raises(HeldError) as exc:
        client.set_mode(True, take=True)
    assert exc.value.code_text == "local_hold"


def test_taken_from_history_is_none_for_an_expiry_or_our_own_acquire(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    client.lease_acquire(ME, ttl=300)
    assert BL.taken_from_history(client.lease_history(), ME) is None
    srv.tick(61)
    srv.op({"op": "lease", "act": "show"})
    assert BL.taken_from_history(client.lease_history(), ME) is None


# --- the token on gated verbs, and the refusals --------------------------------------------------


def test_the_holder_sends_its_token_on_a_gated_verb_over_the_forward(rig_of):
    rig, srv, client = ours(rig_of)
    res = client.set_mode(True, take=True)
    rig.session.resets.reset("dut")
    sent = [r for r in srv.seen if r.get("op") == "reset"]
    assert sent and sent[-1]["lease"] == res.token
    assert [p for o, _a, p in rig.control if o == "reset"][-1] == TRUSTED     # the forwarded shell too


def test_the_token_rides_every_request_but_hello(rig_of):
    rig, srv, client = ours(rig_of)
    res = client.set_mode(True, take=True)
    rig.session.shell.call(lambda c: c.ping())
    pings = [r for r in srv.seen if r.get("op") == "ping"]
    assert pings[-1].get("lease") == res.token                     # ignored on READ rows
    assert all("lease" not in r for r in srv.seen if r.get("op") == "hello")


def test_open_mode_adds_nothing_and_needs_nothing(rig_of):
    rig, srv, client = ours(rig_of)
    rig.session.resets.reset("dut")
    assert "lease" not in [r for r in srv.seen if r.get("op") == "reset"][-1]


def test_a_gated_verb_without_the_lease_is_held_with_reason_lease(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW
                        ).lease_acquire(OTHER, ttl=600)
    SH.set_lease_token_source(rig.session.shell.gate_key, None)       # this PC holds nothing
    with pytest.raises(HeldError) as exc:
        reset_routed(rig)
    assert exc.value.data["reason"] == "LEASE" and exc.value.data["code"] == "leased"
    assert OTHER in exc.value.message and exc.value.data["err"] == exc.value.message
    srv.holder = None
    with pytest.raises(HeldError) as nobody:
        reset_routed(rig)
    assert nobody.value.data["code"] == "lease_required"


def test_a_wrong_token_is_bad_token(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW
                        ).lease_acquire(OTHER, ttl=600)
    SH.set_lease_token_source(rig.session.shell.gate_key, lambda: "e" * 32)
    with pytest.raises(HeldError) as exc:
        reset_routed(rig)
    assert exc.value.data["code"] == "bad_token" and exc.value.data["reason"] == "LEASE"


def test_the_refusal_mapping_leaves_other_replies_and_the_lease_verb_alone():
    assert SH.lease_refusal({"ok": True}) is None
    assert SH.lease_refusal({"ok": False, "err": "x locked", "code": "locked"}) is None
    assert SH.lease_refusal({"ok": False, "err": "EBUSY"}) is None
    err = SH.lease_refusal({"ok": False, "err": "reset leased: bob has the board",
                            "code": "leased", "holder": "bob"})
    assert err.holder == "bob" and err.message == "reset leased: bob has the board"
    assert SH.lease_gated({"op": "reset"}) and not SH.lease_gated({"op": "ping"})
    assert not SH.lease_gated({"op": "display", "owner": "query"})
    assert SH.lease_gated({"op": "display", "owner": "dut"})
    assert not SH.lease_gated({"op": "lease", "act": "acquire"})


def test_the_panel_ride_still_runs_first_in_the_chain(rig_of):
    rig, srv, client = ours(rig_of)
    order: list[str] = []
    shell = rig.session.shell
    shell.preamble = lambda c, t: order.append("panel")
    chain = shell._preambles()
    assert chain[0] is shell.preamble and len(chain) == 2
    shell.call(lambda c: order.append("fn"))
    assert order == ["panel", "fn"]


# --- the adapter and the pack hook ----------------------------------------------------------------


def candidate(features: tuple[str, ...]) -> Candidate:
    ident = BoardIdentity(board_type="mps3", shell_id="0x11111111", rm_id="0x22222222",
                          features=features)
    return Candidate(pack="mps3", board_id="mps3@192.0.2.9", label="MPS3 at 192.0.2.9",
                     links=(Link(LinkKind.ETHERNET, "192.0.2.9", "shell control channel"),),
                     name="desk-1", identity=ident)


def test_the_adapter_is_a_board_lease_server(rig_of):
    rig, srv, client = ours(rig_of)
    stub = SimpleNamespace(candidate=candidate((BL.FEATURE,)), shell=rig.session.shell,
                           claim=rig.session.claim, hub=None)
    adapter = BL.make_lease_adapter(stub)
    assert isinstance(adapter, BL.Mps3BoardLease)
    assert (adapter.kind, adapter.target, adapter.name) == ("board", "mps3@192.0.2.9", "desk-1")
    assert adapter.words == lease_backend.WORDS["board"] and adapter.host
    assert adapter.client is not None and hasattr(adapter.client, "lease_acquire")
    assert lease_backend.server_word(adapter) == "the board"
    assert adapter.mode().single_user is False
    srv.mode = "single_user"
    assert lease_backend.lease_required(adapter) is True


def test_the_adapter_says_unknown_when_the_board_cannot_be_asked(rig_of):
    rig, srv, client = ours(rig_of)
    stub = SimpleNamespace(candidate=candidate((BL.FEATURE,)), shell=rig.session.shell,
                           claim=rig.session.claim, hub=None)
    adapter = BL.Mps3BoardLease(stub)
    rig.fake.stop()
    assert adapter.mode() is None
    assert lease_backend.lease_required(adapter) is False        # the board refuses for itself


def test_a_hub_board_keeps_the_hub_as_its_lease(rig_of):
    rig, srv, client = ours(rig_of)
    hub = object()
    stub = SimpleNamespace(candidate=candidate((BL.FEATURE,)), shell=rig.session.shell, hub=hub)
    assert BL.make_lease_adapter(stub) is hub
    assert lease_backend.lease_of(SimpleNamespace(lease=hub, hub=None)) is hub


def test_an_old_board_has_no_lease_adapter(rig_of):
    rig, srv, client = ours(rig_of)
    stub = SimpleNamespace(candidate=candidate(("xvc_dbgbr",)), shell=rig.session.shell, hub=None)
    assert BL.make_lease_adapter(stub) is None
    stub = SimpleNamespace(candidate=candidate((BL.FEATURE,)), shell=None, hub=None)
    assert BL.make_lease_adapter(stub) is None
    assert lease_backend.lease_of(SimpleNamespace(lease=None, hub=None)) is None


def test_the_session_hook_row_sets_session_lease(rig_of):
    rig, srv, client = ours(rig_of)
    assert hasattr(rig.session, "lease")                    # the row exists; old features: no lease
    assert rig.session.lease is None
    assert lease_backend.lease_of(rig.session) is None


def test_lease_for_a_candidate_beside_hub_for(monkeypatch):
    pack = Mps3Pack()
    cand = candidate((BL.FEATURE,))
    adapter = pack.lease_for(cand)
    assert isinstance(adapter, BL.Mps3BoardLease) and adapter.kind == "board"
    assert pack.lease_for(candidate(("xvc_dbgbr",))) is None
    hub = object()
    monkeypatch.setattr(pack, "hub_for", lambda c: hub)
    assert pack.lease_for(cand) is hub


# --- contract follow-ups: ttl on mode, owner, stable who, cancel without a queue id ---------------


def test_mode_on_sends_hms_board_ttl_and_the_grant_uses_it(rig_of):
    rig, srv, client = ours(rig_of)
    client.set_ttl(900)
    res = client.set_mode(True, take=True)
    assert srv.acts[-1][1]["ttl_s"] == 900 and res.expires_at == BL.iso(NOW + 900)
    client.set_mode(False)
    assert "ttl_s" not in srv.acts[-1][1]
    client.set_ttl(100)                                        # clamped to the wire's 300 minimum
    client.set_mode(True, take=True)
    assert srv.acts[-1][1]["ttl_s"] == 300


def test_owner_maps_to_can_change_for_on_and_off(rig_of):
    rig, srv, client = ours(rig_of)
    assert client.mode().can_change is True                    # owner: may turn it on
    srv._claimed = lambda: False
    off_limits = client.mode()
    assert off_limits.can_change is False and "owner" in off_limits.change_reason
    srv._claimed = lambda: True
    client.set_mode(True, take=True)
    srv._claimed = lambda: False
    assert client.mode().can_change is True                    # the holder (you) may turn it off
    other = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW)
    assert other.mode().can_change is False
    assert BL.BoardLeaseClient._mode_of({"mode": "open"}).can_change is True   # no `owner` key


def test_every_acquire_poll_sends_the_same_who(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW
                        ).lease_acquire(OTHER, ttl=300)
    for _ in range(2):                                         # two CLI-style fresh clients
        fresh = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW)
        with pytest.raises(KeyboardInterrupt):
            fresh.lease_acquire(ME, ttl=300, sleep=lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    assert [w["who"] for w in srv.queue] == [ME]               # one place, kept
    fresh = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="m", clock=lambda: NOW)
    assert fresh.lease_cancel(ME) is True and srv.queue == []  # found by who, no queue id kept


def test_lease_held_for_our_own_who_keeps_the_stored_token(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    grant, _ = client.lease_acquire(ME, ttl=600)
    again, expires = client.lease_acquire(ME, ttl=600)         # the board: lease_held, no token
    assert again.token == grant.token and client.current_token() == grant.token
    assert expires == BL.iso(NOW + 600)
    assert srv.op({"op": "lease", "act": "acquire", "who": ME})["code"] == "lease_held"


def test_lease_held_for_our_own_who_without_a_key_says_so_and_does_not_loop(rig_of):
    rig, srv, client = ours(rig_of)
    srv.mode = "single_user"
    client.lease_acquire(ME, ttl=600)
    lost = BL.BoardLeaseClient(rig.session, board=rig.board_id, name="mps3-01", who=ME,
                               clock=lambda: NOW, sleep=lambda s: None)   # wiped state dir
    SH.set_lease_token_source(rig.session.shell.gate_key, None)
    n = len([a for a, _ in srv.acts if a == "acquire"])
    with pytest.raises(ActionFailedError) as exc:
        lost.lease_acquire(ME, ttl=600)
    assert "no longer has its key" in exc.value.message and "about 10 min" in exc.value.message
    assert len([a for a, _ in srv.acts if a == "acquire"]) == n + 1       # one try, no loop
