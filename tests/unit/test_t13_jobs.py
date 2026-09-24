"""Team T13: jobs, the per-board gate, and the drop-oldest outbox. Every check has a negative twin."""

from __future__ import annotations

import asyncio
import threading

import pytest

from harness_manager.core.errors import ExitCode, HeldError, IncompatibleError
from harness_manager.core.events import EventBus
from harness_manager.daemon.jobs import BoardGates, Job, JobManager
from harness_manager.daemon.outbox import Outbox


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def seen(bus: EventBus) -> list:
    events: list = []
    bus.subscribe("job.*", events.append)
    return events


@pytest.fixture
def jobs(bus: EventBus):
    manager = JobManager(bus, BoardGates(op_wait_s=1.0))
    yield manager
    manager.shutdown(wait=True)


def finished(job: Job) -> Job:
    assert job.finished.wait(10), "the job never finished"
    return job


# -- the gate --------------------------------------------------------------------------------


def test_a_short_request_runs_when_the_board_is_free():
    gates = BoardGates()
    with gates.op("b"):
        ran = True
    assert ran and gates.busy("b") is None


def test_negative_twin_a_short_request_is_refused_while_a_job_holds_the_board():
    gates = BoardGates()
    job = Job("deploy", "b")
    gates.claim("b", job)
    with pytest.raises(HeldError) as err, gates.op("b"):
        pass
    assert job.id in err.value.message and job.id in err.value.holder
    with gates.op("other"):              # another board is not affected
        pass
    gates.release("b", job)
    with gates.op("b"):
        pass


def _hold_op(gates: BoardGates, board: str) -> tuple[threading.Event, threading.Thread]:
    """A short request that stays in flight on ``board`` until the returned event is set."""
    inside, leave = threading.Event(), threading.Event()

    def op() -> None:
        with gates.op(board):
            inside.set()
            leave.wait(10)

    t = threading.Thread(target=op, daemon=True)
    t.start()
    assert inside.wait(5), "the first request never got the board"
    return leave, t


def test_a_short_request_waits_its_turn_then_is_refused_as_busy():
    # Q1: the HELD gate between two short requests (jobs.py BoardGates.op) had no test.
    gates = BoardGates(op_wait_s=0.2)
    leave, t = _hold_op(gates, "b")
    try:
        with pytest.raises(HeldError) as err, gates.op("b"):
            pass
        assert "busy with another request" in err.value.message
        assert err.value.holder == "harness-manager-daemon" and err.value.code == ExitCode.HELD
        with gates.op("other"):              # another board is not affected
            pass
    finally:
        leave.set()
        t.join(5)
    with gates.op("b"):                      # and the board is free once it leaves
        pass


def test_negative_twin_a_short_request_that_frees_the_board_in_time_lets_the_next_run():
    gates = BoardGates(op_wait_s=5.0)
    leave, t = _hold_op(gates, "b")
    threading.Timer(0.1, leave.set).start()
    with gates.op("b"):                      # waits for it, not refused
        ran = True
    t.join(5)
    assert ran


def test_a_request_waiting_for_the_board_when_a_job_claims_it_names_the_job():
    gates = BoardGates(op_wait_s=1.0)
    leave, t = _hold_op(gates, "b")
    job = Job("deploy", "b")
    claim = threading.Timer(0.05, gates.claim, ("b", job))  # while the request waits
    claim.start()
    try:
        with pytest.raises(HeldError) as err, gates.op("b"):
            pass
        assert claim.finished.is_set() and job.id in err.value.message   # names the job
    finally:
        claim.join(5)
        gates.release("b", job)
        leave.set()
        t.join(5)


def test_one_job_per_board():
    gates = BoardGates()
    gates.claim("b", Job("deploy", "b"))
    with pytest.raises(HeldError):
        gates.claim("b", Job("restore", "b"))
    gates.claim("c", Job("restore", "c"))          # negative twin: another board is free


# -- jobs ------------------------------------------------------------------------------------


