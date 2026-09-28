"""Concurrent identical board reads share one answer (lane SERIAL-6900).

A page's refresh burst, a second tab and the CLI can ask the service for the same board read
at the same moment (``GET /boards/{bid}``, ``/card``, ``/panel``...), and the lease view
(``/lease``: up to three one-shot ssh commands to the hub, whose sshd throttles a burst of new
connections, SERIAL-6900 4d). Each would open the
board's single-client control port once more; the board pack's per-board gate
(``harness_manager_mps3.ctlgate``) now serialises them, but the second answer would be the
same read again. So while one such GET is in flight, an IDENTICAL one (same path and query,
same token, Host and QUIET-POLL headers) waits for it and gets a copy of its answer: one read
of the board, two replies.

Only the board reads in ``SUFFIXES`` are shared, and only ``GET``: an action is never merged,
nor is a read that streams (``display.png``, console exports, the WebSockets). A read that
fails in a way that produced no answer (a dropped client) is not shared: each waiter asks for
itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger(__name__)

#: The board reads that may be shared ("" is the board itself: ``GET /boards/{bid}``).
#: ``lease`` reads the hub (one-shot ssh commands, SERIAL-6900 4d), not the board.
SUFFIXES: tuple[str, ...] = (
    "", "session", "telemetry", "card", "slots", "panel", "panel/frame", "consoles", "claim",
    "display", "overlays", "clocks", "power", "controller/temps", "controller/osc",
    "storage/pending", "xvc", "lease",
)
#: The request headers that change a read's answer (with the path and the query).
KEY_HEADERS: tuple[str, ...] = ("authorization", "host", "x-hm-background", "x-hm-viewer")

_Item = tuple[int, list[tuple[bytes, bytes]], bytes]


class GetCoalescer:
    """The middleware's state: the reads in flight, by key. One per app."""

    def __init__(self, api_prefix: str) -> None:
        self.prefix = f"{api_prefix.rstrip('/')}/boards/"
        self._inflight: dict[tuple[Any, ...], asyncio.Future[_Item | None]] = {}
        #: Replies served from another request's read (tests and the log read it).
        self.shared = 0

    def key(self, request: Request) -> tuple[Any, ...] | None:
        """The read's key, or None when it is not a shareable board read."""
        if request.method != "GET":
            return None
        path = request.url.path
        if not path.startswith(self.prefix):
            return None
        rest = path[len(self.prefix):]
        for suffix in SUFFIXES:
            if suffix:
                if not rest.endswith("/" + suffix):
                    continue
                bid = rest[:-(len(suffix) + 1)]
            else:
                bid = rest
            if bid and "/" not in bid:
                return (path, str(request.url.query),
                        *(request.headers.get(h, "") for h in KEY_HEADERS))
        return None

    async def __call__(self, request: Request,
                       call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        key = self.key(request)
        if key is None:
            return await call_next(request)
        leader = self._inflight.get(key)
        if leader is not None:
            item = await asyncio.shield(leader)
            if item is not None:
                self.shared += 1
                log.debug("shared the answer of %s", request.url.path)
                return _response(item)
            return await call_next(request)          # the first read produced nothing to share
        fut: asyncio.Future[_Item | None] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        item: _Item | None = None
        try:
            response = await call_next(request)
            body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
            item = (response.status_code, list(response.raw_headers), body)
            return _response(item)
        finally:
            self._inflight.pop(key, None)
            if not fut.done():
                fut.set_result(item)


def _response(item: _Item) -> Response:
    status, raw_headers, body = item
    response = Response(content=body, status_code=status)
    response.raw_headers = list(raw_headers)
    return response
