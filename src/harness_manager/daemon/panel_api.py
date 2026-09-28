"""The front-panel routes of harness-manager-daemon, and presence for its open boards (lane P1).

docs/API.md "Front panel" (additive):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/panel`` | ``{panel: PanelState or null, reason, identify: {available, reason, until}, support, presence}``; ``?state=0`` leaves the panel unread (``panel`` null) |
| ``GET /boards/{bid}/panel/frame`` | ``{rows, roles, source, observed_at, note}`` |
| ``POST /boards/{bid}/identify`` ``{seconds?}`` | ``{until, until_ms, seconds, next_at, lease_holder?, note?, opened_for_identify?}``; 422 UNAVAILABLE with the reason on bare metal; 409 ALREADY within 10 s of the last start |

``harness_manager.services.presence.PresenceService`` does the work. This module wires it:

- it tracks every board the daemon opens (``session.opened``) and forgets it when it
  closes (``session.closed``): no hello ever goes to a closed board;
- the beat's own hello goes through the board's gate: while a job runs on the board it is
  skipped (409 HELD would be the answer), except beside a lease job, which never talks to
  the board (it waits on the hub);
- the hello's lease is the lease service's cached view (``hub_api`` sets ``d.leases``), and
  its job is the board's running job with its progress;
- QUIET-POLL: a beat goes only when the daemon's background gate allows it (``d.quiet``,
  ``services/quiet.py``): a page views the board, its lease is not someone else's, the
  policy is not ``off``, and the board did not just turn a connection away.

The reads go through the gate like every other board request (409 HELD naming the job while
one runs). ``POST /identify`` is short, not a job.

**Identify (lane LOCATE, docs/design/BOARD_LOCATE.md).** An explicit action only (a click or
a command), so the background gate never holds it back and no beat ever sends it:

- at most one start per board every 10 s, whoever asks through this daemon
  (``PresenceService.limiter``): 409 ALREADY with ``data.retry_after_s``; a stop (0 s) is
  never limited, and a start that failed gives its slot back;
- **no lease needed** (the Linux harness's ``locate`` has no claim lock: any peer, so it goes
  over the normal hub tunnel). When the board's hub lease is someone else's (the lease
  service's CACHED view: Identify never waits on the hub) the answer names the holder
  (``lease_holder``, ``note``); the board's banner shows them who asked;
- a board this daemon knows but has not open (the sidebar's other boards) is opened for
  the one request and closed again (``opened_for_identify: true``), as the CLI's
  ``identify`` does; presence never tracks it. 409 HELD when another process holds it.

``?state=0`` (CCR PANEL-5) answers what the board can do and the presence without reading
the board's panel: the client's ``session.panel.support()`` (``client.remote``) asks it, so
an Identify from the CLI costs the board one ``locate`` and nothing else, as before.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from typing import Any

from harness_manager.cli.output import jsonable
from harness_manager.core import capabilities as C
from harness_manager.core.errors import (
    AbsentError,
    AlreadyError,
    HarnessError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.core.panel import HelloJob
from harness_manager.services.presence import PresenceService, check_seconds, require_panel
from harness_manager.services.quiet import lease_not_mine

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
#: CCR PANEL-2: a beat takes the lease service's last view (no hub call) while it is at most
#: this old; an older one, or none (a lease change drops it), is read again.
LEASE_VIEW_MAX_AGE_S = 60.0
#: LOCATE: the lock note of a board opened for one Identify (a board not open here).
IDENTIFY_NOTE = "identify (opened for one request)"


def _flag(value: str | None, name: str, default: bool) -> bool:
    if value is None or value == "":
        return default
    low = value.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise UsageError(f"{name} must be true or false, not {value!r}")


def beat_lease_view(leases: Any, hub: Any) -> dict[str, Any] | None:
    """The lease view a presence beat relays (CCR PANEL-2): the lease service's cached view
    when it has a recent one (no ssh to the hub), else an ordinary read."""
    if hub is None or leases is None:
        return None
    cached = leases.view(hub, cached_only=True, max_age_s=LEASE_VIEW_MAX_AGE_S)
    return cached if cached is not None else leases.view(hub)


def lease_for_identify(leases: Any, session: Any) -> str:
    """The hub lease's holder when it is not ours, for an Identify's note (lane LOCATE): the
    EXPLICIT rule (``lease_not_mine``) over the lease service's CACHED view only, so Identify
    never waits on the hub. "" with no hub, no lease service, or no recent view."""
    hub = getattr(session, "hub", None)
    if leases is None or hub is None:
        return ""
    try:
        view = leases.view(hub, cached_only=True, max_age_s=LEASE_VIEW_MAX_AGE_S)
    except HarnessError:
        return ""
    return lease_not_mine(view) if view is not None else ""


def identify_lease_note(holder: str) -> dict[str, Any]:
    """The answer's extra keys when someone else holds the hub lease: Identify needs no lease
    (the board's ``locate`` has no claim lock), and the board's banner names who asked."""
    if not holder:
        return {}
    return {"lease_holder": holder,
            "note": f"the hub lease is {holder}'s: Identify needs no lease; the board's "
                    "banner shows who asked"}


def register(ctx: RouteContext) -> None:
    d = ctx.daemon

    def lease_view(session: Any) -> dict[str, Any] | None:
        return beat_lease_view(getattr(d, "leases", None), getattr(session, "hub", None))

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

    # QUIET-POLL: a beat is background contact; the daemon's gate (services/quiet.py) says
    # when it may go (a viewer, the lease not someone else's, policy, no back-off).
    quiet = getattr(d, "quiet", None)
    presence = PresenceService(d.bus, lease_view=lease_view, leases=getattr(d, "leases", None),
                               job_of=job_of, gate=beat_gate,
                               allow=quiet.check if quiet is not None else None,
                               noted=quiet.observe if quiet is not None else None)
    d.presence = presence                  # other lanes and tests read it here

    #: LOCATE: boards opened for one Identify right now (never tracked for presence).
    transient: set[str] = set()
    transient_mu = threading.Lock()

    def opened(ev: Event) -> None:
        with transient_mu:
            if ev.board_id in transient:
                return
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
    def panel_state(bid: str, state: str | None = None) -> _JSON:
        with_state = _flag(state, "state", True)              # 400 before the board
        s = ctx.board(bid)
        with d.gates.op(bid):
            body = presence.read(bid, s, reason_for=reason_for(s), with_state=with_state)
        return _JSON(ok(board_id=bid, **jsonable(body)))

    # -- Identify (LOCATE) --------------------------------------------------------------------

    def locate_on(bid: str, s: Any, seconds: int) -> dict[str, Any]:
        require_panel(s, C.LOCATE, reason_for(s)(C.LOCATE))
        holder = lease_for_identify(getattr(d, "leases", None), s) if seconds else ""
        out = presence.identify(bid, s, seconds)
        out.update(identify_lease_note(holder))
        return out

    def identify_closed(bid: str, seconds: int) -> dict[str, Any]:
        """Identify a board this daemon knows but has not open: open it for this one request
        (its lock, noted ``IDENTIFY_NOTE``), never tracked for presence, then close it."""
        cand = d.known().get(bid)
        if cand is None:
            raise AbsentError(f"{bid} is not a board this Harness Manager knows",
                              hint="scan for boards, or add it by address")
        if seconds and presence.limiter.wait_s(bid) > 0:
            presence.limiter.claim(bid)                  # raises ALREADY with the wait
        with transient_mu:
            if bid in transient:
                raise AlreadyError(f"an Identify on {bid} is already on its way",
                                   hint="wait for it to finish")
            transient.add(bid)
        try:
            s = d.engine.open(cand, note=IDENTIFY_NOTE)
            try:
                with d.gates.op(bid):
                    out = locate_on(bid, s, seconds)
            finally:
                d.engine.close(bid)
        finally:
            with transient_mu:
                transient.discard(bid)
        out["opened_for_identify"] = True
        return out

    @ctx.api.post("/boards/{bid:path}/identify")
    def identify(bid: str, body: JsonBody = None) -> _JSON:
        seconds = check_seconds(_obj(body).get("seconds"))        # 400 before the board
        if bid in d.engine.open_boards():
            s = ctx.board(bid)
            with d.gates.op(bid):
                out = locate_on(bid, s, seconds)
        else:
            out = identify_closed(bid, seconds)
        return _JSON(ok(board_id=bid, **out))

