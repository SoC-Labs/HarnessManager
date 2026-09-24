"""LR-A: fpgahub 0.3.0's outputs, parsed exactly as its source prints them. Each check has a twin.

The texts here are what fpgahub v0.3.0's cli.py prints (see the format table in
``harness_manager_mps3/hub.py``); nothing in this file runs a hub, ssh or fpgahub.
"""

from __future__ import annotations

import json
import re

import pytest

from harness_manager.core.errors import UnreachableError, UsageError
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3.hub import QueueEntry

HELD = "held by alice@mapstone-dev (user alice, expires 2026-09-24T13:00:00.123456Z)\n"

#: rich Table (box.HEAVY_HEAD), as `fpgahub lease show` draws its Queue with COLUMNS=400.
QUEUE_HEAVY = """\
               Queue
┏━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ Pos ┃ Holder             ┃ User  ┃
┡━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━┩
│ 1   │ bob@mapstone-dev   │ bob   │
│ 2   │ carol@mapstone-dev │ carol │
└─────┴────────────────────┴───────┘
"""
#: rich's ASCII fallback (box.ASCII) when the hub's stdout is not UTF-8.
QUEUE_ASCII = """\
               Queue
+----------------------------------+
| Pos | Holder             | User  |
|-----+--------------------+-------|
| 1   | bob@mapstone-dev   | bob   |
| 2   | carol@mapstone-dev | carol |
+----------------------------------+
"""
WANT_QUEUE = (QueueEntry(1, "bob@mapstone-dev", "bob"), QueueEntry(2, "carol@mapstone-dev", "carol"))


# -- lease show -----------------------------------------------------------------------------------


@pytest.mark.parametrize("table", [QUEUE_HEAVY, QUEUE_ASCII], ids=["heavy", "ascii"])
def test_lease_show_reads_the_holder_and_the_queue_table(table):
    st = hubmod.parse_lease_show(HELD + table)
    assert (st.held, st.holder, st.user, st.expires_at) == (
        True, "alice@mapstone-dev", "alice", "2026-09-24T13:00:00.123456Z")
    assert st.queue == WANT_QUEUE
    assert st.head == WANT_QUEUE[0] and st.position_of("carol@mapstone-dev") == 2
    assert st.position_of("dave@mapstone-dev") is None


def test_negative_twin_lease_show_with_nobody_waiting_has_an_empty_queue():
    st = hubmod.parse_lease_show(HELD)
    assert st.held and st.queue == () and st.head is None
    free = hubmod.parse_lease_show("not leased\n")
    assert (free.held, free.holder, free.queue) == (False, "", ())


def test_lease_show_free_but_with_a_queue_and_ansi_colour():
    st = hubmod.parse_lease_show("\x1b[33mnot leased\x1b[0m\r\n" + QUEUE_HEAVY)
    assert not st.held and st.queue == WANT_QUEUE


@pytest.mark.parametrize("text", ["", "   \n", "HTTP 500\n", "held by\n",
                                  "Queue\n│ 1 │ bob@mapstone-dev │ bob │\n"])
def test_lease_show_that_says_neither_held_nor_free_raises_never_reads_as_free(text):
    with pytest.raises(UnreachableError) as exc:
        hubmod.parse_lease_show(text)
    assert "neither" in str(exc.value)


def test_negative_twin_a_queue_row_it_cannot_read_raises():
    for bad in ("│ x   │ bob@mapstone-dev │ bob │", "│ 1   │ bob@mapstone-dev │",
                "│ 1   │ bob@mapst… │ bob │"):
        with pytest.raises(UnreachableError):
            hubmod.parse_lease_show(HELD + bad + "\n")


# -- whoami ---------------------------------------------------------------------------------------


WHOAMI = {"kind": "unix", "name": "david", "role": "admin", "host": "mapstone-dev",
          "holder": "david@mapstone-dev", "audit_id": "unix:david", "token_name": None,
          "groups": [], "limits": {"max_ttl_s": None}}


