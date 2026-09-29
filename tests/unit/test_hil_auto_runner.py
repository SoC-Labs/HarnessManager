"""HIL-AUTO: the runner's safety rules, against a scripted (counting) Harness Manager CLI.

``ScriptedHm`` answers every verb with the JSON the real CLI prints; a test queues other
answers to make the board, the hub or the lease misbehave. Every rule has its negative twin.
The real CLI against the virtual boards is ``tests/integration/test_hil_auto_virtual.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.checks import plans as P
from harness_manager.checks.run import EXIT_FAIL, EXIT_PASS, EXIT_STOP, Options, Runner, main
from tests.fakes.hil_auto import ScriptedHm, held, is_write, refused, unreachable, verb_of

B = "192.168.10.101"
SID = "0x44ee76d5"


class Sleeps(list):
    """A sleep that only records, and the clock it moves (no test waits in real time)."""

    def __init__(self, start: float = 1_790_000_000.0) -> None:
        super().__init__()
        self.now = start

    def __call__(self, seconds: float) -> None:
        self.append(round(seconds, 1))
        self.now += max(seconds, 0.0)

    def clock(self) -> float:
        return self.now


def run(ev: Path, hm: ScriptedHm, *extra: str, plan: str = "linux-netboot",
        sleeps: Sleeps | None = None, clock=None) -> int:
    sleeps = sleeps if sleeps is not None else Sleeps()
    return main(["run", "--plan", plan, "--board", B, "--evidence", str(ev), "--gap", "1",
                 *extra], invoker=hm, sleep=sleeps, clock=clock or sleeps.clock)


def summary(ev: Path) -> dict:
    return json.loads((ev / "summary.json").read_text())


def verdicts(ev: Path) -> dict[str, str]:
    return {c["id"]: c["verdict"] for c in summary(ev)["checks"]}


def bare() -> ScriptedHm:
    return ScriptedHm(static="0x72bb0a36", impl="bare-metal")


# --- the happy runs ------------------------------------------------------------------------------


@pytest.mark.parametrize("plan,hm", [("linux-netboot", ScriptedHm()),
                                     ("linux-nocard", ScriptedHm(card=False)),
                                     ("linux", ScriptedHm(netboot=False, claim="mine")),
                                     ("bare-metal", bare())])
def test_a_healthy_board_passes_every_automatic_check(tmp_path, plan, hm):
    rc = run(tmp_path / "ev", hm, "--writes", "safe", plan=plan)
    s = summary(tmp_path / "ev")
    assert rc == EXIT_PASS and s["exit"] == 0 and s["totals"]["fail"] == 0
    assert s["end_state"]["greybox"] is True and hm.rm == P.GREYBOX_RM
    assert (tmp_path / "ev" / "REPORT.md").read_text().startswith(f"# HIL-AUTO: {plan}")
    # the evidence: one file per check that ran, named as the runbook names it
    first = "a1_info.json" if plan.startswith("linux") else "r1_info.json"
    ev = json.loads((tmp_path / "ev" / first).read_text())
    assert ev["verdict"] == "pass" and ev["command"].startswith("harness-manager --json info")
    assert ev["stdout_json"]["identity"]["shell_id"] and ev["seconds"] >= 0


# --- 1. the lease: held HERE, or nothing starts ----------------------------------------------------


@pytest.mark.parametrize("holder,here", [(None, False), ("david-hm", False), ("soak", False)])
def test_no_lease_here_refuses_to_start_and_sends_nothing_else(tmp_path, holder, here):
    hm = ScriptedHm()
    hm.lease_holder, hm.lease_here = holder, here
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    assert rc == EXIT_STOP
    assert hm.verbs() == ["lease show"]                       # the gate, then nothing
    s = summary(tmp_path / "ev")
    assert s["refused_start"].startswith("refused to start") and s["iterations"] == []
    assert "STOPPED: refused to start" in (tmp_path / "ev" / "REPORT.md").read_text()


def test_twin_a_lease_held_here_starts(tmp_path):
    hm = ScriptedHm()
    assert run(tmp_path / "ev", hm) == EXIT_PASS
    assert "info" in hm.verbs()


def test_the_runner_never_touches_the_lease(tmp_path):
    hm = ScriptedHm()
    run(tmp_path / "ev", hm, "--writes", "safe", "--repeat", "2", "--interval", "60")
    assert {v for v in hm.verbs() if v.startswith("lease")} == {"lease show"}


def test_a_lease_lost_mid_run_stops_before_the_next_section_and_leaves_the_board(tmp_path):
    hm = ScriptedHm()
    original = hm.v_program

    def program_then_lose(argv):
        out = original(argv)
        hm.lease_here, hm.lease_holder = False, "someone-else"      # revoked while swapping
        return out

    hm.v_program = program_then_lose
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and "the lease was lost" in s["stopped"]["reason"]
    # nothing is sent to a board somebody else now holds: no restore, no info after the loss
    after = hm.verbs()[hm.verbs().index("program") + 1:]
    assert set(after) <= {"info", "lease show"} and "restore" not in after
    assert s["end_state"]["greybox"] is False and "NOT attempted" in s["end_state"]["restore"]


# --- 2. --writes none never sends a write -----------------------------------------------------------


@pytest.mark.parametrize("plan,hm", [("linux-netboot", ScriptedHm()),
                                     ("linux-nocard", ScriptedHm(card=False)),
                                     ("linux", ScriptedHm(netboot=False, claim="mine")),
                                     ("bare-metal", bare())])
def test_writes_none_never_sends_a_write_verb(tmp_path, plan, hm):
    rc = run(tmp_path / "ev", hm, "--repeat", "2", "--interval", "60", plan=plan)
    assert rc == EXIT_PASS and hm.calls
    assert [a for a in hm.calls if is_write(a)] == []
    assert {"program", "restore", "mcc temp", "identify"}.isdisjoint(hm.verbs())
    skipped = {c["id"] for c in summary(tmp_path / "ev" / "iter-001")["checks"]
               if c["reason"].startswith("--writes none")}
    assert skipped == {c.id for c in P.build(plan).checks() if c.tier == P.SAFE and not c.skip}


def test_the_nocard_plan_on_a_card_less_board_sends_no_card_slot_or_reset_verb(tmp_path):
    hm = ScriptedHm(card=False)
    rc = run(tmp_path / "ev", hm, "--writes", "safe", "--repeat", "2", "--interval", "60",
             plan="linux-nocard")
    assert rc == EXIT_PASS
    assert [verb_of(a) for a in hm.calls if is_write(a)] == ["identify", "mcc temp", "program",
                                                             "restore"] * 2
    assert not {"card status", "slot push", "slot commit", "slot rollback", "card clear",
                "mcc reboot", "mcc cmd", "reset", "power", "sd"} & set(hm.verbs())
    v = verdicts(tmp_path / "ev" / "iter-001")
    assert v["C1"] == "pass" and v["C2"] == v["D4"] == v["F6"] == v["G3"] == "skipped"


def test_twin_the_nocard_plan_fails_c1_on_a_board_with_a_card(tmp_path):
    rc = run(tmp_path / "ev", ScriptedHm(), plan="linux-nocard")       # a blank card is in
    s = summary(tmp_path / "ev")
    assert rc == EXIT_FAIL and s["first_failure"]["id"] == "C1"
    assert s["first_failure"]["reason"].startswith("exit 0, expected 12")


def test_twin_writes_safe_sends_the_swaps_and_the_mcc_read_and_nothing_else(tmp_path):
    hm = ScriptedHm()
    run(tmp_path / "ev", hm, "--writes", "safe")
    writes = [verb_of(a) for a in hm.calls if is_write(a)]
    assert writes == ["identify", "mcc temp", "program", "restore"]
    assert not any("--keep-on-card" in a or "--force" in a for a in hm.calls)


def test_a_write_hidden_in_a_read_check_is_refused_unsent_by_the_allow_list(tmp_path):
    """The tier in the plan is data; the allow-list in ``Runner.invoke`` is the rule."""
    bad = P.Check("A2", "A", "a doctored check", P.READ,
                  ("program", "{B}", "nanosoc", "--yes", "--keep-on-card"),
                  (P.E("ok", "true"),), evidence="a2_bad")
    plan = P.build("linux-netboot")
    sections = tuple(P.Section(s.id, s.title, tuple(bad if c.id == "A2" else c
                                                    for c in s.checks))
                     for s in plan.sections)
    doctored = P.Plan(plan.name, plan.runbook, plan.static, plan.impl, sections)
    hm = ScriptedHm()
    sl = Sleeps()
    r = Runner(doctored, Options(board=B, evidence=tmp_path / "ev", writes="safe", gap_s=1),
               hm, sleep=sl, clock=sl.clock, log=lambda _t: None)
    assert r.run() == EXIT_STOP
    assert "program" not in hm.verbs()
    assert "refused to send" in r.iterations[0].stopped["reason"]


# --- 3. stops -----------------------------------------------------------------------------------------


def test_an_identity_mismatch_stops_with_exit_2_and_restores_greybox(tmp_path):
    hm = ScriptedHm()
    hm.static_after_swap = "0x12345678"               # the board is not what we loaded onto
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP
    assert s["stopped"]["check"] == "E1b" and "unexpected identity" in s["stopped"]["reason"]
    assert verdicts(tmp_path / "ev")["E1b"] == "stopped"
    assert verdicts(tmp_path / "ev")["Z2"] == "skipped"          # the plan stopped ...
    assert hm.verbs().count("restore") == 1 and hm.rm == P.GREYBOX_RM   # ... the finally did it
    assert s["end_state"]["greybox"] is True
    assert "STOPPED for safety" in (tmp_path / "ev" / "REPORT.md").read_text()


def test_twin_the_expected_identity_runs_to_the_end_with_one_restore_from_the_plan(tmp_path):
    hm = ScriptedHm()
    assert run(tmp_path / "ev", hm, "--writes", "safe") == EXIT_PASS
    assert hm.verbs().count("restore") == 1 and verdicts(tmp_path / "ev")["Z2"] == "pass"


def test_a_wrong_static_at_the_first_identity_stops_before_any_change(tmp_path):
    hm = ScriptedHm(static="0x61bc6789")               # rolled back to another mint
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and s["stopped"]["check"] == "A1"
    assert "program" not in hm.verbs() and "restore" not in hm.verbs()
    assert s["end_state"]["restore"].startswith("not needed")


def test_twin_expect_static_names_the_board_that_is_there(tmp_path):
    hm = ScriptedHm(static="0x61bc6789")
    assert run(tmp_path / "ev", hm, "--writes", "safe", "--expect-static", "0x61BC6789") == 0


def test_a_reset_guard_refusal_stops_and_is_never_forced(tmp_path):
    hm = ScriptedHm()
    hm.add("program", held("slot B is being written (12.3/29 MB); a reset now can wedge the "
                           "card. Wait ~6 min"))
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and s["stopped"]["check"] == "E1"
    assert "never forces" in s["stopped"]["reason"]
    assert hm.verbs().count("program") == 1                           # never retried
    assert not any("--force" in a or "--consent" in a for a in hm.calls)
    # the refused swap changed nothing: the finally reads greybox and sends no restore
    assert "restore" not in hm.verbs() and s["end_state"]["restore"].startswith("on greybox at the end")


def test_twin_a_guard_refusal_of_the_final_restore_is_waited_out_never_forced(tmp_path):
    hm = ScriptedHm()
    hm.static_after_swap = "0x12345678"                         # stop after the swap
    busy = held("slot B is being read back (verifying); a reset now can wedge the card")
    hm.add("restore", busy, busy)
    sleeps = Sleeps()
    rc = run(tmp_path / "ev", hm, "--writes", "safe", sleeps=sleeps)
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and hm.verbs().count("restore") == 3
    assert sleeps.count(60.0) == 2 and s["end_state"]["greybox"] is True
    assert not any("--force" in a for a in hm.calls)


@pytest.mark.parametrize("answer,words", [
    (held("another client holds the control channel"), "HELD"),
    (refused("xvc locked: board claimed (use ssh)"), "refused (exit 15)"),
])
def test_held_or_a_claim_lock_refusal_stops(tmp_path, answer, words):
    hm = ScriptedHm()
    hm.add("panel show", answer)
    rc = run(tmp_path / "ev", hm)
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and s["stopped"]["check"] == "A2" and words in s["stopped"]["reason"]
    assert verdicts(tmp_path / "ev")["A3"] == "skipped"                # nothing after a stop


def test_an_unreachable_read_backs_off_and_then_passes(tmp_path):
    hm = ScriptedHm()
    hm.add("xvc status", unreachable(), unreachable())
    sleeps = Sleeps()
    assert run(tmp_path / "ev", hm, sleeps=sleeps) == EXIT_PASS
    a3 = next(c for c in summary(tmp_path / "ev")["checks"] if c["id"] == "A3")
    assert a3["verdict"] == "pass" and a3["attempts"] == 3
    assert [s for s in sleeps if s >= 30] == [30.0, 60.0]              # QUIET-POLL's back-off


def test_twin_unreachable_more_than_n_times_stops(tmp_path):
    hm = ScriptedHm()
    hm.add("xvc status", unreachable(), unreachable(), unreachable())
    rc = run(tmp_path / "ev", hm, "--max-unreachable", "3")
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and "unreachable 3 times at A3" in s["stopped"]["reason"]
    assert hm.verbs().count("xvc status") == 3


def test_an_unreachable_write_is_never_retried(tmp_path):
    hm = ScriptedHm()
    hm.add("program", unreachable("the push timed out"))
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    assert rc == EXIT_STOP and hm.verbs().count("program") == 1
    assert "never retried" in summary(tmp_path / "ev")["stopped"]["reason"]


# --- the MCC read: tty_00 busy is a skip, never a failure, never retried --------------------------


def test_tty00_busy_is_skipped_not_failed_and_not_retried(tmp_path):
    hm = ScriptedHm()
    hm.add("mcc temp", held("another process on the hub names the MCC console "
                            "/dev/mps3_01_pl/tty_00 on its command line, so it may open it at "
                            "any moment (pid 4242: python3 soak_linux.py): nothing was typed"))
    assert run(tmp_path / "ev", hm, "--writes", "safe") == EXIT_PASS
    d4a = next(c for c in summary(tmp_path / "ev")["checks"] if c["id"] == "D4a")
    assert d4a["verdict"] == "skipped" and d4a["reason"].startswith("tty_00 busy")
    assert hm.verbs().count("mcc temp") == 1


def test_twin_a_held_mcc_read_that_is_not_tty00_stops(tmp_path):
    hm = ScriptedHm()
    hm.add("mcc temp", held("another client holds the control channel"))
    assert run(tmp_path / "ev", hm, "--writes", "safe") == EXIT_STOP


# --- expectations ------------------------------------------------------------------------------


def test_a_failed_expectation_is_reported_with_its_runbook_id(tmp_path):
    hm = ScriptedHm()
    hm.panel_source = "garbled"
    rc = run(tmp_path / "ev", hm)
    s = summary(tmp_path / "ev")
    assert rc == EXIT_FAIL
    assert s["first_failure"]["id"] == "A2" and "panel.source" in s["first_failure"]["reason"]
    assert verdicts(tmp_path / "ev")["A3"] == "pass"                   # a failure is not a stop
    report = (tmp_path / "ev" / "REPORT.md").read_text()
    assert "**FAIL** (exit 1)" in report and "**A2** Front panel: panel.source" in report
    assert "Evidence: `a2_panel.json`" in report


def test_twin_stop_on_first_fail_ends_there_but_still_restores(tmp_path):
    hm = ScriptedHm()
    hm.add("info", lambda h, a: h.v_info(a), lambda h, a: (0, {
        "ok": True, "identity": {"shell_id": SID, "harness_impl": "linux", "rm_id": "0x0100000a"},
        "health": {"reachable": False}}))
    rc = run(tmp_path / "ev", hm, "--writes", "safe", "--stop-on-first-fail")
    s = summary(tmp_path / "ev")
    assert rc == EXIT_FAIL and s["first_failure"]["id"] == "E1b"
    assert verdicts(tmp_path / "ev")["Z2"] == "skipped" and hm.rm == P.GREYBOX_RM


# --- --repeat, --until, signals -----------------------------------------------------------------------


def test_repeat_3_writes_three_folders_and_an_aggregate(tmp_path):
    hm = ScriptedHm()
    hm.add("panel show", lambda h, a: h.v_panel_show(a),
           lambda h, a: (0, {"ok": True, "panel": {"source": "weird", "touch": {}}}))
    sleeps = Sleeps()
    rc = run(tmp_path / "ev", hm, "--writes", "safe", "--repeat", "3", "--interval", "120",
             sleeps=sleeps)
    ev = tmp_path / "ev"
    assert rc == EXIT_FAIL
    for n in (1, 2, 3):
        assert json.loads((ev / f"iter-{n:03d}" / "summary.json").read_text())["iteration"] == n
        assert (ev / f"iter-{n:03d}" / "REPORT.md").exists()
        assert (ev / f"iter-{n:03d}" / "a1_info.json").exists()
    agg = summary(ev)
    assert [i["exit"] for i in agg["iterations"]] == [0, 1, 0]
    assert agg["failed_in"] == {"A2": [2]}
    assert hm.verbs().count("program") == 3 and hm.verbs().count("restore") == 3
    assert sum(s for s in sleeps if s >= 10) == pytest.approx(240, abs=5)  # the interval, x2
    report = (ev / "REPORT.md").read_text()
    assert "3 of 3 iterations" in report and "- **A2**: iteration 2" in report


def test_twin_repeat_1_writes_the_runbook_layout_straight_into_the_folder(tmp_path):
    run(tmp_path / "ev", ScriptedHm())
    assert not list((tmp_path / "ev").glob("iter-*"))
    assert (tmp_path / "ev" / "a1_info.json").exists()


def test_until_stops_cleanly_mid_plan_restores_and_exits(tmp_path):
    t = {"now": 1_790_000_000.0}

    def clock() -> float:
        t["now"] += 10.0          # every look at the clock is 10 s later
        return t["now"]

    hm = ScriptedHm()
    from datetime import datetime
    end = datetime.fromtimestamp(t["now"] + 100 + 60).astimezone().isoformat()
    rc = run(tmp_path / "ev", hm, "--writes", "safe", "--repeat", "5", "--interval", "60",
             "--until", end, "--margin", "1", clock=clock)
    s = summary(tmp_path / "ev")
    assert rc == EXIT_PASS
    ran = [i for i in s["iterations"]]
    assert len(ran) == 1 and ran[0]["ended_early"].startswith("the deadline")
    assert hm.rm == P.GREYBOX_RM


def test_until_with_hh_mm_is_the_next_such_time():
    from datetime import datetime

    from harness_manager.checks.run import parse_until
    now = datetime(2026, 9, 28, 18, 0).astimezone()
    assert parse_until("08:30", now) == now.replace(hour=8, minute=30) + __import__(
        "datetime").timedelta(days=1)
    assert parse_until("19:15", now) == now.replace(hour=19, minute=15)


def test_a_signal_finishes_the_check_restores_and_stops(tmp_path):
    hm = ScriptedHm()
    plan = P.build("linux-netboot")
    sl = Sleeps()
    r = Runner(plan, Options(board=B, evidence=tmp_path / "ev", writes="safe", gap_s=1),
               hm, sleep=sl, clock=sl.clock, log=lambda _t: None)
    original = hm.v_program

    def program_then_signal(argv):
        out = original(argv)
        r.halt.set()                                  # Ctrl-C while the swap runs
        return out

    hm.v_program = program_then_signal
    assert r.run() == EXIT_STOP
    assert r.iterations[0].ended_early == "interrupted by a signal"
    assert hm.rm == P.GREYBOX_RM and r.end["greybox"] is True


# --- the announcement and the end state ------------------------------------------------------------


def test_announce_only_writes_the_announcement_and_sends_nothing(tmp_path, capsys):
    hm = ScriptedHm()
    rc = run(tmp_path / "ev", hm, "--writes", "safe", "--until", "08:30", "--repeat", "40",
             "--interval", "1200", "--announce-only")
    text = (tmp_path / "ev" / "ANNOUNCE.txt").read_text()
    assert rc == EXIT_PASS and hm.calls == []
    assert "plan linux-netboot" in text and "writes safe" in text
    assert "E1 Load the ILA design" in text and "D4a" in text and "never " in text
    assert "no MCC REBOOT" in text and text == capsys.readouterr().out
    # the run itself may then use the same folder (it holds only the announcement)
    assert run(tmp_path / "ev", hm, "--writes", "safe") == EXIT_PASS


def test_twin_a_folder_with_evidence_is_never_overwritten(tmp_path):
    run(tmp_path / "ev", ScriptedHm())
    hm = ScriptedHm()
    assert run(tmp_path / "ev", hm) == EXIT_STOP and hm.calls == []


def test_the_end_state_records_the_claim_as_found_and_says_there_is_no_unclaim_verb(tmp_path):
    hm = ScriptedHm(claim="other")
    run(tmp_path / "ev", hm, "--writes", "safe")
    claim = summary(tmp_path / "ev")["end_state"]["claim"]
    assert claim["start"] == claim["end"] == "other" and claim["unchanged"] is True
    assert "no verb to unclaim" in claim["note"]
    assert not any(verb_of(a) == "board claim" for a in hm.calls)


def test_twin_bare_metal_has_no_claim_in_its_end_state(tmp_path):
    run(tmp_path / "ev", bare(), "--writes", "safe", plan="bare-metal")
    assert "claim" not in summary(tmp_path / "ev")["end_state"]


@pytest.mark.parametrize("args", [["--interval", "5", "--repeat", "2"], ["--gap", "0"],
                                  ["--expect-static", "44ee76d5"], ["--until", "25:99x"]])
def test_no_tight_loops_and_no_bad_arguments(tmp_path, args):
    hm = ScriptedHm()
    assert run(tmp_path / "ev", hm, *args) == EXIT_STOP and hm.calls == []


def test_the_commands_are_paced(tmp_path):
    sleeps = Sleeps()
    hm = ScriptedHm()
    run(tmp_path / "ev", hm, "--gap", "3", sleeps=sleeps)
    # every command after the first waits for the gap (the fake answers at once)
    assert len([s for s in sleeps if 2.5 <= s <= 3.0]) >= len(hm.calls) - 1


def test_tty00_busy_as_the_cli_really_says_it_an_unavailable_reading_is_skipped(tmp_path):
    hm = ScriptedHm()
    hm.add("mcc temp", lambda h, a: (0, {"ok": True, "readings": [{
        "name": "mcc_temp", "available": False, "value": None, "source": "mcc-console (hub)",
        "reason": "another process on the hub has the MCC console /dev/mps3_01_pl/tty_00 open "
                  "(pid 7: fpgahub share): nothing was typed"}]}))
    assert run(tmp_path / "ev", hm, "--writes", "safe") == EXIT_PASS
    assert verdicts(tmp_path / "ev")["D4a"] == "skipped"


def test_twin_an_unavailable_reading_for_another_reason_is_a_failure(tmp_path):
    hm = ScriptedHm()
    hm.add("mcc temp", lambda h, a: (0, {"ok": True, "readings": [{
        "name": "mcc_temp", "available": False, "value": None, "source": "mcc-console (hub)",
        "reason": "no MCC prompt on /dev/mps3_01_pl/tty_00 after a bare CR"}]}))
    assert run(tmp_path / "ev", hm, "--writes", "safe") == EXIT_FAIL
    assert verdicts(tmp_path / "ev")["D4a"] == "fail"


# --- the subprocess invoker (what david's run uses) ---------------------------------------------------


def test_the_subprocess_invoker_passes_argv_and_exit_and_gives_no_stdin():
    import sys

    from harness_manager.checks.run import SubprocessInvoker
    inv = SubprocessInvoker([sys.executable, "-c",
                             "import json, sys; d = sys.stdin.read(); "
                             "print(json.dumps({'argv': sys.argv[1:], 'stdin': d})); "
                             "sys.exit(3)"])
    out = inv(["--json", "info", B], 30.0)
    assert out.rc == 3 and json.loads(out.stdout) == {"argv": ["--json", "info", B], "stdin": ""}


def test_twin_a_command_that_outlives_its_timeout_is_stopped_and_has_no_exit():
    import sys

    from harness_manager.checks.run import SubprocessInvoker
    out = SubprocessInvoker([sys.executable, "-c", "import time; time.sleep(60)"])([], 1.0)
    assert out.rc is None and "timed out after 1 s" in out.stderr and out.seconds < 20


def test_the_default_runner_uses_this_checkouts_cli_and_refuses_without_a_lease(tmp_path):
    """No invoker given: ``python -m harness_manager.cli.main`` as a subprocess, as david runs
    it. With no hub table for the board there is no lease to be held here."""
    rc = main(["run", "--plan", "linux-netboot", "--board", B, "--evidence",
               str(tmp_path / "ev")])
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and s["refused_start"].startswith("refused to start")
    assert "not behind a hub" in s["refused_start"]
    gate = json.loads((tmp_path / "ev" / "0_lease_gate.json").read_text())
    assert gate["stdout_json"]["error"]["name"] == "ABSENT"


# --- the lease's expiry is a deadline too ---------------------------------------------------------


def _lease_end(hm: ScriptedHm) -> float:
    from datetime import datetime
    return datetime.fromisoformat(hm.lease_expires).timestamp()


def _iso_at(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_a_lease_that_expires_within_the_margin_refuses_the_start(tmp_path):
    sleeps = Sleeps()
    hm = ScriptedHm()
    hm.lease_expires = _iso_at(sleeps.now + 5 * 60)            # 5 min left, margin 10
    rc = run(tmp_path / "ev", hm, "--writes", "safe", sleeps=sleeps)
    assert rc == EXIT_STOP and hm.verbs() == ["lease show"]
    assert "--ttl that outlasts the run" in summary(tmp_path / "ev")["refused_start"]


def test_twin_a_lease_that_expires_mid_run_ends_the_run_before_it_and_restores(tmp_path):
    sleeps = Sleeps()
    hm = ScriptedHm()
    hm.lease_expires = _iso_at(sleeps.now + 30 * 60)           # 30 min: one iteration fits
    rc = run(tmp_path / "ev", hm, "--writes", "safe", "--repeat", "10", "--interval", "600",
             sleeps=sleeps)
    agg = summary(tmp_path / "ev")
    assert rc == EXIT_PASS and 1 <= len(agg["iterations"]) < 10
    why = [agg["ended_early"] or ""] + [i["ended_early"] or "" for i in agg["iterations"]]
    assert any(w.startswith("the lease expires") for w in why)
    assert sleeps.now < _lease_end(hm)                     # it ended before the lease did
    assert hm.rm == P.GREYBOX_RM
    last = agg["iterations"][-1]
    assert last["exit"] == 0


def test_the_subprocess_invoker_never_uses_a_running_service():
    import sys

    from harness_manager.checks.run import SubprocessInvoker
    inv = SubprocessInvoker([sys.executable, "-c",
                             "import os; print(os.environ.get('HARNESS_MANAGER_NO_DAEMON'))"])
    assert inv([], 30.0).stdout.strip() == "1"


def test_a_stopped_check_keeps_its_own_evidence(tmp_path):
    hm = ScriptedHm()
    hm.add("panel show", refused("usd locked: board claimed (use ssh)"))
    run(tmp_path / "ev", hm)
    a2 = json.loads((tmp_path / "ev" / "a2_panel.json").read_text())
    assert a2["verdict"] == "stopped" and a2["exit"] == 15
    assert a2["stdout_json"]["error"]["message"].startswith("usd locked")


def test_a_hub_that_misses_a_lease_check_is_asked_again_not_a_lost_lease(tmp_path):
    hm = ScriptedHm()
    calls = {"n": 0}

    def flaky(h, a):
        calls["n"] += 1
        return (7, {"ok": False, "error": {"code": 7, "name": "UNREACHABLE",
                                           "message": "cannot reach the hub", "hint": ""}}) \
            if calls["n"] == 1 else h.v_lease_show(a)

    hm.add("lease show", lambda h, a: h.v_lease_show(a), flaky)   # the gate, then a miss
    sleeps = Sleeps()
    assert run(tmp_path / "ev", hm, sleeps=sleeps) == EXIT_PASS
    assert 30.0 in sleeps


def test_twin_a_hub_that_never_answers_the_lease_check_stops(tmp_path):
    def miss(_h, _a):
        return 7, {"ok": False, "error": {"code": 7, "name": "UNREACHABLE",
                                          "message": "cannot reach the hub", "hint": ""}}

    def show(h, a):
        return h.v_lease_show(a)

    hm = ScriptedHm()
    hm.add("lease show", show, show, miss, miss, miss)   # the gate, 0.3, then §A's check
    assert run(tmp_path / "ev", hm) == EXIT_STOP
    s = summary(tmp_path / "ev")
    assert "did not answer the lease check 3 times" in s["stopped"]["reason"]
    assert s["stopped"]["check"] == "A1" and "info" not in hm.verbs()


# --- HIL-IDLOC: A5 board identity and A6 locate on both images -----------------------------------


def _ev(ev: Path, name: str) -> dict:
    return json.loads((ev / name).read_text())


def _result(ev: Path, cid: str) -> dict:
    return next(c for c in summary(ev)["checks"] if c["id"] == cid)


@pytest.mark.parametrize("plan,card", [("linux-nocard", False), ("linux-netboot", True)])
def test_v7n_passes_a5_and_a6_with_its_identity_and_a_blink(tmp_path, plan, card):
    hm = ScriptedHm(image="v7n", card=card)
    rc = run(tmp_path / "ev", hm, "--writes", "safe", plan=plan)
    assert rc == EXIT_PASS, summary(tmp_path / "ev")["first_failure"]
    v = verdicts(tmp_path / "ev")
    assert v["A5"] == v["A6"] == "pass" and v["A4"] == "manual"
    assert hm.blinks == [5]                                    # one 5 s blink, nothing else
    a5 = _result(tmp_path / "ev", "A5")
    # the pass line names the image and what it said: the v0.16 default, no bake
    assert a5["reason"].startswith("unset: label MPS3 (source default), hostname mps3")
    assert "via identity; image 2.0.0, features usd,stats,identity,locate" in a5["reason"]
    ev = _ev(tmp_path / "ev", "a5_identity.json")
    assert ev["command"] == f"harness-manager --json board identity {B}"
    assert ev["stdout_json"]["identity"]["reported"]["source"]["label"] == "default"
    a6 = _result(tmp_path / "ev", "A6")
    assert a6["exit"] == 0 and a6["reason"].startswith("blinked 5 s")
    assert _ev(tmp_path / "ev", "a6_locate.json")["argv"] == ["identify", B, "--seconds", "5"]
    assert "A5" in (tmp_path / "ev" / "REPORT.md").read_text()


@pytest.mark.parametrize("plan,card", [("linux-nocard", False), ("linux-netboot", True)])
def test_twin_v6n_passes_a5_as_recorded_and_a6_as_not_on_this_image(tmp_path, plan, card):
    hm = ScriptedHm(image="v6n", card=card)
    rc = run(tmp_path / "ev", hm, "--writes", "safe", plan=plan)
    assert rc == EXIT_PASS, summary(tmp_path / "ev")["first_failure"]
    assert hm.blinks == []                                    # refused: nothing blinked
    a5 = _result(tmp_path / "ev", "A5")
    # board 1's hard-coded identity on this board: a clash, recorded, never a failure
    assert a5["verdict"] == "pass" and a5["reason"].startswith("clash: label - (source -)")
    assert "via identify; image 1.9.0, features usd,stats" in a5["reason"]
    a6 = _result(tmp_path / "ev", "A6")
    assert (a6["verdict"], a6["exit"]) == ("pass", 12)
    assert a6["reason"] == "not on this image: refused, exit 12 (harness feature 'locate')"


def test_twin_locate_refused_for_another_reason_fails_a6(tmp_path):
    hm = ScriptedHm(image="v6n")
    hm.add("identify", lambda _hm, _a: (12, {"ok": False, "error": {
        "code": 12, "name": "UNAVAILABLE", "hint": "",
        "message": "Identify is unavailable: this board has no front panel HM can read"}}))
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    a6 = _result(tmp_path / "ev", "A6")
    assert rc == EXIT_FAIL and a6["verdict"] == "fail"
    assert "harness feature 'locate'" in a6["reason"] and "no front panel" in a6["reason"]
    assert verdicts(tmp_path / "ev")["Z2"] == "pass"          # a fail is not a stop


@pytest.mark.parametrize("rc_body", [
    (1, {"ok": False, "error": {"code": 1, "name": "ACTION_FAILED", "hint": "",
                                "message": "locate failed: the board said invalid"}}),
    (0, {"ok": True, "board_id": B, "seconds": 0, "until_ms": 0}),     # did not blink
])
def test_twin_locate_any_other_answer_fails_a6_even_on_v7n(tmp_path, rc_body):
    hm = ScriptedHm(image="v7n")
    hm.add("identify", lambda _hm, _a: rc_body)
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    assert rc == EXIT_FAIL and _result(tmp_path / "ev", "A6")["verdict"] == "fail"


@pytest.mark.parametrize("answer", [
    lambda _hm, _a: (1, "Traceback (most recent call last):\n  ...\nKeyError: 'label'\n"),
    lambda _hm, _a: (0, "not json\n"),
    lambda _hm, _a: (0, {"ok": True, "board_id": B}),              # no identity block
    lambda _hm, _a: (0, {"ok": True, "board_id": B, "identity": {"status": "fine"}}),
])
def test_twin_the_identity_verb_crashing_or_malformed_fails_a5(tmp_path, answer):
    hm = ScriptedHm(image="v7n")
    hm.add("board identity", answer)
    rc = run(tmp_path / "ev", hm)
    a5 = _result(tmp_path / "ev", "A5")
    assert rc == EXIT_FAIL and a5["verdict"] == "fail", a5


def test_twin_a_v7n_source_outside_the_contract_fails_a5(tmp_path):
    hm = ScriptedHm(image="v7n")
    real = hm.v_board_identity

    def odd(_hm, argv):
        rc, body = real(argv)
        body["identity"]["reported"]["source"]["label"] = "guess"
        return rc, body
    hm.add("board identity", odd)
    rc = run(tmp_path / "ev", hm)
    assert rc == EXIT_FAIL and "source" in _result(tmp_path / "ev", "A5")["reason"]


@pytest.mark.parametrize("image", ["v7n", "v6n"])
def test_writes_none_skips_a6_and_still_reads_a5(tmp_path, image):
    hm = ScriptedHm(image=image, card=False)
    rc = run(tmp_path / "ev", hm, plan="linux-nocard")
    assert rc == EXIT_PASS
    v = verdicts(tmp_path / "ev")
    assert v["A5"] == "pass" and v["A6"] == "skipped"
    assert _result(tmp_path / "ev", "A6")["reason"].startswith("--writes none")
    assert "identify" not in hm.verbs() and hm.blinks == []
    assert "board identity" in hm.verbs()
