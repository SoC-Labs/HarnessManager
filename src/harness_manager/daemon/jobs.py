"""Long operations as jobs on worker threads, and the per-board operation gate.

The engine is synchronous and a deploy takes seconds, an SD write minutes. So
the long operations (deploy, restore, reboot, SD backup/install/restore, debug
up) run as JOBS: the request returns ``202 {"job": id}`` at once, a worker
thread runs the engine call, and progress and completion go out as events
(``job.started``, ``job.progress {job, phase, done, total}``, ``job.done {job,
result}``, ``job.failed {job, error}``). ``GET /jobs/{id}`` returns the same
state for a client that was not listening.

The gate. A board's control port accepts ONE client, and a swap parks it
(harness handover §5). If the web UI polled ``info`` while a deploy ran, it
could take the port between two steps of the swap and break it. So every
request that talks to a board goes through ``BoardGates``:

- one job per board at a time; a second is refused with ``HeldError`` (409);
- while a job runs, a request that would talk to that board is refused at once
  with ``HeldError`` naming the job, instead of racing it;
- short requests to the same board run one at a time (they wait for each other).

Requests that do not touch the board (lock owner, debug status, console names,
job state) never take the gate.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from harness_manager.cli.output import jsonable
from harness_manager.core.errors import ActionFailedError, HarnessError, HeldError
from harness_manager.core.events import Event, EventBus

from .wire import error_object

log = logging.getLogger(__name__)

#: How long a short request waits for another short request on the same board.
OP_WAIT_S = 60.0
#: ``job.progress`` events are throttled to phase changes, completion, and this interval.
PROGRESS_INTERVAL_S = 0.1

ProgressFn = Callable[[str, int, int], None]


class Job:
    def __init__(self, kind: str, board_id: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.board_id = board_id
        self.state = "running"                 # running | done | failed
        self.result: Any = None
        self.error: dict[str, Any] | None = None
        self.progress = {"phase": "", "done": 0, "total": 0}
        self.phases: list[str] = []            # every phase seen, in order (additive to API.md)
        self.started_at = time.time()
        self.ended_at: float | None = None
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self.finished = threading.Event()

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {
                "job": self.id, "kind": self.kind, "board_id": self.board_id,
                "state": self.state, "progress": dict(self.progress),
                "phases": list(self.phases), "started_at": self.started_at,
                "ended_at": self.ended_at,
            }
            if self.state == "done":
                out["result"] = self.result
            if self.error is not None:
                out["error"] = self.error
        return out

    def describe(self) -> str:
        return f"{self.kind} job {self.id}"


def busy_error(board_id: str, job: Job) -> HeldError:
    err = HeldError(f"{board_id} is busy: {job.describe()} is running",
                    holder=f"harness-manager-daemon {job.describe()}",
                    hint=f"wait for it to finish (GET /api/v1/jobs/{job.id}; "
                         "progress arrives on the events WebSocket)")
    err.data = {"job": job.id, "kind": job.kind, "board_id": board_id}  # type: ignore[attr-defined]
    return err


class BoardGates:
    def __init__(self, op_wait_s: float = OP_WAIT_S) -> None:
        self.op_wait_s = op_wait_s
        self._mu = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._jobs: dict[str, Job] = {}

    def _lock(self, board_id: str) -> threading.Lock:
        with self._mu:
            return self._locks.setdefault(board_id, threading.Lock())

    def busy(self, board_id: str) -> Job | None:
        with self._mu:
            return self._jobs.get(board_id)

    def running(self) -> list[Job]:
        with self._mu:
            return list(self._jobs.values())

    def claim(self, board_id: str, job: Job) -> None:
        with self._mu:
            other = self._jobs.get(board_id)
            if other is not None:
                raise busy_error(board_id, other)
            self._jobs[board_id] = job

    def release(self, board_id: str, job: Job) -> None:
        with self._mu:
            if self._jobs.get(board_id) is job:
                del self._jobs[board_id]

    @contextmanager
    def hold(self, board_id: str) -> Iterator[None]:
        """A job's hold: waits for in-flight short requests, then keeps the board."""
        lock = self._lock(board_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def op(self, board_id: str) -> Iterator[None]:
        """A short request: refused at once while a job runs; else one at a time."""
        job = self.busy(board_id)
        if job is not None:
            raise busy_error(board_id, job)
        lock = self._lock(board_id)
        if not lock.acquire(timeout=self.op_wait_s):
            job = self.busy(board_id)
            if job is not None:
                raise busy_error(board_id, job)
            raise HeldError(f"{board_id} is busy with another request",
                            holder="harness-manager-daemon", hint="retry in a moment")
        try:
            yield
        finally:
            lock.release()


class JobManager:
    def __init__(self, bus: EventBus, gates: BoardGates, *, workers: int = 8,
                 keep: int = 200) -> None:
        self.bus = bus
        self.gates = gates
        self.keep = keep
        self._mu = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="harness-manager-daemon-job")
        self._closed = False

    def get(self, job_id: str) -> Job | None:
        with self._mu:
            return self._jobs.get(job_id)

    def running(self) -> list[Job]:
        return self.gates.running()

    def recent(self) -> list[Job]:
        """Every job still kept (the last ``keep``), oldest first."""
        with self._mu:
            return list(self._jobs.values())

    def submit(self, kind: str, board_id: str, fn: Callable[[ProgressFn], Any],
               *, serialise: Callable[[Any], Any] = jsonable) -> Job:
        """Claim the board, record the job, start it. ``HeldError`` if the board has a job."""
        if self._closed:
            raise ActionFailedError("harness-manager-daemon is shutting down", hint="start it again")
        job = Job(kind, board_id)
        self.gates.claim(board_id, job)
        with self._mu:
            self._jobs[job.id] = job
            while len(self._jobs) > self.keep:
                oldest = next(iter(self._jobs.values()))
                if oldest.state == "running":
                    break
                self._jobs.popitem(last=False)
        self._publish("job.started", job, {"job": job.id, "kind": kind})
        log.info("%s started on %s", job.describe(), board_id or "the engine")
        try:
            self._pool.submit(self._run, job, fn, serialise)
        except RuntimeError as exc:        # the pool is shut down
            self.gates.release(board_id, job)
            self._fail(job, ActionFailedError(f"harness-manager-daemon is shutting down ({exc})"))
            raise ActionFailedError("harness-manager-daemon is shutting down", hint="start it again") \
                from exc
        return job

    def _progress_fn(self, job: Job) -> ProgressFn:
        def progress(phase: str, done: int, total: int) -> None:
            now = time.monotonic()
            with job._lock:
                job.progress = {"phase": phase, "done": int(done or 0), "total": int(total or 0)}
                new_phase = not job.phases or job.phases[-1] != phase
                if new_phase:
                    job.phases.append(phase)
                due = bool(new_phase or (total and done >= total)
                           or now - job._last_emit >= PROGRESS_INTERVAL_S)
                if due:
                    job._last_emit = now
            if due:
                self._publish("job.progress", job, {"job": job.id, "phase": phase,
                                                    "done": int(done or 0),
                                                    "total": int(total or 0)})
        return progress

    def _run(self, job: Job, fn: Callable[[ProgressFn], Any],
             serialise: Callable[[Any], Any]) -> None:
        error: HarnessError | None = None
        result: Any = None
        try:
            with self.gates.hold(job.board_id):
                result = serialise(fn(self._progress_fn(job)))
        except HarnessError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - a bug in a job must still end the job
            log.exception("%s failed", job.describe())
            error = HarnessError(f"internal error: {type(exc).__name__}: {exc}")
        finally:
            # Free the board BEFORE saying so, so a client that reacts to job.done
            # is never refused by this job's own claim.
            self.gates.release(job.board_id, job)
        if error is not None:
            self._fail(job, error)
        else:
            with job._lock:
                job.result = result
                job.state = "done"
                job.ended_at = time.time()
            job.finished.set()
            log.info("%s done in %.1f s", job.describe(), job.ended_at - job.started_at)
            self._publish("job.done", job, {"job": job.id, "result": result})

    def _fail(self, job: Job, exc: HarnessError) -> None:
        err = error_object(exc)
        with job._lock:
            job.error = err
            job.state = "failed"
            job.ended_at = time.time()
        job.finished.set()
        # The field record: a failed deploy or reboot said nothing in daemon.log before.
        log.warning("%s on %s failed after %.1f s: %s: %s", job.describe(),
                    job.board_id or "the engine", job.ended_at - job.started_at,
                    err.get("name"), err.get("message"))
        self._publish("job.failed", job, {"job": job.id, "error": err})

    def _publish(self, topic: str, job: Job, data: dict[str, Any]) -> None:
        self.bus.publish(Event(topic, job.board_id, data))

    def shutdown(self, wait: bool = False) -> None:
        self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=True)