def test_whoami_json_gives_the_holder_string_leases_are_recorded_under():
    got = hubmod.parse_whoami(json.dumps(WHOAMI, indent=2) + "\n")
    assert got["holder"] == "david@mapstone-dev" and got["audit_id"] == "unix:david"


@pytest.mark.parametrize("text", ["", "name    david\nholder  david@mapstone-dev\n", "{}",
                                  json.dumps({**WHOAMI, "holder": ""}),
                                  json.dumps({**WHOAMI, "holder": "da vid@x"}),
                                  json.dumps({**WHOAMI, "holder": "d\x1b[2Jx"}), "[1, 2]"])
def test_negative_twin_whoami_without_a_usable_holder_raises(text):
    with pytest.raises(UnreachableError):
        hubmod.parse_whoami(text)


# -- board list / board lease show / revoke ----------------------------------------------------------


GROUPS = {"groups": [
    {"board": "kr260_01", "size": 1, "is_paired": False, "members": [{"name": "kr260_01_ps", "role": "ps"}]},
    {"board": "mps3_01", "size": 1, "is_paired": False, "members": [{"name": "mps3_01_pl", "role": "pl"}]},
]}


def test_board_list_json_maps_each_board_to_its_targets():
    groups = hubmod.parse_groups(json.dumps(GROUPS, indent=2))
    assert ("mps3_01", ["mps3_01_pl"]) in groups and ("kr260_01", ["kr260_01_ps"]) in groups


@pytest.mark.parametrize("text", ["", "{}", '{"groups": 3}', "Boards\nmps3_01 1 mps3_01_pl\n"])
def test_negative_twin_board_list_without_groups_raises(text):
    with pytest.raises(UnreachableError):
        hubmod.parse_groups(text)


def test_board_lease_show_json_gives_each_members_lease():
    data = {"board": "mps3_01", "state": "held", "members": [
        {"board": "mps3_01_pl", "current": {"board": "mps3_01_pl", "holder": "alice@mapstone-dev",
                                            "user": "alice", "expires_at": "x", "tier": "interactive"}},
        {"board": "mps3_01_mcc", "current": None}], "queue": []}
    got = hubmod.parse_board_lease(json.dumps(data))
    assert got["mps3_01_pl"]["holder"] == "alice@mapstone-dev" and got["mps3_01_mcc"] is None


@pytest.mark.parametrize("data", [{}, {"members": [{"current": None}]},
                                  {"members": [{"board": "a", "current": {"user": "x"}}]}])
def test_negative_twin_board_lease_show_that_does_not_name_members_raises(data):
    with pytest.raises(UnreachableError):
        hubmod.parse_board_lease(json.dumps(data))


def test_revoke_output_names_what_was_kicked_and_by_whom():
    assert hubmod.parse_revoke("revoked mps3_01_pl (by unix:bob)\n") == {
        "revoked": ["mps3_01_pl"], "by": "unix:bob"}
    assert hubmod.parse_revoke("revoked mps3_01_ps, mps3_01_pl (by token:ci-1)\n")["revoked"] == [
        "mps3_01_ps", "mps3_01_pl"]


def test_negative_twin_revoke_with_nothing_held_and_unreadable_replies():
    assert hubmod.parse_revoke("no lease to revoke\n") == {"revoked": [], "by": ""}
    for text in ("", "revoked\n", "Aborted!\n", "{'revoked': []}"):
        with pytest.raises(UnreachableError):
            hubmod.parse_revoke(text)


# -- lease history ----------------------------------------------------------------------------------


EVENT_KEYS = ("ts", "event", "board", "holder", "user", "position", "ttl_s", "expires_at",
              "source", "error")


def rec(ts: str, event: str, holder: str | None, **kw) -> dict:
    return {**dict.fromkeys(EVENT_KEYS), "ts": ts, "event": event, "board": "mps3_01_pl",
            "holder": holder, **kw}


def test_lease_history_reads_rich_print_json_output():
    events = [rec("2026-09-24T12:00:00+00:00", "lease.acquired", "alice@mapstone-dev")]
    text = json.dumps({"board": "mps3_01_pl", "events": events}, indent=2, ensure_ascii=False)
    coloured = text.replace('"event"', '\x1b[1;34m"event"\x1b[0m')
    assert hubmod.parse_lease_history(coloured) == events


