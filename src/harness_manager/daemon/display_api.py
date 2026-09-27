"""The Live display (LCD mirror) in the daemon API (lane LM3), loaded through ``app.EXTENSIONS``.

docs/design/LCD_MIRROR.md §7.2-§7.4 is the design; docs/API.md "Live display" the contract
(bearer auth and the error envelope as everywhere):

| Method and path | Returns |
|---|---|
| ``WS /boards/{bid}/display/ws?token=&ack=1&rate=`` | text frames: the status (first, then on every change); binary frames: one UPDATE each, in the board's own layout (the first a keyframe); the client sends ``{"ack": seq}`` after drawing, ``{"rate": hz}`` when it wants another pace |
| ``GET /boards/{bid}/display`` | ``{available, unavailable, state, reason, mode, hello, owner, flags, regs, badges, presented, hatched, seq, t_ms, frames, resets, rtt_ms, rate, rate_asked, fps, bytes_per_s, viewers, counters}`` |
| ``GET /boards/{bid}/display.png?scale=1..4&hatch=0|1&format=png|raw`` | the presented picture: ``image/png``, or with ``format=raw`` 153,600 bytes of RGB565 LE (``X-Display-Width``/``-Height``/``-Format``) |

Rules:

- **The lease holder only (D3).** The source is the board pack's ``display_adapter`` hook
  (lane LM2: ``pack.display_adapter(session)`` -> a ``DisplayAdapter`` or None; else
  ``session.display``). Its ``display_reason()`` carries the lease rule and the feature
  gate. No adapter, or a reason, is refused BEFORE anything attaches: 422 UNAVAILABLE
  (capability ``display_mirror``) over HTTP; on the WebSocket a text frame
  ``{state: "refused", reason, error}`` and a close with 4000 + the exit code (4012) and
  the reason. A missing or wrong token is 401 on every route (the WebSocket's is an HTTP
  denial, as every HM socket's).
- **Backpressure is per viewer, drop-to-latest.** ``DisplayService`` keeps each tab's dirty
  set; with ``ack=1`` (the default) a tab has ONE message in flight until it acks, and its
  next message holds the latest record of every tile it lacks. A tab that never acks gets
  nothing more and is never fed stale tiles. The events ``Outbox`` is NOT used (it drops
  the oldest frames: a dropped tile stays stale for ever, §7.3).
- **The upstream** is one per board, shared by every tab, the PNG and the CLI; it closes
  30 s after the last viewer leaves, at once when the board closes. When it ends for good
  (a lease or claim lost, the board closed) each socket gets the final status and a close
  with 1000: the client reconnects, and is checked again.
- **permessage-deflate is off** for the whole daemon (``server.uvicorn_config``).

Events: ``display.state`` ``{state, mode, owner, badges, reason}`` (docs/CONTRACTS.md), from
``DisplayService`` on the engine bus.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from fastapi import Response, WebSocket, WebSocketDisconnect

from harness_manager.core.display import DISPLAY_MIRROR, H, W, owner_name
from harness_manager.core.errors import HarnessError, UnavailableError, UsageError
from harness_manager.core.events import Event

from .app import _JSON, RouteContext, _retrieve, ok
from .wire import UNAUTHORISED, auth_error, error_body, error_object

log = logging.getLogger(__name__)

#: The board pack's hook (lane LM2): ``pack.display_adapter(session)`` -> adapter or None.
HOOK = "display_adapter"
#: ``GET display.png``: how long a first picture may take (the upstream opens, KEY, keyframe).
PICTURE_WAIT_S = 10.0
FORMATS = ("png", "raw")
RAW_FORMAT = "rgb565le"
#: A WebSocket close reason is at most 123 bytes of UTF-8 (RFC 6455 §5.5).
CLOSE_REASON_MAX = 123
#: The socket's close when the board's upstream ended for good (the console's convention).
CLOSE_ENDED = 1000
#: ``GET .../display`` always has every field: a board never opened has these.
STATUS_DEFAULTS: dict[str, Any] = {
    "flags": None, "regs": None, "seq": None, "t_ms": None, "frames": None, "resets": None,
    "rtt_ms": None, "rate": None, "rate_asked": None, "fps": 0.0, "bytes_per_s": 0,
    "counters": {}}


def _flag(value: str | None, name: str, default: bool) -> bool:
    if value is None or value == "":
        return default
    low = value.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise UsageError(f"{name} must be 0 or 1, not {value!r}")


def _whole(value: Any, name: str, lo: int, hi: int) -> int:
    """A whole number lo..hi from a query string or a JSON value (never a bool)."""
    if isinstance(value, bool):
        raise UsageError(f"{name} must be a whole number {lo}-{hi}, not {value!r}")
    try:
        n = int(value, 10) if isinstance(value, str) else value
    except ValueError:
        n = None
    if not isinstance(n, int) or not lo <= n <= hi:
        raise UsageError(f"{name} must be a whole number {lo}-{hi}, not {value!r}")
    return n


def close_reason(text: str) -> str:
    """``text`` cut to a WebSocket close reason (123 bytes of UTF-8, whole characters)."""
    return text.encode("utf-8")[:CLOSE_REASON_MAX].decode("utf-8", "ignore")


def display_source(engine: Any, session: Any) -> Any | None:
    """The board's ``DisplayAdapter``, or None: the pack's ``display_adapter(session)`` hook
    (lane LM2), else ``session.display`` (docs/design/LCD_MIRROR.md §7.1)."""
    pack = None
    try:
        pack = engine.packs().get(session.candidate.pack)
    except Exception:  # noqa: BLE001 - an engine without packs (a test's) has no hook
        pack = None
    hook = getattr(pack, HOOK, None)
    if callable(hook):
        return hook(session)
    return getattr(session, "display", None)


def refusal(adapter: Any, session: Any) -> HarnessError | None:
    """Why the live picture may not be opened now (D3: the lease holder only; the feature
    gate), as the typed error the routes answer with; None when it may.
    ``display_reason()`` may raise a ``HarnessError`` itself (a lease that cannot be read)."""
    if adapter is None:
        pack = getattr(getattr(session, "candidate", None), "pack", "") or "this"
        return UnavailableError(DISPLAY_MIRROR, f"the {pack} pack has no live display for "
                                                "this board (the Front panel's text mirror "
                                                "still shows it)")
    why = adapter.display_reason()
    if why:
        return UnavailableError(DISPLAY_MIRROR, str(why))
    return None


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    guard = threading.Lock()
    sources: dict[str, tuple[Any, Any]] = {}          # board -> (session, adapter)
    owned: list[Any] = []                             # a service this module made

    def service() -> Any:
        """The compositor: the engine's own (``engine.display``) when it has one, else one
        this daemon owns. A test sets ``daemon.display`` first (timings, a clock)."""
        with guard:
            svc = getattr(d, "display", None)
            if svc is None:
                svc = getattr(d.engine, "display", None)
                if svc is None or not callable(getattr(svc, "attach", None)):
                    from harness_manager.services.display import DisplayService

                    svc = DisplayService(d.engine)
                    owned.append(svc)
                d.display = svc
            return svc

    def source_for(bid: str, session: Any) -> Any | None:
        """The board's adapter, made once per open session (an adapter may hold a forward)."""
        with guard:
            held = sources.get(bid)
            if held is not None and held[0] is session:
                return held[1]
        adapter = display_source(d.engine, session)
        if adapter is not None:
            with guard:
                sources[bid] = (session, adapter)
        return adapter

    def checked_source(bid: str) -> Any:
        """The board's adapter once it may be opened; else the typed refusal is raised."""
        session = ctx.board(bid)                      # 404 ABSENT: not open
        adapter = source_for(bid, session)
        err = refusal(adapter, session)
        if err is not None:
            raise err
        return adapter

    # -- the board closes: its upstream closes at once ----------------------------------------

    def closed(ev: Event) -> None:
        with guard:
            sources.pop(ev.board_id, None)
            svc = getattr(d, "display", None)
        if svc is not None:
            svc.close(ev.board_id, "the board was closed")

    d.bus.subscribe("session.closed", closed)
    original_close = d.close

    def close() -> None:
        for svc in owned:
            try:
                svc.shutdown()
            except Exception:  # noqa: BLE001 - closing the daemon must finish
                log.exception("the display service failed to shut down")
        original_close()

    d.close = close

    # -- HTTP -----------------------------------------------------------------------------------

    @api.get("/boards/{bid:path}/display.png")
    def display_png(bid: str, scale: str | None = None, hatch: str | None = None,
                    format: str | None = None) -> Response:  # noqa: A002 - the query's name
        fmt = (format or "png").strip().lower()
        if fmt not in FORMATS:
            raise UsageError(f"format must be one of {', '.join(FORMATS)}, not {format!r}")
        k = 1 if scale in (None, "") else _whole(scale, "scale", 1, 4)
        hatched = _flag(hatch, "hatch", False)
        if fmt == "raw" and (k != 1 or hatched):
            raise UsageError("format=raw is the panel's own pixels: no scale or hatch",
                             hint="leave scale and hatch out, or ask for format=png")
        adapter = checked_source(bid)
        pic = service().picture(bid, adapter, wait_s=PICTURE_WAIT_S)
        headers = {"X-Display-Width": str(W), "X-Display-Height": str(H),
                   "X-Display-Seq": str(pic.seq), "X-Display-Hatched": str(len(pic.hatched)),
                   "X-Display-Owner": owner_name(pic.owner)}
        if fmt == "raw":
            headers["X-Display-Format"] = RAW_FORMAT
            return Response(pic.rgb565, media_type="application/octet-stream", headers=headers)
        headers["X-Display-Scale"] = str(k)
        return Response(pic.png(scale=k, hatch=hatched), media_type="image/png",
                        headers=headers)

    @api.get("/boards/{bid:path}/display")
    def display_status(bid: str) -> Any:
        session = ctx.board(bid)
        try:
            err = refusal(source_for(bid, session), session)
        except HarnessError as exc:                   # the lease could not be read: say so
            err = exc
        why = "" if err is None else getattr(err, "reason", "") or err.message
        return _JSON(ok(board_id=bid, available=err is None, unavailable=why,
                        **{**STATUS_DEFAULTS, **service().status(bid)}))

    # -- the WebSocket --------------------------------------------------------------------------

    async def deny(websocket: WebSocket, exc: HarnessError, status: int) -> None:
        response = _JSON(error_body(exc), status_code=status)
        try:
            await websocket.send_denial_response(response)
        except RuntimeError:        # a server without the denial-response extension
            await websocket.close(code=4000 + int(exc.code), reason=close_reason(exc.message))

    async def refuse(websocket: WebSocket, exc: HarnessError) -> None:
        """After the handshake: say why in a text frame, then close 4000 + the exit code."""
        reason = getattr(exc, "reason", "") or exc.message
        try:
            await websocket.send_text(json.dumps({"state": "refused", "reason": reason,
                                                  "error": error_object(exc)}, default=str))
            await websocket.close(code=4000 + int(exc.code), reason=close_reason(reason))
        except (RuntimeError, OSError, WebSocketDisconnect):
            pass                                      # the client left first

    def open_viewer(bid: str, ack: bool, rate: int | None, wake: Any) -> Any:
        adapter = checked_source(bid)
        return service().attach(bid, adapter, ack=ack, rate=rate, wake=wake)

    @ctx.wsr.websocket("/boards/{bid:path}/display/ws")
    async def display_ws(websocket: WebSocket, bid: str) -> None:
        if not d.check_token(websocket.query_params.get("token")):
            await deny(websocket, auth_error(), UNAUTHORISED)
            return
        await websocket.accept()
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()

        def poke() -> None:                           # any thread: the viewer has news
            loop.call_soon_threadsafe(wake.set)

        try:
            q = websocket.query_params
            ack = _flag(q.get("ack"), "ack", True)
            rate = None if q.get("rate") in (None, "") else _whole(q.get("rate"), "rate", 0, 255)
            viewer = await asyncio.to_thread(open_viewer, bid, ack, rate, poke)
        except HarnessError as exc:
            await refuse(websocket, exc)
            return
        notes: list[str] = []                         # error frames for the sender to send

        async def sender() -> None:
            wake.set()                                # the first status, and a ready picture
            while True:
                await wake.wait()
                wake.clear()
                while notes:
                    await websocket.send_text(notes.pop(0))
                st = viewer.next_status()
                if st is not None:
                    await websocket.send_text(json.dumps(st, default=str))
                msg = viewer.next_message()
                if msg is not None:
                    await websocket.send_bytes(msg)
                if viewer.ended:                      # the last word, once, then the close
                    st = viewer.next_status()
                    if st is not None:
                        await websocket.send_text(json.dumps(st, default=str))
                    why = viewer.status().get("reason") or "closed"
                    await websocket.close(code=CLOSE_ENDED, reason=close_reason(why))
                    return

        def note(exc: HarnessError) -> None:
            notes.append(json.dumps({"error": error_object(exc)}, default=str))
            wake.set()

        async def receiver() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                text = message.get("text")
                if text is None:                      # binary from the client: reserved
                    continue
                try:
                    body = json.loads(text)
                    if not isinstance(body, dict):
                        raise UsageError("a display socket takes {\"ack\": seq} or "
                                         "{\"rate\": hz}")
                    if "ack" in body:
                        _whole(body["ack"], "ack", 0, 0xFFFFFFFF)
                        viewer.ack(body["ack"])
                    if "rate" in body:
                        viewer.set_rate(_whole(body["rate"], "rate", 0, 255))
                except ValueError:
                    note(UsageError("a display socket's text frames are JSON",
                                    hint='{"ack": seq} after drawing; {"rate": hz}'))
                except HarnessError as exc:
                    note(exc)

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        for task in tasks:
            task.add_done_callback(_retrieve)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            viewer.close()                            # quick, thread-safe: the grace starts
            current = asyncio.current_task()
            cancelling = getattr(current, "cancelling", None)
            if current is None or cancelling is None or not cancelling():
                await asyncio.wait(tasks)
