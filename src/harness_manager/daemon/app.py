"""The harness-manager-daemon FastAPI application: every endpoint in docs/API.md.

``create_app(engine, token=...)`` wraps an ``Engine`` it does not own (the
caller closes it). The server module builds the real one; tests pass an
engine pointed at ``VirtualMps3``.

Threading. The engine is synchronous, so every REST endpoint is a plain
``def``: FastAPI runs it on its worker threads and the event loop is never
blocked. Long operations are jobs (``jobs.JobManager``). Engine events reach
WebSockets through ``outbox.Outbox`` queues, which never block the thread
that publishes.

Routing. Board ids may contain ``/`` (a USB-only board is ``mps3@usb:/dev/...``),
so the board routes use a ``path`` converter and are registered most-specific
first; the bare ``/boards/{bid}`` routes come last.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import importlib
import importlib.util
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from harness_manager import __version__
from harness_manager.cli.output import reading_json
from harness_manager.client.codec import from_json
from harness_manager.core import capabilities as C
from harness_manager.core.errors import (
    AbsentError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.pack import (
    BoardSession,
    OverlayRef,
    ProbeHints,
    card_status_of,
    keep_refusal,
    preflight_refusal,
)
from harness_manager.services import reset_guard
from harness_manager.services.quiet import (
    VIEWER_HEADER,
    BackgroundGate,
    Quiet,
    is_contention,
    lease_elsewhere,
    lease_not_mine,
    policy_for,
    request_is_background,
)

from . import configured
from .jobs import BoardGates, Job, JobManager, busy_error
from .outbox import Batch, Outbox
from .wire import (
    UNAUTHORISED,
    auth_error,
    encode_event,
    error_body,
    error_object,
    http_status,
    ok,
    owner_json,
    parse_topics,
    wants,
)

log = logging.getLogger(__name__)

API = "/api/v1"
ADAPTERS = ("deploy", "consoles", "debug", "resets", "clocks", "telemetry", "controller",
            "storage", "power", "panel", "os_slots", "card")
#: WebSocket frames: consoles send board bytes in frames of at most this size.
MAX_WS_FRAME = 64 * 1024

PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Harness Manager</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem">
<h1>SoC Labs Harness Manager</h1>
<p>The web UI is not installed in this build.</p>
<p>harness-manager-daemon is running: the API is at <code>/api/v1</code> (docs/API.md), and the
<code>harness-manager</code> CLI uses it automatically.</p>
</body></html>
"""


#: A request body: any JSON (validated by the endpoint, so a bad one is a USAGE error).
JsonBody = Annotated[Any, Body()]


#: Extension router modules, loaded in this order if present (docs/API.md).
EXTENSIONS = ("consoles_api", "hub_api", "power_api", "update_api", "xdc_api", "panel_api",
              "kit_api", "xvc_api", "harness_api", "settings_api", "claim_api", "card_api",
              "hubs_api", "display_api",   # SET-UI: Settings > Hubs; LM3: the Live display
              "quiet_api",                 # QUIET-POLL: viewers and the background gate
              "identity_api")              # BOARD-ID: label/IP/MAC and the fix


@dataclass
class RouteContext:
    """What an extension router gets: the daemon and the helpers the core routes use.

    Module-level helpers (``_JSON``, ``ok``, ``_obj``, ``_str``, ``_number``,
    ``_bool``, ``_abs_path``, ``JsonBody``) are imported from this module directly.
    """

    daemon: Any
    api: Any                          # APIRouter under /api/v1, bearer auth applied
    wsr: Any                          # WebSocket router under /api/v1 (auth per socket)
    board: Callable[[str], Any]       # board id -> open BoardSession (AbsentError if not)
    require: Callable[..., Any]       # (session, attr, capability) -> adapter or UnavailableError
    accepted: Callable[[Any], Any]    # Job -> 202 {ok, job}


class _JSON(JSONResponse):
    """JSON that never fails to render: unknown objects become strings."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"),
                          default=str).encode("utf-8")


class _Unauthorised(Exception):
    pass


class _Quiet(Exception):
    """QUIET-POLL: a background read the gate held back; ``body`` is the whole answer."""

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__(body.get("quiet", {}).get("text", "quiet"))
        self.body = body


# --- static files ---------------------------------------------------------------------------


def find_static_dir() -> Path | None:
    """``<harness_manager.web package>/static`` if it holds an ``index.html`` (Team T14's UI)."""
    try:
        spec = importlib.util.find_spec("harness_manager.web")
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    for location in spec.submodule_search_locations or ():
        static = Path(location) / "static"
        if (static / "index.html").is_file():
            return static
    return None


# --- events -----------------------------------------------------------------------------------


class _EventClient:
    def __init__(self, topics: tuple[str, ...], outbox: Outbox) -> None:
        self.topics = topics
        self.outbox = outbox


class EventHub:
    """One subscription on the engine bus, fanned out to every events WebSocket."""

    def __init__(self, bus: EventBus) -> None:
        self._mu = threading.Lock()
        self._clients: set[_EventClient] = set()
        self._unsub = bus.subscribe("*", self._on_event)

    def _on_event(self, ev: Event) -> None:
        with self._mu:
            targets = [c for c in self._clients if wants(c.topics, ev.topic)]
        if not targets:
            return
        frame = encode_event(ev)
        for client in targets:
            client.outbox.put(frame)

    def add(self, client: _EventClient) -> None:
        with self._mu:
            self._clients.add(client)

    def remove(self, client: _EventClient) -> None:
        with self._mu:
            self._clients.discard(client)

    @property
    def clients(self) -> int:
        with self._mu:
            return len(self._clients)

    def close(self) -> None:
        self._unsub()
        with self._mu:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.outbox.close()


def dropped_event_frame(batch: Batch) -> str:
    return json.dumps({"topic": "events.dropped", "board_id": "",
                       "data": {"dropped": batch.dropped_items}, "at": time.time()})


def dropped_console_frame(batch: Batch) -> str:
    return json.dumps({"dropped": batch.dropped_bytes, "dropped_frames": batch.dropped_items})


# --- consoles ---------------------------------------------------------------------------------


class ConsoleBridge:
    """Board bytes from a ``ConsoleStream`` into an ``Outbox``, on a thread of its own.

    ``stream.read`` is blocking, so a thread pumps it; it never blocks on the
    socket (the outbox drops the oldest bytes instead). Console state changes
    from the engine bus become ``{"state": ...}`` text frames.
    """

    def __init__(self, stream: Any, outbox: Outbox, bus: EventBus, board_id: str,
                 key: str, name: str) -> None:
        self.stream = stream
        self.outbox = outbox
        self.board_id = board_id
        self.key = key
        self.name = name
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name=f"harness-manager-daemon-console-{name}")
        self._unsub = bus.subscribe("console.state", self._on_state)

    def start(self) -> None:
        self._thread.start()

    def _on_state(self, ev: Event) -> None:
        if ev.board_id != self.board_id or ev.data.get("name") not in (self.key, self.name):
            return
        self.outbox.put(json.dumps({"state": ev.data.get("state", ""), "name": self.name,
                                    "detail": ev.data.get("detail", "")}))

    def _pump(self) -> None:
        seen = 0
        while not self._stop.is_set():
            try:
                data = self.stream.read(timeout=0.2)
            except Exception:  # noqa: BLE001 - a broken stream ends this socket, nothing else
                log.exception("console %s of %s failed", self.name, self.board_id)
                data, closed = b"", True
            else:
                closed = bool(getattr(self.stream, "closed", False))
            dropped = int(getattr(self.stream, "dropped", 0) or 0)
            if dropped > seen:
                self.outbox.note_dropped(nbytes=dropped - seen)
                seen = dropped
            if data:
                self.outbox.put(bytes(data))
            elif closed:
                if not self._stop.is_set():
                    self.outbox.put(json.dumps({"state": "closed", "name": self.name,
                                                "detail": "the console was closed"}))
                    self.outbox.close()
                return

    def close(self) -> None:
        self._stop.set()
        self._unsub()
        try:
            self.stream.close()
        except Exception:  # noqa: BLE001
            log.exception("closing console %s of %s failed", self.name, self.board_id)
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)