def test_negative_twin_lease_history_empty_is_empty_malformed_raises():
    assert hubmod.parse_lease_history('{"board": "mps3_01_pl", "events": []}') == []
    assert hubmod.parse_lease_history('{"events": [1, {"ts": 3}, {"ts": "t", "event": "e"}]}') == [
        {"ts": "t", "event": "e"}]
    for text in ("", "no lease events for mps3_01_pl in the tail buffer", '{"board": "x"}', "{oops"):
        with pytest.raises(UnreachableError):
            hubmod.parse_lease_history(text)


# -- who took it -------------------------------------------------------------------------------------


ALICE, BOB = "alice@mapstone-dev", "bob@mapstone-dev"


def revoke_burst(note: bool) -> list[dict]:
    """What lease_history returns after bob forced alice off (fpgahub 0.3.0 order)."""
    evs = [rec("2026-09-24T12:00:00+00:00", "lease.acquired", ALICE),
           rec("2026-09-24T12:01:00+00:00", "lease.queued", BOB, position=1)]
    if note:
        evs.append({"ts": "2026-09-24T12:03:59.9+00:00", "event": hubmod.ADMIN_REVOKED, "by": BOB,
                    "reason": "no answer", "prior_holder": ALICE, "board": "mps3_01_pl", "id": "x"})
    return evs + [rec("2026-09-24T12:04:00+00:00", "lease.revoked", ALICE),
                  rec("2026-09-24T12:04:00.1+00:00", "lease.promoted", BOB),
                  rec("2026-09-24T12:04:00.2+00:00", "lease.released", ALICE)]


def test_taken_names_the_forcer_and_reason_from_the_revoke_note():
    assert hubmod.taken_from_history(revoke_burst(note=True), ALICE) == {
        "by": BOB, "reason": "no answer", "at": "2026-09-24T12:04:00+00:00"}


def test_taken_without_a_note_names_whoever_got_the_board():
    assert hubmod.taken_from_history(revoke_burst(note=False), ALICE) == {
        "by": BOB, "reason": "", "at": "2026-09-24T12:04:00+00:00"}


def test_negative_twin_an_expiry_or_a_later_tenure_is_not_taken():
    expired = revoke_burst(note=False)[:2] + [rec("2026-09-24T13:00:00+00:00", "lease.expired", ALICE)]
    assert hubmod.taken_from_history(expired, ALICE) is None
    back = revoke_burst(note=True) + [rec("2026-09-24T12:30:00+00:00", "lease.acquired", ALICE)]
    assert hubmod.taken_from_history(back, ALICE) is None
    assert hubmod.taken_from_history(revoke_burst(note=True), BOB) is None      # bob was not kicked
    assert hubmod.taken_from_history([], ALICE) is None


def test_a_note_far_from_the_revoke_is_not_its_reason():
    evs = revoke_burst(note=True)
    evs[2]["ts"] = "2026-09-24T11:00:00+00:00"                     # an hour before: another revoke
    evs.sort(key=lambda e: hubmod._ts_key(e["ts"]))
    assert hubmod.taken_from_history(evs, ALICE)["reason"] == ""


# -- names, times, reasons ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mps3_01_pl", "a", "A-b.c_9", "x" * 64])
def test_valid_names(name):
    assert hubmod.valid_name(name)


@pytest.mark.parametrize("name", ["", ".", "..", "...", "x" * 65, "a/b", "a b", "a;b", "$(x)",
                                  "é", "a\nb", None, 7])
def test_negative_twin_names_that_are_not_allowed(name):
    assert not hubmod.valid_name(name)


def test_times_parse_with_z_offset_or_none():
    assert hubmod.parse_ts("2026-09-24T12:00:00Z") == hubmod.parse_ts("2026-09-24T12:00:00+00:00")
    assert hubmod.parse_ts("2026-09-24T13:00:00+01:00") == hubmod.parse_ts("2026-09-24T12:00:00")
    assert hubmod.parse_ts(hubmod.utc_now()) is not None


