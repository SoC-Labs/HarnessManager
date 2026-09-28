"""HIL-GUI: the unattended checks THROUGH a running Harness Manager service (``hil_api.py``).

The service is the real daemon app under uvicorn (``t13_daemon.LiveDaemon``, ``daemon.json``
written, so the CLI finds it); the board is the Linux harness fake behind the fake hub
(``test_hil_auto_virtual.linux_lab``: card-less, board 2) or the bare-metal lab. The run's
commands are Harness Manager's REAL CLI (``InProcessHm``), routed only to the service
(``HARNESS_MANAGER_CLI_ENGINE=…require_service``, as the service's own subprocesses are).

Every rule has its twin:

- the service holding the board (the app has it open) is not another holder: the run passes;
  twin: the old in-process route against the same service stops HELD (the trap of 09-28), and
  a different client (another host's card job) still stops the run, never forced;
- the lease is heartbeated for the run's length: the run outlives the lease's first expiry;
  twin: in-process, the same lease ends the run before it expires; someone else's lease
  refuses the start (409 HELD, naming them), and a free lease needs ``take_lease``;
- Stop finishes the check and puts greybox back; one run per board; the board cannot be
  closed, nor the service stopped, while a run is active.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from harness_manager.checks.run import EXIT_PASS, EXIT_STOP
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from tests.fakes.hil_auto import InProcessHm, is_write, verb_of
from tests.fakes.t13_daemon import LiveDaemon, bid_path
from tests.integration.test_hil_auto_virtual import BOARD, NoSleep, acquire, linux_lab

pytestmark = pytest.mark.timeout(240)

BID = f"mps3@{BOARD}:6900"


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


class Service:
    """The live service, its checks runs and a fake clock the runs share."""

    def __init__(self, live: LiveDaemon, sleep: NoSleep) -> None:
        self.live, self.sleep = live, sleep
        self.d = live.app.state.daemon
        self.hm = InProcessHm()
        self.d.checks.invoker_factory = lambda _run: self.hm
        self.d.checks.clock = sleep.clock
        self.d.checks.sleep = sleep

    def call(self, method: str, path: str, **kw: Any) -> Any:
        with self.live.client() as c:
            return c.request(method, f"/api/v1{path}", **kw)

    def open(self) -> None:
        r = self.call("POST", "/boards", json={"target": BOARD, "note": "the page"})
        assert r.status_code == 200, r.text

    def start(self, **body: Any) -> Any:
        return self.call("POST", f"{bid_path(BID)[len('/api/v1'):]}/checks", json=body)

    def status(self) -> dict[str, Any]:
        r = self.call("GET", f"{bid_path(BID)[len('/api/v1'):]}/checks")
        assert r.status_code == 200, r.text
        return r.json()

    def wait_end(self, timeout: float = 180.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.status()
            if st["run"] is None and st["last"] is not None:
                return st["last"]
            time.sleep(0.05)
        raise AssertionError(f"the run did not end: {self.status()['run']}")


@contextmanager
def service(sleep: NoSleep | None = None) -> Iterator[Service]:
    eng = Engine(EngineConfig(state_dir=state_dir()))
    try:
        with LiveDaemon(eng, write_json=True) as live:
            presence = getattr(live.app.state.daemon, "presence", None)
            if presence is not None:
                presence._stop.set()          # no presence beat in these runs
            yield Service(live, sleep if sleep is not None else NoSleep())
            eng.close_all()
    finally:
        eng.close_all()


@pytest.fixture
def nocard(tmp_path, monkeypatch) -> Iterator[Any]:
    with linux_lab(tmp_path, monkeypatch, card=False) as rig:
        # The service's own route: the CLI goes to the running service and only there.
        monkeypatch.delenv("HARNESS_MANAGER_NO_DAEMON", raising=False)
        monkeypatch.setenv("HARNESS_MANAGER_CLI_ENGINE", "harness_manager.cli.engine:require_service")
        yield rig


def summary(ev: str | Path) -> dict[str, Any]:
    return json.loads((Path(ev) / "summary.json").read_text())


def verdicts(ev: str | Path) -> dict[str, str]:
    return {c["id"]: c["verdict"] for c in summary(ev)["checks"]}


# --- the service holding the board is not another holder ------------------------------------------


def test_a_run_through_the_service_that_holds_the_board_passes(nocard, capsys):
    with service() as svc:
        svc.open()                                        # the app has the board open
        acquire(capsys)                                   # david's lease, held here
        r = svc.start(plan="linux-nocard", writes="safe", repeat=1, until="")
        assert r.status_code == 200, r.text
        run = r.json()["run"]
        assert run["state"] in ("starting", "running") and run["route"] == "service"
        last = svc.wait_end()
        v = verdicts(last["evidence"])
        assert last["state"] == "done" and last["result"] == "PASS", (v, last["reason"])
        assert last["exit"] == EXIT_PASS
        assert {k for k, x in v.items() if x == "pass"} == {
            "0.2", "0.3", "0.4", "A1", "A2", "A3", "B1", "C1", "D1", "D4a", "E1", "E1b", "Z2",
            "Z2b"}
        # every command went through the service, which still holds the board
        assert BID in svc.d.engine.open_boards()
        assert nocard.fake.current_rm_id == 0 and summary(last["evidence"])["end_state"]["greybox"]
        assert [verb_of(a) for a in svc.hm.calls if is_write(a)] == ["mcc temp", "program",
                                                                      "restore"]
        announce = (Path(last["evidence"]) / "ANNOUNCE.txt").read_text()
        assert "in the Harness Manager service" in announce and "heartbeats it" in announce
        assert "the app's Checks section: Stop" in announce
        # the past runs list it, with its report
        st = svc.status()
        assert st["runs"][0]["id"] == run["id"] and st["runs"][0]["report"] is True
        rep = svc.call("GET", f"{bid_path(BID)[len('/api/v1'):]}/checks/{run['id']}/report")
        assert rep.status_code == 200 and rep.json()["text"].startswith("# HIL-AUTO: linux-nocard")


def test_twin_the_in_process_route_against_the_same_service_stops_held(nocard, tmp_path,
                                                                      capsys, monkeypatch):
    """The trap of 09-28: the old runner forced the in-process engine; the service holding
    the board then refused every command (exit 4) and the run stopped."""
    from harness_manager.checks.run import main as hil_main

    with service() as svc:
        svc.open()
        acquire(capsys)
        monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
        monkeypatch.delenv("HARNESS_MANAGER_CLI_ENGINE", raising=False)
        sleep = NoSleep()
        rc = hil_main(["run", "--plan", "linux-nocard", "--board", BOARD, "--evidence",
                       str(tmp_path / "ev"), "--gap", "1", "--writes", "safe", "--in-process"],
                      invoker=InProcessHm(), sleep=sleep, clock=sleep.clock)
        s = summary(tmp_path / "ev")
        assert rc == EXIT_STOP and "HELD" in s["stopped"]["reason"], s["stopped"]
        assert nocard.fake.accepted_pushes == []


def test_twin_a_different_client_holding_the_board_still_stops_the_run(nocard, capsys):
    """Another host's card push (the reset guard's card job) is another holder: the run
    stops at the swap, HELD, and nothing is forced."""
    with service() as svc:
        svc.open()
        acquire(capsys)
        nocard.fake.hold_job("writing", act="push", slot="B")
        assert svc.start(plan="linux-nocard", writes="safe", repeat=1, until="").status_code == 200
        last = svc.wait_end()
        s = summary(last["evidence"])
        assert last["result"] == "STOPPED" and last["exit"] == EXIT_STOP
        assert s["stopped"]["check"] == "E1" and "never forces" in s["stopped"]["reason"]
        assert not any("--force" in a for a in svc.hm.calls)
        assert nocard.fake.accepted_pushes == []


# --- the lease is held for the run's length ---------------------------------------------------------


def _expiry() -> float:
    from datetime import datetime

    from tests.fakes.l1_fake_hub import EXPIRES

    return datetime.fromisoformat(EXPIRES).timestamp()


def _near_expiry() -> NoSleep:
    """A clock one hour before the fake hub's lease expiry (``l1_fake_hub.EXPIRES``)."""
    sleep = NoSleep()
    sleep.now = _expiry() - 3600
    return sleep


