"""Who is viewing a board, and what background contact with it does (lane QUIET-POLL).

docs/API.md "Background reads" (additive):

| Method and path | Body | Returns |
|---|---|---|
| ``PUT /boards/{bid}/viewers/{vid}`` | ``{ttl_s?}`` (default 45, at most 300) | ``{board_id, viewer, background}`` |
| ``DELETE /boards/{bid}/viewers/{vid}`` | none | ``{board_id, viewer, removed}`` |
| ``GET /boards/{bid}/background`` | none | ``{board_id, background}`` |

``background`` is ``BackgroundGate.state``: ``{allowed, kind, text, holder, retry_in_s,
policy, viewers, refusals, detail}``. ``kind`` is empty when background contact may go
ahead, else ``off`` | ``lease`` | ``no_viewer`` | ``busy``.

A page that SHOWS a board (selected, and the page visible) puts its viewer, refreshes it
every 20 s, and deletes it when it looks elsewhere, hides or closes; a viewer nobody
refreshes lapses after its ``ttl_s``. While no viewer is live, the service contacts the board
in the background not at all (no presence beat, no background read): only explicit actions.

None of these routes touches the board, and none takes its gate: they answer during a job.
A read marked ``X-HM-Background: 1`` is answered by the gate itself (``app.background_gate``).
"""

from __future__ import annotations

import re
from typing import Any

from harness_manager.core.errors import UsageError

from .app import _JSON, JsonBody, RouteContext, _obj, ok

#: A viewer id: what a page makes up for itself (letters, digits, - and _).
_VIEWER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _viewer(vid: str) -> str:
    if not _VIEWER.match(vid or ""):
        raise UsageError(f"viewer id {vid!r} must be 1-64 letters, digits, '-' or '_'")
    return vid


def _ttl(body: dict[str, Any]) -> float | None:
    ttl = body.get("ttl_s")
    if ttl is None:
        return None
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise UsageError(f"ttl_s must be a positive number of seconds, not {ttl!r}")
    return float(ttl)


def register(ctx: RouteContext) -> None:
    d = ctx.daemon

    @ctx.api.put("/boards/{bid:path}/viewers/{vid}")
    def view(bid: str, vid: str, body: JsonBody = None) -> _JSON:
        ctx.board(bid)                                   # 404 ABSENT for a closed board
        viewer = _viewer(vid)
        d.quiet.view(bid, viewer, _ttl(_obj(body)))
        return _JSON(ok(board_id=bid, viewer=viewer, background=d.background_state(bid)))

    @ctx.api.delete("/boards/{bid:path}/viewers/{vid}")
    def unview(bid: str, vid: str) -> _JSON:
        viewer = _viewer(vid)
        return _JSON(ok(board_id=bid, viewer=viewer, removed=d.quiet.unview(bid, viewer)))

    @ctx.api.get("/boards/{bid:path}/background")
    def background(bid: str) -> _JSON:
        ctx.board(bid)
        return _JSON(ok(board_id=bid, background=d.background_state(bid)))