#: The only shape Python 3.10's datetime.fromisoformat reads (3.11+ reads more).
PY310_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?([+-]\d{2}:\d{2})?")


@pytest.mark.parametrize("value,want", [
    ("2026-09-24T12:03:59.9+00:00", "2026-09-24T12:03:59.900000+00:00"),     # CI: 3.10 said None
    ("2026-09-24T12:04:00.123Z", "2026-09-24T12:04:00.123000+00:00"),
    ("2026-09-24T12:04:00.1234567z", "2026-09-24T12:04:00.123456+00:00"),
    ("2026-09-24 13:04:00+0100", "2026-09-24T13:04:00+01:00"),
    ("2026-09-24T12:04+01", "2026-09-24T12:04:00+01:00"),
    ("2026-09-24T12:04:00,5", "2026-09-24T12:04:00.500000"),
    (" 2026-09-24T12:04:00.123456+00:00 ", "2026-09-24T12:04:00.123456+00:00"),
])
def test_times_are_normalised_to_what_python_3_10_reads(value, want):
    got = hubmod.iso_for_fromisoformat(value)
    assert got == want and PY310_ISO.fullmatch(got)
    assert hubmod.parse_ts(value) == hubmod.parse_ts(want) is not None


@pytest.mark.parametrize("value", ["", "yesterday", "2026-13-01T00:00:00", None, 1727179200,
                                   "2026-09-24", "2026-09-24T12:00:00+00:00 junk", "12:00:00"])
def test_negative_twin_times_that_do_not_parse(value):
    assert hubmod.parse_ts(value) is None


# -- merging our revoke notes into fpgahub's history ---------------------------------------------


def ev(ts: str, event: str, **kw) -> dict:
    return {"ts": ts, "event": event, **kw}


def note(ts: str, nid: str = "n") -> dict:
    return {"ts": ts, "event": hubmod.ADMIN_REVOKED, "id": nid}


T0, T1, T2 = "2026-09-24T12:00:00+00:00", "2026-09-24T12:00:00.015625+00:00", "2026-09-24T12:00:01Z"


def test_a_note_lands_between_the_events_either_side_of_it():
    hist = [ev(T0, "lease.acquired"), ev(T2, "lease.revoked")]
    assert [e["event"] for e in hubmod.merge_history(hist, [note(T1)])] == [
        "lease.acquired", hubmod.ADMIN_REVOKED, "lease.revoked"]


def test_negative_twin_a_tie_puts_fpgahubs_event_first_every_time():
    """Windows' ~16 ms clock gives equal times: the result must not depend on it."""
    hist = [ev(T0, "lease.acquired"), ev(T1, "lease.queued"), ev(T1, "lease.revoked"),
            ev(T1, "lease.promoted")]
    notes = [note(T1, "b"), note(T1, "a")]
    got = hubmod.merge_history(hist, notes)
    assert [e["event"] for e in got] == [e["event"] for e in hist] + [hubmod.ADMIN_REVOKED] * 2
    assert [e["id"] for e in got[-2:]] == ["a", "b"]                        # then by id
    assert hubmod.merge_history(hist, list(reversed(notes))) == got


def test_fpgahubs_own_order_is_never_changed():
    hist = [ev(T2, "lease.acquired"), ev(T0, "lease.queued"), ev("garbled", "lease.revoked")]
    got = hubmod.merge_history(hist, [note(T1)])
    assert [e for e in got if e["event"] != hubmod.ADMIN_REVOKED] == hist
    assert hubmod.merge_history(hist, []) == hist and hubmod.merge_history([], [note(T1)]) == [note(T1)]


def test_revoke_reasons_are_one_line_and_bounded():
    assert hubmod.check_reason("force-released by bob@mapstone-dev via Harness Manager: x")
    assert hubmod.check_reason("x" * hubmod.REASON_MAX)
    for bad in ("", "  ", "x" * (hubmod.REASON_MAX + 1), "a\nb", "a\x1b[2Jb", None):
        with pytest.raises(UsageError):
            hubmod.check_reason(bad)