def test_the_lease_is_heartbeated_for_the_runs_length(nocard, capsys):
    sleep = _near_expiry()
    with service(sleep) as svc:
        leases = svc.d.leases
        real = sleep.__call__

        def beat(seconds: float) -> None:        # the heartbeat thread's rounds, on this clock
            real(seconds)
            if seconds >= 30:
                leases.beat_due(force=True)

        svc.d.checks.sleep = beat
        svc.open()
        acquire(capsys)
        before = nocard.hub.heartbeats
        r = svc.start(plan="linux-nocard", writes="none", repeat=3, interval_s=1800, until="")
        assert r.status_code == 200, r.text
        last = svc.wait_end()
        agg = summary(last["evidence"])
        assert last["result"] == "PASS" and len(agg["iterations"]) == 3, agg.get("ended_early")
        assert agg["ended_early"] is None
        assert nocard.hub.heartbeats >= before + 2                 # kept alive through the waits
        assert sleep.now > _expiry()                              # past the first expiry
        assert last["lease"]["kept"] is True and last["lease"]["taken"] is False


def test_twin_in_process_the_same_lease_ends_the_run_before_it_expires(nocard, tmp_path,
                                                                        capsys, monkeypatch):
    from harness_manager.checks.run import main as hil_main

    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    monkeypatch.delenv("HARNESS_MANAGER_CLI_ENGINE", raising=False)
    acquire(capsys)
    sleep = _near_expiry()
    rc = hil_main(["run", "--plan", "linux-nocard", "--board", BOARD, "--evidence",
                   str(tmp_path / "ev"), "--gap", "1", "--repeat", "3", "--interval", "1800"],
                  invoker=InProcessHm(), sleep=sleep, clock=sleep.clock)
    agg = summary(tmp_path / "ev")
    assert rc == EXIT_PASS and len(agg["iterations"]) < 3
    assert (agg["ended_early"] or "").startswith("the lease expires")