def test_a_job_reports_started_progress_done_in_order(jobs, seen):
    def work(progress):
        progress("push", 0, 4)
        progress("push", 4, 4)
        progress("verify", 1, 1)
        return {"rm_id": "0x1"}

    job = finished(jobs.submit("deploy", "b", work))
    topics = [e.topic for e in seen]
    assert topics[0] == "job.started" and topics[-1] == "job.done"
    assert [e.data["phase"] for e in seen if e.topic == "job.progress"] == ["push", "push", "verify"]
    assert seen[-1].data == {"job": job.id, "result": {"rm_id": "0x1"}}
    state = job.to_json()
    assert state["state"] == "done" and state["phases"] == ["push", "verify"]
    assert state["result"] == {"rm_id": "0x1"} and "error" not in state
    assert all(e.board_id == "b" for e in seen)


def test_negative_twin_a_failing_job_carries_its_error(jobs, seen):
    def work(progress):
        raise IncompatibleError("wrong shell", hint="use another")

    job = finished(jobs.submit("deploy", "b", work))
    assert seen[-1].topic == "job.failed"
    assert seen[-1].data["error"]["code"] == ExitCode.INCOMPATIBLE
    state = job.to_json()
    assert state["state"] == "failed" and state["error"]["name"] == "INCOMPATIBLE"
    assert "result" not in state


def test_a_bug_in_a_job_is_an_internal_error_not_a_crash(jobs, seen):
    job = finished(jobs.submit("deploy", "b", lambda progress: 1 / 0))
    assert job.error["code"] == ExitCode.FAILED and "ZeroDivisionError" in job.error["message"]


def test_the_board_is_free_before_job_done_is_published(bus, jobs):
    busy_at_done = []
    bus.subscribe("job.done", lambda ev: busy_at_done.append(jobs.gates.busy("b")))
    finished(jobs.submit("deploy", "b", lambda progress: None))
    assert busy_at_done == [None]


def test_negative_twin_the_board_is_busy_while_the_job_runs(jobs):
    release = threading.Event()
    job = jobs.submit("deploy", "b", lambda progress: release.wait(10))
    try:
        assert jobs.gates.busy("b") is job
        with pytest.raises(HeldError):
            jobs.submit("restore", "b", lambda progress: None)
    finally:
        release.set()
    finished(job)
    assert jobs.gates.busy("b") is None


def test_progress_is_throttled_but_every_phase_is_reported(jobs, seen):
    def work(progress):
        for i in range(500):
            progress("push", i, 10_000)
        progress("verify", 0, 1)

    finished(jobs.submit("deploy", "b", work))
    phases = [e.data["phase"] for e in seen if e.topic == "job.progress"]
    assert "push" in phases and phases[-1] == "verify"
    assert len(phases) < 100                  # negative twin: not one event per call


# -- the outbox ------------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def test_the_outbox_delivers_in_order_without_drops_under_its_limits():
    async def main():
        box = Outbox(asyncio.get_running_loop(), max_items=10, max_bytes=1000)
        for i in range(5):
            box.put(f"f{i}")
        return await box.take()

    batch = run(main())
    assert batch.items == ["f0", "f1", "f2", "f3", "f4"]
    assert (batch.dropped_items, batch.dropped_bytes, batch.closed) == (0, 0, False)


def test_negative_twin_a_full_outbox_drops_the_oldest_and_counts_it():
    async def main():
        box = Outbox(asyncio.get_running_loop(), max_items=3, max_bytes=10**6)
        for i in range(10):
            box.put(f"f{i}")
        return await box.take(), box.total_dropped_items

    batch, total = run(main())
    assert batch.items == ["f7", "f8", "f9"] and batch.dropped_items == 7 and total == 7


def test_the_byte_budget_drops_old_chunks_too():
    async def main():
        box = Outbox(asyncio.get_running_loop(), max_items=100, max_bytes=10)
        for chunk in (b"aaaa", b"bbbb", b"cccc", b"dddd"):
            box.put(chunk)
        return await box.take()

    batch = run(main())
    assert batch.items == [b"cccc", b"dddd"] and batch.dropped_bytes == 8


def test_a_put_from_another_thread_wakes_the_taker_and_close_ends_it():
    async def main():
        box = Outbox(asyncio.get_running_loop())
        threading.Thread(target=lambda: (box.put("x"), box.close())).start()
        got = []
        while True:
            batch = await asyncio.wait_for(box.take(), 5)
            got += batch.items
            if batch.closed:
                return got

    assert run(main()) == ["x"]


def test_negative_twin_a_closed_outbox_takes_no_more_frames():
    async def main():
        box = Outbox(asyncio.get_running_loop())
        box.close()
        box.put("late")
        return await box.take()

    batch = run(main())
    assert batch.closed and batch.items == []