def _frames(items: list, coalesce: bool) -> Iterator[str | bytes]:
    """Frames in order; consecutive byte chunks joined up to ``MAX_WS_FRAME``."""
    pending = bytearray()
    for item in items:
        if isinstance(item, (bytes, bytearray)) and coalesce:
            pending += item
            while len(pending) >= MAX_WS_FRAME:
                yield bytes(pending[:MAX_WS_FRAME])
                del pending[:MAX_WS_FRAME]
            continue
        if pending:
            yield bytes(pending)
            pending.clear()
        yield item
    if pending:
        yield bytes(pending)


async def _serve(websocket: WebSocket, outbox: Outbox, *,
                 on_receive: Callable[[dict[str, Any]], Awaitable[None]],
                 dropped_frame: Callable[[Batch], str], coalesce: bool = False) -> None:
    """Run one accepted WebSocket until either side ends it."""

    async def sender() -> None:
        while True:
            batch = await outbox.take()
            if batch.dropped_items or batch.dropped_bytes:
                await websocket.send_text(dropped_frame(batch))
            for frame in _frames(batch.items, coalesce):
                if isinstance(frame, (bytes, bytearray)):
                    await websocket.send_bytes(bytes(frame))
                else:
                    await websocket.send_text(frame)
            if batch.closed:
                await websocket.close(code=1000)
                return

    async def receiver() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            await on_receive(message)

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    for task in tasks:
        task.add_done_callback(_retrieve)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        current = asyncio.current_task()
        # Never await while this task is itself being cancelled (the server is closing
        # the connection): a second await here would swallow part of the cancellation.
        # Task.cancelling() is Python 3.11+; 3.10 cannot tell, and awaits.
        cancelling = getattr(current, "cancelling", None)
        if current is None or cancelling is None or not cancelling():
            await asyncio.wait(tasks)


def _retrieve(task: asyncio.Task) -> None:
    """Consume a finished socket task's exception so asyncio never reports it as lost."""
    if not task.cancelled() and task.exception() is not None:
        log.debug("websocket ended: %r", task.exception())


# --- the application ---------------------------------------------------------------------------