def test_someone_elses_lease_refuses_the_start_and_names_them(nocard):
    with service() as svc:
        svc.open()
        nocard.hub.steal("soak-runner")
        r = svc.start(plan="linux-nocard", take_lease=True)
        assert r.status_code == 409 and r.json()["error"]["name"] == "HELD"
        assert "soak-runner" in r.json()["error"]["message"]
        assert svc.status()["run"] is None and svc.hm.calls == []
        # the preview says the same, and starts nothing
        p = svc.start(plan="linux-nocard", announce_only=True)
        assert p.status_code == 200 and p.json()["refusal"]["name"] == "HeldError"
        assert svc.status()["run"] is None


def test_twin_a_free_lease_needs_take_lease_then_is_taken_and_released(nocard):
    with service() as svc:
        svc.open()
        r = svc.start(plan="linux-nocard", writes="none", repeat=1, until="")
        assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
        assert "take it for the run" in r.json()["error"]["message"]
        assert "take_lease" in r.json()["error"]["hint"]
        r = svc.start(plan="linux-nocard", writes="none", repeat=1, until="", take_lease=True)
        assert r.status_code == 200, r.text
        last = svc.wait_end()
        assert last["result"] == "PASS", last["reason"]
        assert last["lease"]["taken"] is True and last["lease"]["released"] is True
        assert nocard.hub.current is None                  # given back at the end


# --- stop, one run per board, the board stays open ---------------------------------------------------


def test_stop_finishes_the_check_and_restores_greybox(nocard, capsys):
    with service() as svc:
        svc.open()
        acquire(capsys)
        real = svc.hm.__class__.__call__
        stopped = []

        def stop_after_program(hm, argv, timeout):
            out = real(hm, argv, timeout)
            if "program" in argv and not stopped:
                stopped.append(svc.call("DELETE", f"{bid_path(BID)[len('/api/v1'):]}/checks"))
            return out

        svc.hm.__class__ = type("StoppingHm", (InProcessHm,), {"__call__": stop_after_program})
        assert svc.start(plan="linux-nocard", writes="safe", repeat=5, interval_s=600,
                         until="").status_code == 200
        last = svc.wait_end()
        assert stopped and stopped[0].json()["run"]["stop_requested"] is True
        s = summary(last["evidence"])
        assert last["result"] == "STOPPED" and "Stop" in (last["reason"] or "")
        assert len(s["iterations"]) == 1 and s["end_state"]["greybox"] is True
        assert nocard.fake.current_rm_id == 0
        assert [verb_of(a) for a in svc.hm.calls].count("restore") == 1


