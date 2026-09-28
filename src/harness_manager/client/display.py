"""The Live display (LCD mirror) over harness-manager-daemon's API, for the CLI's ``display`` verb.

``docs/API.md`` "Live display" is the contract (``daemon/display_api.py`` serves it):

- ``RemoteDisplay.status(session)``: ``GET /boards/{bid}/display``, the whole status dict
  (``available``, ``unavailable``, ``state``, ``mode``, ``owner``, ``flags``, ``badges``, ...);
- ``RemoteDisplay.still(session, fmt=, scale=, hatch=)``: ``GET .../display.png``, the PNG (or
  with ``fmt="raw"`` the panel's 153,600 bytes of RGB565 LE) and its ``X-Display-*`` headers;
- ``RemoteDisplay.view(session, rate=)``: a ``RemoteDisplayView`` on ``WS .../display/ws``: it
  draws each UPDATE into a ``PictureModel`` and acks it after drawing, so the daemon sends
  this view only the latest tiles it lacks, at the pace it draws (drop-to-latest).

A refusal is the route's typed error, raised here: 409 HELD naming the lease holder, 422
UNAVAILABLE with the reason (the Linux harness with lcd_mirror is missing, no claim, ...);
on the socket, the ``{"state": "refused", "error"}`` frame. ``PictureModel`` is the viewer's
side of the wire (``core.display_wire``); the CLI's in-process view feeds it the in-process
compositor's messages, so both paths draw the same bytes the same way.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Any

from harness_manager.core import display_wire as w
from harness_manager.core.display import DISPLAY_MIRROR
from harness_manager.core.errors import HarnessError, UnavailableError, UnreachableError

from .codec import error_from_json
from .http import q

log = logging.getLogger(__name__)

#: The PNG route waits up to 30 s for a keyframe (``display_api.PICTURE_WAIT_S``, the SSH
#: forward coming up included); the request a little more, so the route's own error arrives.
STILL_TIMEOUT_S = 45.0
#: How far back ``fps`` counts drawn UPDATEs.
FPS_WINDOW_S = 2.0
#: A WebSocket close code 4000 + an exit code carries the source's own error (``display_api``).
CLOSE_ERROR_BASE = 4000
#: The socket's first frame is the status, or the refusal: how long a new view waits for it.
FIRST_FRAME_S = 10.0


def _board_id(session: Any) -> str:
    return str(session.candidate.board_id)


class PictureModel:
    """What one viewer holds: each viewer UPDATE (the board's own layout, all five
    encodings, ``core.display_wire``) applied to a row-major 320x240 RGB565 LE picture."""

    def __init__(self) -> None:
        self.frame = bytearray(w.FRAME_BYTES)
        self.valid: frozenset[int] = frozenset()
        self.seq: int | None = None
        self.status = 0
        self.owner = w.OWNER_UNKNOWN
        self.messages = 0
        self.tiles = 0
        self._drawn: deque[float] = deque()

    def apply(self, msg: bytes) -> int:
        """Draw one UPDATE; returns its ``seq`` (what the viewer acks)."""
        if len(msg) < w.HEADER_SIZE:
            raise w.WireError(f"a viewer message of {len(msg)} B is shorter than its header")
        magic, typ, _rsvd, ln = w.HEADER.unpack_from(msg)
        if magic != w.MAGIC or typ != w.T_UPDATE or ln != len(msg) - w.HEADER_SIZE:
            raise w.WireError("a viewer message is not one whole UPDATE")
        u = w.parse_update(bytes(msg[w.HEADER_SIZE:]))
        for rec in u.tiles:
            w.put_tile_in_frame(self.frame, rec.idx, w.decode_tile(rec.enc, rec.payload))
        self.valid = w.valid_tiles(u.valid)
        self.seq, self.status, self.owner = u.seq, u.status, u.owner
        self.messages += 1
        self.tiles += len(u.tiles)
        now = time.monotonic()
        self._drawn.append(now)
        while self._drawn and now - self._drawn[0] > FPS_WINDOW_S:
            self._drawn.popleft()
        return u.seq

    @property
    def presented(self) -> bool:
        return self.messages > 0

    @property
    def hatched(self) -> frozenset[int]:
        """The tiles the picture does not know (VALID=0): drawn hatched."""
        return frozenset(range(w.NTILES)) - self.valid

    @property
    def flags(self) -> w.StatusFlags:
        return w.StatusFlags(self.status)

    def fps(self) -> float:
        """UPDATEs drawn per second over the last ``FPS_WINDOW_S``."""
        now = time.monotonic()
        return round(sum(1 for t in self._drawn if now - t <= FPS_WINDOW_S) / FPS_WINDOW_S, 1)

    def rgb565(self) -> bytes:
        return bytes(self.frame)


def close_error(code: int, reason: str) -> HarnessError:
    """The error a display socket's close stands for: 4000 + an exit code is the source's own
    error; any other close means the live display ended with ``reason``."""
    why = reason or "the live display closed"
    if CLOSE_ERROR_BASE < code < CLOSE_ERROR_BASE + 100:
        return error_from_json({"code": code - CLOSE_ERROR_BASE, "message": why,
                                "capability": DISPLAY_MIRROR, "reason": why})
    if code == 1000:
        return UnavailableError(DISPLAY_MIRROR, why)
    return UnreachableError(f"the live display socket closed (code {code}): {why}",
                            hint="`harness-manager daemon status` checks the daemon")


class RemoteDisplayView:
    """A live view over ``WS /boards/{bid}/display/ws?ack=1&rate=``. ``pump`` takes what
    arrives, draws each UPDATE into ``model`` and acks it; ``status()`` is the latest status
    frame. A refusal or the end of the upstream is raised from ``pump`` as its typed error."""

    def __init__(self, engine: Any, board_id: str, *, rate: int) -> None:
        from .remote import ws_connect

        self.board_id = board_id
        self.model = PictureModel()
        self._status: dict[str, Any] = {"board": board_id, "state": "connecting", "reason": ""}
        self._closed = False
        url = engine.http.ws_url(f"/boards/{q(board_id)}/display/ws", ack="1", rate=str(rate))
        self._ws = ws_connect(url)
        self._heard = False
        try:
            self._first(FIRST_FRAME_S)
        except BaseException:
            self.close()
            raise

    def _first(self, timeout: float) -> None:
        """Wait for the first text frame (the status, or the route's refusal, raised here)."""
        deadline = time.monotonic() + timeout
        while not self._heard and time.monotonic() < deadline:
            self.pump(min(0.1, max(0.0, deadline - time.monotonic())))

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def _text(self, text: str) -> None:
        try:
            body = json.loads(text)
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        err = body.get("error")
        if isinstance(err, dict):
            self._heard = True
            if body.get("state") == "refused":           # the route refused this viewer
                self._status = {**self._status, "state": "refused",
                                "reason": str(body.get("reason") or "")}
                raise error_from_json(err)
            log.debug("the display socket rejected a frame of ours: %s", err)
            return
        if "state" in body:
            self._status = body
            self._heard = True

    def pump(self, timeout: float) -> bool:
        """Take every message that arrives within ``timeout``; True when the picture changed."""
        from websockets.exceptions import ConnectionClosed

        changed = False
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._closed:
            left = deadline - time.monotonic()
            try:
                msg = self._ws.recv(timeout=max(0.0, left))
            except TimeoutError:
                return changed
            except ConnectionClosed as exc:
                self._closed = True
                rcvd = exc.rcvd
                code, reason = (rcvd.code, rcvd.reason) if rcvd is not None else (1006, "")
                raise close_error(code, reason or str(self._status.get("reason") or "")) \
                    from None
            if isinstance(msg, str):
                self._text(msg)
                continue
            seq = self.model.apply(bytes(msg))
            changed = True
            try:
                self._ws.send(json.dumps({"ack": seq}))
            except ConnectionClosed:
                pass                                     # the close is read next time round
        return changed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - closing a view never fails the verb
            log.debug("closing the display socket failed", exc_info=True)

    def __enter__(self) -> RemoteDisplayView:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class RemoteDisplay:
    """The daemon's Live display routes (the module docstring)."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def _path(self, session: Any, leaf: str = "") -> str:
        return f"/boards/{q(_board_id(session))}/display{leaf}"

    def status(self, session: Any) -> dict[str, Any]:
        payload = self._engine.http.get(self._path(session))
        return {k: v for k, v in payload.items() if k != "ok"}

    def still(self, session: Any, *, fmt: str = "png", scale: int = 1,
              hatch: bool = False) -> tuple[bytes, dict[str, str]]:
        query = "?format=raw" if fmt == "raw" else f"?scale={int(scale)}&hatch={int(bool(hatch))}"
        return self._engine.http.get_bytes(self._path(session, ".png") + query,
                                           timeout=STILL_TIMEOUT_S)

    def view(self, session: Any, *, rate: int) -> RemoteDisplayView:
        return RemoteDisplayView(self._engine, _board_id(session), rate=rate)


__all__ = ["PictureModel", "RemoteDisplay", "RemoteDisplayView", "close_error"]
