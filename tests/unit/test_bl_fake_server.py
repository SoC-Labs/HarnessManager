"""The in-memory board lease server (tests/fakes/bl_board_lease.py) keeps the v0.21 rules the
HM-A client tests lean on: panel access, then claim, then the lease; reads open; relative
seconds; the frozen reply shapes."""

from __future__ import annotations

from tests.fakes.bl_board_lease import BoardLeaseServer

SHOW_KEYS = {"ok", "op", "act", "mode", "persistent", "holder", "queue", "local_hold_s",
             "reclaim_s"}


def srv(claimed=True) -> BoardLeaseServer:
    return BoardLeaseServer(claimed=lambda: claimed)


def lease(s, act, **kw):
    return s.op({"op": "lease", "act": act, **kw})


def test_the_gate_is_open_in_shared_mode():
    assert srv().gate("reset", {"op": "reset"}) is None


def test_single_user_gate_codes_leased_required_and_bad_token():
    s = srv()
    on = lease(s, "mode", mode="single_user")           # the owner is granted the lease
    assert s.gate("reset", {"op": "reset", "lease": on["token"]}) is None
    lease(s, "release", lease=on["token"])
    assert s.gate("reset", {"op": "reset"})["code"] == "lease_required"
    got = lease(s, "acquire", who="a", ttl_s=300)
    refused = s.gate("reset", {"op": "reset"})
    assert refused["code"] == "leased" and refused["err"].startswith("reset leased: held by a (")
    assert s.gate("reset", {"op": "reset", "lease": "0" * 32})["code"] == "bad_token"
    assert s.gate("reset", {"op": "reset", "lease": got["token"]}) is None


def test_panel_access_is_asked_first_and_refuses_every_act_but_the_reads():
    s = srv()
    s.access_arm()
    assert s.gate("reset", {"op": "reset"})["code"] == "busy_access"
    assert lease(s, "acquire", who="a")["code"] == "busy_access"
    assert lease(s, "show")["ok"] and lease(s, "history")["ok"]
    assert s.panel_events[-1]["k"] == "access_arm"


def test_gated_verbs_are_the_state_changing_ones():
    s = srv()
    for req in ({"op": "reset"}, {"op": "swap"}, {"op": "display", "owner": "dut"},
                {"op": "touch_cal", "act": "set"}):
        assert s.gated(req)
    for req in ({"op": "ping"}, {"op": "display", "owner": "query"}, {"op": "stats"}):
        assert not s.gated(req)


def test_mode_on_is_the_owners_and_grants_the_lease_with_relative_times():
    denied = lease(srv(claimed=False), "mode", mode="single_user")
    assert denied["ok"] is False and denied["code"] == "denied"
    s = srv()
    on = lease(s, "mode", mode="single_user")
    assert on["ok"] and on["token"] and on["expires_in_s"] == 3600 and on["mode"] == "single_user"
    s.tick(40)
    shown = lease(s, "show", lease=on["token"])
    assert set(shown) == SHOW_KEYS and shown["persistent"] is True
    assert set(shown["holder"]) == {"who", "app", "held_s", "expires_in_s", "you"}
    assert shown["holder"]["expires_in_s"] == 3560 and shown["holder"]["held_s"] == 40
    assert shown["holder"]["you"] is True and lease(s, "show")["holder"]["you"] is False
    s.tick(3561)
    assert lease(s, "show")["holder"] is None


def test_a_queued_acquire_is_ok_true_granted_false_and_the_head_is_reserved_120_s():
    s = srv()
    holder = lease(s, "acquire", who="a", ttl_s=600)
    assert holder["granted"] is True
    q = lease(s, "acquire", who="b", ttl_s=600)
    assert q["ok"] is True and q["granted"] is False and q["code"] == "lease_held"
    assert q["position"] == 1 and q["id"]
    c = lease(s, "acquire", who="c", ttl_s=600)
    assert c["position"] == 2
    lease(s, "release", lease=holder["token"])
    assert lease(s, "acquire", who="c", ttl_s=600)["granted"] is False       # b is reserved
    assert lease(s, "acquire", who="b", ttl_s=600)["granted"] is True
    assert lease(s, "acquire", who="b", ttl_s=600)["code"] == "lease_held"    # already held


def test_a_reservation_passes_to_the_next_waiter_after_120_s():
    s = srv()
    holder = lease(s, "acquire", who="a", ttl_s=600)
    lease(s, "acquire", who="b", ttl_s=600)
    lease(s, "acquire", who="c", ttl_s=600)
    lease(s, "release", lease=holder["token"])
    s.tick(121)
    assert lease(s, "acquire", who="b", ttl_s=600)["granted"] is False   # b's 120 s are over
    assert lease(s, "acquire", who="c", ttl_s=600)["granted"] is True


def test_history_events_are_ev_who_age_s_info_and_the_touchscreen_break_is_recorded():
    s = srv()
    lease(s, "mode", mode="single_user")
    s.local_break()
    shown = lease(s, "show")
    assert shown["mode"] == "open" and shown["local_hold_s"] == 900 and shown["holder"] is None
    assert lease(s, "mode", mode="single_user")["code"] == "local_hold"
    ev = lease(s, "history", n=3)["events"]
    assert set(ev[-1]) == {"ev", "who", "age_s", "info"} and ev[-1]["ev"] == "break"
    assert ev[-1]["who"] == "" and s.panel_events[-1]["k"] == "lease_break"
    s.tick(901)
    assert lease(s, "mode", mode="single_user")["ok"]


def test_respond_keep_and_revoke_follow_the_request_window():
    s = srv()
    holder = lease(s, "acquire", who="a", ttl_s=600)
    asked = lease(s, "request", who="b", msg="need it")
    assert lease(s, "revoke", confirm="wrong")["code"] == "invalid"
    assert lease(s, "revoke", confirm="mps3-01")["code"] == "busy"
    s.tick(121)
    kept = lease(s, "respond", lease=holder["token"], id=asked["id"], accept=False, minutes=15)
    assert kept["expires_in_s"] == 900
    assert lease(s, "history", n=1)["events"][-1] == {"ev": "keep", "who": "a", "age_s": 0,
                                                      "info": "keep 15"}
    lease(s, "request", who="b", msg="again")
    s.tick(121)
    assert lease(s, "revoke", confirm="mps3-01")["ok"] and lease(s, "show")["holder"] is None
