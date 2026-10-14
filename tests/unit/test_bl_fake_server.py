"""The in-memory board lease server (tests/fakes/bl_board_lease.py) keeps the v0.21 rules the
HM-A client tests lean on: claim first, then the lease; reads open; relative seconds."""

from __future__ import annotations

from tests.fakes.bl_board_lease import BoardLeaseServer


def srv(claimed=True) -> BoardLeaseServer:
    return BoardLeaseServer(claimed=lambda: claimed)


def lease(s, act, **kw):
    return s.op({"op": "lease", "act": act, **kw})


def test_the_gate_is_open_in_shared_mode():
    assert srv().gate("reset", {"op": "reset"}) is None


def test_single_user_gate_codes_leased_required_and_bad_token():
    s = srv()
    assert lease(s, "mode", mode="single_user")["ok"]
    assert s.gate("reset", {"op": "reset"})["code"] == "lease_required"
    got = lease(s, "acquire", who="a", ttl_s=60)
    assert s.gate("reset", {"op": "reset"})["code"] == "leased"
    assert s.gate("reset", {"op": "reset", "lease": "0" * 32})["code"] == "bad_token"
    assert s.gate("reset", {"op": "reset", "lease": got["token"]}) is None


def test_gated_verbs_are_the_state_changing_ones():
    s = srv()
    for req in ({"op": "reset"}, {"op": "swap"}, {"op": "display", "owner": "dut"},
                {"op": "touch_cal", "act": "set"}):
        assert s.gated(req)
    for req in ({"op": "ping"}, {"op": "display", "owner": "query"}, {"op": "stats"}):
        assert not s.gated(req)


def test_mode_on_needs_a_claim_and_times_are_relative():
    assert lease(srv(claimed=False), "mode", mode="single_user")["ok"] is False
    s = srv()
    lease(s, "mode", mode="single_user")
    lease(s, "acquire", who="a", ttl_s=100)
    s.tick(40)
    shown = lease(s, "show")
    assert shown["holder"]["expires_in_s"] == 60 and shown["holder"]["held_s"] == 40
    s.tick(61)
    assert lease(s, "show")["holder"] is None


def test_touchscreen_break_turns_the_mode_off_and_holds_remote_on():
    s = srv()
    lease(s, "mode", mode="single_user")
    lease(s, "acquire", who="a", ttl_s=100)
    s.local_break()
    assert lease(s, "show")["mode"] == "open" and lease(s, "show")["local_hold_s"] == 900
    assert lease(s, "mode", mode="single_user")["code"] == "local_hold"
    assert lease(s, "history", n=3)["events"][-1]["event"] == "lease_break"
    s.tick(901)
    assert lease(s, "mode", mode="single_user")["ok"]