def test_one_run_per_board_and_the_board_stays_open_while_it_runs(nocard, capsys):
    with service() as svc:
        svc.open()
        acquire(capsys)
        svc.d.checks.sleep = None            # the schedule waits in real time (until Stop)
        r = svc.start(plan="linux-nocard", writes="none", start_at="23:59", until="",
                      repeat=1)
        assert r.status_code == 200 and r.json()["run"]["state"] == "scheduled"
        again = svc.start(plan="linux-nocard", writes="none", repeat=1, until="")
        assert again.status_code == 409 and again.json()["error"]["name"] == "ALREADY"
        close = svc.call("DELETE", bid_path(BID)[len("/api/v1"):])
        assert close.status_code == 409 and "checks run" in close.json()["error"]["message"]
        down = svc.call("POST", "/daemon/shutdown", json={})
        assert down.status_code == 409 and "checks" in down.json()["error"]["message"]
        # Stop cancels a run that has not started: nothing was sent
        stop = svc.call("DELETE", f"{bid_path(BID)[len('/api/v1'):]}/checks")
        assert stop.status_code == 200
        last = svc.wait_end()
        assert last["state"] == "cancelled" and svc.hm.calls == []
        # twin: with no run, the board closes
        assert svc.call("DELETE", bid_path(BID)[len("/api/v1"):]).status_code == 200


def test_auto_picks_the_plan_from_the_board(nocard, capsys):
    with service() as svc:
        svc.open()
        acquire(capsys)
        p = svc.start(plan="auto", announce_only=True, writes="none")
        assert p.status_code == 200, p.text
        assert p.json()["plan"] == "linux-nocard"
        assert "no user microSD" in p.json()["auto"]["why"]
        assert "plan linux-nocard" in p.json()["announce"]


# --- the command line hands a run to the running service -----------------------------------------------


def test_the_command_line_hands_the_run_to_the_running_service(nocard, tmp_path, capsys):
    """``python -m tools.hil run`` with a service running for the state dir: no refusal (the
    trap of 09-28), the service runs it, the command line follows it and exits with its code."""
    from harness_manager.checks.run import main as hil_main

    with service() as svc:
        acquire(capsys)                                       # the board is not open yet
        rc = hil_main(["run", "--plan", "linux-nocard", "--board", BOARD, "--evidence",
                       str(tmp_path / "ev"), "--gap", "1", "--writes", "safe"],
                      sleep=lambda _s: time.sleep(0.05))
        err = capsys.readouterr().err
        assert rc == EXIT_PASS, err
        assert "the Harness Manager service runs it" in err and "ended: done, PASS" in err
        assert verdicts(tmp_path / "ev")["E1"] == "pass"
        assert BID in svc.d.engine.open_boards()             # the service opened it for the run
        assert "in the Harness Manager service" in (tmp_path / "ev" / "ANNOUNCE.txt").read_text()


def test_twin_in_process_the_command_line_still_runs_here(nocard, tmp_path, capsys, monkeypatch):
    """No service (or --in-process): the old route, every command on the CLI's in-process
    engine, and the lease's expiry is a deadline again."""
    from harness_manager.checks.run import main as hil_main

    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    monkeypatch.delenv("HARNESS_MANAGER_CLI_ENGINE", raising=False)
    acquire(capsys)
    sleep = NoSleep()
    rc = hil_main(["run", "--plan", "linux-nocard", "--board", BOARD, "--evidence",
                   str(tmp_path / "ev"), "--gap", "1"], invoker=InProcessHm(), sleep=sleep,
                  clock=sleep.clock)
    assert rc == EXIT_PASS
    assert "kill -INT" in (tmp_path / "ev" / "ANNOUNCE.txt").read_text()
