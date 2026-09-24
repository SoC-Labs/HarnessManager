"""The front-panel routes of harness-manager-daemon, and presence for its open boards (lane P1).

docs/API.md "Front panel" (additive):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/panel`` | ``{panel: PanelState or null, reason, identify: {available, reason, until}, support, presence}`` |
| ``GET /boards/{bid}/panel/frame`` | ``{rows, roles, source, observed_at, note}`` |
| ``POST /boards/{bid}/identify`` ``{seconds?}`` | ``{until, seconds}``; 422 UNAVAILABLE with the reason on bare metal |

``harness_manager.services.presence.PresenceService`` does the work. This module wires it:

- it tracks every board the daemon opens (``session.opened``) and forgets it when it
  closes (``session.closed``): no hello ever goes to a closed board;
- the beat's own hello goes through the board's gate: while a job runs on the board it is
  skipped (409 HELD would be the answer), except beside a lease job, which never talks to
  the board (it waits on the hub);
- the hello's lease is the lease service's cached view (``hub_api`` sets ``d.leases``), and
  its job is the board's running job with its progress.

The reads go through the gate like every other board request (409 HELD naming the job while
one runs). ``POST /identify`` is short, not a job.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from harness_manager.cli.output import jsonable
from harness_manager.core import capabilities as C
from harness_manager.core.errors import HarnessError, UnavailableError
from harness_manager.core.events import Event
from harness_manager.core.panel import HelloJob
from harness_manager.services.presence import PresenceService, check_seconds, require_panel

from .app import _JSON, JsonBody, RouteContext, _obj, ok
from .jobs import busy_error

#: Jobs that wait on the hub, not the board: a hello may go beside them.
HUB_ONLY_JOBS = frozenset({"lease", "lease_request", "lease_force"})
#: A job's kind as the panel says it (HM's words, at most 8 characters on the wire).
JOB_WORDS = {"deploy": "program", "restore": "restore", "reboot": "reboot",
             "lease": "lease", "lease_request": "lease", "lease_force": "lease",
             "power_cycle": "power", "debug_up": "debug", "sd_backup": "backup",
             "sd_install": "install", "sd_restore": "restore", "update_check": "update",
             "update_harness": "update", "update_rollback": "rollback"}


def register(ctx: RouteContext) -> None:
    d = ctx.daemon

    def lease_view(session: Any) -> dict[str, Any] | None:
        hub = getattr(session, "hub", None)
        leases = getattr(d, "leases", None)
        if hub is None or leases is None:
            return None
        return leases.view(hub)

    def job_of(board_id: str) -> HelloJob | None:
        job = d.gates.busy(board_id)
        if job is None:
            return None
        done, total = job.progress.get("done", 0), job.progress.get("total", 0)
        pct = int(100 * done / total) if total else 0
        return HelloJob(kind=JOB_WORDS.get(job.kind, job.kind), percent=pct)

    @contextlib.contextmanager
    def beat_gate(board_id: str) -> Iterator[None]:
        job = d.gates.busy(board_id)
        if job is not None:
            if job.kind not in HUB_ONLY_JOBS:
                raise busy_error(board_id, job)
            yield                    # nothing else talks to the board while a lease job waits
            return
        with d.gates.op(board_id):
            yield

    presence = PresenceService(d.bus, lease_view=lease_view, leases=getattr(d, "leases", None),
                               job_of=job_of, gate=beat_gate)
    d.presence = presence                  # other lanes and tests read it here

    def opened(ev: Event) -> None:
        try:
            session = d.engine.session(ev.board_id)
        except HarnessError:
            return
        if getattr(session, "panel", None) is not None:
            presence.track(ev.board_id, session)

    def closed(ev: Event) -> None:
        presence.untrack(ev.board_id)

    d.bus.subscribe("session.opened", opened)
    d.bus.subscribe("session.closed", closed)
    presence.start()

    original_close = d.close

    def close() -> None:
        presence.close()
        original_close()

    d.close = close

    def reason_for(session: Any) -> Any:
        def why(capability: str) -> str:
            try:
                ctx.require(session, "panel", capability)
            except UnavailableError as exc:
                return exc.reason
            return ""
        return why

    # -- routes (multi-segment first: "/panel" would also match "/panel/frame") -------------

    @ctx.api.get("/boards/{bid:path}/panel/frame")
    def panel_frame(bid: str) -> _JSON:
        s = ctx.board(bid)
        with d.gates.op(bid):
            require_panel(s, C.FRONT_PANEL, reason_for(s)(C.FRONT_PANEL))
            frame = presence.frame(bid, s)
        return _JSON(ok(board_id=bid, **jsonable(frame)))

    @ctx.api.get("/boards/{bid:path}/panel")
    def panel_state(bid: str) -> _JSON:
        s = ctx.board(bid)
        with d.gates.op(bid):
            body = presence.read(bid, s, reason_for=reason_for(s))
        return _JSON(ok(board_id=bid, **jsonable(body)))

    @ctx.api.post("/boards/{bid:path}/identify")
    def identify(bid: str, body: JsonBody = None) -> _JSON:
        s = ctx.board(bid)
        seconds = check_seconds(_obj(body).get("seconds"))        # 400 before the board
        with d.gates.op(bid):
            require_panel(s, C.LOCATE, reason_for(s)(C.LOCATE))
            out = presence.identify(bid, s, seconds)
        return _JSON(ok(board_id=bid, **out))

