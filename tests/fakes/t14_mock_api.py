"""A mock ``harness-manager-daemon`` for the web UI (Team T14): docs/API.md v1 over an in-process engine.

It implements the frozen API contract (docs/API.md) with FastAPI over any
object that follows ``harness_manager.core.services.Engine``: by default the GUI's
``DemoEngine`` (three scripted boards), or the real ``Engine`` over
``VirtualMps3``. It serves the UI from ``harness_manager.web`` exactly as the daemon
will (``harness_manager.web.mount_static``), so the page runs here unchanged.

It is a test double, not the daemon: the real server is Team T13's
``harness_manager.daemon``, and the browser tests run against that. The mock follows
the behaviour T13 settled where API.md leaves it open (``OPEN_POINTS``), and
``tests/web/test_t14_mock_contract.py`` checks its route table against both
API.md and the real daemon's.

Run the UI by hand over the demo boards (the real harness-manager-daemon app by default,
this mock with ``--mock``)::

    .venv/bin/python -m tests.fakes.t14_mock_api --port 8765
    # then open the printed http://127.0.0.1:8765/#token=... URL
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import itertools
import json
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from harness_manager.cli.output import error_json, jsonable, reading_json
from harness_manager.core import capabilities as C
from harness_manager.core.errors import (
    AbsentError,
    ExitCode,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus, _matches
from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind
from harness_manager.core.pack import ProbeHints
from harness_manager.core.session import LockOwner

from .l3_week_plan import EXTENSION_ROUTES, SimClocks, WeekPlanSim
from .l3_week_plan import register as register_week_plan
from .t14_lease_requests import LEASE_REQUEST_ROUTES, LeaseRequestSim
from .t14_lease_requests import register as register_lease_requests

API = "/api/v1"
VERSION = "0.0.1-t14-mock"
UI_NOTE = "harness-manager-ui"

#: HTTP status per ExitCode, from the table in docs/API.md.
HTTP_STATUS: dict[ExitCode, int] = {
    ExitCode.USAGE: 400,
    ExitCode.ABSENT: 404,
    ExitCode.HELD: 409,
    ExitCode.ALREADY: 409,
    ExitCode.UNREACHABLE: 502,
    ExitCode.UNAVAILABLE: 422,
    ExitCode.NOTHING_ON_TARGET: 422,
    ExitCode.INCOMPATIBLE: 409,
    ExitCode.REFUSED: 409,
}

#: Where docs/API.md is open, and what harness-manager-daemon (T13) does there; the mock does the same.
OPEN_POINTS: dict[str, str] = {
    "GET /boards/{bid}": "BoardInfo fields flattened next to ok (the CLI's `info --json`)",
    "GET /boards/{bid}/debug": "DebugStatus fields flattened next to ok",
    "POST /boards/{bid}/debug/down": "DebugStatus fields flattened next to ok",
    "POST /boards": "candidate = a Candidate object; {board_id, info} with info null + info_error "
                    "when the first read fails (the session is open); 409 ALREADY when open",
    "GET /boards": "every board the daemon has probed or opened, open or not, plus `job`; "
                   "SIDEBAR-UX: and every board boards.toml configures (source config, not "
                   "contacted), each row with `source` and, when it has a table, `configured`",
    "DELETE /boards/{bid}": "?release=true (LEASE-UI) releases the lease THIS Harness Manager "
                            "holds (lease.here) first and adds `released` (null: none held "
                            "here); a failed release leaves the board open",
    "401": "error REFUSED (15): 'session expired: run harness-manager ui again'",
    "jobs": "while a job runs on a board, the board's other requests are 409 HELD naming it",
    "POST /deploy": "preflight first; a refusal is 409 (14/15) with error.data.{overlay, "
                    "preflight} and no job; keep_on_card true then reads the card, and a card "
                    "that cannot take it is 422 (12) with error.data.{overlay, card}, no job; "
                    "FIX-PACK-7: the job refuses 15 (error.data.debug_down) when the board's "
                    "OpenOCD cannot be stopped first, and force true swaps anyway (a warning)",
    "overlay": "a name, an rm_id, or the OverlayRef object",
    "console WS": "text frames {state,name,detail} / {dropped,dropped_frames} / {error}; "
                  "binary frames carry bytes both ways; {state:closed} then close 1000",
    "refused WS": "an HTTP denial with the envelope, or close 4000 + exit code",
}

#: Routes harness-manager-daemon serves beyond API.md. Empty since the lead documented T13's
#: additions (session, jobs, daemon/shutdown); kept so a new one has a place to go.
ADDITIVE_ROUTES: tuple[tuple[str, str], ...] = ()

#: The route table: (method, path template). Kept literal so a test can diff it with API.md.
#: The week-plan additions (tests/fakes/l3_week_plan.py) and the lease requests
#: (docs/LEASE_REQUESTS.md; tests/fakes/t14_lease_requests.py) join it below.
ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/health"),
    ("GET", "/packs"),
    ("POST", "/probe"),
    ("GET", "/boards"),
    ("POST", "/boards"),
    ("DELETE", "/boards/{bid}"),
    ("GET", "/boards/{bid}"),
    ("GET", "/boards/{bid}/lock"),
    ("GET", "/boards/{bid}/telemetry"),
    ("GET", "/boards/{bid}/overlays"),
    ("GET", "/boards/{bid}/card"),
    ("POST", "/boards/{bid}/preflight"),
    ("POST", "/boards/{bid}/deploy"),
    ("POST", "/boards/{bid}/restore"),
    ("POST", "/boards/{bid}/reset"),
    ("GET", "/boards/{bid}/clocks"),
    ("POST", "/boards/{bid}/clocks"),
    ("GET", "/boards/{bid}/consoles"),
    ("WS", "/boards/{bid}/consoles/{name}"),
    ("POST", "/boards/{bid}/consoles/{name}/export"),
    ("GET", "/boards/{bid}/debug"),
    ("POST", "/boards/{bid}/debug/detect"),
    ("POST", "/boards/{bid}/debug/up"),
    ("POST", "/boards/{bid}/debug/down"),
    ("GET", "/boards/{bid}/controller/temps"),
    ("GET", "/boards/{bid}/controller/osc"),
    ("POST", "/boards/{bid}/controller/reboot"),
    ("POST", "/boards/{bid}/controller/command"),
    ("GET", "/boards/{bid}/storage/pending"),
    ("POST", "/boards/{bid}/storage/backup"),
    ("POST", "/boards/{bid}/storage/install"),
    ("POST", "/boards/{bid}/storage/restore"),
    ("POST", "/boards/{bid}/lab/{verb}"),
    ("GET", "/help/tabs"),
    ("GET", "/jobs/{id}"),
    ("GET", "/jobs"),
    ("GET", "/boards/{bid}/session"),
    ("POST", "/daemon/shutdown"),
    ("WS", "/events"),
) + tuple(r for routes in EXTENSION_ROUTES.values() for r in routes) + tuple(
    # LR-C may list the four in EXTENSION_ROUTES["hub_api"] too: each route once.
    r for r in LEASE_REQUEST_ROUTES if r not in EXTENSION_ROUTES.get("hub_api", ())
) + ADDITIVE_ROUTES


# --- jobs --------------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    board_id: str
    kind: str
    state: str = "running"                  # running | done | failed
    result: Any = None
    error: dict[str, Any] | None = None
    progress: dict[str, Any] = field(default_factory=lambda: {"phase": "", "done": 0,
                                                               "total": 0})
    phases: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    def describe(self) -> str:
        return f"{self.kind} job {self.id}"

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"job": self.id, "board_id": self.board_id, "kind": self.kind,
                               "state": self.state, "progress": dict(self.progress),
                               "phases": list(self.phases), "started_at": self.started_at,
                               "ended_at": self.ended_at}
        if self.state == "done":
            out["result"] = jsonable(self.result)
        if self.error is not None:
            out["error"] = self.error
        return out


class Jobs:
    """Long operations: 202 {job}, then job.* events on the engine bus."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise AbsentError(f"no job {job_id!r}", hint="job ids come from a 202 reply")
        return job

    def running(self, board_id: str | None = None) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values()
                    if (board_id is None or j.board_id == board_id) and j.state == "running"]

    def recent(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def gate(self, board_id: str) -> None:
        """harness-manager-daemon's rule: while a job runs on a board, its other requests are refused."""
        for job in self.running(board_id):
            err = HeldError(f"{board_id} is busy: {job.describe()} is running",
                            holder=f"harness-manager-daemon {job.describe()}",
                            hint=f"wait for it to finish (GET /api/v1/jobs/{job.id})")
            # As harness-manager-daemon: the job that holds it (a queued lease is kind "lease").
            err.data = {"job": job.id, "kind": job.kind, "board_id": board_id}  # type: ignore[attr-defined]
            raise err

    def start(self, board_id: str, kind: str,
              work: Callable[[Callable[[str, int, int], None]], Any],
              beside: tuple[str, ...] = ()) -> Job:
        """``beside``: job kinds this one may run next to on the same board (a force release
        runs beside its own queued lease request, which the revoke promotes)."""
        job = Job(id=f"j{next(self._ids)}-{uuid.uuid4().hex[:6]}", board_id=board_id, kind=kind)
        with self._lock:
            for other in self._jobs.values():
                if other.board_id == board_id and other.state == "running" \
                        and other.kind not in beside:
                    raise HeldError(f"{board_id} is busy: {other.describe()} is running",
                                    holder=f"harness-manager-daemon {other.describe()}")
            self._jobs[job.id] = job
        self.bus.publish(Event("job.started", board_id, {"job": job.id, "kind": kind}))

        def progress(phase: str, done: int, total: int) -> None:
            job.progress = {"phase": phase, "done": int(done), "total": int(total)}
            if not job.phases or job.phases[-1] != phase:
                job.phases.append(phase)
            self.bus.publish(Event("job.progress", board_id, {"job": job.id, "phase": phase,
                                                              "done": int(done),
                                                              "total": int(total)}))

        def run() -> None:
            try:
                result = work(progress)
            except HarnessError as exc:
                job.error = error_json(exc)["error"]
            except Exception as exc:  # noqa: BLE001 - reported as the job's error
                job.error = {"code": 1, "name": "FAILED", "message": str(exc),
                             "hint": "this is a bug in the mock daemon"}
            # Free the board BEFORE saying so (as harness-manager-daemon does): a client reacting to
            # job.done is never refused by this job's own claim.
            job.ended_at = time.time()
            if job.error is not None:
                job.state = "failed"
                self.bus.publish(Event("job.failed", board_id, {"job": job.id,
                                                                "error": job.error}))
            else:
                job.result = result
                job.state = "done"
                self.bus.publish(Event("job.done", board_id, {"job": job.id,
                                                              "result": jsonable(result)}))

        threading.Thread(target=run, daemon=True, name=f"job-{job.id}").start()
        return job


# --- the app -----------------------------------------------------------------------------


def auth_error() -> HarnessError:
    return RefusedError("missing or wrong token",
                        hint="`harness-manager ui` opens the UI with the current token")


def _err(exc: HarnessError) -> JSONResponse:
    return JSONResponse(error_json(exc), status_code=HTTP_STATUS.get(exc.code, 500))


def _ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, **jsonable(data)}


