"""The hub routes of harness-manager-daemon: the SSH tunnel and hub leases (lanes L1, LR-C).

docs/API.md, "Week-plan additions: the hub" (frozen):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/tunnel`` | ``{tunnel: {via, host, state, ports: {remote: local}, detail} or null}`` |
| ``GET /boards/{bid}/lease`` | ``{lease: {target, board, holder, expires_at, mine} or null, hub: HOST or null}`` |
| ``POST /boards/{bid}/lease`` ``{ttl_s?}`` | 202 job ``lease``; done when HELD (it may queue); result ``{lease}`` |
| ``DELETE /boards/{bid}/lease`` | ``{ok}``: releases this client's lease (or cancels its queued request) |

docs/LEASE_REQUESTS.md, "API" (frozen 2026-09-24, lane LR-C):

| Method and path | Body | Returns |
|---|---|---|
| ``POST /boards/{bid}/lease/request`` | ``{message?, ttl_s?}`` | 202 job ``lease_request``; phases ``queued``, ``notified``, ``answered``, ``force-available``, ``held``. A "keep" answer does not end it (D1); it ends with ``{lease}`` when held, or ``{left: true}`` when we leave (D7). 409 ALREADY when the lease is already this principal's (this or another session, CCR-A2) |
| ``POST /boards/{bid}/lease/respond`` | ``{id, answer, minutes?, message?}`` | 200 ``{ok}`` |
| ``POST /boards/{bid}/lease/force`` | ``{confirm: true, confirm_board?}`` | 202 job ``lease_force``; result ``{lease}``. Before any revoke: 400 USAGE without ``confirm: true``; 422 UNAVAILABLE with the time left; 409 REFUSED (or ALREADY) with the reason (D3). D12: when ``GET /lease`` says ``lease.holder_kind`` is not ``"hm"`` (maybe a script), ``confirm_board`` must be the board's name: 400 USAGE without it, 409 REFUSED with another name |
| ``DELETE /boards/{bid}/lease/queue`` | none | 200 ``{left: bool}`` |
| ``DELETE /boards/{bid}/lease/taken`` | none | 200 ``{dismissed: bool}``; ``GET /lease`` then has ``taken: null`` until the next forced release (D11) |

``GET /lease`` adds ``queue``, ``request``, ``incoming`` (each with its ``answer``, D5),
``taken`` and ``board`` (D4): the lease service's ``view`` builds them (lane LR-B) and
the route passes the view through. LEASE-BOARD adds ``board`` to every ``lease`` object
(``GET``, the acquire and request jobs' results, ``released``) and to ``lease.state``: the
physical board (``mps3_01``) when known, else null. ``target`` stays: it is what is leased.

Events: ``lease.state {target, board, state, holder, expires_at}``; ``tunnel.state``
(the tunnel's status) whenever an open board's tunnel changes state (CCR L1-4
appends the topic). A board whose hub is reached over fpgahub's REST API (T8,
CCR T8-2) also streams the hub's own events while it is open: ``hub.event`` and
``hub.stream`` (``harness_manager.transports.hub_events``); each one drops the
lease service's cached view and settles a revoked or expired lease at once. The lease service publishes ``lease.wanted``,
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

import inspect
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from harness_manager import naming
from harness_manager.cli.cmd_hub import (
    clean_message,
    force_refusal,
    full_view,
    keep_minutes,
    request_id,
    request_refusal,
)
from harness_manager.core.errors import AbsentError, HarnessError, UsageError
from harness_manager.core.events import Event
from harness_manager.services.lease import (
    DEFAULT_TTL_S,
    LeaseService,
    check_want,
    lease_name,
    typed_names,
    view_confirm_error,
)

from .app import _JSON, JsonBody, RouteContext, _obj, ok
from .jobs import BoardGates, Job, JobManager, busy_error

log = logging.getLogger(__name__)

TUNNEL_TOPIC = "tunnel.state"
LEASE_KIND = "lease"
REQUEST_KIND = "lease_request"
FORCE_KIND = "lease_force"
LEASE_KINDS = frozenset({LEASE_KIND, REQUEST_KIND, FORCE_KIND})
#: The events the lease service publishes for requests (docs/LEASE_REQUESTS.md, "Events").
REQUEST_TOPICS = ("lease.wanted", "lease.answered", "lease.force_available", "lease.taken",
                  "lease.left", "lease.tapped")


def _ttl(body: dict[str, Any]) -> int | None:
    """``ttl_s``, checked; None when not given: the hub's own (``[hubs.<name>] lease_ttl`` /
    ``request_ttl``, CCR SET-HUB-3), else the service's default."""
    if "ttl_s" not in body:
        return None
    value = body.get("ttl_s", DEFAULT_TTL_S)
    if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 86400:
        raise UsageError(f"ttl_s must be whole seconds from 60 to 86400, not {value!r}",
                         hint="the lease lapses after it unless heartbeated; the service "
                              "heartbeats it while the board is open")
    return value


