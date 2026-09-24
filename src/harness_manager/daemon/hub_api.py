"""The hub routes of harness-manager-daemon: the SSH tunnel and hub leases (lanes L1, LR-C).

docs/API.md, "Week-plan additions: the hub" (frozen):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/tunnel`` | ``{tunnel: {via, host, state, ports: {remote: local}, detail} or null}`` |
| ``GET /boards/{bid}/lease`` | ``{lease: {target, holder, expires_at, mine} or null, hub: HOST or null}`` |
| ``POST /boards/{bid}/lease`` ``{ttl_s?}`` | 202 job ``lease``; done when HELD (it may queue); result ``{lease}`` |
| ``DELETE /boards/{bid}/lease`` | ``{ok}``: releases this client's lease (or cancels its queued request) |

docs/LEASE_REQUESTS.md, "API" (frozen 2026-09-24, lane LR-C):

| Method and path | Body | Returns |
|---|---|---|
| ``POST /boards/{bid}/lease/request`` | ``{message?, ttl_s?}`` | 202 job ``lease_request``; phases ``queued``, ``notified``, ``answered``, ``force-available``, ``held``; result ``{lease}`` or ``{answered: {...}}``. 409 ALREADY when the lease is already this principal's (this or another session, CCR-A2) |
| ``POST /boards/{bid}/lease/respond`` | ``{id, answer, minutes?, message?}`` | 200 ``{ok}`` |
| ``POST /boards/{bid}/lease/force`` | ``{confirm: true}`` | 202 job ``lease_force``; result ``{lease}``. Before any revoke: 409 REFUSED (or ALREADY) with the reason, 422 UNAVAILABLE with the time left, 422 USAGE without ``confirm: true`` |
| ``DELETE /boards/{bid}/lease/queue`` | none | 200 ``{left: bool}`` |

``GET /lease`` adds ``queue``, ``request``, ``incoming`` and ``taken``: the lease
service's ``view`` builds them (lane LR-B) and the route passes the view through.

Events: ``lease.state {target, state, holder, expires_at}``; ``tunnel.state``
(the tunnel's status) whenever an open board's tunnel changes state (CCR L1-4
appends the topic). The lease service publishes ``lease.wanted``,
``lease.answered``, ``lease.force_available``, ``lease.taken`` and ``lease.left``
on the engine bus; the events WebSocket forwards every bus topic as it is.

Gates. None of the lease routes take the board's operation gate: they talk to
the hub, not to the board, so a queued lease (a job, which does hold the board)
can still be read, answered, forced and cancelled. Two exceptions:

- ``respond`` with ``release`` is refused (409 HELD) while a non-lease job runs
  on the board: releasing mid-deploy hands a half-programmed board to the next
  person. "keep" is always accepted.
- ``force`` is a job. It normally runs BESIDE this board's own queued
  ``lease_request`` job (a revoke promotes that queued request), so it does not
  claim the board gate the request job holds; with no request job running it is
  an ordinary board job. Any other running job refuses it (409 HELD).

The service heartbeats a lease while its board is open here (``LeaseService.track``
on ``session.opened``), whether the service or the CLI acquired it (they share
the token store).

Loaded by ``create_app`` through ``EXTENSIONS`` (``register(ctx)``), before the
core ``/boards/{bid:path}`` routes, whose path converter is greedy.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from harness_manager.cli.cmd_hub import (
    clean_message,
    force_refusal,
    full_view,
    keep_minutes,
    request_id,
    request_refusal,
)
from harness_manager.core.errors import HarnessError, UsageError
from harness_manager.core.events import Event
from harness_manager.services.lease import DEFAULT_TTL_S, LeaseService

from .app import _JSON, JsonBody, RouteContext, _obj, ok
from .jobs import BoardGates, Job, JobManager, busy_error
from .wire import error_body

log = logging.getLogger(__name__)

TUNNEL_TOPIC = "tunnel.state"
LEASE_KIND = "lease"
REQUEST_KIND = "lease_request"
FORCE_KIND = "lease_force"
LEASE_KINDS = frozenset({LEASE_KIND, REQUEST_KIND, FORCE_KIND})
#: The events the lease service publishes for requests (docs/LEASE_REQUESTS.md, "Events").
REQUEST_TOPICS = ("lease.wanted", "lease.answered", "lease.force_available", "lease.taken",
                  "lease.left")
#: 422, not the usual 400 for USAGE: docs/LEASE_REQUESTS.md fixes it for a force without
#: ``confirm: true`` (the UI's confirm modal was skipped, not a malformed request).
FORCE_UNCONFIRMED_STATUS = 422


def _ttl(body: dict[str, Any]) -> int:
    value = body.get("ttl_s", DEFAULT_TTL_S)
    if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 86400:
        raise UsageError(f"ttl_s must be whole seconds from 60 to 86400, not {value!r}",
                         hint="the lease lapses after it unless heartbeated; the service "
                              "heartbeats it while the board is open")
    return value


def _now() -> float:
    return time.time()


class _BesideJobs:
    """Jobs that run beside a board's queued ``lease_request`` job (the force-release).

    The request job holds the board's gate until the lease is ours, and a force is
    what makes it ours, so the force cannot wait for that gate. Such jobs get their
    own ``JobManager`` (its own gates: one force per board at a time) on the same bus,
    so ``job.*`` events are the same; ``GET /jobs`` and ``GET /jobs/{id}`` see them
    because ``get``/``recent`` of the daemon's manager are widened here.
    """

    def __init__(self, d: Any) -> None:
        self.d = d
        self.side = JobManager(d.bus, BoardGates(), workers=2, keep=50)
        main_get, main_recent = d.jobs.get, d.jobs.recent

        def get(job_id: str) -> Job | None:
            return main_get(job_id) or self.side.get(job_id)

        def recent() -> list[Job]:
            return sorted([*main_recent(), *self.side.recent()], key=lambda j: j.started_at)

        d.jobs.get = get
        d.jobs.recent = recent

    def submit(self, kind: str, board_id: str, fn: Callable[[Any], Any]) -> Job:
        busy = self.d.gates.busy(board_id)
        if busy is None:
            return self.d.jobs.submit(kind, board_id, fn)
        if busy.kind != REQUEST_KIND:
            raise busy_error(board_id, busy)
        return self.side.submit(kind, board_id, fn)

    def running(self, board_id: str) -> Job | None:
        return self.side.gates.busy(board_id)

    def close(self) -> None:
        self.side.shutdown(wait=False)


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    leases = LeaseService(d.state_dir, d.bus)
    d.leases = leases                     # other lanes and tests read it here
    beside = _BesideJobs(d)
    mu = threading.Lock()
    requesting: dict[str, threading.Event] = {}     # board id -> its request job's cancel

    def stop_request(board_id: str) -> bool:
        """Stop this board's queued request job, if one runs (it then leaves the queue)."""
        with mu:
            cancel = requesting.get(board_id)
        if cancel is None or cancel.is_set():
            return False
        cancel.set()
        return True

    # -- follow boards opening and closing ------------------------------------------------

    def opened(ev: Event) -> None:
        try:
            session = d.engine.session(ev.board_id)
        except HarnessError:
            return
        hub = getattr(session, "hub", None)
        if hub is not None:
            leases.track(ev.board_id, hub)
        tunnel = getattr(getattr(session, "reach", None), "tunnel", None)
        if tunnel is not None and hasattr(tunnel, "watch"):
            bid = ev.board_id
            tunnel.watch(lambda st: d.bus.publish(Event(TUNNEL_TOPIC, bid, st)))

    def closed(ev: Event) -> None:
        leases.untrack(ev.board_id)
        leases.cancel_acquire(ev.board_id)
        stop_request(ev.board_id)         # docs/LEASE_REQUESTS.md: closing leaves the queue

    d.bus.subscribe("session.opened", opened)
    d.bus.subscribe("session.closed", closed)

    # The daemon has no shutdown hook for extensions: stop the heartbeat thread and any
    # queued acquire or request (each then removes its queue entry) when the daemon closes.
    original_close = d.close

    def close() -> None:
        with mu:
            cancels = list(requesting.values())
        for cancel in cancels:
            cancel.set()
        leases.close()
        beside.close()
        original_close()

    d.close = close

    def hub_of(bid: str) -> Any:
        session = ctx.board(bid)
        return leases.require_hub(getattr(session, "hub", None), bid)

    # -- routes -----------------------------------------------------------------------------

    @ctx.api.get("/boards/{bid:path}/tunnel")
    def tunnel(bid: str) -> _JSON:
        session = ctx.board(bid)
        reach = getattr(session, "reach", None)
        status = reach.status() if reach is not None else None
        hub = getattr(session, "hub", None)
        if status is not None and hub is not None and hasattr(hub, "share_status"):
            status = {**status, "shares": hub.share_status()}
        return _JSON(ok(tunnel=status))

    # Multi-segment lease routes first: "/boards/{bid:path}/lease" would also match them.

    @ctx.api.post("/boards/{bid:path}/lease/request")
    def lease_request(bid: str, body: JsonBody = None) -> _JSON:
        b = _obj(body)
        ttl = _ttl(b)
        message = clean_message(b.get("message"))
        hub = hub_of(bid)
        refusal = request_refusal(full_view(leases.view(hub)), hub.target)
        if refusal is not None:
            raise refusal
        cancel = threading.Event()

        def run(progress: Callable[[str, int, int], None]) -> Any:
            try:
                return leases.request(bid, hub, message=message, ttl_s=ttl, progress=progress,
                                      cancel=cancel)
            finally:
                with mu:
                    if requesting.get(bid) is cancel:
                        del requesting[bid]

        with mu:        # registered before run() can finish (its finally takes mu)
            job = d.jobs.submit(REQUEST_KIND, bid, run)
            requesting[bid] = cancel
        return ctx.accepted(job)

    @ctx.api.post("/boards/{bid:path}/lease/respond")
    def lease_respond(bid: str, body: JsonBody = None) -> _JSON:
        b = _obj(body)
        rid = request_id(b.get("id"))
        answer = b.get("answer")
        if answer not in ("release", "keep"):
            raise UsageError(f"answer must be \"release\" or \"keep\", not {answer!r}")
        if answer == "keep":
            minutes = keep_minutes(b.get("minutes"))
        else:
            if b.get("minutes") not in (None, 0):
                raise UsageError("minutes goes with \"keep\"; \"release\" releases now")
            minutes = 0
        message = clean_message(b.get("message"))
        hub = hub_of(bid)
        busy = d.gates.busy(bid)
        if answer == "release" and busy is not None and busy.kind not in LEASE_KINDS:
            raise busy_error(bid, busy)
        out = leases.respond(bid, hub, rid, answer, minutes=minutes, message=message)
        return _JSON(ok(**{k: v for k, v in (out or {}).items() if k != "ok"}))

    @ctx.api.post("/boards/{bid:path}/lease/force")
    def lease_force(bid: str, body: JsonBody = None) -> _JSON:
        b = _obj(body)
        if b.get("confirm") is not True:
            err = UsageError("force-release needs \"confirm\": true",
                             hint="it kicks the holder off the board now; the UI asks "
                                  "\"Are you sure?\" first")
            return _JSON(error_body(err), status_code=FORCE_UNCONFIRMED_STATUS)
        hub = hub_of(bid)
        refusal = force_refusal(full_view(leases.view(hub)), _now(), hub.target)
        if refusal is not None:
            raise refusal

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress("revoke", 0, 1)
            out = leases.force(bid, hub, confirm=True)
            progress("held", 1, 1)
            return out

        return ctx.accepted(beside.submit(FORCE_KIND, bid, run))

    @ctx.api.delete("/boards/{bid:path}/lease/queue")
    def lease_leave(bid: str) -> _JSON:
        hub = hub_of(bid)
        # leave() first, so it finds our queue entry (and says so); then stop the request
        # job, whose own cleanup removes anything it re-queued in between.
        out = leases.leave(bid, hub) or {}
        stopped = stop_request(bid)
        rest = {k: v for k, v in out.items() if k not in ("ok", "left")}
        return _JSON(ok(left=bool(out.get("left")) or stopped, **rest))

    @ctx.api.get("/boards/{bid:path}/lease")
    def lease_view(bid: str) -> _JSON:
        session = ctx.board(bid)
        # The service's view is the frozen shape (it adds queue/request/incoming/taken);
        # passed through as it is, so the keys it has not added yet are simply absent.
        return _JSON(ok(**leases.view(getattr(session, "hub", None))))

    @ctx.api.post("/boards/{bid:path}/lease")
    def lease_acquire(bid: str, body: JsonBody = None) -> _JSON:
        b = _obj(body)
        ttl = _ttl(b)
        session = ctx.board(bid)
        hub = leases.require_hub(getattr(session, "hub", None), bid)
        job = d.jobs.submit(LEASE_KIND, bid, lambda progress: leases.acquire(
            hub, board_id=bid, ttl_s=ttl, progress=progress))
        return ctx.accepted(job)

    @ctx.api.delete("/boards/{bid:path}/lease")
    def lease_release(bid: str) -> _JSON:
        session = ctx.board(bid)
        with mu:
            queued = bid in requesting
        if queued:
            # A queued request: DELETE /lease cancels it as it cancels a queued acquire.
            hub = leases.require_hub(getattr(session, "hub", None), bid)
            out = leases.leave(bid, hub) or {}
            stop_request(bid)
            return _JSON(ok(cancelled=True, left=True,
                            **{k: v for k, v in out.items() if k not in ("ok", "left")}))
        out = leases.release(getattr(session, "hub", None), board_id=bid)
        return _JSON(ok(**{k: v for k, v in out.items() if k != "ok"}))