def _accepted(job: Job) -> JSONResponse:
    return JSONResponse({"ok": True, "job": job.id}, status_code=202)


def _candidate_from_json(data: dict[str, Any]) -> Candidate:
    links = tuple(Link(LinkKind(lk["kind"]), lk.get("address", ""), lk.get("detail", ""))
                  for lk in data.get("links", ()))
    ident = data.get("identity")
    identity = None
    if isinstance(ident, dict):
        fields = {k: v for k, v in ident.items() if k in BoardIdentity.__dataclass_fields__}
        if "features" in fields:
            fields["features"] = tuple(fields["features"])
        identity = BoardIdentity(**fields)
    return Candidate(pack=data.get("pack", "mps3"), board_id=data["board_id"], links=links,
                     label=data.get("label", ""), evidence=data.get("evidence", ""),
                     identity=identity)


class MockDaemonApp:
    """The state behind the routes: the engine, the boards it has seen, the jobs."""

    def __init__(self, engine: Any, token: str) -> None:
        self.engine = engine
        self.token = token
        self.jobs = Jobs(engine.bus)
        self._known: dict[str, Candidate] = {}
        self._lock = threading.Lock()
        # deploy.progress -> job.progress for the board's running deploy job.
        engine.bus.subscribe("deploy.progress", self._deploy_progress)

    def _deploy_progress(self, ev: Event) -> None:
        for job in self.jobs.running(ev.board_id):
            if job.kind in ("deploy", "restore"):
                job.progress = {"phase": ev.data.get("phase", ""),
                                "done": int(ev.data.get("bytes", 0) or 0),
                                "total": int(ev.data.get("total", 0) or 0)}
                self.engine.bus.publish(Event("job.progress", ev.board_id,
                                              {"job": job.id, **job.progress}))

    def remember(self, cands: list[Candidate]) -> None:
        with self._lock:
            for c in cands:
                self._known[c.board_id] = c

    def known(self) -> list[Candidate]:
        with self._lock:
            known = dict(self._known)
        for bid in self.engine.open_boards():
            if bid not in known:
                known[bid] = self.engine.session(bid).candidate
        return list(known.values())

    def lookup(self, board_id: str) -> Candidate | None:
        with self._lock:
            return self._known.get(board_id)

    def session(self, bid: str) -> Any:
        return self.engine.session(bid)