def _now() -> float:
    return time.time()


def _view(leases: Any, hub: Any) -> dict[str, Any]:
    """``leases.view(hub)``, with the background queue (ui2 api-hub, G11) when the service
    reads it (``background=True``; a stand-in service from before it takes no such key)."""
    try:
        takes = "background" in inspect.signature(leases.view).parameters
    except (TypeError, ValueError):
        takes = False
    return leases.view(hub, background=True) if takes else leases.view(hub)


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
    leases = LeaseService(d.state_dir, d.bus, origin="service")   # IDLE-LEASE: grants say so
    d.leases = leases                     # other lanes and tests read it here
    beside = _BesideJobs(d)
    streams: dict[str, Any] = {}          # board id -> hub_events.HubEventStream (T8)
    d.hub_streams = streams
    # CCR T8-4 gives the service these; until it lands, the stream still runs and the
    # 10 s view cache and the heartbeat settle a change instead.
    on_hub_event = getattr(leases, "on_hub_event", None)
    if callable(on_hub_event):
        d.bus.subscribe("hub.event", on_hub_event)
    forget = getattr(leases, "forget", None) or getattr(leases, "_forget", None)
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
            start_stream(ev.board_id, hub)
        tunnel = getattr(getattr(session, "reach", None), "tunnel", None)
        if tunnel is not None and hasattr(tunnel, "watch"):
            bid = ev.board_id
            tunnel.watch(lambda st: d.bus.publish(Event(TUNNEL_TOPIC, bid, st)))

    def start_stream(board_id: str, hub: Any) -> None:
        """fpgahub's event stream for a REST-mode hub with events on (CCR T8-2)."""
        client = getattr(hub, "client", None)
        if getattr(client, "transport", "") != "rest" or \
                not getattr(getattr(client, "config", None), "events", False):
            return
        with mu:
            if board_id in streams:
                return
        from harness_manager.transports import hub_events

        stream = hub_events.attach_bus(
            d.bus, board_id, client,
            on_resync=(lambda h=hub: forget(h)) if callable(forget) else None)
        with mu:
            if board_id not in streams:
                streams[board_id] = stream
                return
        stream.close()                    # lost a race with another open: keep the first

    def stop_stream(board_id: str) -> None:
        with mu:
            stream = streams.pop(board_id, None)
        if stream is not None:
            stream.close()

    def closed(ev: Event) -> None:
        leases.untrack(ev.board_id)
        leases.cancel_acquire(ev.board_id)
        stop_request(ev.board_id)         # docs/LEASE_REQUESTS.md: closing leaves the queue
        stop_stream(ev.board_id)

    d.bus.subscribe("session.opened", opened)
    d.bus.subscribe("session.closed", closed)

    # The daemon has no shutdown hook for extensions: stop the heartbeat thread and any
    # queued acquire or request (each then removes its queue entry) when the daemon closes.
    original_close = d.close

    def close() -> None:
        with mu:
            cancels = list(requesting.values())
            ids = list(streams)
        for cancel in cancels:
            cancel.set()
        for board_id in ids:
            stop_stream(board_id)
        leases.close()
        beside.close()
        original_close()

    d.close = close

    def hub_of(bid: str, *, closed_ok: bool = False) -> Any:
        return leases.require_hub(board_hub(bid, closed_ok=closed_ok), bid)

    def board_hub(bid: str, *, closed_ok: bool = False) -> Any:
        """The board's hub adapter (None: no hub). ``closed_ok`` (ui2 api-hub, G3: "Request
        without opening"): a board this service lists but has not open gets its hub from the
        board pack, with no contact (``hub_boards``); an unlisted one is 404 as before."""
        try:
            return getattr(ctx.board(bid), "hub", None)
        except AbsentError:
            hubs = getattr(d, "board_hubs", None)
            if not closed_ok or hubs is None or hubs.candidate(bid) is None:
                raise
        hub = hubs.adapter(bid)
        if hub is not None:
            leases.note_board(bid, hub)       # its lease_known follows what is read here
        return hub

    def is_open(bid: str) -> bool:
        return bid in d.engine.open_boards()

    def names_of(bid: str) -> tuple[str, ...]:
        """What the UI calls the board (N1 name first), then its address (D12's typed name)."""
        cand = getattr(ctx.board(bid), "candidate", None)
        if cand is None:
            return ()
        return (getattr(cand, "name", "") or "", naming.address_of(cand))

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
        want = check_want(b.get("want_s"))         # ui2 api-hub (G11): how long they want it
        hub = hub_of(bid, closed_ok=True)          # ui2 api-hub (G3): without opening it
        opened = is_open(bid)
        view = full_view(leases.view(hub))
        # LEASE-BOARD: refusals name the physical board (mps3_01) when the view knows it.
        refusal = request_refusal(view, lease_name(view.get("board"), hub.target))
        if refusal is not None:
            raise refusal
        cancel = threading.Event()

        def run(progress: Callable[[str, int, int], None]) -> Any:
            try:
                extra: dict[str, Any] = {"want_s": want} if want else {}   # (pre-G11 services)
                if not opened:
                    extra["heartbeat"] = False     # G3: a closed board's lease lives its TTL
                return leases.request(bid, hub, message=message, ttl_s=ttl, progress=progress,
                                      cancel=cancel, **extra)
            except HarnessError:
                if cancel.is_set():
                    # We stopped it (leave, DELETE /lease, close): leaving is not a failure
                    # (D7), whether the service returns {left} or raises its "cancelled".
                    return {"left": True}
                raise
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
        if b.get("confirm") is not True:            # 400 USAGE (D3: API.md's table)
            raise UsageError("force-release needs \"confirm\": true",
                             hint="it kicks the holder off the board now; the UI asks "
                                  "\"Are you sure?\" first")
        confirm_board = b.get("confirm_board")
        hub = hub_of(bid)
        view = full_view(leases.view(hub))
        refusal = force_refusal(view, _now(), lease_name(view.get("board"), hub.target))
        if refusal is not None:
            raise refusal
        # D12: a holder no Harness Manager session answered for may be a script: the board's
        # name typed, or 400 USAGE (missing) / 409 REFUSED (another name), before the job.
        names = names_of(bid)
        refusal = view_confirm_error(
            view, confirm_board,
            typed_names(names[0] if names else "", view.get("board"), hub.target, *names[1:]),
            hub.target)
        if refusal is not None:
            raise refusal

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress("revoke", 0, 1)
            out = leases.force(bid, hub, confirm=True, confirm_board=confirm_board,
                               board_names=names)
            progress("held", 1, 1)
            return out

        return ctx.accepted(beside.submit(FORCE_KIND, bid, run))

    @ctx.api.delete("/boards/{bid:path}/lease/queue")
    def lease_leave(bid: str) -> _JSON:
        hub = hub_of(bid, closed_ok=True)                  # ui2 api-hub (G3)
        # leave() first, so it finds our queue entry (and says so); then stop the request
        # job, whose own cleanup removes anything it re-queued in between.
        out = leases.leave(bid, hub) or {}
        stopped = stop_request(bid)
        rest = {k: v for k, v in out.items() if k not in ("ok", "left")}
        return _JSON(ok(left=bool(out.get("left")) or stopped, **rest))

    @ctx.api.delete("/boards/{bid:path}/lease/taken")
    def lease_dismiss_taken(bid: str) -> _JSON:
        """D11: the victim closed the "force-released by ..." banner. Local; no gate."""
        return _JSON(ok(dismissed=bool(leases.dismiss_taken(
            hub_of(bid, closed_ok=True)))))                # ui2 api-hub (G3)

    @ctx.api.get("/boards/{bid:path}/lease")
    def lease_view(bid: str) -> _JSON:
        hub = board_hub(bid, closed_ok=True)               # ui2 api-hub (G3): open or not
        # The service's view is the frozen shape (it adds queue/request/incoming/taken);
        # passed through as it is, so the keys it has not added yet are simply absent.
        # ui2 api-hub (G11): with the background queue (over SSH one more read, reused 60 s)
        return _JSON(ok(**_view(leases, hub)))

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
        hub = board_hub(bid, closed_ok=True)               # ui2 api-hub (G3): open or not
        with mu:
            queued = bid in requesting
        if queued:
            # A queued request: DELETE /lease cancels it as it cancels a queued acquire.
            hub = leases.require_hub(hub, bid)
            out = leases.leave(bid, hub) or {}
            stop_request(bid)
            return _JSON(ok(cancelled=True, left=True,
                            **{k: v for k, v in out.items() if k not in ("ok", "left")}))
        out = leases.release(hub, board_id=bid)
        return _JSON(ok(**{k: v for k, v in out.items() if k != "ok"}))
