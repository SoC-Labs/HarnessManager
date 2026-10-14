"""Single-user mode: "Who can use this board" (Release 3, lane HM-C; BOARD_LEASE_HM_PLAN.md).

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/lease/mode`` | ``{board_id, available, reason, single_user, can_change, change_reason, since_s, by, persisted, local_hold_s, courtesy, label, ttl_min}`` |
| ``POST /boards/{bid}/lease/mode`` ``{single_user, confirm: true, confirm_board?}`` | 202 job ``lease_mode``; result ``{mode, lease}`` |

A desk board can be its own lease server (feature ``board_lease``); this switches it between
Shared and One person at a time. The state is the board's, so every PC sees it. Turning it ON
takes the lease in the same step. ``label`` is ``lease_backend.COURTESY_LABEL`` while it is on.

Errors, all before any job: 400 USAGE (``single_user`` not true or false, or no
``confirm: true``); 422 UNAVAILABLE (the board lacks ``board_lease``, or sits behind a hub:
"Behind a hub, the hub's lease already does this."); 409 REFUSED (the board says this person
cannot change it: not the owner or the holder); 409 ALREADY (it is already in that mode).
Event ``lease.mode {board, single_user, by, at, via: "hm"}`` on the bus when it changed.

Loaded by ``create_app`` through ``EXTENSIONS`` (``register(ctx)``) before the core
``/boards/{bid:path}`` routes, whose path converter is greedy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from harness_manager.cli.cmd_hub import mode_json, mode_unavailable
from harness_manager.core.errors import (
    AbsentError,
    AlreadyError,
    HarnessError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.services import lease_backend as lb

from .app import _JSON, JsonBody, RouteContext, _obj, ok

log = logging.getLogger(__name__)

KIND = "lease_mode"
TOPIC = "lease.mode"
CAPABILITY = "board_lease"


TTL_SETTING = "lease.board_ttl_min"
DEFAULT_TTL_MIN = 15


def _ttl_min(state_dir: Any) -> int:
    """The board-lease TTL HM uses, in minutes (setting ``lease.board_ttl_min``, default 15)."""
    try:
        from harness_manager.settings import runtime

        value = int(runtime.value(TTL_SETTING, state_dir=state_dir, strict=False))
    except (HarnessError, OSError, ValueError, TypeError, KeyError):
        return DEFAULT_TTL_MIN
    return value if value > 0 else DEFAULT_TTL_MIN


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _principal(adapter: Any) -> str:
    getter = getattr(getattr(adapter, "client", None), "principal", None)
    if callable(getter):
        try:
            who = getter()
            return who if isinstance(who, str) else ""
        except HarnessError:
            return ""
    return ""


def register(ctx: RouteContext) -> None:
    d = ctx.daemon

    def adapter_of(bid: str) -> Any:
        return lb.lease_of(ctx.board(bid))

    def require_switchable(adapter: Any) -> None:
        why = mode_unavailable(adapter)
        if why:
            raise UnavailableError(CAPABILITY, why)

    @ctx.api.get("/boards/{bid:path}/lease/mode")
    def lease_mode_view(bid: str) -> _JSON:
        try:
            adapter = adapter_of(bid)
        except AbsentError:
            hubs = getattr(d, "board_hubs", None)
            if hubs is None or hubs.candidate(bid) is None:
                raise
            return _JSON(ok(**_view(bid, None, "Open the board to see who can use it.")))
        why = mode_unavailable(adapter)
        mode = None if why else lb.mode_of(adapter)
        if not why and mode is None:
            why = "The board did not report its mode."
        return _JSON(ok(**_view(bid, mode, why)))

    @ctx.api.post("/boards/{bid:path}/lease/mode")
    def lease_mode_set(bid: str, body: JsonBody = None) -> _JSON:
        b = _obj(body)
        want = b.get("single_user")
        if not isinstance(want, bool):
            raise UsageError("single_user must be true (one person at a time) or false (shared)")
        if b.get("confirm") is not True:
            raise UsageError("changing who can use the board needs \"confirm\": true",
                             hint="turning it on takes the lease now; the app asks first")
        confirm_board = b.get("confirm_board")
        if confirm_board is not None and not isinstance(confirm_board, str):
            raise UsageError("confirm_board must be the board's name, as text")
        session = ctx.board(bid)
        adapter = lb.lease_of(session)
        require_switchable(adapter)
        mode = lb.mode_of(adapter)
        if mode is not None:
            if mode.single_user == want:
                state = "one person at a time" if want else "shared"
                raise AlreadyError(f"this board is already {state}")
            if not mode.can_change:
                raise RefusedError(mode.change_reason or
                                   "only the board's owner or the lease holder can change this",
                                   hint="claim the board first, or ask the holder")
        board = getattr(getattr(session, "candidate", None), "board_id", "") or bid

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress("switch", 0, 1)
            out = adapter.client.set_mode(want, take=want, confirm_board=confirm_board)
            progress("switch", 1, 1)
            leases = getattr(d, "leases", None)
            forget = getattr(leases, "forget", None)
            if callable(forget):
                forget(adapter)
            if want and leases is not None:
                leases.track(bid, adapter)            # heartbeat the lease just taken
            after = lb.mode_of(adapter)
            lease = out.get("lease") if isinstance(out, dict) else None
            d.bus.publish(Event(TOPIC, bid, {
                "board": board, "single_user": want, "by": _principal(adapter),
                "at": _now_iso(), "via": "hm"}))
            return {"mode": mode_json(after) or {"single_user": want}, "lease": lease}

        return ctx.accepted(d.jobs.submit(KIND, bid, run))

    def _view(bid: str, mode: lb.LeaseMode | None, why: str) -> dict[str, Any]:
        shown = mode_json(mode) or {
            "single_user": False, "can_change": False, "change_reason": why, "since_s": 0,
            "by": "", "persisted": True, "local_hold_s": 0, "courtesy": False, "label": ""}
        return {"board_id": bid, "available": mode is not None and not why, "reason": why,
                **shown, "ttl_min": _ttl_min(d.state_dir)}
