"""LR-C: the lease-request rules the daemon and the CLI share (``cli.cmd_hub``). Each check has a twin."""

from __future__ import annotations

import pytest

from harness_manager.cli import cmd_hub as h
from harness_manager.core.errors import ExitCode, UsageError

NOW = 1_790_000_000.0          # 2026-09-21T...; any fixed epoch
T = "mps3_01_pl"


def iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def view(*, mine=False, holder="alice@lab-pc", request=True, position=1, deadline_in=-1.0,
         answer=None, force_available=False, force_reason="") -> dict:
    lease = None if holder is None else {"target": T, "holder": holder, "mine": mine}
    req = None
    if request:
        req = {"id": "r1", "position": position, "deadline_at": iso(NOW + deadline_in),
               "created_at": iso(NOW + deadline_in - 120), "answer": answer,
               "force_available": force_available, "force_reason": force_reason}
    return {"lease": lease, "hub": "hub", "queue": [], "request": req, "incoming": [],
            "taken": None}


def test_force_is_open_after_the_deadline_at_the_head_with_no_answer():
    assert h.force_refusal(view(), NOW, T) is None
    assert h.force_refusal(view(force_available=True, deadline_in=+60), NOW, T) is None


def test_negative_twin_force_refusals_carry_the_reason_and_the_code():
    cases = [
        (view(deadline_in=+95), ExitCode.UNAVAILABLE, "1:35 left to answer"),
        (view(position=2), ExitCode.REFUSED, "position 2"),
        (view(position=0), ExitCode.REFUSED, "not in the queue"),
        (view(request=False), ExitCode.REFUSED, "no request"),
        (view(mine=True, holder="me@here"), ExitCode.ALREADY, "already yours"),
        (view(holder=None), ExitCode.REFUSED, "nobody holds"),
        (view(answer={"answer": "keep", "minutes": 15, "message": "B1",
                      "at": iso(NOW - 60)}), ExitCode.REFUSED, "keep"),
        (view(answer={"answer": "release", "minutes": 0, "message": "",
                      "at": iso(NOW)}), ExitCode.REFUSED, "on its way"),
    ]
    for v, code, words in cases:
        err = h.force_refusal(v, NOW, T)
        assert err is not None and err.code == code and words in err.message, (words, err)
    early = h.force_refusal(view(deadline_in=+95), NOW, T)
    assert early.data["time_left_s"] == 95 and early.data["request_id"] == "r1"
    kept = h.force_refusal(view(answer={"answer": "keep", "minutes": 15, "message": "",
                                        "at": iso(NOW - 60)}), NOW, T)
    assert kept.data["time_left_s"] == 14 * 60 and "14:00" in kept.message


def test_a_keep_that_ran_out_no_longer_blocks_force():
    ran_out = {"answer": "keep", "minutes": 5, "message": "", "at": iso(NOW - 301)}
    assert h.force_refusal(view(answer=ran_out), NOW, T) is None


def test_negative_twin_a_stale_view_before_the_deadline_is_left_to_the_service():
    # The cached view says "not available" (computed before the deadline); the deadline
    # has passed since: the service decides, rather than a refusal with a stale reason.
    assert h.force_refusal(view(force_available=False, force_reason="until 12:02"), NOW, T) is None
    # ... but a view with no deadline at all is refused with the service's reason.
    v = view(force_reason="the hub note is unreadable")
    v["request"]["deadline_at"] = ""
    err = h.force_refusal(v, NOW, T)
    assert err.code == ExitCode.REFUSED and "unreadable" in err.message


def test_messages_are_cleaned_and_capped():
    assert h.clean_message(None) == "" and h.clean_message("  a\nb\t c\x1b ") == "a b  c"
    assert h.clean_message("é" * h.MAX_MESSAGE) == "é" * h.MAX_MESSAGE


def test_negative_twin_bad_messages_ids_and_minutes_are_usage_errors():
    for bad in (7, ["x"], "x" * (h.MAX_MESSAGE + 1)):
        with pytest.raises(UsageError):
            h.clean_message(bad)
    for bad in ("", "../x", "a b", "r" * 65, None, 5):
        with pytest.raises(UsageError):
            h.request_id(bad)
    for bad in (0, 7, 90, True, "15", None):
        with pytest.raises(UsageError):
            h.keep_minutes(bad)
    assert [h.keep_minutes(m) for m in (5, 15, 30, 60)] == [5, 15, 30, 60]
    assert h.request_id("req-1.a_B") == "req-1.a_B"


def test_times_parse_from_the_hub_notes():
    assert h.parse_iso("2026-09-24T12:00:00Z") == h.parse_iso("2026-09-24T12:00:00+00:00")
    assert h.parse_iso("2026-09-24T12:00:00") == h.parse_iso("2026-09-24T12:00:00+00:00")
    assert h.fmt_left(120) == "2:00" and h.fmt_left(0.2) == "0:01" and h.fmt_left(-5) == "0:00"


def test_negative_twin_unparseable_times_are_none():
    for bad in (None, "", "soon", 12, "2026-13-40T00:00:00"):
        assert h.parse_iso(bad) is None


def test_the_tsv_row_appends_the_request_columns():
    v = view(force_available=True)
    v["queue"] = [{"position": 1, "holder": "me@here", "mine": True}]
    v["request"]["answer"] = {"answer": "keep", "minutes": 15}
    v["taken"] = {"by": "bob@x"}
    row = h._row(T, "hub", v)
    assert len(row) == len(h.LEASE_COLUMNS)
    assert row[6:] == [1, 1, "r1", "keep:15", True, 0, "bob@x"]


def test_negative_twin_an_old_view_still_fills_every_column():
    row = h._row(T, "hub", {"lease": None, "hub": "hub"})
    assert len(row) == len(h.LEASE_COLUMNS) and row[2] == "free"
    assert row[6:] == [0, None, "", "", "", 0, ""]
    assert h.full_view({"lease": None, "hub": None, "queue": None}) == {
        "lease": None, "hub": None, "queue": [], "request": None, "incoming": [], "taken": None}
