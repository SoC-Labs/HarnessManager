"""HIL checks in the service (lane HIL-GUI): the unattended runbooks, started from the app.

``daemon/hil_api.py`` serves it (``docs/API.md`` "HIL checks"); the app's Checks section drives
it. A run is ``harness_manager.checks.run.Runner``, unchanged in its rules, on a thread of the
service:

- **Through the service.** The runner's subprocesses use the CLI's normal routing to THIS
  service (``SubprocessInvoker(route="service")``: the service or UNREACHABLE, never an
  in-process engine), so they share the service's board session: the service holding the
  board is not "another holder". A different process or client still stops the run (HELD).
- **The board stays open** in the service from the start (or the schedule) to the end: closing
  it (``DELETE /boards/{bid}``) and stopping the service (``POST /daemon/shutdown`` without
  ``force``) are refused, naming the run.
- **The lease is held until the run ends.** A lease THIS Harness Manager holds (``here``) is
  heartbeated by the lease service while the board is open here (``LeaseService.track``), so
  its first expiry is not a deadline (``Options.lease_kept``). A free lease is taken for the
  run when ``take_lease`` says so (the Start dialog offers it), and released when the run ends.
  Someone else's lease refuses the start (409 HELD, naming them). Nothing is ever forced.
- **One run per board** (409 ALREADY for a second one), scheduled (``start_at``) or running.
- **Stop** (``DELETE``) is the runner's SIGINT: it finishes the check it is on, puts greybox
  back (in the runner's ``finally``), writes ``REPORT.md``. A scheduled run that has not
  started is cancelled, with nothing sent.
- **QUIET-POLL**: a run is an explicit action (someone pressed Start), so the viewer rules
  do not gate it; it is paced by the runner's own rules (``--gap``, ``--interval``).

Evidence: a new folder per run, ``<state dir>/checks/<board>/<YYYYMMDD-HHMMSS>-<plan>`` unless
the request names one; ``<state dir>/checks/index.json`` lists the runs (newest last), so the
past runs survive a restart. Events (``docs/CONTRACTS.md``): ``checks.state`` when a run's
state changes, ``checks.progress`` as it goes (an iteration, a check, a result, the wait).

A run lives in the service's memory: a service that is killed ends it where it is (the board
may then be left swapped; ``REPORT.md`` of the last iteration says where), and a scheduled run
does not survive a restart.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from harness_manager import naming
from harness_manager.checks import plans as P
from harness_manager.checks import run as R
from harness_manager.core.errors import (
    AbsentError,
    AlreadyError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event

log = logging.getLogger(__name__)

TOPIC_STATE = "checks.state"
TOPIC_PROGRESS = "checks.progress"
CAPABILITY = "HIL checks"

SCHEDULED, STARTING, RUNNING, STOPPING = "scheduled", "starting", "running", "stopping"
DONE, CANCELLED, REFUSED, FAILED = "done", "cancelled", "refused", "failed"
INTERRUPTED = "interrupted"          # a past run whose service process ended mid-run
ACTIVE = (SCHEDULED, STARTING, RUNNING, STOPPING)
RESULT = {R.EXIT_PASS: "PASS", R.EXIT_FAIL: "FAIL", R.EXIT_STOP: "STOPPED"}

#: The Start form's defaults (docs/HIL_AUTO.md "In the app"): until the next 08:30, every 30 min
DEFAULT_UNTIL = "08:30"
DEFAULT_INTERVAL_S = 1800
DEFAULT_WRITES = "safe"
#: The most iterations a run asks for when the request names none (``until`` ends it first)
MAX_REPEAT = 200
#: How long a take-lease start waits for the hub before it refuses (the lease was free at
#: Start; a queue here means someone took it meanwhile)
TAKE_LEASE_TIMEOUT_S = 120.0
#: How long ``close()`` (the service stopping) waits for runs to put greybox back
CLOSE_WAIT_S = 180.0
#: Past runs listed per board
HISTORY = 30
INDEX_KEEP = 500
_RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "board"


def next_time(text: str, now: float) -> float:
    """``HH:MM`` (the next such local time) or ISO 8601, as epoch seconds; USAGE if neither."""
    try:
        return R.parse_until(str(text), datetime.fromtimestamp(now).astimezone()).timestamp()
    except ValueError as exc:
        raise UsageError(f"{text!r} is not HH:MM or an ISO 8601 time") from exc


@dataclass
class RunRequest:
    """A validated ``POST /boards/{bid}/checks`` body."""

    plan: str
    auto: dict[str, str] | None
    writes: str
    repeat: int
    interval_s: float
    until: float | None
    margin_s: float
    start_at: float | None
    take_lease: bool
    evidence: Path | None
    expect_static: str | None
    gap_s: float
    max_unreachable: int
    stop_on_first_fail: bool


@dataclass
class Run:
    id: str
    board_id: str
    target: str
    req: RunRequest
    evidence: Path
    created_at: float
    state: str = SCHEDULED
    started_at: float | None = None
    ended_at: float | None = None
    iteration: int = 0
    check: dict[str, Any] | None = None
    phase: str = ""
    counts: dict[str, int] = field(default_factory=dict)        # this iteration
    totals: dict[str, int] = field(default_factory=dict)        # the whole run
    iterations: list[dict[str, Any]] = field(default_factory=list)
    next_at: float | None = None
    first_failure: dict[str, Any] | None = None
    last_check: dict[str, Any] | None = None
    exit: int | None = None
    reason: str = ""
    announce: str = ""
    lease: dict[str, Any] = field(default_factory=dict)
    stop_requested: bool = False
    lines: deque = field(default_factory=lambda: deque(maxlen=40))
    n_lines: int = 0
    mu: threading.Lock = field(default_factory=threading.Lock)     # lines, read by the API
    runner: Any = None
    thread: threading.Thread | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def result(self) -> str | None:
        return RESULT.get(self.exit) if self.exit is not None else None

    def iteration_dir(self, n: int) -> str:
        return "" if self.req.repeat == 1 else f"iter-{n:03d}"

    def public(self) -> dict[str, Any]:
        r = self.req
        with self.mu:
            lines, n_lines = list(self.lines), self.n_lines
        return {
            "id": self.id, "board_id": self.board_id, "target": self.target, "state": self.state,
            "result": self.result, "exit": self.exit, "reason": self.reason or None,
            "plan": r.plan, "auto": r.auto, "writes": r.writes, "repeat": r.repeat,
            "interval_s": r.interval_s, "until": _iso(r.until), "margin_s": r.margin_s,
            "start_at": _iso(r.start_at), "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at), "ended_at": _iso(self.ended_at),
            "evidence": str(self.evidence), "iteration": self.iteration, "check": self.check,
            "phase": self.phase or None, "counts": dict(self.counts), "totals": dict(self.totals),
            "iterations": list(self.iterations), "next_at": _iso(self.next_at),
            "first_failure": self.first_failure, "last_check": self.last_check,
            "lease": dict(self.lease),
            "stop_requested": self.stop_requested, "announce": self.announce,
            "log": lines, "log_total": n_lines, "route": R.ROUTE_SERVICE,
        }

    def row(self) -> dict[str, Any]:
        """Its line in ``index.json``."""
        return {"id": self.id, "board_id": self.board_id, "target": self.target,
                "plan": self.req.plan, "writes": self.req.writes, "state": self.state,
                "result": self.result, "exit": self.exit, "reason": self.reason or None,
                "created_at": _iso(self.created_at), "started_at": _iso(self.started_at),
                "ended_at": _iso(self.ended_at), "evidence": str(self.evidence),
                "repeat": self.req.repeat, "iterations": len(self.iterations)}


def _zeros() -> dict[str, int]:
    return dict.fromkeys(R.VERDICTS, 0)


class HilRuns:
    """The service's checks runs. ``engine`` is the daemon's; ``leases`` returns its
    ``LeaseService`` (None: no hub support); ``gates`` its ``BoardGates`` (the auto plan's
    reads take the board's op gate). ``invoker_factory(run)`` makes the runner's invoker (tests
    pass the CLI in-process); ``clock``/``sleep`` are the runner's (tests pass fake ones)."""

    def __init__(self, engine: Any, state_dir: Path, *, bus: Any = None,
                 leases: Callable[[], Any] | None = None, gates: Any = None,
                 invoker_factory: Callable[[Run], R.Invoker] | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.engine = engine
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "checks"
        self.bus = bus if bus is not None else getattr(engine, "bus", None)
        self._leases = leases or (lambda: None)
        self.gates = gates
        self.invoker_factory = invoker_factory
        self.clock = clock
        self.sleep = sleep
        self._mu = threading.Lock()
        self._active: dict[str, Run] = {}
        self._last: dict[str, Run] = {}
        self._closed = False

    # -- reads (none of them touches the board) ----------------------------------------------

    def active(self, board_id: str) -> Run | None:
        with self._mu:
            return self._active.get(board_id)

    def running_boards(self) -> list[str]:
        with self._mu:
            return list(self._active)

    def defaults(self) -> dict[str, Any]:
        now = self.clock()
        return {"until": _iso(next_time(DEFAULT_UNTIL, now)), "until_text": DEFAULT_UNTIL,
                "interval_s": DEFAULT_INTERVAL_S, "writes": DEFAULT_WRITES,
                "margin_min": 10, "gap_s": 3.0, "max_unreachable": 3}

    @staticmethod
    def plans() -> list[dict[str, Any]]:
        out = []
        for name in sorted(P.PLANS):
            plan = P.build(name)
            checks = [c for c in plan.checks() if not c.skip and c.tier != P.MANUAL]
            out.append({"name": name, "runbook": plan.runbook, "static": plan.static,
                        "impl": plan.impl,
                        "read": sum(c.tier == P.READ for c in checks),
                        "safe": sum(c.tier == P.SAFE for c in checks)})
        return out

    def status(self, board_id: str) -> dict[str, Any]:
        run = self.active(board_id)
        with self._mu:
            last = self._last.get(board_id)
        return {"board_id": board_id, "run": run.public() if run else None,
                "last": last.public() if last is not None and run is None else None,
                "runs": self.history(board_id), "plans": self.plans(),
                "defaults": self.defaults()}

    # -- the index and the past runs -----------------------------------------------------------

    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _read_index(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("runs") if isinstance(data, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def _write_row(self, run: Run) -> None:
        with self._mu:
            rows = [r for r in self._read_index() if r.get("id") != run.id]
            rows.append(run.row())
            rows = rows[-INDEX_KEEP:]
            R._write_json(self._index_path(), {"runs": rows})

    def history(self, board_id: str) -> list[dict[str, Any]]:
        """This board's past runs, newest first: the index row plus what the run's
        ``summary.json`` says (per-iteration counts and first failures)."""
        rows = [r for r in self._read_index() if r.get("board_id") == board_id]
        live = self.active(board_id)
        out = []
        for row in reversed(rows[-HISTORY:]):
            item = dict(live.row() if live is not None and live.id == row.get("id") else row)
            if item.get("state") in ACTIVE and (live is None or live.id != item.get("id")):
                # A run of an earlier service process that never ended here: it stopped
                # with that process (killed, or restarted). Its REPORT.md says where it was.
                item.update(state=INTERRUPTED, reason="the service stopped while the run was on: "
                                                      "REPORT.md of the last iteration says where "
                                                      "it was (the board may be left swapped)")
            ev = Path(str(row.get("evidence") or ""))
            summary = _load_json(ev / "summary.json") if row.get("evidence") else None
            item["report"] = (ev / "REPORT.md").is_file() if row.get("evidence") else False
            item["announce"] = (ev / "ANNOUNCE.txt").is_file() if row.get("evidence") else False
            if isinstance(summary, dict):
                item["summary"] = _summary_brief(summary)
            out.append(item)
        return out

    def _run_row(self, board_id: str, run_id: str) -> dict[str, Any]:
        if not _RUN_ID.match(run_id or ""):
            raise UsageError(f"{run_id!r} is not a run id (YYYYMMDD-HHMMSS-xxxx)")
        row = next((r for r in self._read_index()
                    if r.get("id") == run_id and r.get("board_id") == board_id), None)
        if row is None:
            active = self.active(board_id)
            if active is not None and active.id == run_id:
                return active.row()
            raise AbsentError(f"no checks run {run_id} on {board_id}",
                              hint="GET /boards/{bid}/checks lists the runs")
        return row

    def report(self, board_id: str, run_id: str, *, iteration: int | None = None,
               name: str = "REPORT.md") -> dict[str, Any]:
        """A run's ``REPORT.md`` (or ``ANNOUNCE.txt``), or an iteration's ``REPORT.md``."""
        row = self._run_row(board_id, run_id)
        ev = Path(str(row.get("evidence") or ""))
        if name not in ("REPORT.md", "ANNOUNCE.txt"):
            raise UsageError(f"{name!r}: only REPORT.md and ANNOUNCE.txt are served")
        if iteration is not None:
            if name != "REPORT.md" or not isinstance(iteration, int) or iteration < 1:
                raise UsageError("iteration is a whole number from 1, for REPORT.md")
            path = ev / f"iter-{iteration:03d}" / "REPORT.md"
            if not path.is_file() and iteration == 1 and (ev / "REPORT.md").is_file() \
                    and not (ev / "iter-001").exists():
                path = ev / "REPORT.md"             # one iteration: written at the top
        else:
            path = ev / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise AbsentError(f"{path} is not there (yet)",
                              hint="REPORT.md is written when an iteration or the run ends") \
                from None
        return {"board_id": board_id, "run": run_id, "iteration": iteration, "path": str(path),
                "name": name, "text": text}

    # -- starting a run ------------------------------------------------------------------------

    def parse(self, board_id: str, body: dict[str, Any]) -> RunRequest:
        """The request, checked (400 USAGE for anything wrong). ``plan`` may be ``auto``
        (resolved by ``start``/``preview``)."""
        now = self.clock()
        plan = body.get("plan", "auto") or "auto"
        if not isinstance(plan, str) or (plan != "auto" and plan not in P.PLANS):
            raise UsageError(f"plan must be auto or one of {', '.join(sorted(P.PLANS))}, "
                             f"not {plan!r}")
        writes = body.get("writes", DEFAULT_WRITES)
        if writes not in ("none", "safe"):
            raise UsageError(f"writes must be \"none\" (read only) or \"safe\", not {writes!r}")
        interval = _num(body, "interval_s", DEFAULT_INTERVAL_S)
        if interval < R.MIN_INTERVAL_S:
            raise UsageError(f"interval_s must be at least {R.MIN_INTERVAL_S:g} (no tight loops)")
        gap = _num(body, "gap_s", 3.0)
        if gap < R.MIN_GAP_S:
            raise UsageError(f"gap_s must be at least {R.MIN_GAP_S:g}")
        margin = _num(body, "margin_min", 10.0)
        if margin < 0:
            raise UsageError("margin_min must not be negative")
        maxu = body.get("max_unreachable", 3)
        if isinstance(maxu, bool) or not isinstance(maxu, int) or maxu < 1:
            raise UsageError("max_unreachable must be a whole number from 1")
        start_at = None
        if body.get("start_at") not in (None, ""):
            start_at = next_time(body["start_at"], now)
        begin = start_at if start_at is not None else now
        until = None
        text = body.get("until", DEFAULT_UNTIL)
        if text not in (None, ""):
            until = next_time(text, begin)
            if until <= begin:
                raise UsageError("until must be after the start")
        repeat = body.get("repeat")
        if repeat is None:
            repeat = MAX_REPEAT if until is None else max(
                1, min(MAX_REPEAT, int((until - begin) // interval) + 1))
        if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 10000:
            raise UsageError("repeat must be a whole number from 1 to 10000")
        take = body.get("take_lease", False)
        if not isinstance(take, bool):
            raise UsageError("take_lease must be true or false")
        stop_first = body.get("stop_on_first_fail", False)
        if not isinstance(stop_first, bool):
            raise UsageError("stop_on_first_fail must be true or false")
        static = body.get("expect_static") or None
        if static is not None and (not isinstance(static, str)
                                   or not re.fullmatch(r"0x[0-9a-fA-F]{8}", static)):
            raise UsageError("expect_static is 0x plus 8 hex digits")
        evidence = body.get("evidence") or None
        ev_path = None
        if evidence is not None:
            if not isinstance(evidence, str) or not Path(evidence).is_absolute():
                raise UsageError("evidence must be an absolute path (a new folder)",
                                 hint="the service's working directory is not yours")
            ev_path = Path(evidence)
            if ev_path.exists() and any(p.name != "ANNOUNCE.txt" for p in ev_path.iterdir()):
                raise RefusedError(f"{ev_path} already holds evidence: give a new folder "
                                   "(evidence is never overwritten)")
        return RunRequest(plan=plan, auto=None, writes=writes, repeat=repeat,
                          interval_s=interval, until=until, margin_s=60.0 * margin,
                          start_at=start_at, take_lease=take, evidence=ev_path,
                          expect_static=static.lower() if static else None, gap_s=gap,
                          max_unreachable=maxu, stop_on_first_fail=stop_first)

    def _session(self, board_id: str) -> Any:
        try:
            return self.engine.session(board_id)
        except HarnessError:
            raise AbsentError(f"{board_id} is not open in the Harness Manager service",
                              hint="open the board first: the run keeps it open until it "
                                   "ends") from None

    def resolve_plan(self, board_id: str, req: RunRequest) -> RunRequest:
        """``plan: auto`` -> the plan ``checks.plans.auto_plan`` picks from the board's
        identity and its user microSD, read now (an explicit action: someone pressed Start)."""
        if req.plan != "auto":
            return req
        session = self._session(board_id)
        ident: dict[str, Any] | None = None
        card: dict[str, Any] | None = None
        gate = self.gates.op(board_id) if self.gates is not None else contextlib.nullcontext()
        with gate:
            ident = _jsonable(getattr(self.engine.info(board_id), "identity", None))
            if ident and ident.get("harness_impl") == "linux":
                card = self._card(session)
        plan, why = P.auto_plan(ident, card)
        req.plan, req.auto = plan, {"plan": plan, "why": why}
        return req

    def _card(self, session: Any) -> dict[str, Any] | None:
        """The user microSD as ``GET /boards/{bid}/card`` reads it (with the OS slots), or
        None when it cannot be read (the auto plan then says it assumed a blank card)."""
        from harness_manager.core.pack import card_status_of

        deploy = getattr(self.engine, "deploy", None)
        if deploy is None:
            return None
        try:
            status = card_status_of(deploy, session)
            annotate = getattr(getattr(session, "card", None), "annotate", None)
            if status.store and callable(annotate):
                status = annotate(status)
        except HarnessError as exc:
            log.info("checks: the card of %s could not be read: %s", session, exc.message)
            return None
        return _jsonable(status)

    def lease_state(self, board_id: str) -> tuple[str, dict[str, Any], Any]:
        """(``here`` | ``free`` | ``elsewhere`` | ``other`` | ``none``, the lease view, the hub).
        One hub read (the lease service's 10 s cache)."""
        session = self._session(board_id)
        hub = getattr(session, "hub", None)
        leases = self._leases()
        if hub is None or leases is None:
            return "none", {}, hub
        view = leases.view(hub)
        lease = view.get("lease") if isinstance(view, dict) else None
        if not lease:
            return "free", view, hub
        if lease.get("here"):
            return "here", view, hub
        return ("elsewhere" if lease.get("mine") else "other"), view, hub

    def _lease_refusal(self, board_id: str, state: str, view: dict[str, Any],
                       take: bool) -> HarnessError | None:
        lease = (view or {}).get("lease") or {}
        name = lease.get("board") or view.get("board") or lease.get("target") or board_id
        if state == "none":
            return UnavailableError(CAPABILITY, f"{board_id} is not behind a hub: the checks "
                                                "need its hub lease held here (every plan's "
                                                "first gate)")
        if state in ("other", "elsewhere"):
            holder = lease.get("holder") or "someone else"
            also = " (your hub name, another session or tool: not this Harness Manager)" \
                if state == "elsewhere" else ""
            return HeldError(f"the hub lease on {name} is held by {holder}{also}",
                             holder=holder, hint="the checks need the lease free or held by this "
                                                 "Harness Manager; ask for it (Request board) "
                                                 "or wait. A run never forces a lease")
        if state == "free" and not take:
            return RefusedError(f"nobody holds the hub lease on {name}: take it for the run",
                                hint='"take_lease": true: the service takes it when the run '
                                     "starts, holds it until the run ends, then releases it")
        return None

    def preview(self, board_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``announce_only``: the announcement the run would write, and nothing started."""
        req = self.resolve_plan(board_id, self.parse(board_id, body))
        session = self._session(board_id)
        state, view, _hub = self.lease_state(board_id)
        refusal = self._lease_refusal(board_id, state, view, req.take_lease)
        target = naming.address_of(session.candidate)
        evidence = req.evidence or self._new_evidence(board_id, req.plan, peek=True)
        run = Run(id="preview", board_id=board_id, target=target, req=req, evidence=evidence,
                  created_at=self.clock())
        run.lease = self._lease_public(state, view, take=req.take_lease and state == "free")
        return {"board_id": board_id, "plan": req.plan, "auto": req.auto,
                "announce": self._announce(run, req.start_at or self.clock()),
                "lease": run.lease, "refusal": None if refusal is None else {
                    "name": type(refusal).__name__, "code": int(refusal.code),
                    "message": refusal.message, "hint": refusal.hint},
                "run": run.public()}

    def start(self, board_id: str, body: dict[str, Any]) -> Run:
        if self._closed:
            raise UnavailableError(CAPABILITY, "the service is stopping")
        existing = self.active(board_id)
        if existing is not None:
            err = AlreadyError(f"a checks run is already {existing.state} on {board_id} "
                               f"(run {existing.id}, plan {existing.req.plan})",
                               hint="one run per board: stop it first (the app's Stop, or "
                                    "DELETE /boards/{bid}/checks)")
            err.data = {"run": existing.id}     # type: ignore[attr-defined]
            raise err
        req = self.resolve_plan(board_id, self.parse(board_id, body))
        session = self._session(board_id)
        state, view, _hub = self.lease_state(board_id)
        refusal = self._lease_refusal(board_id, state, view, req.take_lease)
        if refusal is not None:
            raise refusal
        now = self.clock()
        run_id = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        evidence = req.evidence or self._new_evidence(board_id, req.plan, stamp=run_id[:15])
        run = Run(id=run_id, board_id=board_id, target=naming.address_of(session.candidate),
                  req=req, evidence=evidence, created_at=now,
                  state=SCHEDULED if req.start_at and req.start_at > now else STARTING)
        run.lease = self._lease_public(state, view, take=req.take_lease and state == "free")
        with self._mu:
            if board_id in self._active:            # lost a race with another Start
                raise AlreadyError(f"a checks run is already active on {board_id}")
            self._active[board_id] = run
        try:
            evidence.mkdir(parents=True, exist_ok=True)
            run.announce = self._announce(run, req.start_at or now)
            (evidence / "ANNOUNCE.txt").write_text(run.announce, encoding="utf-8")
            self._write_row(run)
        except OSError as exc:
            with self._mu:
                self._active.pop(board_id, None)
            raise UsageError(f"cannot write the evidence folder {evidence}: {exc}") from exc
        self._say(run, f"{'scheduled for ' + _iso(req.start_at) if run.state == SCHEDULED else 'starting'}"
                       f": plan {req.plan}, writes {req.writes}, evidence {evidence}")
        self._publish_state(run)
        run.thread = threading.Thread(target=self._thread, args=(run,), daemon=True,
                                      name=f"hil-run-{board_id}")
        run.thread.start()
        return run

    def _new_evidence(self, board_id: str, plan: str, *, stamp: str = "",
                      peek: bool = False) -> Path:
        stamp = stamp or datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
        base = self.root / _slug(board_id) / f"{stamp}-{plan}"
        if peek:
            return base
        path, n = base, 1
        while path.exists():
            n += 1
            path = base.with_name(f"{base.name}-{n}")
        return path

    @staticmethod
    def _lease_public(state: str, view: dict[str, Any], *, take: bool) -> dict[str, Any]:
        lease = (view or {}).get("lease") or {}
        return {"state": state, "holder": lease.get("holder") or "",
                "expires_at": lease.get("expires_at") or "",
                "board": lease.get("board") or (view or {}).get("board") or "",
                "take": take, "taken": False, "released": False, "kept": state == "here"}

    def _announce(self, run: Run, started: float) -> str:
        plan = P.build(run.req.plan, static=run.req.expect_static)
        return R.announce_text(plan, self._options(run), started)

    def _options(self, run: Run) -> R.Options:
        r = run.req
        stop_at = None if r.until is None else r.until - r.margin_s
        if run.lease.get("take"):
            lease = ("free at Start: the Harness Manager service takes it when the run starts, "
                     "heartbeats it until the run ends, then releases it; the runner only checks "
                     "it is held here")
        else:
            lease = ("held by this Harness Manager; the service heartbeats it until the run "
                     "ends (the board stays open in the service); the runner only checks it is "
                     "held here, and never acquires, requests, forces or releases one")
        return R.Options(board=run.target, evidence=run.evidence, writes=r.writes,
                         repeat=r.repeat, interval_s=r.interval_s,
                         stop_on_first_fail=r.stop_on_first_fail, stop_at=stop_at,
                         until=r.until, gap_s=r.gap_s, max_unreachable=r.max_unreachable,
                         margin_s=r.margin_s, route=R.ROUTE_SERVICE, lease_kept=True,
                         lease_line=lease,
                         stop_line=("the app's Checks section: Stop (DELETE /api/v1/boards/"
                                    f"{run.board_id}/checks): it finishes the check, restores "
                                    "greybox, writes REPORT.md"),
                         where_line=(f"on {socket.gethostname()}, in the Harness Manager service "
                                     f"(pid {os.getpid()}, run {run.id})"))

    # -- stopping ------------------------------------------------------------------------------

    def stop(self, board_id: str) -> Run:
        """The runner's SIGINT: finish the check, restore greybox, write the report. A
        scheduled run that has not started is cancelled (nothing was sent)."""
        run = self.active(board_id)
        if run is None:
            raise AbsentError(f"no checks run is active on {board_id}",
                              hint="GET /boards/{bid}/checks lists the past runs")
        if not run.stop_requested:
            run.stop_requested = True
            run.cancel.set()
            runner = run.runner
            if runner is not None:
                runner.halt.set()
                self._say(run, "stop asked: finishing the check, then greybox and the report")
            if run.state in (RUNNING, STARTING):
                run.state = STOPPING
            self._publish_state(run)
        return run

    def guard_close(self, board_id: str) -> None:
        """``DELETE /boards/{bid}``: refused while a run is active (the run needs the board
        open here: its commands share this session and its lease is heartbeated while open)."""
        run = self.active(board_id)
        if run is not None:
            err = HeldError(f"a checks run is {run.state} on {board_id} (run {run.id}): the "
                            "board stays open until it ends",
                            holder=f"harness-manager-daemon checks run {run.id}",
                            hint="stop the run first (Checks: Stop); it puts greybox back")
            err.data = {"run": run.id, "kind": "checks"}   # type: ignore[attr-defined]
            raise err

    def guard_shutdown(self, force: bool) -> None:
        """``POST /daemon/shutdown``: refused while a run is active, unless forced (then the
        runs are stopped, and the service waits for them to put greybox back)."""
        with self._mu:
            runs = list(self._active.values())
        if runs and not force:
            names = ", ".join(f"run {r.id} on {r.board_id} ({r.state})" for r in runs)
            err = HeldError(f"harness-manager-daemon has checks running: {names}",
                            holder="harness-manager-daemon checks",
                            hint="stop them first (Checks: Stop), or stop with --force: each run "
                                 "then finishes its check and restores greybox before the "
                                 "service exits")
            err.data = {"runs": [r.id for r in runs]}   # type: ignore[attr-defined]
            raise err

    def close(self, wait_s: float = CLOSE_WAIT_S) -> None:
        """The service is stopping: stop every run and wait (bounded) for its end state."""
        self._closed = True
        with self._mu:
            runs = list(self._active.values())
        for run in runs:
            with contextlib.suppress(HarnessError):
                self.stop(run.board_id)
        deadline = time.monotonic() + wait_s
        for run in runs:
            t = run.thread
            if t is not None and t is not threading.current_thread():
                t.join(timeout=max(0.0, deadline - time.monotonic()))
                if t.is_alive():
                    log.warning("checks run %s on %s did not finish within %.0f s of the "
                                "service stopping: the board may be left swapped", run.id,
                                run.board_id, wait_s)

    # -- the run's thread ----------------------------------------------------------------------

    def _thread(self, run: Run) -> None:
        try:
            if run.state == SCHEDULED and not self._wait_start(run):
                run.state, run.reason = CANCELLED, "stopped before its start: nothing was sent"
                return
            run.state = STARTING
            run.started_at = self.clock()
            self._publish_state(run)
            why = self._take_lease(run)
            if why:
                run.state, run.reason, run.exit = REFUSED, f"refused to start: {why}", R.EXIT_STOP
                self._write_refusal(run)
                return
            self._run(run)
        except Exception as exc:  # noqa: BLE001 - a bug: say so in the run, never lose the board
            log.exception("checks run %s on %s failed", run.id, run.board_id)
            run.state, run.reason = FAILED, f"internal error: {type(exc).__name__}: {exc}"
        finally:
            self._end(run)

    def _wait_start(self, run: Run) -> bool:
        start = run.req.start_at or 0.0
        self._publish_state(run)
        while not run.cancel.is_set():
            left = start - self.clock()
            if left <= 0:
                return True
            if self.sleep is not None:
                self.sleep(min(left, 30.0))
            else:
                run.cancel.wait(min(left, 30.0))
        return False

    def _take_lease(self, run: Run) -> str:
        """"" when the lease is held here for the run (taken now when it was free and the
        request said so), else why not. Never forces; a queue refuses."""
        try:
            state, view, hub = self.lease_state(run.board_id)
        except HarnessError as exc:
            return exc.message
        run.lease.update(self._lease_public(state, view, take=run.lease.get("take", False)))
        leases = self._leases()
        if state == "here":
            leases.track(run.board_id, hub)          # heartbeated while the board is open here
            run.lease["kept"] = True
            self._say(run, "the lease is held here: the service heartbeats it until the run ends")
            return ""
        refusal = self._lease_refusal(run.board_id, state, view, bool(run.lease.get("take")))
        if refusal is not None:
            return refusal.message
        # free, and the request said take it
        self._say(run, "taking the lease for the run")
        try:
            out = leases.acquire(hub, board_id=run.board_id, timeout_s=TAKE_LEASE_TIMEOUT_S,
                                 heartbeat=True)
        except HarnessError as exc:
            return f"the lease could not be taken: {exc.message}"
        lease = (out or {}).get("lease") or {}
        run.lease.update(state="here", taken=True, kept=True, holder=lease.get("holder", ""),
                         expires_at=lease.get("expires_at", ""))
        self._say(run, f"lease taken ({lease.get('holder', '')}); released when the run ends")
        return ""

    def _run(self, run: Run) -> None:
        plan = P.build(run.req.plan, static=run.req.expect_static)
        opts = self._options(run)
        started = self.clock()
        run.announce = R.announce_text(plan, opts, started)
        with contextlib.suppress(OSError):
            (run.evidence / "ANNOUNCE.txt").write_text(run.announce, encoding="utf-8")
        invoker = (self.invoker_factory(run) if self.invoker_factory is not None
                   else R.SubprocessInvoker(R.default_hm(), route=R.ROUTE_SERVICE,
                                            state_dir=self.state_dir))
        runner = R.Runner(plan, opts, invoker, clock=self.clock, sleep=self.sleep,
                          log=lambda text: self._say(run, text),
                          notify=lambda kind, data: self._progress(run, kind, data))
        run.runner = runner
        if run.stop_requested:
            runner.halt.set()
        run.state = STOPPING if run.stop_requested else RUNNING
        self._publish_state(run)
        run.exit = runner.run()
        if runner.refused_start:
            run.state, run.reason = REFUSED, runner.refused_start
        else:
            run.state = DONE
            stopped = next((it.stopped for it in runner.iterations if it.stopped), None)
            if runner.end.get("stop") and not stopped:
                run.reason = runner.end.get("restore", "")
            elif stopped:
                run.reason = f"stopped at {stopped.get('check')}: {stopped.get('reason')}"
            elif run.stop_requested:
                run.reason = "stopped from the app (Stop): greybox restored"
            elif runner.ended_early:
                run.reason = f"ended: {runner.ended_early}"

    def _write_refusal(self, run: Run) -> None:
        """A run that never reached the runner still leaves a REPORT.md and summary.json."""
        with contextlib.suppress(OSError):
            R._write_json(run.evidence / "summary.json",
                          {"plan": run.req.plan, "board": run.target, "exit": R.EXIT_STOP,
                           "refused_start": run.reason, "iterations": [], "checks": []})
            (run.evidence / "REPORT.md").write_text(
                f"# HIL-AUTO: {run.req.plan} on {run.target}\n\n"
                f"- **STOPPED: {run.reason}** (exit 2; nothing touched the board)\n",
                encoding="utf-8")

    def _end(self, run: Run) -> None:
        try:
            self._release_if_taken(run)
        finally:
            run.ended_at = self.clock()
            run.check, run.next_at, run.phase = None, None, ""
            if run.state in ACTIVE:
                run.state = DONE
            with contextlib.suppress(OSError):
                self._write_row(run)
            with self._mu:
                if self._active.get(run.board_id) is run:
                    del self._active[run.board_id]
                self._last[run.board_id] = run
            self._say(run, f"ended: {run.state}" + (f", {run.result}" if run.result else "")
                      + (f" ({run.reason})" if run.reason else ""))
            self._publish_state(run)

    def _release_if_taken(self, run: Run) -> None:
        """The run took the lease: give it back, once greybox is back (never someone else's)."""
        if not run.lease.get("taken"):
            return
        leases = self._leases()
        try:
            session = self.engine.session(run.board_id)
            hub = getattr(session, "hub", None)
            if leases is None or hub is None or leases.store.get(hub.host, hub.target) is None:
                return
            leases.release(hub, board_id=run.board_id)
            run.lease.update(released=True, state="free", kept=False)
            self._say(run, "the lease the run took is released")
            # the board is still open here: the heartbeat goes on for a lease acquired later
            leases.track(run.board_id, hub)
        except HarnessError as exc:
            run.lease["release_error"] = exc.message
            self._say(run, f"the lease the run took was not released: {exc.message}")

    # -- progress ------------------------------------------------------------------------------

    def _progress(self, run: Run, kind: str, data: dict[str, Any]) -> None:
        publish = True
        if kind == "iteration":
            run.iteration = int(data.get("n", 0))
            run.counts = _zeros()
            run.last_check = None
            run.check, run.next_at, run.phase = None, None, "checks"
        elif kind == "check":
            run.check = {k: data.get(k) for k in ("id", "section", "title", "tier", "started")}
        elif kind == "result":
            res = data.get("result") or {}
            verdict = res.get("verdict", "")
            run.counts[verdict] = run.counts.get(verdict, 0) + 1
            if not run.totals:
                run.totals = _zeros()
            run.totals[verdict] = run.totals.get(verdict, 0) + 1
            if verdict in (R.FAIL, R.STOPPED) and run.first_failure is None:
                sub = run.iteration_dir(int(data.get("iteration") or run.iteration))
                ev = res.get("evidence") or ""
                run.first_failure = {"id": res.get("id"), "title": res.get("title"),
                                     "iteration": data.get("iteration"),
                                     "verdict": verdict, "reason": res.get("reason"),
                                     "hint": res.get("hint") or "",
                                     "evidence": f"{sub}/{ev}" if sub and ev else ev}
            publish = bool(res.get("command"))           # executed, not a plan's static line
            if publish:
                run.check = None
                run.last_check = {"id": res.get("id"), "title": res.get("title"),
                                  "verdict": verdict, "iteration": data.get("iteration")}
        elif kind == "iteration_end":
            run.iterations.append({k: data.get(k) for k in (
                "n", "exit", "counts", "first_failure", "stopped", "ended_early")})
            run.iterations[-1]["result"] = RESULT.get(data.get("exit"))
            run.check = None
        elif kind == "waiting":
            run.next_at = data.get("next_at")
            run.phase = "waiting"
        elif kind == "end_state":
            run.check, run.next_at, run.phase = None, None, "end state"
        elif kind == "end":
            publish = False
        if publish:
            self._publish(TOPIC_PROGRESS, run, {
                "run": run.id, "kind": kind, "state": run.state, "iteration": run.iteration,
                "repeat": run.req.repeat, "check": run.check, "counts": dict(run.counts),
                "totals": dict(run.totals), "next_at": _iso(run.next_at),
                "phase": run.phase or None, "first_failure": run.first_failure,
                "last_check": run.last_check,
                "result": (data.get("result") or {}).get("verdict") if kind == "result" else None,
                "id": (data.get("result") or {}).get("id") if kind == "result" else None})

    def _say(self, run: Run, text: str) -> None:
        stamp = datetime.fromtimestamp(self.clock()).strftime("%H:%M:%S")
        with run.mu:
            run.lines.append(f"{stamp}  {text}")
            run.n_lines += 1
        log.info("checks %s on %s: %s", run.id, run.board_id, text)

    def _publish_state(self, run: Run) -> None:
        self._publish(TOPIC_STATE, run, {
            "run": run.id, "state": run.state, "result": run.result, "exit": run.exit,
            "reason": run.reason or None, "plan": run.req.plan, "writes": run.req.writes,
            "start_at": _iso(run.req.start_at), "until": _iso(run.req.until),
            "evidence": str(run.evidence), "lease": dict(run.lease)})

    def _publish(self, topic: str, run: Run, data: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(Event(topic, run.board_id, data))
        except Exception:  # noqa: BLE001 - an event never breaks a run
            log.exception("publishing %s", topic)


# --- helpers ---------------------------------------------------------------------------------------


def _num(body: dict[str, Any], key: str, default: float) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError(f"{key} must be a number, not {value!r}")
    return float(value)


def _jsonable(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    from harness_manager.cli.output import jsonable

    out = jsonable(obj)
    return out if isinstance(out, dict) else None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _summary_brief(s: dict[str, Any]) -> dict[str, Any]:
    """What the past-runs list shows of a ``summary.json`` (one iteration or many)."""
    if isinstance(s.get("iterations"), list) and "checks" not in s:
        iters = [{k: i.get(k) for k in ("n", "exit", "counts", "first_failure", "ended_early",
                                        "stopped")}
                 for i in s["iterations"] if isinstance(i, dict)]
    else:
        iters = [{"n": s.get("iteration", 1), "exit": s.get("exit"), "counts": s.get("totals"),
                  "first_failure": s.get("first_failure"), "ended_early": s.get("ended_early"),
                  "stopped": s.get("stopped")}] if s.get("checks") else []
    for it in iters:
        it["result"] = RESULT.get(it.get("exit"))
    return {"exit": s.get("exit"), "result": RESULT.get(s.get("exit")),
            "refused_start": s.get("refused_start"), "end_state": s.get("end_state"),
            "ended_early": s.get("ended_early"), "iterations": iters,
            "started": s.get("started"), "ended": s.get("ended")}


def later(seconds: float, now: float | None = None) -> str:
    """An ISO time ``seconds`` from now (tests and the docs' examples)."""
    base = datetime.fromtimestamp(now if now is not None else time.time()).astimezone()
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")
