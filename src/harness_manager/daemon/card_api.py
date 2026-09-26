"""The board's user microSD and OS slots over the daemon API, read-only (lane LINUX-SLOTS).

docs/API.md "User microSD and OS slots (LINUX-SLOTS, ``card_api.py``)":

- ``GET /boards/{bid}/card`` -> ``{available, reason, card, line}``: the user microSD (D13:
  present, the store's state, the power-on default) and, on the Linux harness, the OS
  slots on the same card. ``line`` is the Board tile's one line (present / slots);
- ``GET /boards/{bid}/slots`` -> ``{available, reason, slots}``: the Linux harness's OS
  slots A/B (running, default, where a push goes, the card job).

A harness without them is not an error: ``available`` is false and ``reason`` says why
(a bare-metal harness has no OS slots; one without the ``usd`` feature has no card
store). Both go through the board gate like every board read. The changes (push,
commit, rollback, card commit/clear) are the CLI's (``harness-manager slot|card``): they
need the lease and a confirm, and write the card, so the page does not offer them.
"""

from __future__ import annotations

from typing import Any

from harness_manager.core.errors import UnavailableError
from harness_manager.services.slots import (
    SlotService,
    card_line,
    card_status_json,
    slot_status_json,
)

from .app import _JSON, RouteContext, ok


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    svc = SlotService()

    @api.get("/boards/{bid:path}/card")
    def card_read(bid: str) -> Any:
        s = ctx.board(bid)
        with d.gates.op(bid):
            try:
                st = svc.card_status(s)
            except UnavailableError as exc:
                return _JSON(ok(board_id=bid, available=False, reason=exc.reason, card=None,
                                line=f"n/a: {exc.reason}"))
        return _JSON(ok(board_id=bid, available=True, reason="", card=card_status_json(st),
                        line=card_line(st)))

    @api.get("/boards/{bid:path}/slots")
    def slots_read(bid: str) -> Any:
        s = ctx.board(bid)
        with d.gates.op(bid):
            try:
                st = svc.status(s)
            except UnavailableError as exc:
                return _JSON(ok(board_id=bid, available=False, reason=exc.reason, slots=None))
        return _JSON(ok(board_id=bid, available=True, reason="", slots=slot_status_json(st)))