def create_app(engine: Any | None = None, *, token: str = "t14-token",
               serve_ui: bool = True) -> FastAPI:
    """The mock daemon as an ASGI app. ``engine`` defaults to a ``DemoEngine``."""
    if engine is None:
        from harness_manager.demo import DemoEngine

        engine = DemoEngine(speed=1.0)
    state = MockDaemonApp(engine, token)
    app = FastAPI(title="harness-manager-daemon (T14 mock)", version=VERSION, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.daemon = state

    class _Auth(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            path = request.url.path
            if path.startswith(API) and path != f"{API}/health":
                given = request.headers.get("authorization", "")
                if given != f"Bearer {state.token}":
                    return JSONResponse(error_json(auth_error()), status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
            return await call_next(request)

    app.add_middleware(_Auth)

    @app.exception_handler(HarnessError)
    async def _harness_error(request: Request, exc: HarnessError) -> JSONResponse:
        return _err(exc)

    @app.exception_handler(Exception)
    async def _other_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"ok": False, "error": {
            "code": 1, "name": "FAILED", "message": f"{type(exc).__name__}: {exc}",
            "hint": "this is a bug in the mock daemon"}}, status_code=500)

    eng = engine
    # The frozen week-plan additions (lanes L1, L2, L4), simulated: tests/fakes/l3_week_plan.py.
    sim = WeekPlanSim(state)
    app.state.sim = sim
    register_week_plan(app, state, sim, _ok, _accepted)
    # T10's XDC routes (docs/API.md "XDC export"): the real xdc service, the mock's boards.
    from .t10_mock_xdc import register as register_xdc
    register_xdc(app, state, _ok)
    # P1's front-panel routes (docs/API.md "Front panel"): a simulated panel per demo board.
    from .p1_mock_panel import PanelSim
    from .p1_mock_panel import register as register_panel
    app.state.panel = PanelSim(eng)
    register_panel(app, state, app.state.panel, _ok)
    # KIT-CORE's kit routes (docs/API.md "DUT build kits"): the real kit_api over the mock.
    from .kit_mock import register as register_kit
    register_kit(app, state, _accepted)
    # XVC-CORE's fabric-debug routes (docs/API.md "Fabric debug over XVC"), simulated.
    from .x3_mock_xvc import register as register_xvc
    app.state.xvc = register_xvc(app, state, sim, _ok, _accepted)
    # LINUX-CLAIM's SSH claim routes (docs/API.md "SSH claim of a Linux harness"), simulated.
    from .lc_mock_claim import register as register_claim
    app.state.claim = register_claim(app, state, _ok, _accepted)
    # BOARD-ID's identity routes (docs/API.md "Board identity"), simulated.
    from .idn_mock_identity import register as register_identity
    app.state.identity = register_identity(app, state, _ok, _accepted)
    # HARNESS-CAT's harness versions routes (docs/API.md "Harness versions"): the real
    # routes and catalogue over a simulated update service.
    from .hcat_mock_harness import register as register_harness
    app.state.harness = register_harness(app, state, sim, _accepted)
    # --- ui2 api-hub: GET /hubs/{name}/leases and GET /identity/clashes, simulated. Before the
    # settings routes: those register the real hubs_api, whose route this one shadows here.
    ui2_hub_register(app, state, sim, _ok)
    # --- end ui2 api-hub ---
    # SET-API's settings routes (docs/API.md "Settings"): the real routes over a real resolver
    # in a temporary directory of the mock's own (never the user's settings or keyring).
    from .settings_mock import register as register_settings
    app.state.settings = register_settings(app, state, _accepted)
    # --- sd-flash: the card-writer routes (docs/API.md "SD cards in this PC's card reader"):
    # the REAL routes over --demo's simulated readers, the setting from the settings above.
    from .cardwriter_mock import register as register_cardwriter
    register_cardwriter(app, state, _accepted, app.state.settings)
    # --- end sd-flash ---
    # LINUX-SLOTS' card routes (docs/API.md "User microSD and OS slots"), simulated.
    from .lxslots_mock_card import CardSim
    from .lxslots_mock_card import register as register_card
    app.state.card = CardSim()
    register_card(app, state, app.state.card, _ok)
    # --- ui2 api-build --- (G4 readings history, G6 slot rollback and card commit/clear)
    ui2_register(app, state, app.state.card)
    # --- end ui2 api-build ---
    # LM3's Live display routes (docs/API.md "Live display"): the real display_api over a
    # FakeLcdMirror per demo board (tests/fakes/lm3_mock_display.py).
    from .lm3_mock_display import register as register_display
    app.state.display = register_display(app, state, sim)
    # QUIET-POLL's viewer routes (docs/API.md "Background reads"): the demo's gate.
    from .qp_mock_quiet import register as register_quiet
    app.state.quiet = register_quiet(app, state, _ok)
    # FIX-PACK-2: the service's own tool variables (docs/API.md), scripted.
    from .fp2_mock_env import register as register_env
    register_env(app, _ok)
    # HIL-GUI's checks routes (docs/API.md "HIL checks"): the plans, no runs.
    from .hil_gui_mock import register as register_checks
    register_checks(app, state, _ok)
    # Lease requests, force release and leaving the queue (LR-A..C build the real ones).
    sim.requests = LeaseRequestSim(sim)
    register_lease_requests(app, state, sim.requests, _ok, _accepted)
    # P3: a tap on the panel's request banner tells the lease side (CCR PANEL-1), as presence
    # tells the daemon's lease service.
    app.state.panel.leases = sim.requests

    async def ws_deny(ws: WebSocket, exc: HarnessError) -> None:
        """harness-manager-daemon's refusal: an HTTP denial with the envelope, else close 4000 + code."""
        try:
            await ws.send_denial_response(JSONResponse(
                error_json(exc), status_code=401 if exc.code == ExitCode.REFUSED
                else HTTP_STATUS.get(exc.code, 500)))
        except RuntimeError:
            await ws.close(code=4000 + int(exc.code), reason=exc.message[:100])

    # -- system ---------------------------------------------------------------------------

    @app.get(f"{API}/health")
    def health() -> dict[str, Any]:
        import os

        # UPDATE-UI: once a simulated apply restarted onto another version, /health says so
        return _ok(version=sim.health_version or VERSION,
                   service="harness-manager-daemon (T14 mock)", pid=os.getpid())

    @app.get(f"{API}/packs")
    def packs() -> dict[str, Any]:
        packs = sorted(eng.packs().items())
        caps = {n: [{"name": c.name, "title": c.title, "needs_hint": c.needs_hint}
                    for c in p.capability_specs()] for n, p in packs}
        return _ok(packs={name: pack.title for name, pack in packs}, capabilities=caps)

    @app.post(f"{API}/probe")
    def probe(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:  # noqa: B008
        hints = ProbeHints(hosts=tuple(body.get("hosts") or ()),
                           serial_ports=tuple(body.get("serial_ports") or ()),
                           volumes=tuple(body.get("volumes") or ()),
                           scan_usb=bool(body.get("scan_usb", True)),
                           scan_network=bool(body.get("scan_network", True)),
                           timeout_s=float(body.get("timeout_s", 2.0)))
        for job in state.jobs.running():
            raise HeldError(f"{job.describe()} is running on {job.board_id}; a probe now could "
                            "take that board's control port", holder=f"harness-manager-daemon {job.describe()}")
        found = list(eng.probe(hints))
        for host in hints.hosts:
            # An explicit address the scan did not answer still becomes a candidate.
            if not any(host in lk.address for c in found for lk in c.links):
                with contextlib.suppress(HarnessError):
                    found.append(eng.candidate_for(host))
        state.remember(found)
        via = str(body.get("via") or "")
        if via.startswith("ssh:"):           # week plan (L1): reached through the hub's tunnel
            for c in found:
                sim.behind_hub(c.board_id, host=via[4:], lease="none")
        return _ok(candidates=found)

    @app.get(f"{API}/help/tabs")
    def help_tabs() -> dict[str, Any]:
        from harness_manager.cli.helptext import tabs

        return _ok(tabs=[{"name": n, "text": t} for n, t in tabs()])

    @app.post(f"{API}/daemon/shutdown")
    def shutdown(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:  # noqa: B008
        running = state.jobs.running()
        if running and not body.get("force"):
            raise HeldError(f"harness-manager-daemon is running {running[0].describe()}", holder="harness-manager-daemon",
                            hint="wait for it, or stop with --force")
        raise UnavailableError("daemon_shutdown", "the T14 mock is not started by `harness-manager daemon`")

    @app.get(f"{API}/jobs")
    def jobs() -> dict[str, Any]:
        return {"ok": True, "jobs": [j.as_json() for j in state.jobs.recent()]}

    @app.get(f"{API}/jobs/{{job_id}}")
    def job(job_id: str) -> dict[str, Any]:
        return {"ok": True, **state.jobs.get(job_id).as_json()}

    # -- boards ---------------------------------------------------------------------------

    def board_row(c: Candidate, source: str = "", conf: dict[str, Any] | None = None) -> dict[str, Any]:
        is_open = c.board_id in eng.open_boards()
        holder: LockOwner | None
        try:
            holder = eng.lock_owner(c.board_id)
        except HarnessError:
            holder = None
        row: dict[str, Any] = {"board_id": c.board_id, "open": is_open, "candidate": c,
                               "source": source or ("open" if is_open else "probe")}
        if conf is not None:
            row["configured"] = conf
        if holder is not None:
            row["holder"] = holder
        running = state.jobs.running(c.board_id)
        if running:
            row["job"] = running[0].id
        # FIX-PACK-4: the hub lease as the service last knew it (LeaseService.last_known):
        # the week-plan sim remembers each lease it served (l3_week_plan.lease_known).
        sim_ = getattr(app.state, "sim", None)
        known = sim_.lease_known(c.board_id) if sim_ is not None else None
        if known is not None:
            row["lease_known"] = known
        row.update(ui2_board_keys(eng, sim, c, is_open))          # ui2 api-hub (G2, G3)
        return row

    @app.get(f"{API}/boards")
    def boards() -> dict[str, Any]:
        # SIDEBAR-UX: the boards boards.toml configures too (the daemon's own listing,
        # daemon/configured.py), built from the file with no contact.
        from harness_manager.daemon.configured import board_rows

        known = {c.board_id: c for c in state.known()}
        return _ok(boards=[board_row(c, source, conf) for _bid, c, source, conf in
                           board_rows(eng, known, set(eng.open_boards()))])

    @app.post(f"{API}/boards")
    def open_board(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:  # noqa: B008
        target, cdata = body.get("target"), body.get("candidate")
        if bool(target) == bool(cdata):
            raise UsageError("give exactly one of target or candidate",
                             hint="target: an address; candidate: a /probe result")
        if cdata and body.get("via"):       # L1 as built: a probed candidate carries its route
            raise UsageError("via goes with target, not with a candidate",
                             hint="a candidate from POST /probe already carries its route")
        if target:
            cand = eng.candidate_for(str(target))
        else:
            cand = state.lookup(str(cdata.get("board_id", ""))) or _candidate_from_json(cdata)
            from harness_manager.daemon import configured as _conf  # SIDEBAR-UX, as the daemon

            if cand.evidence == _conf.EVIDENCE:
                cand = dataclasses.replace(cand, evidence=_conf.OPENED)
        eng.open(cand, note=str(body.get("note") or UI_NOTE))
        state.remember([cand])
        via = str(body.get("via") or "")
        if via.startswith("ssh:"):
            sim.behind_hub(cand.board_id, host=via[4:], lease="none")
        try:
            return _ok(board_id=cand.board_id, info=eng.info(cand.board_id))
        except HarnessError as exc:
            # The session is open (the lock is held); the board did not answer yet.
            return _ok(board_id=cand.board_id, info=None, info_error=error_json(exc)["error"])

    @app.delete(f"{API}/boards/{{bid}}")
    def close_board(bid: str, release: str | None = None) -> dict[str, Any]:
        # LEASE-UI: ``?release=true`` releases the lease THIS Harness Manager holds first
        # (harness-manager-daemon: ``Daemon.release_lease_here``); ``released`` is it, or null.
        want = str(release or "").strip().lower()
        if want not in ("", "0", "false", "no", "1", "true", "yes"):
            raise UsageError(f"release must be true or false, not {release!r}")
        state.jobs.gate(bid)
        extra = {"released": sim.release_here(bid)} if want in ("1", "true", "yes") else {}
        eng.close(bid)
        return _ok(**extra)

    @app.get(f"{API}/boards/{{bid}}/session")
    def session_view(bid: str) -> dict[str, Any]:
        s = state.session(bid)
        adapters = {a: getattr(s, a, None) is not None for a in (
            "deploy", "consoles", "debug", "resets", "clocks", "telemetry", "controller", "storage")}
        adapters["shell"] = getattr(s, "shell", None) is not None
        resets = getattr(s, "resets", None)
        running = state.jobs.running(bid)
        services = {n: getattr(getattr(eng, n, None), "reason", None)
                    for n in ("deploy", "consoles", "debug", "telemetry")}
        return _ok(board_id=bid, candidate=s.candidate, adapters=adapters,
                   reset_targets=list(resets.reset_targets()) if resets else [],
                   job=running[0].id if running else None,
                   job_kind=running[0].kind if running else None, services=services,
                   **ui2_route_keys(eng, bid))                    # ui2 api-hub (G2)

    @app.get(f"{API}/boards/{{bid}}")
    def info(bid: str) -> dict[str, Any]:
        state.jobs.gate(bid)
        t0 = time.monotonic()                                   # ui2 api-build (G4)
        i = eng.info(bid)
        extra = ui2_info_extra(app, state, bid, (time.monotonic() - t0) * 1000.0)  # G4
        claim = app.state.claim.get(bid) if hasattr(app.state, "claim") else None
        net = app.state.identity.get(bid) if hasattr(app.state, "identity") else None
        return _ok(candidate=i.candidate, identity=i.identity, health=i.health,
                   capabilities=i.capabilities, unavailable=i.unavailable,
                   **({"claim": claim} if claim is not None else {}),
                   **({"net_identity": net} if net is not None else {}), **extra)

    @app.get(f"{API}/boards/{{bid}}/lock")
    def lock(bid: str) -> dict[str, Any]:
        return _ok(holder=eng.lock_owner(bid))

    @app.get(f"{API}/boards/{{bid}}/telemetry")
    def telemetry(bid: str) -> dict[str, Any]:
        state.jobs.gate(bid)
        now = time.time()
        readings = list(eng.telemetry.readings(state.session(bid)))
        ui2_history(app, state).note_readings(bid, readings)   # ui2 api-build (G4)
        return _ok(readings=[reading_json(r, now) for r in readings])

    # -- deploy ---------------------------------------------------------------------------

    def find_overlay(session: Any, spec: Any) -> Any:
        """By name, by rm_id, or by the OverlayRef object the API returned (as harness-manager-daemon)."""
        every = list(eng.deploy.overlays(session))
        if isinstance(spec, dict):
            keys = [k for k in ("name", "rm_id", "static_id", "source") if spec.get(k)]
            if not keys:
                raise UsageError("the overlay object needs at least a name")
            matches = [o for o in every if all(str(getattr(o, k)).lower() == str(spec[k]).lower()
                                               for k in keys)]
            label = spec.get("name") or spec.get("rm_id")
        elif isinstance(spec, str) and spec:
            label = spec
            matches = [o for o in every if o.name == spec] or [
                o for o in every if spec.lower().startswith("0x") and o.rm_id.lower() == spec.lower()]
        else:
            raise UsageError("overlay must be a name, an rm_id or an overlay object")
        if not matches:
            raise AbsentError(f"no overlay named {label!r} for this board",
                              hint="GET overlays lists them")
        return matches[0]

    @app.get(f"{API}/boards/{{bid}}/overlays")
    def overlays(bid: str) -> dict[str, Any]:
        state.jobs.gate(bid)
        session = state.session(bid)
        loadable, blocked = eng.deploy.compatible(session)
        return _ok(loadable=list(loadable), blocked=dict(blocked),
                   overlays=list(eng.deploy.overlays(session)))

    @app.get(f"{API}/boards/{{bid}}/card")
    def card(bid: str) -> dict[str, Any]:
        from harness_manager.core.pack import card_status_of
        from harness_manager.services.slots import card_line

        state.jobs.gate(bid)
        status = card_status_of(eng.deploy, state.session(bid))
        return _ok(card=status, line=card_line(status))    # line: LINUX-SLOTS, additive

    @app.post(f"{API}/boards/{{bid}}/preflight")
    def preflight(bid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        from harness_manager.core.pack import preflight_refusal

        state.jobs.gate(bid)
        session = state.session(bid)
        ov = find_overlay(session, body.get("overlay"))
        items = list(eng.deploy.preflight(session, ov))
        out: dict[str, Any] = {"overlay": ov, "items": items}
        refusal = preflight_refusal(items, ov.name)
        if refusal is not None:
            out["refusal"] = error_json(refusal)["error"]
        return _ok(**out)

    @app.post(f"{API}/boards/{{bid}}/deploy", status_code=202)
    def deploy(bid: str, body: dict[str, Any] = Body(...)) -> JSONResponse:  # noqa: B008
        from harness_manager.core.pack import card_status_of, keep_refusal, preflight_refusal

        state.jobs.gate(bid)
        session = state.session(bid)
        ui2_require_holder(sim, bid, "deploy", _lease_body(body))  # ui2 api-hub (G7)
        keep = body.get("keep_on_card", False)
        if not isinstance(keep, bool):
            raise UsageError(f"keep_on_card must be true or false, not {keep!r}")
        force = _force(body)                                       # FIX-PACK-7
        ov = find_overlay(session, body.get("overlay"))
        items = list(eng.deploy.preflight(session, ov))
        refusal = preflight_refusal(items, ov.name)
        if refusal is not None:            # refused BEFORE any job: nothing is pushed
            refusal.data = {"overlay": ov, "preflight": items}  # type: ignore[attr-defined]
            raise refusal
        if keep:                           # Keep on the card: the card must take it, first
            card = card_status_of(eng.deploy, session)
            refused = keep_refusal(card)
            if refused is not None:
                refused.data = {"overlay": ov, "card": card}  # type: ignore[attr-defined]
                raise refused
            return _accepted(state.jobs.start(
                bid, "deploy", lambda progress: eng.deploy.deploy(session, ov, keep_on_card=True,
                                                                  **force)))
        return _accepted(state.jobs.start(bid, "deploy",
                                          lambda progress: eng.deploy.deploy(session, ov, **force)))

    @app.post(f"{API}/boards/{{bid}}/restore", status_code=202)
    def restore(bid: str,
                body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        session = state.session(bid)
        force = _force(body)                                       # FIX-PACK-7
        ui2_require_holder(sim, bid, "restore", _lease_body(body))  # ui2 api-hub (G7)
        return _accepted(state.jobs.start(
            bid, "restore", lambda progress: eng.deploy.restore_baseline(session, **force)))

    # -- reset, clocks --------------------------------------------------------------------

    @app.post(f"{API}/boards/{{bid}}/reset")
    def reset(bid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        state.jobs.gate(bid)
        ui2_require_holder(sim, bid, "reset", body)                # ui2 api-hub (G7)
        target = str(body.get("target", ""))
        resets = state.session(bid).resets
        if resets is None:
            raise UnavailableError(C.RESET_DUT, "this board has no reset adapter")
        resets.reset(target)
        return _ok(target=target)

    def clock_adapter(bid: str) -> Any:
        adapter = state.session(bid).clocks
        if adapter is not None:
            return adapter
        # DemoEngine sessions have none: the mock plays the MPS3 `clock` verb (presets).
        if C.CLOCK_DUT in eng.info(bid).capabilities:
            return SimClocks(sim, bid)
        raise UnavailableError(C.CLOCK_DUT, eng.info(bid).unavailable.get(
            C.CLOCK_DUT, "this board has no clock adapter in this build"))

    @app.get(f"{API}/boards/{{bid}}/clocks")
    def clocks(bid: str) -> dict[str, Any]:
        state.jobs.gate(bid)
        return _ok(readings=[reading_json(r) for r in clock_adapter(bid).clocks()])

    @app.post(f"{API}/boards/{{bid}}/clocks")
    def set_clock(bid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        state.jobs.gate(bid)
        ui2_require_holder(sim, bid, "clocks", body)               # ui2 api-hub (G7)
        return _ok(reading=reading_json(clock_adapter(bid).set_clock(
            str(body.get("name") or "dut"), float(body["mhz"]))))

    # -- consoles -------------------------------------------------------------------------

    @app.get(f"{API}/boards/{{bid}}/consoles")
    def consoles(bid: str) -> dict[str, Any]:
        return _ok(names=list(eng.consoles.names(state.session(bid))),
                   consoles=ui2_console_rows(eng, sim, bid,                # ui2 api-hub (G1b)
                                             sim.consoles_view(bid)))

    @app.post(f"{API}/boards/{{bid}}/consoles/{{name}}/export")
    def export(bid: str, name: str,
               body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:  # noqa: B008
        port = eng.consoles.export_tcp(state.session(bid), name, int(body.get("port") or 0))
        return _ok(port=port)

    @app.websocket(f"{API}/boards/{{bid}}/consoles/{{name}}")
    async def console_ws(ws: WebSocket, bid: str, name: str) -> None:
        if ws.query_params.get("token") != state.token:
            await ws_deny(ws, auth_error())
            return
        loop = asyncio.get_running_loop()
        try:
            stream = await loop.run_in_executor(
                None, lambda: eng.consoles.subscribe(state.session(bid), name))
        except HarnessError as exc:
            await ws_deny(ws, exc)
            return
        await ws.accept()
        await ws.send_text(json.dumps({"state": "up", "name": name, "detail": ""}))
        stop = threading.Event()
        out: asyncio.Queue[bytes | str | None] = asyncio.Queue()

        def post(item: bytes | str | None) -> None:
            # From engine threads; the socket (and its loop) may already be gone.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(out.put_nowait, item)

        def reader() -> None:
            try:
                while not stop.is_set():
                    chunk = stream.read(0.2)
                    if chunk:
                        post(bytes(chunk))
            except HarnessError as exc:
                post(json.dumps({"state": "closed", "name": name, "detail": str(exc)}))
            finally:
                post(None)

        def on_state(ev: Event) -> None:
            if ev.board_id == bid and ev.data.get("name") == name:
                post(json.dumps({k: ev.data.get(k) for k in ("state", "detail", "endpoint")
                                 if ev.data.get(k) is not None}))

        unsub = eng.bus.subscribe("console.state", on_state)
        threading.Thread(target=reader, daemon=True, name=f"ws-console-{name}").start()

        async def pump_out() -> None:
            while True:
                item = await out.get()
                if item is None:
                    return
                if isinstance(item, bytes):
                    await ws.send_bytes(item)
                else:
                    await ws.send_text(item)

        warned = [""]                            # ui2 api-hub (G1b): once per reason

        async def pump_in() -> None:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                data = msg.get("bytes")          # text frames from a client are ignored
                if data:
                    ok_, why = ui2_console_writable(eng, sim, bid, name)   # ui2 api-hub (G1b)
                    if not ok_:
                        if warned[0] != why:
                            warned[0] = why
                            from harness_manager.daemon.console_access import held_error
                            post(json.dumps({"error": error_json(
                                held_error(bid, name, why))["error"]}))
                        continue
                    try:
                        await loop.run_in_executor(None, stream.write, data)
                    except HarnessError as exc:
                        post(json.dumps({"error": error_json(exc)["error"]}))

        tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()
            unsub()
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                stream.close()
            with contextlib.suppress(Exception):
                await ws.close()

    # -- debug ----------------------------------------------------------------------------

    @app.get(f"{API}/boards/{{bid}}/debug")
    def debug_status(bid: str) -> dict[str, Any]:
        return {"ok": True, **jsonable(eng.debug.status(state.session(bid)))}

    @app.post(f"{API}/boards/{{bid}}/debug/detect")
    def debug_detect(bid: str) -> dict[str, Any]:
        state.jobs.gate(bid)
        return _ok(idcode=eng.debug.detect(state.session(bid)))

    @app.post(f"{API}/boards/{{bid}}/debug/up", status_code=202)
    def debug_up(bid: str,
                 body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        state.jobs.gate(bid)
        session = state.session(bid)
        ui2_require_holder(sim, bid, "debug_up", body)             # ui2 api-hub (G7)
        return _accepted(state.jobs.start(bid, "debug_up",
                                          lambda progress: eng.debug.up(session)))

    @app.post(f"{API}/boards/{{bid}}/debug/down")
    def debug_down(bid: str) -> dict[str, Any]:
        state.jobs.gate(bid)
        return {"ok": True, **jsonable(eng.debug.down(state.session(bid)))}

    # -- controller and storage -----------------------------------------------------------

    def existing_backup(raw: Any) -> Path:
        # harness-manager-daemon (cli.cmd_board.backup_record) refuses an archive that is not there.
        path = Path(str(raw or ""))
        if not raw or not path.is_file():
            raise AbsentError(f"no backup archive at {path}",
                              hint="make one with `harness-manager sd TARGET backup DIR`")
        return path

    def controller(bid: str) -> Any:
        adapter = state.session(bid).controller
        if adapter is None:
            raise UnavailableError(C.CONSOLE_CONTROLLER, "needs the Debug USB cable")
        return adapter

    def storage(bid: str, capability: str = C.STORAGE_BACKUP) -> Any:
        adapter = state.session(bid).storage
        if adapter is None:
            raise UnavailableError(capability, "needs the Debug USB cable (or a card reader)")
        return adapter

    @app.get(f"{API}/boards/{{bid}}/controller/temps")
    def temps(bid: str) -> dict[str, Any]:
        return _ok(readings=[reading_json(r) for r in controller(bid).temperatures()])

    @app.get(f"{API}/boards/{{bid}}/controller/osc")
    def osc(bid: str) -> dict[str, Any]:
        return _ok(readings=[reading_json(r) for r in controller(bid).oscillators()])

    @app.post(f"{API}/boards/{{bid}}/controller/reboot", status_code=202)
    def reboot(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        state.jobs.gate(bid)
        ui2_require_holder(sim, bid, "reboot", body)               # ui2 api-hub (G7)
        adapter = state.session(bid).controller
        if adapter is None:
            raise UnavailableError(C.REBOOT_BOARD, "needs the Debug USB cable, a networked "
                                                   "power plug, or harness firmware with "
                                                   "'mccif' or 'mcc_local' (net-protocol v0.18)")
        # No wait_s: the adapter picks it by harness implementation (T12: Linux waits longer).
        wait_s = float(body["wait_s"]) if body.get("wait_s") is not None else None
        return _accepted(state.jobs.start(
            bid, "reboot", lambda progress: adapter.reboot(progress, wait_s=wait_s)))

    @app.post(f"{API}/boards/{{bid}}/controller/command")
    def command(bid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        reply = controller(bid).command(str(body["line"]), arm=bool(body.get("arm")))
        return _ok(reply=reply)

    @app.get(f"{API}/boards/{{bid}}/storage/pending")
    def pending(bid: str) -> dict[str, Any]:
        adapter = state.session(bid).storage
        return _ok(pending=adapter.pending() if adapter is not None else None)

    @app.post(f"{API}/boards/{{bid}}/storage/backup", status_code=202)
    def backup(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        state.jobs.gate(bid)
        adapter = storage(bid, C.STORAGE_BACKUP)
        dest = Path(body.get("dest_dir") or ".")
        return _accepted(state.jobs.start(bid, "sd_backup",
                                          lambda progress: adapter.backup(dest, progress)))

    @app.post(f"{API}/boards/{{bid}}/storage/install", status_code=202)
    def install(bid: str, body: dict[str, Any] = Body(...)) -> JSONResponse:  # noqa: B008
        adapter = storage(bid, C.STORAGE_INSTALL)
        files = {str(k): Path(v) for k, v in dict(body.get("files") or {}).items()}
        record = adapter.load_backup(existing_backup(body.get("backup_path")))
        return _accepted(state.jobs.start(
            bid, "sd_install",
            lambda progress: adapter.install(files, backup=record, progress=progress)))

    @app.post(f"{API}/boards/{{bid}}/storage/restore", status_code=202)
    def sd_restore(bid: str, body: dict[str, Any] = Body(...)) -> JSONResponse:  # noqa: B008
        state.jobs.gate(bid)
        adapter = storage(bid, C.STORAGE_INSTALL)
        record = adapter.load_backup(existing_backup(body.get("backup_path")))
        return _accepted(state.jobs.start(bid, "sd_restore",
                                          lambda progress: adapter.restore(record, progress)))

    @app.post(f"{API}/boards/{{bid}}/lab/{{verb}}")
    def lab(bid: str, verb: str,
            body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:  # noqa: B008
        session = state.session(bid)
        if verb not in ("link", "display", "macgen", "dutrx"):
            raise UsageError(f"unknown lab verb {verb!r}", hint="link, display, macgen, dutrx")
        if getattr(session, "shell", None) is None:
            raise UnavailableError(f"{session.candidate.pack}.shell",
                                   "this engine's session has no shell control channel")
        raise UnavailableError(f"{session.candidate.pack}.{verb}",
                               "the T14 mock does not run lab verbs")

    # -- events ---------------------------------------------------------------------------

    @app.websocket(f"{API}/events")
    async def events(ws: WebSocket) -> None:
        if ws.query_params.get("token") != state.token:
            await ws_deny(ws, auth_error())
            return
        patterns = [p.strip() for p in (ws.query_params.get("topics") or "*").split(",")
                    if p.strip()]
        await ws.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()

        def forward(ev: Event) -> None:
            if any(_matches(p, ev.topic) for p in patterns):
                frame = json.dumps({"topic": ev.topic, "board_id": ev.board_id,
                                    "data": jsonable(ev.data), "at": ev.at})
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(queue.put_nowait, frame)

        unsub = eng.bus.subscribe("*", forward)

        async def pump_out() -> None:
            while True:
                await ws.send_text(await queue.get())

        async def pump_in() -> None:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            unsub()
            for t in tasks:
                t.cancel()

    if serve_ui:
        from harness_manager.web import mount_static

        mount_static(app, "/")
    return app


# --- ui2 api-build ---
# UI2-API-BUILD (docs/planning/UI_V2_PLAN.md §2 G4, G6; docs/API.md "Readings kept by this
# service" and "OS slots and the card: roll back, commit, clear"): the readings history (the
# real ``services.history`` ring, fed by the mock's telemetry route and seeded by the engine
# as the daemon's is), the additive keys of GET /boards/{bid} (``info_fields`` over the same
# ``facts_of`` seam), and the slot and card changes over ``CardSim``: 400 without confirm, 422
# with the reason when the board has no OS slots or card, 409 HELD while a job runs, then a
# 202 job whose result is the daemon's shape. The kit routes (G5, G8) are the real kit_api's
# (tests/fakes/kit_mock.py).


def ui2_history(app: FastAPI, state: Any) -> Any:
    from harness_manager.services.history import ReadingsHistory

    hist = getattr(app.state, "readings", None)
    if hist is None:
        seed = getattr(state.engine, "readings_seed", None)
        hist = app.state.readings = ReadingsHistory(seed=seed if callable(seed) else None)
    return hist


def ui2_info_extra(app: FastAPI, state: Any, bid: str, answer_ms: float) -> dict[str, Any]:
    from harness_manager.services.history import facts_of, info_fields

    ui2_history(app, state).note_answer(bid, answer_ms)
    try:
        session = state.session(bid)
    except HarnessError:
        session = None
    return info_fields(answer_ms, facts_of(session))


def _ui2_confirmed(body: dict[str, Any], what: str) -> None:
    if body.get("confirm") is not True:
        raise UsageError(f"to {what}, send confirm: true",
                         hint="the page asks first; the CLI asks, or takes --yes")


def ui2_register(app: FastAPI, state: Any, sim: Any) -> None:
    """The ui2 api-build routes the mock serves itself (the kit routes are the real ones)."""
    from harness_manager.core.pack import SlotStatus
    from harness_manager.services.history import query_args
    from harness_manager.services.slot_health import extend_json
    from harness_manager.services.slots import card_line, card_status_json, slot_status_json

    from .lxslots_mock_card import NO_SLOTS

    @app.get(f"{API}/boards/{{bid}}/readings/history")
    def readings_history(bid: str, name: str | None = None, since: str | None = None,
                         limit: str | None = None) -> dict[str, Any]:
        hist = ui2_history(app, state)
        names, when, count = query_args(name, since, limit, hist.capacity)
        return _ok(**hist.history(bid, names=names, since=when, limit=count))

    def slots_json(st: SlotStatus) -> dict[str, Any]:
        return extend_json(slot_status_json(st), st)

    def card_json(bid: str) -> dict[str, Any]:
        doc = card_status_json(sim.card(bid))
        return doc

    @app.post(f"{API}/boards/{{bid}}/slots/rollback", status_code=202)
    def slots_rollback(bid: str, body: dict[str, Any] = Body(default_factory=dict)  # noqa: B008
                       ) -> JSONResponse:
        state.session(bid)
        _ui2_confirmed(body, "roll the OS slot back")
        reboot = body.get("reboot", True)
        if not isinstance(reboot, bool):
            raise UsageError(f"reboot must be true or false, not {reboot!r}")
        state.jobs.gate(bid)
        st = sim.card(bid).os_slots
        if bid in sim.no_store or st is None:
            raise UnavailableError("OS slot update", NO_SLOTS)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            import dataclasses as dc

            now = sim.card(bid).os_slots
            progress("rollback", 0, 0)
            other = "B" if now.running == "A" else "A"
            if now.pending_commit:
                after = dc.replace(now, default=now.running)
                note, rebooted = f"the commit of slot {now.pending_commit} is undone", False
            else:
                info = now.slots.get(other)
                if info is None or not info.valid:
                    raise RefusedError(f"slot {other} holds no valid image to roll back to "
                                       f"({info.state if info else 'absent'})",
                                       hint="push a known-good image instead")
                after = dc.replace(now, default=other, running=other if reboot else now.running,
                                   target="" if not reboot else now.running)
                rebooted = reboot
                note = (f"slot {other} runs again (rebooted)" if reboot
                        else f"slot {other} boots at the next reboot")
            sim.cards[bid] = dc.replace(sim.card(bid), os_slots=after)
            return {"board_id": bid, "act": "rollback", "slot": after.default,
                    "rebooted": rebooted, "fell_back": None, "note": note,
                    "slots": slots_json(after)}

        return _accepted(state.jobs.start(bid, "slot_rollback", run))

    # FIX-PACK-6: slot push/commit/verify (the CLI's, through the service). The sim moves the
    # slot pointers only; nothing is read from the image paths.
    def slot_change(bid: str, body: dict[str, Any], kind: str, what: str | None,
                    change: Callable[[Any], tuple[Any, dict[str, Any]]]) -> JSONResponse:
        import dataclasses as dc

        state.session(bid)
        if what is not None:
            _ui2_confirmed(body, what)
        slot = body.get("slot")
        if slot not in (None, "A", "B"):
            raise UsageError(f"slot must be A or B, not {slot!r}")
        state.jobs.gate(bid)
        if bid in sim.no_store or sim.card(bid).os_slots is None:
            raise UnavailableError("OS slot update", NO_SLOTS)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress(kind.split("_", 1)[1], 0, 0)
            after, extra = change(sim.card(bid).os_slots)
            sim.cards[bid] = dc.replace(sim.card(bid), os_slots=after)
            return {"board_id": bid, "act": kind.split("_", 1)[1], **extra,
                    "slots": slots_json(after)}

        return _accepted(state.jobs.start(bid, kind, run))

    @app.post(f"{API}/boards/{{bid}}/slots/push", status_code=202)
    def slots_push(bid: str, body: dict[str, Any] = Body(default_factory=dict)  # noqa: B008
                   ) -> JSONResponse:
        def change(now: Any) -> tuple[Any, dict[str, Any]]:
            import dataclasses as dc

            if not now.target:
                raise RefusedError("no free slot: roll back the pending commit first (rule 1)")
            return dc.replace(now, staged=now.target), {
                "slot": now.target, "rolled_back_first": "",
                "image": str(body.get("image") or "linux_slot.img").rsplit("/", 1)[-1],
                "static_id": str(body.get("static_id") or "")}

        return slot_change(bid, body, "slot_push", "push an OS image to the board's card",
                           change)

    @app.post(f"{API}/boards/{{bid}}/slots/commit", status_code=202)
    def slots_commit(bid: str, body: dict[str, Any] = Body(default_factory=dict)  # noqa: B008
                     ) -> JSONResponse:
        def change(now: Any) -> tuple[Any, dict[str, Any]]:
            import dataclasses as dc

            pick = now.staged or now.target
            if not pick or (body.get("slot") and body["slot"] != pick):
                raise RefusedError("nothing pushed to commit")
            after = dc.replace(now, default=pick, target="", staged="")
            return after, {"slot": pick, "note": f"slot {pick} boots at the next reboot"}

        return slot_change(bid, body, "slot_commit", "commit an OS slot", change)

    @app.post(f"{API}/boards/{{bid}}/slots/verify", status_code=202)
    def slots_verify(bid: str, body: dict[str, Any] = Body(default_factory=dict)  # noqa: B008
                     ) -> JSONResponse:
        def change(now: Any) -> tuple[Any, dict[str, Any]]:
            other = "B" if now.default == "A" else "A"
            return now, {"slot": body.get("slot") or other}

        return slot_change(bid, body, "slot_verify", None, change)

    def card_change(bid: str, body: dict[str, Any], kind: str, what: str,
                    change: Callable[[], dict[str, Any]]) -> JSONResponse:
        state.session(bid)
        _ui2_confirmed(body, what)
        state.jobs.gate(bid)
        card = sim.card(bid)
        if bid in sim.no_store or not card.store:
            raise UnavailableError("user microSD", "this harness has no microSD store")
        if not card.present:
            raise RefusedError(f"no card in the USER microSD slot: nothing to {what}",
                               hint="the board boots exactly as it always has without one; "
                                    "insert a card first")
        return _accepted(state.jobs.start(bid, kind, lambda progress: change()))

    @app.post(f"{API}/boards/{{bid}}/card/commit", status_code=202)
    def card_commit(bid: str, body: dict[str, Any] = Body(default_factory=dict)  # noqa: B008
                    ) -> JSONResponse:
        def change() -> dict[str, Any]:
            import dataclasses as dc

            ident = state.engine.info(bid).identity
            old = sim.card(bid)
            slot = "A" if (old.default or {}).get("slot") == "B" else "B"
            committed = {"rm_id": ident.rm_id, "rm_name": ident.rm_name,
                         "static_id": ident.shell_id, "slot": slot}
            sim.cards[bid] = dc.replace(old, default=dict(committed), state="valid",
                                        boot="loaded")
            return {"board_id": bid, "committed": committed, "card": card_json(bid),
                    "line": card_line(sim.card(bid)),
                    "note": f"{ident.rm_name or ident.rm_id} is the power-on default "
                            f"(store slot {slot})"}

        return card_change(bid, body, "card_commit",
                           "write the running overlay to the card as its power-on default",
                           change)

    @app.post(f"{API}/boards/{{bid}}/card/clear", status_code=202)
    def card_clear(bid: str, body: dict[str, Any] = Body(default_factory=dict)  # noqa: B008
                   ) -> JSONResponse:
        def change() -> dict[str, Any]:
            import dataclasses as dc

            sim.cards[bid] = dc.replace(sim.card(bid), default=None, boot="greybox")
            return {"board_id": bid, "card": card_json(bid), "line": card_line(sim.card(bid)),
                    "note": "no power-on default: the greybox loads at the next power-on"}

        return card_change(bid, body, "card_clear", "clear the card's power-on default", change)
# --- end ui2 api-build ---
# --- ui2 api-hub -------------------------------------------------------------------------------
# UI v2 (lane UI2-API-HUB, docs/API.md "UI v2: hub leases, the Debug USB route, consoles and
# clashes"): the mock's answers, from the week-plan sim's hubs (``sim.hubs``, ``behind_hub``)
# and the identity sim, through the daemon's own pure rules (console_access, mcc_route,
# identity_api.clash_groups), so the page codes against the same shapes.


def ui2_board_keys(eng: Any, sim: Any, cand: Candidate, is_open: bool) -> dict[str, Any]:
    """``GET /boards`` rows: ``hub`` (G3) and ``mcc_route``/``mcc_route_reason`` (G2)."""
    from harness_manager.daemon import mcc_route

    hub = sim.hubs.get(cand.board_id)
    out: dict[str, Any] = {"hub": {"name": hub["host"], "host": hub["host"],
                                   "target": hub["target"], "transport": "ssh"}
                           if hub else None}
    if is_open:
        out.update(ui2_route_keys(eng, cand.board_id))
    else:
        ident = getattr(cand, "identity", None)
        out["mcc_route"], out["mcc_route_reason"] = mcc_route.from_links(
            cand.links, getattr(ident, "features", ()) if ident else ())
    return out


def ui2_route_keys(eng: Any, bid: str) -> dict[str, Any]:
    """``GET /session`` and ``GET /boards/{bid}``: ``mcc_route``, ``mcc_route_reason`` (G2)."""
    from harness_manager.daemon import mcc_route

    session = eng.session(bid)
    route, why = mcc_route.of_session(session, session.identity())
    return {"mcc_route": route, "mcc_route_reason": why}


def ui2_console_rows(eng: Any, sim: Any, bid: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Console rows plus ``role``, ``writable``, ``read_only_reason`` (G1b), by the daemon's
    rule over the sim's lease (``behind_hub``: mine | elsewhere | other | none)."""
    from harness_manager.daemon import console_access as CA

    impl = str(getattr(eng.session(bid).identity(), "harness_impl", "") or "")
    hub = sim.hubs.get(bid)
    lease = dict(hub["lease"]) if hub and hub.get("lease") else None
    out = []
    for row in rows:
        name = str(row["name"])
        role = CA.role_of(str(row.get("alias_of") or name), name, impl)
        writable, why = CA.rule(role, hub is not None, lease, "")
        out.append({**row, "role": role, "writable": writable, "read_only_reason": why})
    return out


def _force(body: dict[str, Any] | None) -> dict[str, bool]:
    """FIX-PACK-7: ``force: true`` in a deploy or restore body (the daemon's rule): swap even
    when the board's OpenOCD cannot be stopped first. The keyword only when asked."""
    force = (body or {}).get("force", False)
    if not isinstance(force, bool):
        raise UsageError(f"force must be true or false, not {force!r}")
    return {"force": True} if force else {}


def _lease_body(body: dict[str, Any] | None) -> dict[str, Any]:
    """FIX-PACK-7 (the daemon's rule): ``force`` alone is the swap's, not the lease escape; the
    lease gate sees it only with ``consent``."""
    b = body or {}
    return b if "consent" in b else {k: v for k, v in b.items() if k != "force"}


def ui2_require_holder(sim: Any, bid: str, kind: str, body: dict[str, Any] | None) -> None:
    """G7: the daemon's drive gate (``daemon/drive_gate.py``) over the sim's lease: a board
    behind a hub drives for the lease holder here only (409 HELD, ``error.data.reason: LEASE``),
    or with ``force`` and ``consent: "RESET <bid>"``."""
    from harness_manager.daemon.drive_gate import REASON, WHAT, escape

    hub = sim.hubs.get(bid)
    if hub is None:
        return
    lease = hub.get("lease")
    here = bool(lease and lease.get("here", lease.get("mine")))
    if here:
        return
    force, consent = escape(body)
    what = WHAT.get(kind, kind)
    holder = (lease or {}).get("holder") or "nobody"
    reason = (f"{holder} holds the lease on {hub['target']}" if lease
              else f"nobody holds the lease on {hub['target']}")
    if force:
        if consent.strip() != f"RESET {bid}":
            raise RefusedError(f"cannot {what} without the hub lease: force needs the typed "
                               f"phrase ({reason})", hint=f"type exactly: RESET {bid}")
        return
    err = HeldError(f"cannot {what}: on a board behind a hub it is for the lease holder only, "
                    f"and {reason}", holder=holder,
                    hint=f"take or request the lease; or force it: force true with consent "
                         f"\"RESET {bid}\"")
    err.data = {"reason": REASON, "lease": {  # type: ignore[attr-defined]
        "required": True, "mine": bool((lease or {}).get("mine")), "here": False,
        "holder": holder, "target": hub["target"]}}
    raise err


def ui2_console_writable(eng: Any, sim: Any, bid: str, name: str) -> tuple[bool, str]:
    """G1b: may this client's keystrokes reach console ``name`` (the daemon's rule)."""
    row = ui2_console_rows(eng, sim, bid, [{"name": name}])[0]
    return bool(row["writable"]), str(row["read_only_reason"])


def ui2_hub_register(app: FastAPI, state: Any, sim: Any, ok: Any) -> None:
    from harness_manager.daemon.identity_api import clash_groups
    from harness_manager.services import board_identity as BI

    @app.get(f"{API}/hubs/{{name}}/leases")
    def hub_leases(name: str, refresh: str | None = None) -> dict[str, Any]:
        """G3: every target on hub ``name`` (a host, as ``behind_hub`` sets it), one read."""
        now = time.time()
        with sim._lock:
            boards = {bid: dict(h) for bid, h in sim.hubs.items() if h["host"] == name}
        if not boards:
            raise AbsentError(f"no board this service lists is behind a hub named {name!r}",
                              hint="GET /boards: each row's hub.name")
        at = datetime_iso(now)
        targets: dict[str, dict[str, Any]] = {}
        for bid, h in boards.items():
            row = targets.setdefault(h["target"], {
                "target": h["target"], "board": h.get("board") or h["target"].rsplit("_", 1)[0],
                "boards": [], "state": "free", "holder": "", "user": "", "expires_at": "",
                "queue_length": 0, "waiting": False, "next": "", "in_use": False,
                "mine": False, "here": False, "confirmed_at": at, "source": "overview"})
            row["boards"].append(bid)
            lease = h.get("lease")
            if lease:
                row.update(state="held", holder=lease["holder"], user=lease.get("user", ""),
                           expires_at=lease.get("expires_at", ""), in_use=True,
                           mine=bool(lease.get("mine")), here=bool(lease.get("here")))
            req = sim.requests.view(bid) if sim.requests is not None else {}
            row["queue_length"] = len(req.get("queue") or [])
        return ok(hub=name, host=name, transport="ssh", read_at=at, age_s=0.0, cached=False,
                  targets=sorted(targets.values(), key=lambda r: r["target"]))

    @app.get(f"{API}/identity/clashes")
    def identity_clashes() -> dict[str, Any]:
        """G10: clashes across the identity sim's boards and the boards each has seen."""
        ident = getattr(app.state, "identity", None)
        records: dict[str, dict[str, Any]] = {}
        now = time.time()
        for bid, b in (getattr(ident, "boards", None) or {}).items():
            rep = b["reported"]
            src = rep.get("source") if isinstance(rep.get("source"), dict) else {}
            records[bid] = {**{f: rep.get(f, "") for f in BI.FIELDS},
                            "label_source": src.get("label", ""),
                            "target": (b.get("hub") or {}).get("target", ""),
                            "address": bid.split("@", 1)[-1], "at": now, "name": ""}
            for o in b.get("others") or []:
                who = str(o.get("who") or "")
                if o.get("kind") == "board" and who and who not in records:
                    records[who] = {**{f: o.get(f, "") for f in BI.FIELDS},
                                    "label_source": o.get("label_source", ""), "target": "",
                                    "address": "", "at": now, "name": who}
        return ok(clashes=clash_groups(records, now=now), boards_seen=len(records),
                  checked_at=BI._iso(now))


def datetime_iso(t: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
# --- end ui2 api-hub ---------------------------------------------------------------------------


# --- running it ---------------------------------------------------------------------------


class ServedApp:
    """Any ASGI app on 127.0.0.1 in a background uvicorn thread (tests and demos).

    ``ServedApp(app, token)`` serves the given app: the mock below, or the real
    ``harness_manager.daemon.app.create_app(engine, token=...)``. ``app.state.daemon``
    must have ``engine`` and ``remember(candidates)`` (both servers do)::

        with ServedApp(app, token) as d:
            d.url        # http://127.0.0.1:PORT
            d.ui_url     # http://127.0.0.1:PORT/#token=TOKEN
    """

    def __init__(self, app: Any, token: str, *, port: int = 0, lifespan: str = "off") -> None:
        import uvicorn

        self.app = app
        self.engine = app.state.daemon.engine
        self.token = token
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self.port = self._sock.getsockname()[1]
        config = uvicorn.Config(self.app, log_level="warning", lifespan=lifespan,
                                ws_ping_interval=None)
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    def seed(self, *candidates: Candidate) -> None:
        """Make boards known without a probe (as if a probe had found them)."""
        self.app.state.daemon.remember(list(candidates))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ui_url(self) -> str:
        return f"{self.url}/#token={self.token}"

    def start(self) -> ServedApp:
        self._thread = threading.Thread(target=self._server.run,
                                        kwargs={"sockets": [self._sock]}, daemon=True,
                                        name="t14-served-app")
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("the server did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        with contextlib.suppress(Exception):
            self.engine.close_all()
        self._sock.close()

    def __enter__(self) -> ServedApp:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


class MockDaemon(ServedApp):
    """The T14 mock, served: ``with MockDaemon(engine) as d: d.ui_url``."""

    def __init__(self, engine: Any | None = None, *, token: str = "t14-token",
                 port: int = 0) -> None:
        super().__init__(create_app(engine, token=token), token, port=port)


def real_daemon(engine: Any, *, token: str, state_dir: Path | None = None) -> ServedApp:
    """Team T13's harness-manager-daemon app over ``engine``, served the same way (lifespan on, so it
    closes its boards and jobs when it stops)."""
    from harness_manager.daemon.app import create_app as daemon_app

    return ServedApp(daemon_app(engine, token=token, state_dir=state_dir), token,
                     lifespan="on")


@contextlib.contextmanager
def running(engine: Any | None = None, **kw: Any) -> Iterator[MockDaemon]:
    with MockDaemon(engine, **kw) as d:
        yield d


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the T14 mock harness-manager-daemon over DemoEngine")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default="t14-demo")
    parser.add_argument("--speed", type=float, default=1.0, help="DemoEngine pacing factor")
    parser.add_argument("--sd-journal", action="store_true",
                        help="leave an interrupted SD install on the USB board")
    parser.add_argument("--mock", action="store_true",
                        help="serve this mock instead of the real harness-manager-daemon app (T13)")
    args = parser.parse_args(argv)
    from harness_manager.demo import BOARD_USB, DemoEngine

    engine = DemoEngine(speed=args.speed, console_chatter=True)
    if args.sd_journal:
        engine.set_sd_journal(BOARD_USB, {"op": "install", "state": "interrupted",
                                          "current": "MB/HBI0309C/AN536/images.txt",
                                          "backup": {"path": "/tmp/backup.zip"}})
    server = (MockDaemon(engine, token=args.token, port=args.port) if args.mock else ServedApp(
        __import__("harness_manager.daemon.app", fromlist=["create_app"]).create_app(
            engine, token=args.token), args.token, port=args.port, lifespan="on"))
    with server as d:
        print(f"{'T14 mock' if args.mock else 'harness-manager-daemon'} over DemoEngine: {d.ui_url}",
              flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0



# --- bringup-usb ---
# BRINGUP-USB: the bring-up routes (docs/API.md "Bring-up over the Debug USB", bringup_api.py)
# on every mock app this module builds (tests/fakes/bringup_mock.py). MockDaemon calls
# create_app by name, so wrapping it here is enough; the UI mount stays last.
_create_app_before_bringup = create_app


def create_app(engine: Any | None = None, *, token: str = "t14-token",  # noqa: F811
               serve_ui: bool = True) -> FastAPI:
    from .bringup_mock import attach

    return attach(_create_app_before_bringup(engine, token=token, serve_ui=serve_ui), _ok)
# --- end bringup-usb ---

if __name__ == "__main__":
    raise SystemExit(main())