class Daemon:
    """What the endpoints share: the engine, the token, jobs, gates and the event hub."""

    def __init__(self, engine: Any, *, token: str, state_dir: Path | None,
                 shutdown: Callable[[], None] | None, event_limits: tuple[int, int],
                 console_limits: tuple[int, int]) -> None:
        self.engine = engine
        self.token = token
        self.state_dir = Path(state_dir) if state_dir is not None else Path(
            getattr(engine, "state_dir", Path.cwd()))
        self.bus: EventBus = engine.bus
        self.gates = BoardGates()
        self.jobs = JobManager(self.bus, self.gates)
        self.hub = EventHub(self.bus)
        self.shutdown = shutdown
        self.event_limits = event_limits
        self.console_limits = console_limits
        self._mu = threading.Lock()
        self._candidates: dict[str, Candidate] = {}
        self._unlog = self.bus.subscribe("*", _log_event)
        # QUIET-POLL (services/quiet.py): background contact with a board happens only while
        # a UI views it, never while its hub lease is someone else's, never under policy
        # "off", and backs off when the board turns a connection away. The demo's boards are
        # scripted (``fake_boards``): its gate says yes to everything.
        self.quiet = BackgroundGate(policy_of=self._poll_policy, lease_of=self._lease_elsewhere,
                                    enabled=not getattr(engine, "fake_boards", False))
        self._unquiet = [self.bus.subscribe("session.opened", self._quiet_opened),
                         self.bus.subscribe("session.closed", self._quiet_closed)]
        # QUIET-POLL follow-up 1: the console broker never re-dials a board whose lease is
        # someone else's, and refuses a new explicit console there (services/console.py).
        broker = getattr(engine, "consoles", None) if self.quiet.enabled else None
        if broker is not None and hasattr(type(broker), "lease_holder"):
            broker.lease_holder = self._console_lease_holder

    # -- QUIET-POLL -------------------------------------------------------------------------

    def _poll_policy(self, board_id: str) -> str:
        try:
            links = self.engine.session(board_id).candidate.links
        except HarnessError:
            links = ()
        return policy_for(board_id, links)

    def _lease_elsewhere(self, board_id: str,
                         rule: Callable[[dict[str, Any] | None], str] = lease_elsewhere) -> str:
        """The lease holder when ``rule`` says the board's lease is not ours (the lease
        service's cached view when recent: no extra hub call on every poll). The default is
        the BACKGROUND rule (``lease_elsewhere``: this process holds no token for it); a hub
        that cannot be asked raises (the gate then stays quiet)."""
        leases = getattr(self, "leases", None)
        if leases is None:
            return ""
        try:
            hub = getattr(self.engine.session(board_id), "hub", None)
        except HarnessError:
            return ""
        if hub is None:
            return ""
        view = leases.view(hub, cached_only=True, max_age_s=LEASE_VIEW_MAX_AGE_S)
        return rule(view if view is not None else leases.view(hub))

    def release_lease_here(self, board_id: str) -> dict[str, Any] | None:
        """LEASE-UI: release the board's hub lease when THIS Harness Manager holds it (its
        token is in the store), for ``DELETE /boards/{bid}?release=true``. None when there is
        no hub, no lease service or no lease held here (another session's lease is theirs to
        release); a hub that refuses or cannot be reached raises, and the board stays open."""
        leases = getattr(self, "leases", None)
        if leases is None:
            return None
        try:
            hub = getattr(self.engine.session(board_id), "hub", None)
        except HarnessError:
            return None
        if hub is None or leases.store.get(hub.host, hub.target) is None:
            return None
        out = leases.release(hub, board_id=board_id)
        return out.get("released")

    def _console_lease_holder(self, board_id: str) -> str:
        # Consoles are explicit (and their re-dial rides an explicit open): the principal
        # rule, ``mine``, unchanged by REVIEW-W5 1.
        try:
            return self._lease_elsewhere(board_id, lease_not_mine)
        except HarnessError as exc:        # the hub did not answer: consoles are not blocked
            log.debug("lease of %s for its consoles: %s", board_id, exc.message)
            return ""

    def _quiet_opened(self, ev: Event) -> None:
        """The session reports each call on the board's single-client channels to the gate
        (CCR QUIET-1, ``BoardSession.set_observer``), so a refusal any caller meets backs the
        background polls off."""
        try:
            session = self.engine.session(ev.board_id)
        except HarnessError:
            return
        seam = getattr(session, "set_observer", None)
        if callable(seam):
            bid, gate = ev.board_id, self.quiet
            seam(lambda channel, exc: gate.observe(bid, exc, channel))

    def _quiet_closed(self, ev: Event) -> None:
        self.quiet.forget(ev.board_id)

    def background_state(self, board_id: str) -> dict[str, Any]:
        return self.quiet.state(board_id)

    def with_lease_note(self, board_id: str, info: Any) -> Any:
        """``info`` with a health note naming the lease holder when the board's hub lease is
        someone else's (QUIET-POLL: explicit reads say who holds it). Never touches the board."""
        if not self.quiet.enabled or not dataclasses.is_dataclass(info):
            return info
        holder = self.quiet.holder(board_id)
        health = getattr(info, "health", None)
        if not holder or health is None:
            return info
        note = (f"the hub lease is held by {holder}: Harness Manager reads this board only "
                "when you ask (background reads are paused)")
        return dataclasses.replace(info, health=dataclasses.replace(
            health, notes=(*health.notes, note)))

    def quiet_answer(self, board_id: str, q: Quiet) -> dict[str, Any]:
        """The answer to a background read the gate held back: 200, the board untouched."""
        return ok(board_id=board_id, quiet=q.public(), background=self.quiet.state(board_id))

    def busy_answer(self, board_id: str, exc: HarnessError) -> dict[str, Any] | None:
        """A background read the board turned away: the back-off starts (the pack's observer
        has usually noted it already; noting twice never lengthens it) and the answer says
        "busy (another client)". None when the gate does not treat it as quiet (the demo)."""
        if not self.quiet.enabled:
            return None
        self.quiet.note_busy(board_id, exc.message)
        q = self.quiet.check(board_id)
        return self.quiet_answer(board_id, q) if q is not None else None

    def check_token(self, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(presented.encode("utf-8"),
                                                       self.token.encode("utf-8"))

    def remember(self, candidates: list[Candidate]) -> None:
        with self._mu:
            for cand in candidates:
                self._candidates[cand.board_id] = cand

    def known(self) -> dict[str, Candidate]:
        with self._mu:
            known = dict(self._candidates)
        for board_id in self.engine.open_boards():
            try:
                known[board_id] = self.engine.session(board_id).candidate
            except HarnessError:
                continue
        return known

    def close(self) -> None:
        self._unlog()
        for unsub in self._unquiet:
            unsub()
        self.hub.close()
        self.jobs.shutdown(wait=False)


#: How old a lease view the background gate trusts without asking the hub (the presence
#: beat's rule, CCR PANEL-2).
LEASE_VIEW_MAX_AGE_S = 60.0

#: Engine events worth a line in daemon.log: the board's life story for a field report
#: (Q2). Never console bytes or progress ticks; no event here carries a token.
_LOGGED = {
    "session.opened": ("note",), "session.closed": (),
    "board.identity": ("shell_id", "rm_id", "rm_name", "harness_version"),
    "deploy.done": ("rm_id", "verified"), "deploy.failed": ("stage", "reason"),
    "debug.state": ("state", "pid", "detail"),
    "xvc.state": ("state", "board_slot", "detail"),
    "controller.reboot": ("phase",), "power.cycle": ("phase", "device"),
    "lease.state": ("target", "state", "holder", "expires_at"),
    "update.done": ("version", "result"), "update.failed": ("version", "phase", "reason"),
    # lane OTA-D: the app's own apply, restart and rollback
    "update.applying": ("phase", "from", "to", "reason"), "update.applied": ("from", "to"),
    "update.rolled_back": ("from", "to", "phase", "reason"),
    # lane SET-API: which settings changed and what they need (never a value)
    "settings.changed": ("keys", "apply", "source"),
}


def _log_event(ev: Event) -> None:
    keys = _LOGGED.get(ev.topic)
    if keys is None:
        return
    fields = " ".join(f"{k}={ev.data.get(k)!r}" for k in keys if k in ev.data)
    log.info("%s %s %s", ev.topic, ev.board_id or "-", fields)


def _obj(body: Any) -> dict[str, Any]:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise UsageError("the request body must be a JSON object")
    return body


def _str(body: dict[str, Any], key: str, default: str | None = None) -> str:
    value = body.get(key, default)
    if value is None:
        raise UsageError(f"the request needs {key!r}")
    if not isinstance(value, str) or not value:
        raise UsageError(f"{key} must be a non-empty string, not {value!r}")
    return value


def _number(body: dict[str, Any], key: str, default: float | None = None) -> float:
    value = body.get(key, default)
    if value is None:
        raise UsageError(f"the request needs {key!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError(f"{key} must be a number, not {value!r}")
    return float(value)


def reset_force(body: dict[str, Any]) -> tuple[bool, str]:
    """SLOT-TIMING: a reset's ``{force, consent}`` (``services.reset_guard``)."""
    force = _bool(body, "force", False)
    consent = body.get("consent", "")
    if not isinstance(consent, str):
        raise UsageError("consent must be a string (type exactly: RESET <board_id>)")
    return force, consent


def _query_flag(value: str | None, name: str) -> bool:
    """A true/false query parameter (absent or empty: false)."""
    low = (value or "").strip().lower()
    if low in ("", "0", "false", "no"):
        return False
    if low in ("1", "true", "yes"):
        return True
    raise UsageError(f"{name} must be true or false, not {value!r}")


def _bool(body: dict[str, Any], key: str, default: bool) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise UsageError(f"{key} must be true or false, not {value!r}")
    return value


def _strings(body: dict[str, Any], key: str) -> tuple[str, ...]:
    value = body.get(key) or []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise UsageError(f"{key} must be a list of strings")
    return tuple(value)


def _abs_path(value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise UsageError(f"{key} must be a path")
    path = Path(value)
    if not path.is_absolute():
        raise UsageError(f"{key} must be an absolute path, not {value!r}",
                         hint="the daemon's working directory is not yours; resolve the path first")
    return path


def _serial_url(value: str) -> str:
    return value if "://" in value else f"serial://{value}"


def _same_id(a: str, b: str) -> bool:
    try:
        return int(a, 0) == int(b, 0)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def resolve_overlay(refs: list[OverlayRef], spec: Any) -> OverlayRef:
    """An overlay by name, by rm_id, or by the object the API returned (name/ids/source)."""
    if isinstance(spec, dict):
        keys = [k for k in ("name", "rm_id", "static_id", "source", "static_usercode")
                if spec.get(k)]
        if not keys:
            raise UsageError("the overlay object needs at least a name")
        label = str(spec.get("name") or spec.get("rm_id"))

        def same(ref: OverlayRef, key: str) -> bool:
            mine, theirs = getattr(ref, key), spec[key]
            return _same_id(mine, theirs) if key.endswith("_id") or key.endswith("code") \
                else mine == theirs

        matches = [r for r in refs if all(same(r, k) for k in keys)]
    elif isinstance(spec, str) and spec:
        label = spec
        matches = [r for r in refs if r.name == spec]
        if not matches and spec.lower().startswith("0x"):
            matches = [r for r in refs if _same_id(r.rm_id, spec)]
    else:
        raise UsageError("overlay must be a name, an rm_id or an overlay object",
                         hint='e.g. {"overlay": "nanosoc"}')
    if not matches:
        names = ", ".join(sorted({r.name for r in refs})) or "none"
        raise AbsentError(f"no overlay named {label!r} for this board",
                          hint=f"known: {names} (GET .../overlays shows which load)")
    return matches[0]


def _mark_identity(items: list) -> list:
    try:
        from harness_manager.services.deploy import mark_identity
    except ImportError:
        return items
    return mark_identity(items)


def _sanitise_note(raw: Any) -> str:
    """The daemon's lock note. The CLI's foreground-hold tag is stripped: ``harness-manager
    detach`` signals a holder carrying it, and the holder here is the daemon itself."""
    from harness_manager.cli.context import HOLD_TAG

    text = str(raw or "").replace(HOLD_TAG, "").strip()
    return f"harness-manager-daemon: {text}" if text else "harness-manager-daemon"


def create_app(engine: Any, *, token: str, state_dir: Path | None = None,
               static_dir: Path | str | None = "auto",
               shutdown: Callable[[], None] | None = None,
               event_limits: tuple[int, int] = (2000, 4 * 1024 * 1024),
               console_limits: tuple[int, int] = (4096, 1024 * 1024),
               allowed_hosts: Iterable[str] | None = None) -> FastAPI:
    """The app. ``event_limits``/``console_limits`` are (max frames, max bytes) per socket.

    ``allowed_hosts`` (lane SET-API, ``hosts.py``): the ``Host`` names the app answers to;
    any other is refused with 403 before a route runs. ``None``: no check (an app a test
    builds); ``server.run_daemon`` always passes the list."""
    if not token:
        raise UsageError("harness-manager-daemon needs a token")
    d = Daemon(engine, token=token, state_dir=state_dir, shutdown=shutdown,
               event_limits=event_limits, console_limits=console_limits)
    static = find_static_dir() if static_dir == "auto" else (
        Path(static_dir) if static_dir else None)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        d.close()

    app = FastAPI(title="harness-manager-daemon", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.daemon = d
    if allowed_hosts is not None:
        from .hosts import HostGuard

        app.add_middleware(HostGuard, allowed=frozenset(allowed_hosts))

    # -- errors and headers ----------------------------------------------------------------

    @app.exception_handler(HarnessError)
    async def _harness_error(request: Request, exc: HarnessError) -> JSONResponse:
        bid = request.path_params.get("bid") if request.method in ("GET", "HEAD") else None
        if bid and is_contention(exc) and request_is_background(request.headers):
            # QUIET-POLL: a background read the board turned away (refused, reset, timed
            # out, held): someone else is using it. Back off, and say "busy", not an error.
            # Our OWN job holding the board (``jobs.busy_error``: ``data.job``) is not
            # contention (REVIEW-W5 5): it stays today's 409 HELD, which the page defers on
            # (``heldByJob``), and nothing backs off.
            body = await run_in_threadpool(d.busy_answer, bid, exc)
            if body is not None:
                return _JSON(body)
        return _JSON(error_body(exc), status_code=http_status(exc))

    @app.exception_handler(_Quiet)
    async def _quiet(_request: Request, exc: _Quiet) -> JSONResponse:
        return _JSON(exc.body)

    @app.exception_handler(_Unauthorised)
    async def _unauthorised(_request: Request, _exc: _Unauthorised) -> JSONResponse:
        return _JSON(error_body(auth_error()), status_code=UNAUTHORISED,
                     headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(str(e.get("msg", e)) for e in exc.errors()) or "malformed request"
        return _JSON(error_body(UsageError(f"bad request: {detail}")), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            err: HarnessError = AbsentError(f"no such endpoint: {request.url.path}",
                                            hint="docs/API.md lists the endpoints")
        elif exc.status_code == 405:
            err = UsageError(f"{request.method} is not allowed on {request.url.path}")
        else:
            err = HarnessError(str(exc.detail))
        return _JSON(error_body(err), status_code=exc.status_code)

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - a bug: say so, with the envelope
            log.exception("internal error on %s %s", request.method, request.url.path)
            response = _JSON(error_body(HarnessError(
                f"internal error: {type(exc).__name__}: {exc}")), status_code=500)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith(API) or request.url.path == "/health":
            response.headers["Cache-Control"] = "no-store"
        seen = request.scope.get("state", {}).get("qp_seen")
        if seen is not None:
            # QUIET-POLL: a background read that met another client on the way (the MCC's
            # second reader inside telemetry, say: the read itself still "succeeded") says
            # "busy (another client)", as a refused connect does, and the back-off stands.
            bid, before = seen
            if d.quiet.refusals(bid) > before:
                q = await run_in_threadpool(d.quiet.check, bid)
                if q is not None and q.kind == "busy":
                    body = await run_in_threadpool(d.quiet_answer, bid, q)
                    response = _JSON(body)
                    response.headers["Cache-Control"] = "no-store"
        return response

    # SERIAL-6900: an identical board read already in flight answers this one too (one read
    # of the single-client port, two replies). Added after ``_guard``, so it runs outside it
    # and shares the finished answer.
    from .coalesce import GetCoalescer

    coalescer = GetCoalescer(API)
    app.state.coalescer = coalescer

    @app.middleware("http")
    async def _coalesce(request: Request, call_next):
        return await coalescer(request, call_next)

    # -- auth -----------------------------------------------------------------------------

    def require_auth(request: Request) -> None:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not d.check_token(value.strip()):
            raise _Unauthorised()

    def background_gate(request: Request) -> None:
        """QUIET-POLL: a read nobody clicked (``X-HM-Background: 1``) of an open board goes
        ahead only when the background gate says so; otherwise it is answered at once, 200
        with ``quiet`` (why) and ``background`` (the gate's state), and the board is not
        touched. A viewing page's own read says so too (``X-HM-Viewer``). Actions (anything
        but GET) and reads without the header are never gated."""
        if request.method not in ("GET", "HEAD") or not request_is_background(request.headers):
            return
        bid = request.path_params.get("bid")
        if not bid or bid not in d.engine.open_boards():
            return                       # not a board, or not open: the route says so
        viewer = str(request.headers.get(VIEWER_HEADER, "") or "").strip()
        if viewer:
            d.quiet.view(bid, viewer[:64])
        q = d.quiet.check(bid)
        if q is not None:
            raise _Quiet(d.quiet_answer(bid, q))
        request.state.qp_seen = (bid, d.quiet.refusals(bid))   # checked after the read

    async def ws_auth(websocket: WebSocket) -> bool:
        if d.check_token(websocket.query_params.get("token")):
            return True
        await ws_deny(websocket, auth_error(), status=UNAUTHORISED)
        return False

    async def ws_deny(websocket: WebSocket, exc: HarnessError, status: int | None = None) -> None:
        response = _JSON(error_body(exc), status_code=status or http_status(exc))
        try:
            await websocket.send_denial_response(response)
        except RuntimeError:        # a server without the denial-response extension
            await websocket.close(code=4000 + int(exc.code), reason=exc.message[:100])

    # -- health (no auth) -----------------------------------------------------------------

    def health() -> JSONResponse:
        return _JSON({"ok": True, "version": __version__, "service": "harness-manager-daemon",
                      "pid": os.getpid()})

    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route(f"{API}/health", health, methods=["GET"])

    api = APIRouter(prefix=API, dependencies=[Depends(require_auth), Depends(background_gate)])
    wsr = APIRouter(prefix=API)

    def accepted(job: Job) -> JSONResponse:
        return _JSON(ok(job=job.id), status_code=202)

    def still_open(bid: str, s: BoardSession, what: str) -> None:
        """A board job's first step: the session it was given is still the open one.

        A close may land between the request and the job (the job claims the board just
        after its preflight); running on a closed session failed mid-way, UNREACHABLE,
        with the swap parked. Now it stops before touching the board (Q2).
        """
        try:
            current = d.engine.session(bid)
        except HarnessError:
            current = None
        if current is not s:
            raise AbsentError(f"{bid} was closed before the {what} started; nothing was sent",
                              hint="open the board again, then retry")

    def board(bid: str) -> BoardSession:
        return d.engine.session(bid)

    def adapter_for_job(bid: str, s: BoardSession, attr: str, capability: str) -> Any:
        """The adapter a job route needs, WITHOUT waiting for the board's op gate.

        Having it is a local fact; only its absence talks to the board (to say why). The
        gate is taken for that case alone, so a job is claimed (202) at once instead of
        after a telemetry read on the MCC (seconds): Reboot looked dead (Q1/Q2).
        """
        adapter = getattr(s, attr, None)
        if adapter is not None:
            return adapter
        with d.gates.op(bid):
            return require(s, attr, capability)

    def require(session: BoardSession, attr: str, capability: str) -> Any:
        """The session adapter, or ``UnavailableError`` with the CLI's reason (``Ctx.require``)."""
        import argparse

        from harness_manager.cli.context import Ctx

        return Ctx(argparse.Namespace(), d.engine, "json").require(session, attr, capability)

    # -- extension routers (week plan lanes) ------------------------------------------------
    # Each lane adds routes in its own module ``harness_manager.daemon.<name>`` with
    # ``register(ctx: RouteContext)``. They load HERE, before this file's
    # ``/boards/{bid:path}`` routes: that path converter is greedy, so a route added
    # after it would never be reached.
    ctx = RouteContext(daemon=d, api=api, wsr=wsr, board=board, require=require,
                       accepted=accepted)
    for name in EXTENSIONS:
        try:
            mod = importlib.import_module(f"{__package__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"{__package__}.{name}":
                continue                    # that lane has not landed yet
            raise
        mod.register(ctx)

    # -- engine-wide ------------------------------------------------------------------------

    @api.get("/packs")
    def packs() -> JSONResponse:
        packs = sorted(d.engine.packs().items())
        # T14-1: the pack's own capability titles, so a front-end never mirrors them.
        caps = {n: [{"name": c.name, "title": c.title, "needs_hint": c.needs_hint}
                    for c in p.capability_specs()] for n, p in packs}
        return _JSON(ok(packs={n: p.title for n, p in packs}, capabilities=caps))

    @api.post("/probe")
    def probe(body: JsonBody = None) -> JSONResponse:
        b = _obj(body)
        timeout = _number(b, "timeout_s", 2.0)
        if timeout <= 0:
            raise UsageError("timeout_s must be positive")
        hints = ProbeHints(hosts=_strings(b, "hosts"),
                           serial_ports=tuple(_serial_url(s) for s in _strings(b, "serial_ports")),
                           volumes=_strings(b, "volumes"),
                           scan_usb=_bool(b, "scan_usb", True),
                           scan_network=_bool(b, "scan_network", True), timeout_s=timeout,
                           via=_str(b, "via") if b.get("via") else "")
        # Engine-wide jobs (an update check, an app switch) hold no board's control port.
        running = [j for j in d.jobs.running() if j.board_id]
        if running:
            job = running[0]
            err = HeldError(f"{job.describe()} is running on {job.board_id}; a probe now could "
                            "take that board's control port", holder=f"harness-manager-daemon {job.describe()}",
                            hint="probe again when the job finishes")
            err.data = {"job": job.id, "kind": job.kind, "board_id": job.board_id}  # type: ignore[attr-defined]
            raise err
        found = d.engine.probe(hints)
        d.remember(found)
        return _JSON(ok(candidates=found))

    @api.get("/boards")
    def boards() -> JSONResponse:
        open_ids = set(d.engine.open_boards())
        rows = []
        # SIDEBAR-UX (daemon/configured.py): the boards boards.toml configures are listed
        # too, built from the file with no contact (source "config"), so a restart never
        # drops them from the sidebar; `configured` says how each is reached.
        for board_id, cand, source, conf in configured.board_rows(d.engine, d.known(),
                                                                  open_ids):
            row: dict[str, Any] = {"board_id": board_id, "open": board_id in open_ids,
                                   "candidate": cand, "source": source}
            if conf is not None:
                row["configured"] = conf
            holder = d.engine.lock_owner(board_id)
            if holder is not None:
                row["holder"] = owner_json(holder)
            job = d.gates.busy(board_id)
            if job is not None:
                row["job"] = job.id
                row["job_kind"] = job.kind        # T14-5
            rows.append(row)
        return _JSON(ok(boards=rows))

    @api.post("/boards")
    def open_board(body: JsonBody = None) -> JSONResponse:
        b = _obj(body)
        note = _sanitise_note(b.get("note", ""))
        via = _str(b, "via") if b.get("via") else ""
        if b.get("candidate") is not None:
            if via:
                raise UsageError("via goes with target, not with a candidate",
                                 hint="a candidate from POST /probe already carries its route")
            cand = from_json(Candidate, b["candidate"])
            if cand.evidence == configured.EVIDENCE:
                # SIDEBAR-UX: a boards.toml board is contacted now; its row stops saying not
                cand = dataclasses.replace(cand, evidence=configured.OPENED)
        elif b.get("target"):
            target = _str(b, "target")
            pack = _str(b, "pack", "mps3")
            try:
                # via only when given: engines that predate it (the demo) take two arguments.
                cand = (d.engine.candidate_for(target, pack, via=via) if via
                        else d.engine.candidate_for(target, pack))
            except ValueError as exc:
                raise UsageError(f"target {target!r} is not host[:port] ({exc})",
                                 hint="e.g. 192.168.10.101 or 192.168.10.101:6900") from exc
            extra = tuple(Link(LinkKind.USB_SERIAL, _serial_url(s), "given with serial")
                          for s in _strings(b, "serial")) + tuple(
                Link(LinkKind.USB_MSD, v, "given with volume") for v in _strings(b, "volume"))
            if extra:
                cand = dataclasses.replace(cand, links=cand.links + extra)   # keeps the name (N1)
        else:
            raise UsageError("opening a board needs a target or a candidate",
                             hint='{"target": "192.168.10.101"}, or a candidate from POST /probe')
        d.engine.open(cand, note=note)
        d.remember([cand])
        out: dict[str, Any] = {"board_id": cand.board_id}
        try:
            with d.gates.op(cand.board_id):
                out["info"] = d.engine.info(cand.board_id)
        except HarnessError as exc:
            # The session is open (the lock is held); the board did not answer yet.
            out["info"] = None
            out["info_error"] = error_object(exc)
        return _JSON(ok(**out))

    @api.get("/jobs")
    def jobs() -> JSONResponse:
        return _JSON(ok(jobs=[j.to_json() for j in d.jobs.recent()]))

    @api.get("/jobs/{job_id}")
    def job_state(job_id: str) -> JSONResponse:
        job = d.jobs.get(job_id)
        if job is None:
            raise AbsentError(f"no job {job_id!r}", hint="jobs are kept for the last 200 operations")
        return _JSON(ok(**job.to_json()))

    @api.get("/help/tabs")
    def help_tabs() -> JSONResponse:
        from harness_manager.cli import helptext

        return _JSON(ok(tabs=[{"name": n, "text": t} for n, t in helptext.tabs()]))

    @api.post("/daemon/shutdown")
    def stop_daemon(body: JsonBody = None) -> JSONResponse:
        b = _obj(body)
        force = _bool(b, "force", False)
        running = d.jobs.running()
        if running and not force:
            names = ", ".join(f"{j.describe()} on {j.board_id}" for j in running)
            err = HeldError(f"harness-manager-daemon is running {names}", holder="harness-manager-daemon",
                            hint="wait for it, or stop with --force (the operation is abandoned)")
            err.data = {"job": running[0].id, "kind": running[0].kind,  # type: ignore[attr-defined]
                        "jobs": [{"job": j.id, "kind": j.kind, "board_id": j.board_id}
                                 for j in running]}
            raise err
        if d.shutdown is None:
            raise UnavailableError("daemon_shutdown",
                                   "this server was not started by `harness-manager daemon`")
        threading.Timer(0.2, d.shutdown).start()
        return _JSON(ok(stopping=True, pid=os.getpid()))

    # -- one board: most specific routes first ------------------------------------------------
    # Multi-segment suffixes first: with a path-converted board id,
    # "/boards/{bid}/restore" would otherwise also match "/boards/X/storage/restore".

    @api.post("/boards/{bid:path}/consoles/{name}/export")
    def console_export(bid: str, name: str, body: JsonBody = None) -> JSONResponse:
        b = _obj(body)
        port = b.get("port", 0) or 0
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise UsageError(f"port must be 0..65535, not {port!r}")
        got = d.engine.consoles.export_tcp(board(bid), name, port)
        return _JSON(ok(board_id=bid, name=name, host="127.0.0.1", port=got))

    @api.post("/boards/{bid:path}/debug/detect")
    def debug_detect(bid: str) -> JSONResponse:
        s = board(bid)
        with d.gates.op(bid):
            idcode = d.engine.debug.detect(s)
        return _JSON(ok(board_id=bid, idcode=idcode))

    @api.post("/boards/{bid:path}/debug/up")
    def debug_up(bid: str) -> JSONResponse:
        s = board(bid)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            still_open(bid, s, "debug session")
            progress("starting", 0, 0)
            status = d.engine.debug.up(s)
            progress(getattr(status, "state", "up"), 0, 0)
            return status          # a "failed" status is a result, as the engine returns it

        return accepted(d.jobs.submit("debug_up", bid, run))

    @api.post("/boards/{bid:path}/debug/down")
    def debug_down(bid: str) -> JSONResponse:
        s = board(bid)
        with d.gates.op(bid):
            status = d.engine.debug.down(s)
        return _JSON(ok(board_id=bid, **_fields(status)))

    @api.get("/boards/{bid:path}/controller/temps")
    def controller_temps(bid: str) -> JSONResponse:
        s = board(bid)
        with d.gates.op(bid):
            readings = list(require(s, "controller", C.CONSOLE_CONTROLLER).temperatures())
        now = time.time()
        return _JSON(ok(board_id=bid, readings=[reading_json(r, now) for r in readings]))

    @api.get("/boards/{bid:path}/controller/osc")
    def controller_osc(bid: str) -> JSONResponse:
        s = board(bid)
        with d.gates.op(bid):
            readings = list(require(s, "controller", C.CLOCK_BOARD).oscillators())
        now = time.time()
        return _JSON(ok(board_id=bid, readings=[reading_json(r, now) for r in readings]))

    @api.post("/boards/{bid:path}/controller/reboot")
    def controller_reboot(bid: str, body: JsonBody = None) -> JSONResponse:
        s = board(bid)
        b = _obj(body)
        # No wait_s: the pack picks it from the harness (180 s Linux, 120 s bare-metal).
        wait_s = _number(b, "wait_s") if b.get("wait_s") is not None else None
        if wait_s is not None and wait_s <= 0:
            raise UsageError("wait_s must be positive")
        ctl = adapter_for_job(bid, s, "controller", C.REBOOT_BOARD)
        # SLOT-TIMING: never while the board's card job writes or reads back: the job fails
        # HELD, naming it. Checked IN the job, so the 202 still comes at once (Q1/Q2).
        # {force, consent: "RESET <bid>"} is the recovery of a job that never ends.
        force, consent = reset_force(b)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            with reset_guard.guarded(s, reset_guard.ACTION_MCC_REBOOT, force=force,
                                     consent=consent):
                return ctl.reboot(progress=progress, wait_s=wait_s)

        return accepted(d.jobs.submit("reboot", bid, run))

    @api.post("/boards/{bid:path}/controller/command")
    def controller_command(bid: str, body: JsonBody = None) -> JSONResponse:
        s = board(bid)
        b = _obj(body)
        line = _str(b, "line")
        arm = _bool(b, "arm", False)
        with d.gates.op(bid):
            ctl = require(s, "controller", C.CONSOLE_CONTROLLER)
            if reset_guard.is_reboot_line(line):      # SLOT-TIMING: a REBOOT is a reset
                force, consent = reset_force(b)
                with reset_guard.guarded(s, reset_guard.ACTION_MCC_REBOOT, force=force,
                                         consent=consent):
                    reply = ctl.command(line, arm=arm)
            else:
                reply = ctl.command(line, arm=arm)
        return _JSON(ok(board_id=bid, command=line, reply=reply))

    @api.get("/boards/{bid:path}/storage/pending")
    def storage_pending(bid: str) -> JSONResponse:
        s = board(bid)
        with d.gates.op(bid):
            pending = require(s, "storage", C.STORAGE_BACKUP).pending()
        return _JSON(ok(board_id=bid, pending=pending))

    @api.post("/boards/{bid:path}/storage/backup")
    def storage_backup(bid: str, body: JsonBody = None) -> JSONResponse:
        s = board(bid)
        b = _obj(body)
        dest = (_abs_path(b["dest_dir"], "dest_dir") if b.get("dest_dir")
                else d.state_dir / "backups")
        if dest.exists() and not dest.is_dir():
            raise UsageError(f"{dest} exists and is not a directory",
                             hint="give a directory for the backup archive")
        storage = adapter_for_job(bid, s, "storage", C.STORAGE_BACKUP)
        dest.mkdir(parents=True, exist_ok=True)
        return accepted(d.jobs.submit(
            "sd_backup", bid, lambda progress: storage.backup(dest, progress=progress)))

    @api.post("/boards/{bid:path}/storage/install")
    def storage_install(bid: str, body: JsonBody = None) -> JSONResponse:
        from harness_manager.cli.cmd_board import backup_record

        s = board(bid)
        b = _obj(body)
        raw = b.get("files")
        if not isinstance(raw, dict) or not raw:
            raise UsageError("files must be an object {SD path: absolute source path}")
        files: dict[str, Path] = {}
        for dest, src in raw.items():
            if not isinstance(dest, str) or not dest:
                raise UsageError(f"bad SD path {dest!r}")
            if dest.lower().endswith(".ebf"):
                raise RefusedError(f"refusing to write board-controller firmware ({dest})",
                                   hint=".ebf files are never written to the SD; remove them")
            path = _abs_path(src, f"files[{dest!r}]")
            if not path.is_file():
                raise AbsentError(f"no file at {path}", hint="give files that exist")
            files[dest] = path
        backup_path = _abs_path(b.get("backup_path"), "backup_path")
        with d.gates.op(bid):
            storage = require(s, "storage", C.STORAGE_INSTALL)
            record = backup_record(storage, backup_path)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            storage.install(files, backup=record, progress=progress)
            return {"files": sorted(files), "backup": record}

        return accepted(d.jobs.submit("sd_install", bid, run))

    @api.post("/boards/{bid:path}/storage/restore")
    def storage_restore(bid: str, body: JsonBody = None) -> JSONResponse:
        from harness_manager.cli.cmd_board import backup_record

        s = board(bid)
        backup_path = _abs_path(_obj(body).get("backup_path"), "backup_path")
        with d.gates.op(bid):
            storage = require(s, "storage", C.STORAGE_INSTALL)
            record = backup_record(storage, backup_path)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            storage.restore(record, progress=progress)
            return {"backup": record}

        return accepted(d.jobs.submit("sd_restore", bid, run))

    @api.post("/boards/{bid:path}/lab/{verb}")
    def lab(bid: str, verb: str, body: JsonBody = None) -> JSONResponse:
        from .lab import run_lab

        s = board(bid)
        with d.gates.op(bid):
            data = run_lab(d.engine, s, verb, _obj(body))
        return _JSON(ok(**data))

    @api.get("/boards/{bid:path}/lock")
    def lock(bid: str) -> JSONResponse:
        return _JSON(ok(board_id=bid, holder=owner_json(d.engine.lock_owner(bid))))

    @api.get("/boards/{bid:path}/session")
    def session_view(bid: str) -> JSONResponse:
        s = board(bid)
        adapters = {a: getattr(s, a, None) is not None for a in ADAPTERS}
        shell = getattr(s, "shell", None)
        adapters["shell"] = shell is not None and callable(getattr(shell, "call", None))
        resets = getattr(s, "resets", None)
        job = d.gates.busy(bid)
        # T14-4: whether each engine service works at all (None) or why not (its stub reason).
        services = {n: getattr(getattr(d.engine, n, None), "reason", None)
                    for n in ("deploy", "consoles", "debug", "telemetry")}
        return _JSON(ok(board_id=bid, candidate=s.candidate, adapters=adapters,
                        reset_targets=list(resets.reset_targets()) if resets else [],
                        job=job.id if job else None, job_kind=job.kind if job else None,
                        services=services))

    @api.get("/boards/{bid:path}/telemetry")
    def telemetry(bid: str) -> JSONResponse:
        s = board(bid)
        with d.gates.op(bid):
            readings = list(d.engine.telemetry.readings(s))
        now = time.time()
        return _JSON(ok(board_id=bid, readings=[reading_json(r, now) for r in readings]))

    @api.get("/boards/{bid:path}/overlays")
    def overlays(bid: str) -> JSONResponse:
        s = board(bid)
        with d.gates.op(bid):
            loadable, blocked = d.engine.deploy.compatible(s)
            every = list(d.engine.deploy.overlays(s))
        return _JSON(ok(board_id=bid, loadable=list(loadable), blocked=dict(blocked),
                        overlays=every))

    @api.get("/boards/{bid:path}/card")
    def card(bid: str) -> JSONResponse:
        # Keep on the card: the board's card as a deploy would keep a design on it. Reads only.
        s = board(bid)
        with d.gates.op(bid):
            status = card_status_of(d.engine.deploy, s)
            # LINUX-SLOTS (additive): the pack's card adapter names the default's RM and adds
            # the Linux OS slots on the same card, from this same read; `line` is the Board
            # tile's one line.
            annotate = getattr(getattr(s, "card", None), "annotate", None)
            if status.store and callable(annotate):
                try:
                    status = annotate(status)
                except HarnessError as exc:
                    log.info("card annotate failed on %s: %s", bid, exc)
        from harness_manager.services.slots import card_line

        return _JSON(ok(board_id=bid, card=status, line=card_line(status)))

    def _preflight(bid: str, s: BoardSession, spec: Any) -> tuple[OverlayRef, list, Any]:
        with d.gates.op(bid):
            overlay = resolve_overlay(list(d.engine.deploy.overlays(s)), spec)
            items = _mark_identity(list(d.engine.deploy.preflight(s, overlay)))
        return overlay, items, preflight_refusal(items, overlay.name)

    @api.post("/boards/{bid:path}/preflight")
    def preflight(bid: str, body: JsonBody = None) -> JSONResponse:
        s = board(bid)
        overlay, items, refusal = _preflight(bid, s, _obj(body).get("overlay"))
        out: dict[str, Any] = {"board_id": bid, "overlay": overlay, "items": items}
        if refusal is not None:
            out["refusal"] = error_object(refusal)
        return _JSON(ok(**out))

    @api.post("/boards/{bid:path}/deploy")
    def deploy(bid: str, body: JsonBody = None) -> JSONResponse:
        s = board(bid)
        keep = _bool(_obj(body), "keep_on_card", False)
        overlay, items, refusal = _preflight(bid, s, _obj(body).get("overlay"))
        if refusal is not None:            # refuse BEFORE deploy() is ever called
            refusal.data = {"overlay": overlay, "preflight": items}   # type: ignore[attr-defined]
            raise refusal
        if keep:                           # Keep on the card: the card must take it, first
            with d.gates.op(bid):
                status = card_status_of(d.engine.deploy, s)
            refused = keep_refusal(status)
            if refused is not None:
                refused.data = {"overlay": overlay, "card": status}   # type: ignore[attr-defined]
                raise refused

        def run(progress: Callable[[str, int, int], None]) -> Any:
            still_open(bid, s, "deploy")

            def on_progress(ev: Event) -> None:
                if ev.board_id == bid:
                    progress(str(ev.data.get("phase", "")), int(ev.data.get("bytes", 0) or 0),
                             int(ev.data.get("total", 0) or 0))

            unsubscribe = d.bus.subscribe("deploy.progress", on_progress)
            try:
                if keep:                   # the keyword only when asked (off by default)
                    return d.engine.deploy.deploy(s, overlay, keep_on_card=True)
                return d.engine.deploy.deploy(s, overlay)
            finally:
                unsubscribe()

        return accepted(d.jobs.submit("deploy", bid, run))

    @api.post("/boards/{bid:path}/restore")
    def restore(bid: str) -> JSONResponse:
        s = board(bid)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            still_open(bid, s, "restore")

            def on_progress(ev: Event) -> None:
                if ev.board_id == bid:
                    progress(str(ev.data.get("phase", "")), int(ev.data.get("bytes", 0) or 0),
                             int(ev.data.get("total", 0) or 0))

            unsubscribe = d.bus.subscribe("deploy.progress", on_progress)
            try:
                return d.engine.deploy.restore_baseline(s)
            finally:
                unsubscribe()

        return accepted(d.jobs.submit("restore", bid, run))

    @api.post("/boards/{bid:path}/reset")
    def reset(bid: str, body: JsonBody = None) -> JSONResponse:
        s = board(bid)
        target = _str(_obj(body), "target", "dut")
        with d.gates.op(bid):
            resets = require(s, "resets", C.RESET_DUT)
            targets = list(resets.reset_targets())
            if target not in targets:
                raise UsageError(f"{bid} cannot reset {target!r}",
                                 hint=f"reset targets on this board: {', '.join(targets) or 'none'}")
            resets.reset(target)
        return _JSON(ok(board_id=bid, target=target, result="done"))

    @api.get("/boards/{bid:path}/clocks")
    def clocks(bid: str) -> JSONResponse:
        s = board(bid)
        with d.gates.op(bid):
            readings = list(require(s, "clocks", C.CLOCK_DUT).clocks())
        now = time.time()
        return _JSON(ok(board_id=bid, readings=[reading_json(r, now) for r in readings]))

    @api.post("/boards/{bid:path}/clocks")
    def set_clock(bid: str, body: JsonBody = None) -> JSONResponse:
        s = board(bid)
        b = _obj(body)
        name = _str(b, "name", "dut")
        mhz = _number(b, "mhz")
        if mhz <= 0:
            raise UsageError(f"{mhz:g} MHz is not a clock frequency", hint="give a positive MHz")
        with d.gates.op(bid):
            reading = require(s, "clocks", C.CLOCK_DUT).set_clock(name, mhz)
        return _JSON(ok(board_id=bid, reading=reading_json(reading)))

    @api.get("/boards/{bid:path}/consoles")
    def console_names(bid: str) -> JSONResponse:
        return _JSON(ok(board_id=bid, names=list(d.engine.consoles.names(board(bid)))))

    @api.get("/boards/{bid:path}/debug")
    def debug_status(bid: str) -> JSONResponse:
        st = d.engine.debug.status(board(bid))
        # DEBUG-OCD: which OpenOCD this service would run, and whether it has the adapter.
        report = getattr(d.engine.debug, "openocd_report", None)
        extra = {"openocd": report()} if callable(report) else {}
        return _JSON(ok(board_id=bid, **_fields(st), **extra))

    # -- one board: the bare routes, last ------------------------------------------------------

    @api.get("/boards/{bid:path}")
    def info(bid: str) -> JSONResponse:
        board(bid)
        with d.gates.op(bid):
            board_info = d.engine.info(bid)
        # QUIET-POLL: an explicit read while the lease is someone else's names the holder (a
        # health note, so the shape stays BoardInfo's; GET .../background has the rest).
        return _JSON(ok(**_fields(d.with_lease_note(bid, board_info))))

    @api.delete("/boards/{bid:path}")
    def close(bid: str, release: str | None = None) -> JSONResponse:
        # LEASE-UI (additive): ``?release=true`` releases the hub lease THIS Harness Manager
        # holds on the board first; a failed release leaves the board open. ``released`` is
        # the lease given back, or null when none was held here.
        want = _query_flag(release, "release")
        job = d.gates.busy(bid)
        if job is not None:
            raise busy_error(bid, job)
        was_open = bid in d.engine.open_boards()
        extra: dict[str, Any] = {}
        if want:
            extra["released"] = d.release_lease_here(bid) if was_open else None
        with d.gates.op(bid):
            d.engine.close(bid)
        return _JSON(ok(board_id=bid, closed=was_open, **extra))

    @api.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def unknown(rest: str, request: Request) -> JSONResponse:
        raise AbsentError(f"no such endpoint: {request.method} {API}/{rest}",
                          hint="docs/API.md lists the endpoints")

    # -- WebSockets ------------------------------------------------------------------------------

    @wsr.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        if not await ws_auth(websocket):
            return
        topics = parse_topics(websocket.query_params.get("topics"))
        await websocket.accept()
        max_items, max_bytes = d.event_limits
        outbox = Outbox(asyncio.get_running_loop(), max_items=max_items, max_bytes=max_bytes)
        client = _EventClient(topics, outbox)
        d.hub.add(client)

        async def ignore(_message: dict[str, Any]) -> None:
            return None

        try:
            await _serve(websocket, outbox, on_receive=ignore, dropped_frame=dropped_event_frame)
        finally:
            d.hub.remove(client)
            outbox.close()

    @wsr.websocket("/boards/{bid:path}/consoles/{name}")
    async def console(websocket: WebSocket, bid: str, name: str) -> None:
        if not await ws_auth(websocket):
            return
        broker = d.engine.consoles
        try:
            session = d.engine.session(bid)
            stream = await asyncio.to_thread(broker.subscribe, session, name)
        except HarnessError as exc:
            await ws_deny(websocket, exc)
            return
        key = name
        resolve = getattr(broker, "resolve", None)
        if callable(resolve):
            try:
                key = resolve(session, name)[0]
            except HarnessError:
                pass
        state_of = getattr(broker, "state", None)
        initial = state_of(bid, key) if callable(state_of) else "up"
        try:
            await websocket.accept()
        except BaseException:
            stream.close()               # the client left during the handshake
            raise
        max_items, max_bytes = d.console_limits
        outbox = Outbox(asyncio.get_running_loop(), max_items=max_items, max_bytes=max_bytes)
        outbox.put(json.dumps({"state": initial, "name": name, "detail": ""}))
        bridge = ConsoleBridge(stream, outbox, d.bus, bid, key, name)
        bridge.start()

        async def to_board(message: dict[str, Any]) -> None:
            data = message.get("bytes")
            if data is None:            # text frames from the client are reserved
                return
            try:
                await asyncio.to_thread(stream.write, data)
            except HarnessError as exc:
                outbox.put(json.dumps({"error": error_object(exc)}))

        try:
            await _serve(websocket, outbox, on_receive=to_board,
                         dropped_frame=dropped_console_frame, coalesce=True)
        finally:
            # Off the event loop, and not awaited (see _serve): closing the stream
            # joins the pump thread.
            threading.Thread(target=bridge.close, daemon=True,
                             name=f"harness-manager-daemon-console-close-{name}").start()

    app.include_router(wsr)
    app.include_router(api)

    # -- the web UI -----------------------------------------------------------------------------

    if static is not None:
        from harness_manager import web

        if static.resolve() == Path(web.STATIC_DIR).resolve():
            # T14's mount: CSP (script-src 'self'), nosniff, no-cache, fixed media types.
            web.mount_static(app, "/", name="ui")
        else:                                   # a test's or a developer's own directory
            app.mount("/", StaticFiles(directory=str(static), html=True), name="ui")
    else:
        @app.get("/", response_class=HTMLResponse)
        def placeholder() -> HTMLResponse:
            return HTMLResponse(PLACEHOLDER, headers={"Cache-Control": "no-store"})

    return app


def _fields(obj: Any) -> dict[str, Any]:
    """A dataclass's fields at the top level of the reply (``BoardInfo``, ``DebugStatus``)."""
    from harness_manager.cli.output import jsonable

    return dict(jsonable(obj))               # jsonable's field rules (``omit_none``), once
