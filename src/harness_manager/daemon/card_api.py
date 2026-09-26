"""The Linux harness's OS slots over the daemon API, read-only (lane LINUX-SLOTS).

docs/API.md "User microSD and OS slots (LINUX-SLOTS, ``card_api.py``)":

- ``GET /boards/{bid}/slots`` -> ``{available, reason, slots}``: the OS slots A/B (running,
  default, where a push goes, the card job).

The card itself is L1-CARD's ``GET /boards/{bid}/card`` (core routes), which LINUX-SLOTS
extends additively (the default's RM, the OS slots, the tile's ``line``). A harness
without OS slots is not an error: ``available`` is false and ``reason`` says why. The
changes (push, commit, rollback, card commit/clear) are the CLI's (``harness-manager
slot|card``): they need the lease and a confirm, so the page does not offer them.
"""

from __future__ import annotations

from typing import Any

from harness_manager.core.errors import UnavailableError
from harness_manager.services.slots import SlotService, slot_status_json

from .app import _JSON, RouteContext, ok


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    svc = SlotService()

    @api.get("/boards/{bid:path}/slots")
    def slots_read(bid: str) -> Any:
        s = ctx.board(bid)
        with d.gates.op(bid):
            try:
                st = svc.status(s)
            except UnavailableError as exc:
                return _JSON(ok(board_id=bid, available=False, reason=exc.reason, slots=None))
        return _JSON(ok(board_id=bid, available=True, reason="", slots=slot_status_json(st)))
