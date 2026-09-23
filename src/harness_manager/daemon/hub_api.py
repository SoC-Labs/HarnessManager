"""The hub routes of harness-manager-daemon: the SSH tunnel and hub leases (lane L1).

docs/API.md, "Week-plan additions: the hub" (frozen):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/tunnel`` | ``{tunnel: {via, host, state, ports: {remote: local}, detail} or null}`` |
| ``GET /boards/{bid}/lease`` | ``{lease: {target, holder, expires_at, mine} or null, hub: HOST or null}`` |
| ``POST /boards/{bid}/lease`` ``{ttl_s?}`` | 202 job ``lease``; done when HELD (it may queue); result ``{lease}`` |
| ``DELETE /boards/{bid}/lease`` | ``{ok}``: releases this client's lease (or cancels its queued request) |

Events: ``lease.state {target, state, holder, expires_at}``; and ``tunnel.state``
(the tunnel's status) whenever an open board's tunnel changes state (CCR L1-4
appends the topic).

None of these take the board's operation gate: they talk to the hub, not to
the board, so a queued lease request (a job, which does hold the board) can
still be read and cancelled. The service heartbeats a lease while its board is
open here (``LeaseService.track`` on ``session.opened``), whether the service
or the CLI acquired it (they share the token store).

Loaded by ``create_app`` through ``EXTENSIONS`` (``register(ctx)``), before the
core ``/boards/{bid:path}`` routes, whose path converter is greedy.
"""

from __future__ import annotations

import logging
from typing import Any

from harness_manager.core.errors import HarnessError, UsageError
from harness_manager.core.events import Event
from harness_manager.services.lease import DEFAULT_TTL_S, LeaseService

from .app import _JSON, JsonBody, RouteContext, _obj, ok

log = logging.getLogger(__name__)

TUNNEL_TOPIC = "tunnel.state"


def _ttl(body: dict[str, Any]) -> int:
    value = body.get("ttl_s", DEFAULT_TTL_S)
    if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 86400:
        raise UsageError(f"ttl_s must be whole seconds from 60 to 86400, not {value!r}",
                         hint="the lease lapses after it unless heartbeated; the service "
                              "heartbeats it while the board is open")
    return value


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    leases = LeaseService(d.state_dir, d.bus)
    d.leases = leases                     # other lanes and tests read it here

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

    d.bus.subscribe("session.opened", opened)
    d.bus.subscribe("session.closed", closed)

    # The daemon has no shutdown hook for extensions: stop the heartbeat thread and any
    # queued acquire (it then removes its queue entry) when the daemon closes.
    original_close = d.close

    def close() -> None:
        leases.close()
        original_close()

    d.close = close

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

    @ctx.api.get("/boards/{bid:path}/lease")
    def lease_view(bid: str) -> _JSON:
        session = ctx.board(bid)
        return _JSON(ok(**leases.view(getattr(session, "hub", None))))

    @ctx.api.post("/boards/{bid:path}/lease")
    def lease_acquire(bid: str, body: JsonBody = None) -> _JSON:
        b = _obj(body)
        ttl = _ttl(b)
        session = ctx.board(bid)
        hub = leases.require_hub(getattr(session, "hub", None), bid)
        job = d.jobs.submit("lease", bid, lambda progress: leases.acquire(
            hub, board_id=bid, ttl_s=ttl, progress=progress))
        return ctx.accepted(job)

    @ctx.api.delete("/boards/{bid:path}/lease")
    def lease_release(bid: str) -> _JSON:
        session = ctx.board(bid)
        out = leases.release(getattr(session, "hub", None), board_id=bid)
        return _JSON(ok(**{k: v for k, v in out.items() if k != "ok"}))
