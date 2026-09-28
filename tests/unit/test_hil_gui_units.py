"""HIL-GUI units: the plan a board gets, our own service is not another holder, the two routes.

The runs through a live service are ``tests/integration/test_hil_gui_service.py``; the app's
Checks section is ``tests/web/test_hil_gui_browser.py`` (which also holds the JS ``autoPlan`` to
``AUTO_CASES`` below).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness_manager.checks import plans as P
from harness_manager.checks.run import (
    ENV_ENGINE,
    ENV_NO_DAEMON,
    ENV_STATE_DIR,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_STOP,
    ROUTE_SERVICE,
    SERVICE_ENGINE,
    SubprocessInvoker,
)
from tests.fakes.hil_auto import ScriptedHm, held, verb_of
from tests.unit.test_hil_auto_runner import Sleeps, run, summary, verdicts

# --- which plan fits a board (one table for Python and the app's JS) ------------------------------

LINUX = {"harness_impl": "linux", "features": ["usd"]}
SLOTS_EMPTY = {"A": {"state": "empty"}, "B": {"state": "empty"}}
SLOTS_VALID = {"A": {"state": "valid"}, "B": {"state": "empty"}}

#: (what the board says, the plan). tests/web/test_hil_gui_browser.py feeds the same rows to
#: checks.js ``autoPlan``.
AUTO_CASES = [
    ({"harness_impl": "bare-metal"}, None, "bare-metal"),
    ({"harness_impl": ""}, None, "bare-metal"),
    (None, None, "bare-metal"),
    (LINUX, {"store": True, "present": False, "state": ""}, "linux-nocard"),
    (LINUX, {"store": True, "present": True, "state": "valid",
             "os_slots": {"slots": SLOTS_EMPTY}}, "linux-netboot"),
    (LINUX, {"store": True, "present": True, "state": "valid",
             "os_slots": {"slots": SLOTS_VALID}}, "linux"),
    (LINUX, {"store": True, "present": True, "state": "valid"}, "linux"),
    (LINUX, {"store": True, "present": True, "state": "empty"}, "linux-netboot"),
    (LINUX, None, "linux-netboot"),
]


@pytest.mark.parametrize("identity,card,plan", AUTO_CASES)
def test_the_auto_plan_per_board_kind(identity, card, plan):
    got, why = P.auto_plan(identity, card)
    assert got == plan and why
    assert got in P.PLANS


def test_twin_the_auto_plan_says_when_it_assumed_the_card():
    assert "not read" in P.auto_plan(LINUX, None)[1]
    assert "not read" not in P.auto_plan(LINUX, AUTO_CASES[3][1])[1]


def test_the_auto_cases_file_the_browser_test_reads_is_current():
    path = Path(__file__).resolve().parents[1] / "web" / "hil_gui_auto_cases.json"
    assert json.loads(path.read_text()) == [[i, c, p] for i, c, p in AUTO_CASES], \
        "regenerate it: python -m tests.unit.test_hil_gui_units"


# --- our own service is not another holder ------------------------------------------------------------


def own_request(message: str = "busy with this Harness Manager's own request on the board"):
    def answer(_hm, _argv):
        return 4, {"ok": False, "error": {
            "code": 4, "name": "HELD", "message": message, "hint": "",
            "holder": "harness-manager (this process)", "data": {"reason": "OWN_REQUEST"}}}
    return answer


def test_our_own_services_request_on_a_read_is_asked_again_not_a_stop(tmp_path):
    hm = ScriptedHm()
    hm.add("info", own_request())
    sleeps = Sleeps()
    rc = run(tmp_path / "ev", hm, "--writes", "safe", sleeps=sleeps)
    assert rc == EXIT_PASS, summary(tmp_path / "ev")["first_failure"]
    assert [verb_of(a) for a in hm.calls].count("info") >= 3        # A1 twice, E1b, Z2b
    assert 30.0 in sleeps                                           # the back-off


def test_our_own_services_request_on_a_write_fails_that_check_and_the_run_goes_on(tmp_path):
    hm = ScriptedHm()
    hm.add("program", own_request())
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    v = verdicts(tmp_path / "ev")
    s = summary(tmp_path / "ev")
    assert rc == EXIT_FAIL and v["E1"] == "fail" and s["stopped"] is None
    assert "not another holder" in s["first_failure"]["reason"]
    assert [verb_of(a) for a in hm.calls].count("program") == 1    # a write is never retried
    assert s["end_state"]["greybox"] is True                         # attempted: restored


def test_twin_held_by_another_holder_still_stops(tmp_path):
    hm = ScriptedHm()
    hm.add("info", held("the board's control port is in use by another client"))
    rc = run(tmp_path / "ev", hm, "--writes", "safe")
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and s["stopped"]["check"] == "A1" and "HELD" in s["stopped"]["reason"]


# --- the lease kept by the service is not a deadline -------------------------------------------------


def _runner(tmp_path: Path, hm: ScriptedHm, sleeps: Sleeps, **kw):
    from harness_manager.checks.run import Options, Runner

    opts = Options(board="192.168.10.101", evidence=tmp_path / "ev", writes="none", repeat=3,
                   interval_s=1800, gap_s=1, **kw)
    return Runner(P.build("linux-netboot"), opts, hm, clock=sleeps.clock, sleep=sleeps,
                  log=lambda _t: None)


def _expires_in(hm: ScriptedHm, sleeps: Sleeps, seconds: float) -> None:
    from datetime import datetime, timezone

    hm.lease_expires = datetime.fromtimestamp(sleeps.now + seconds, timezone.utc).isoformat()


def test_a_lease_the_service_keeps_is_not_a_deadline(tmp_path):
    sleeps, hm = Sleeps(), ScriptedHm()
    _expires_in(hm, sleeps, 5 * 60)                      # would refuse the start in-process
    r = _runner(tmp_path, hm, sleeps, lease_kept=True, route=ROUTE_SERVICE)
    assert r.run() == EXIT_PASS and len(r.iterations) == 3 and not r.ended_early


def test_twin_not_kept_the_same_lease_refuses_the_start(tmp_path):
    sleeps, hm = Sleeps(), ScriptedHm()
    _expires_in(hm, sleeps, 5 * 60)
    r = _runner(tmp_path, hm, sleeps)
    assert r.run() == EXIT_STOP and r.refused_start.startswith("refused to start: the lease")


def test_twin_a_kept_lease_must_still_be_held_here(tmp_path):
    sleeps, hm = Sleeps(), ScriptedHm()
    hm.lease_here = False
    r = _runner(tmp_path, hm, sleeps, lease_kept=True, route=ROUTE_SERVICE)
    assert r.run() == EXIT_STOP and "not by this Harness Manager" in r.refused_start


def test_the_runner_notifies_a_watcher_and_a_broken_watcher_changes_nothing(tmp_path):
    sleeps, hm = Sleeps(), ScriptedHm()
    seen: list[str] = []

    def watcher(kind, _data):
        seen.append(kind)
        raise RuntimeError("a watcher bug")

    from harness_manager.checks.run import Options, Runner

    opts = Options(board="192.168.10.101", evidence=tmp_path / "ev", writes="safe", gap_s=1)
    r = Runner(P.build("linux-netboot"), opts, hm, clock=sleeps.clock, sleep=sleeps,
               log=lambda _t: None, notify=watcher)
    assert r.run() == EXIT_PASS
    assert {"iteration", "check", "result", "iteration_end", "end_state", "end"} <= set(seen)


def test_a_stop_is_said_as_a_stop_in_the_report_not_a_safety_stop(tmp_path):
    from harness_manager.checks.run import Options, Runner

    sleeps, hm = Sleeps(), ScriptedHm()
    opts = Options(board="192.168.10.101", evidence=tmp_path / "ev", writes="safe", gap_s=1,
                   route=ROUTE_SERVICE, lease_kept=True)
    r = Runner(P.build("linux-netboot"), opts, hm, clock=sleeps.clock, sleep=sleeps,
               log=lambda _t: None)
    original = hm.v_program

    def program_then_stop(argv):
        out = original(argv)
        r.halt.set()                                    # the app's Stop while the swap runs
        return out

    hm.v_program = program_then_stop
    assert r.run() == EXIT_STOP and r.end["greybox"] is True
    report = (tmp_path / "ev" / "REPORT.md").read_text()
    assert "STOPPED: asked to stop" in report and "for safety" not in report
    assert r.iterations[0].ended_early == "interrupted by a signal (Stop in the app)"


# --- the two routes -------------------------------------------------------------------------------------


def _env_of(inv: SubprocessInvoker) -> dict[str, str]:
    code = ("import json, os; print(json.dumps({k: os.environ.get(k) for k in "
            f"({ENV_NO_DAEMON!r}, {ENV_ENGINE!r}, {ENV_STATE_DIR!r})}}))")
    inv.base = [sys.executable, "-c", code]
    return json.loads(inv([], 30.0).stdout)


def test_the_service_route_goes_only_to_the_service_of_that_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_NO_DAEMON, "1")                    # even when the caller had it
    env = _env_of(SubprocessInvoker([], route=ROUTE_SERVICE, state_dir=tmp_path / "svc"))
    assert env[ENV_NO_DAEMON] is None and env[ENV_ENGINE] == SERVICE_ENGINE
    assert env[ENV_STATE_DIR] == str(tmp_path / "svc")


def test_twin_the_in_process_route_never_uses_a_service():
    env = _env_of(SubprocessInvoker([]))
    assert env[ENV_NO_DAEMON] == "1" and env[ENV_ENGINE] is None


def test_require_service_is_unreachable_without_a_service_never_in_process(monkeypatch):
    from harness_manager.cli import engine as E
    from harness_manager.core.errors import UnreachableError

    monkeypatch.setattr(E.time, "sleep", lambda _s: None)
    with pytest.raises(UnreachableError, match="service for this state dir did not answer"):
        E.require_service(None)


def test_tools_hil_is_the_package_under_its_old_name():
    import harness_manager.checks.plans as plans
    import harness_manager.checks.run as runner
    import tools.hil.plans
    import tools.hil.run

    assert tools.hil.run is runner and tools.hil.plans is plans


if __name__ == "__main__":        # regenerate the browser test's table
    out = Path(__file__).resolve().parents[1] / "web" / "hil_gui_auto_cases.json"
    out.write_text(json.dumps([[i, c, p] for i, c, p in AUTO_CASES], indent=1) + "\n")
    print(out)
