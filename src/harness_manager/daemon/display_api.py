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
  (lane LM2: ``BoardPack.display_adapter(session)`` -> a ``DisplayAdapter`` or None; else
  ``session.display``). It shares the hub API's lease service (``use_leases(d.leases)``,
  before its first ``display_reason()``), and so does the compositor (``svc.leases``), as
  XVC's does. ``display_reason()`` carries the feature gate, the lease rule and the claim.
  No adapter, or a reason, is refused BEFORE anything attaches, in this order (``refusal``):
  a board that can NEVER show it (no adapter, or the adapter's ``display_gate()``: the
  bare-metal harness, an image without ``lcd_mirror``) is 422 UNAVAILABLE whoever holds the
  lease; then 409 HELD naming the ``holder`` when the board is behind a hub whose lease is
  not this client's (the hub API's view); then 422 UNAVAILABLE (capability
  ``display_mirror``) for the claim or the reach; on the WebSocket a text
  frame ``{state: "refused", reason, error}`` and a close with 4000 + the exit code (4004,
  4012) and the reason. A missing or wrong token is 401 on every route (the WebSocket's is
  an HTTP denial, as every HM socket's).
- **Backpressure is per viewer, drop-to-latest.** ``DisplayService`` keeps each tab's dirty
  set; with ``ack=1`` (the default) a tab has ONE message in flight until it acks, and its
  next message holds the latest record of every tile it lacks. A tab that never acks gets
  nothing more and is never fed stale tiles. The events ``Outbox`` is NOT used (it drops
  the oldest frames: a dropped tile stays stale for ever, §7.3).
- **The upstream** is one per board, shared by every tab, the PNG and the CLI; it closes
  30 s after the last viewer leaves, at once when the board closes. When it ends for good
  (a lease or claim lost, the board closed) each socket gets the final status and a close:
  4000 + the exit code when the source's own error ended it (``HeldError``: 4004), else
  1000; the client reconnects, and is checked again. A PNG whose upstream ended that way
  answers with that error (409 HELD naming the holder).
- **permessage-deflate is off** for the whole daemon (``server.uvicorn_config``).

Events: ``display.state`` ``{state, mode, owner, badges, reason}`` (docs/CONTRACTS.md), from
``DisplayService`` on the engine bus.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from typing import Any

from fastapi import Response, WebSocket, WebSocketDisconnect

from harness_manager.core.display import DISPLAY_MIRROR, H, W, owner_name
from harness_manager.core.errors import HarnessError, HeldError, UnavailableError, UsageError
from harness_manager.core.events import Event

from .app import _JSON, RouteContext, _retrieve, ok
from .wire import UNAUTHORISED, auth_error, error_body, error_object

log = logging.getLogger(__name__)

#: The board pack's hook (lane LM2): ``pack.display_adapter(session)`` -> adapter or None.
HOOK = "display_adapter"
#: The adapter's optional hook: why the board can NEVER show the live display as it is now
#: (the feature gate: the bare-metal harness, an image without ``lcd_mirror``), else "".
GATE_HOOK = "display_gate"
#: The adapter's optional hook (FIX-PACK-1): drop its cached facts and its forward, because
#: the board may run another image now.
FORGET_HOOK = "display_forget"
#: The events that mean it (``board.claim`` too, when its host key no longer matches).
FORGET_ON = {"controller.reboot": "the board rebooted (MCC REBOOT)",
             "power.cycle": "the board was power cycled",
             "session.opened": "the board's session was opened again"}
#: ... and the daemon's jobs that reboot the board or change its image (as they start and end).
FORGET_JOBS = {"reboot": "the board rebooted (MCC REBOOT)",
               "power_cycle": "the board was power cycled",
               "sd_install": "the board's SD card was written",
               "sd_restore": "the board's SD card was restored",
               "update_harness": "the harness image was updated",
               "update_rollback": "the harness image was rolled back",
               "harness_rollback": "the harness image was rolled back"}
#: ``GET display.png``: how long a first picture may take (the upstream opens, KEY, keyframe).
PICTURE_WAIT_S = 10.0
FORMATS = ("png", "raw")
RAW_FORMAT = "rgb565le"
#: A WebSocket close reason is at most 123 bytes of UTF-8 (RFC 6455 §5.5).
CLOSE_REASON_MAX = 123
#: A display socket keeps at most this many unsent error frames (the latest ones).
NOTES_MAX = 8
#: The socket's close when the board's upstream ended for good with no error of the source's
#: own (the console's convention); with one, 4000 + its exit code.
CLOSE_ENDED = 1000
#: The reason ``session.closed`` gives (the compositor's own words for it, lane LM2).
BOARD_CLOSED = "closed: the board was closed"
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


class Notes:
    """The error frames a display socket owes its client, the newest ``NOTES_MAX`` only: a
    client that floods bad text frames and never reads cannot grow it (REVIEW-W5 9). One
    event loop adds and takes: no lock."""

    def __init__(self, cap: int = NOTES_MAX) -> None:
        self._q: deque[str] = deque(maxlen=cap)

    def add(self, exc: HarnessError) -> None:
        self._q.append(json.dumps({"error": error_object(exc)}, default=str))

    def take(self) -> list[str]:
        out = list(self._q)
        self._q.clear()
        return out

    def __len__(self) -> int:
        return len(self._q)


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


def lease_holder(leases: Any, session: Any) -> str | None:
    """Who holds the board's lease when it is NOT this client (``"nobody"`` when no one
    does), from the hub API's lease view; None when it is ours, the board has no hub, or
    the view cannot be read (the adapter's reason then stands as UNAVAILABLE)."""
    hub = getattr(session, "hub", None)
    if hub is None or leases is None:
        return None
    try:
        view = leases.view(hub) or {}
    except HarnessError:
        return None
    lease = view.get("lease")
    if not lease:
        return "nobody"
    return None if lease.get("mine") else str(lease.get("holder") or "someone else")


def gate_reason(adapter: Any) -> str:
    """Why the board can NEVER show the live display as it is now (the adapter's optional
    ``display_gate()``: the bare-metal harness, an image without ``lcd_mirror``), else "".
    An adapter without the hook has no gate of its own: its ``display_reason()`` stands."""
    gate = getattr(adapter, GATE_HOOK, None)
    if not callable(gate):
        return ""
    return str(gate() or "")


def refusal(adapter: Any, session: Any, leases: Any = None) -> HarnessError | None:
    """Why the live picture may not be opened now, as the typed error the routes (and the
    CLI's ``display``) answer with; None when it may. In the order a user fixes them:

    1. the board can NEVER show it (no adapter, or ``gate_reason``: the bare-metal harness,
       an image without ``lcd_mirror``): 422 UNAVAILABLE, even when someone else holds the
       lease (taking the lease would not help);
    2. the adapter's ``display_reason()`` says no and the board's lease is someone else's
       (``lease_holder``): 409 HELD naming the holder;
    3. any other reason (the claim, the reach): 422 UNAVAILABLE.

    ``display_gate()`` and ``display_reason()`` may raise a ``HarnessError`` themselves."""
    if adapter is None:
        pack = getattr(getattr(session, "candidate", None), "pack", "") or "this"
        return UnavailableError(DISPLAY_MIRROR, f"the {pack} pack has no live display for "
                                                "this board (the Front panel's text mirror "
                                                "still shows it)")
    use = getattr(adapter, "use_leases", None)
    if leases is not None and callable(use):
        use(leases)                                   # the hub API's view of "mine" (LM2)
    never = gate_reason(adapter)
    if never:
        return UnavailableError(DISPLAY_MIRROR, never)
    why = adapter.display_reason()
    if not why:
        return None
    holder = lease_holder(leases, session)
    if holder is not None:
        err = HeldError(str(why), holder=holder,
                        hint="the live display opens for the lease holder only (as XVC): "
                             "`harness-manager lease request TARGET`")
        err.reason = str(why)                         # type: ignore[attr-defined]
        err.data = {"capability": DISPLAY_MIRROR}     # type: ignore[attr-defined]
        return err
    return UnavailableError(DISPLAY_MIRROR, str(why))


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
            # Share hub_api's lease service (one cache, one view of "mine"), as xvc_api does.
            if getattr(svc, "leases", "absent") is None and getattr(d, "leases", None) is not None:
                svc.leases = d.leases
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
        err = refusal(adapter, session, getattr(d, "leases", None))
        if err is not None:
            raise err
        return adapter

    # -- the board closes: its upstream closes at once ----------------------------------------

    def closed(ev: Event) -> None:
        with guard:
            sources.pop(ev.board_id, None)
            svc = getattr(d, "display", None)
        if svc is not None:
            svc.close(ev.board_id, BOARD_CLOSED)

    d.bus.subscribe("session.closed", closed)

    # -- FIX-PACK-1: the board may run another image: drop what the adapter knew, re-gate ------

    jobs: dict[str, str] = {}                         # a running board-changing job -> why
    forgets: list[threading.Thread] = []              # the forget workers (tests join them)
    d.display_forgets = forgets

    def forget(ev: Event) -> None:
        """A reboot, a power cycle, a new image or a changed SSH host key: the adapter's
        cached facts (and its forward) go, so the next open re-gates on a fresh read
        (``display_forget``)."""
        data = ev.data or {}
        why = FORGET_ON.get(ev.topic, "")
        if ev.topic == "board.claim":
            host_key = data.get("host_key")
            if not (isinstance(host_key, dict) and host_key.get("match") is False):
                return                                # a claim refresh with the same key
            why = "the board's SSH host key changed"
        elif ev.topic == "job.started":
            why = FORGET_JOBS.get(str(data.get("kind") or ""), "")
            if not why:
                return
            with guard:
                jobs[str(data.get("job") or "")] = why
        elif ev.topic in ("job.done", "job.failed"):
            with guard:
                why = jobs.pop(str(data.get("job") or ""), "")
            if not why:
                return
        with guard:
            held = sources.get(ev.board_id)
        fn = getattr(held[1], FORGET_HOOK, None) if held is not None else None
        if not callable(fn):
            return

        def run() -> None:
            try:
                fn(why)                                # closes the forward: never on the bus
            except Exception:  # noqa: BLE001 - a worker never dies loudly
                log.exception("the display adapter of %s failed to forget", ev.board_id)

        worker = threading.Thread(target=run, daemon=True, name=f"display-forget-{ev.board_id}")
        forgets.append(worker)
        worker.start()

    def identity(ev: Event) -> None:
        """The engine noted the new identity on the adapter (``engine.info``): a board that can
        no longer show the mirror (the image lost ``lcd_mirror``) ends its upstream with that
        reason now, instead of reconnecting to a service that is not there."""
        with guard:
            held = sources.get(ev.board_id)
            svc = getattr(d, "display", None)
        if held is None or svc is None or ev.board_id not in svc.boards():
            return
        try:
            gate = gate_reason(held[1])
        except HarnessError:
            return
        if gate:
            svc.close_soon(ev.board_id, gate)

    for topic in (*FORGET_ON, "board.claim", "job.started", "job.done", "job.failed"):
        d.bus.subscribe(topic, forget)
    d.bus.subscribe("board.identity", identity)
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
            err = refusal(source_for(bid, session), session, getattr(d, "leases", None))
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
        notes = Notes()                               # error frames for the sender to send

        async def sender() -> None:
            wake.set()                                # the first status, and a ready picture
            while True:
                await wake.wait()
                wake.clear()
                for text in notes.take():
                    await websocket.send_text(text)
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
                    err = viewer.end_error
                    code = CLOSE_ENDED if err is None else 4000 + int(err.code)
                    await websocket.close(code=code, reason=close_reason(why))
                    return

        def note(exc: HarnessError) -> None:
            notes.add(exc)
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
