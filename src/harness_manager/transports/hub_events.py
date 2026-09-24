"""fpgahub's Server-Sent Events (``GET /api/v1/events``) on the engine bus (team T8).

The hub pushes every lease and share change as it happens (fpgahub ``api/v1.py``
``events_stream``): the stream opens with ``:connected``, then each event is one
``event: <type>`` line and one ``data: {"type", "ts", "data"}`` line and a blank
line. ``HubEventStream`` holds one such stream open for one board, with the same
Bearer credential as the REST client (a header, never the URL), and:

- **publishes** each event about this board's target (or its physical board) as
  ``hub.event {type, ts, target, board, data}`` on the engine bus, and its own state
  as ``hub.stream {state: connecting|up|down|refused|closed, detail, url, reconnects}``
  (both topics: CCR T8-3 appends them to CONTRACTS.md). A queue, a promotion or a
  force-release reaches the UI and ``LeaseService`` in well under a second instead
  of at the next 10 s poll;
- **feeds the client** (``RestHubClient.observe``), which keeps what the REST
  history cannot give back: a revoke's ``by`` and ``reason``, and when each waiter
  queued (the degraded request notes' timer);
- **reconnects** after any drop with a back-off (1, 2, 5, 10, 30 s, reset after a
  good connection), and calls ``on_resync`` after every (re)connect: the hub keeps no
  replay buffer, so an event that fired while the stream was down is recovered by
  re-reading the lease (``LeaseService`` drops its cached view). A 401/403 is
  ``refused``: the stream waits the longest back-off before it tries again, so a
  revoked token does not hammer the hub.

fpgahub sends nothing while idle (no heartbeat comments), so there is no read
timeout by default; TCP keep-alive notices a dead peer, and ``idle_reconnect_s``
(off by default) forces a fresh connection after a long silence.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

from harness_manager.core.events import Event, EventBus

log = logging.getLogger(__name__)

EVENT_TOPIC = "hub.event"
STREAM_TOPIC = "hub.stream"
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
CONNECT_TIMEOUT_S = 15.0

#: The fpgahub event types a board session cares about (the ``types`` filter, exact names).
LEASE_TYPES = (
    "lease.acquired", "lease.queued", "lease.released", "lease.promoted", "lease.expired",
    "lease.heartbeat", "lease.revoked", "lease.admin_revoked", "lease.queue.cancelled",
    "lease.chassis.acquired", "lease.chassis.queued", "lease.chassis.released",
    "lease.chassis.wait_cancelled", "lease.overridden", "lease.pid_reaped",
)
SHARE_TYPES = ("share.started", "share.stopped", "share.auto_started", "share.auto_stopped")
DEFAULT_TYPES = LEASE_TYPES + SHARE_TYPES


def parse_sse(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """SSE text lines -> ``{type, ts, data}`` dicts (fpgahub's ``Event.to_dict``).

    Comments (``:connected``) are skipped; multi-line ``data`` is joined with
    newlines; a frame whose data is not a JSON object becomes
    ``{type, ts: "", data: {"raw": text}}``.
    """
    etype, data = "", []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                text = "\n".join(data)
                try:
                    obj = json.loads(text)
                except ValueError:
                    obj = None
                if isinstance(obj, dict) and "data" in obj:
                    obj.setdefault("type", etype or "message")
                    yield obj
                else:
                    yield {"type": etype or "message", "ts": "", "data": {"raw": text}}
            etype, data = "", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            etype = value
        elif name == "data":
            data.append(value)


class HubEventStream:
    """One supervised SSE connection to fpgahub for one client (see the module docstring)."""

    def __init__(self, client: Any, *, on_event: Callable[[dict[str, Any]], None],
                 on_state: Callable[[dict[str, Any]], None] | None = None,
                 on_resync: Callable[[], None] | None = None,
                 types: Sequence[str] = DEFAULT_TYPES, backoff_s: Sequence[float] = BACKOFF_S,
                 idle_reconnect_s: float | None = None, only_relevant: bool = True) -> None:
        self.client = client
        self._on_event = on_event
        self._on_state = on_state
        self._on_resync = on_resync
        self.types = tuple(types)
        self._backoff = tuple(backoff_s) or (1.0,)
        self._idle = idle_reconnect_s
        self._only_relevant = only_relevant
        self.state = "down"
        self.detail = ""
        self.reconnects = 0
        self.connects = 0
        self._stop = threading.Event()
        self._mu = threading.Lock()
        self._conn: Any = None
        self._thread: threading.Thread | None = None

    # -- state ----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail, "url": getattr(self.client, "url", ""),
                "reconnects": self.reconnects}

    def _set(self, state: str, detail: str = "") -> None:
        changed = (state, detail) != (self.state, self.detail)
        self.state, self.detail = state, detail
        if changed:
            log.info("hub events %s: %s %s", getattr(self.client, "url", ""), state, detail)
            if self._on_state is not None:
                try:
                    self._on_state(self.status())
                except Exception:  # noqa: BLE001 - a watcher never stops the stream
                    log.exception("hub stream state watcher failed")

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> HubEventStream:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="hub-events", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._drop()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._set("closed", "closed with the board")

    def _drop(self) -> None:
        with self._mu:
            conn, self._conn = self._conn, None
        if conn is None:
            return
        sock = getattr(conn, "sock", None)
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            if sys.platform == "win32":
                # Windows: shutdown does not wake a recv blocked in the reader thread, and
                # closing the response would then wait on its buffer lock for ever. Closing
                # the handle itself does wake it.
                with contextlib.suppress(OSError):
                    socket.close(sock.detach())
        with contextlib.suppress(Exception):
            conn.close()

    # -- the loop ---------------------------------------------------------------------------

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self._set("connecting", f"attempt {attempt + 1}" if attempt else "")
            outcome, why = self._once()
            if self._stop.is_set():
                break
            if outcome == "up":            # it was up, then dropped: start the back-off again
                attempt = 0
                self.reconnects += 1
            wait = self._backoff[-1] if outcome == "refused" else \
                self._backoff[min(attempt, len(self._backoff) - 1)]
            self._set("refused" if outcome == "refused" else "down",
                      f"{why}; retrying in {wait:g}s")
            attempt += 1
            if self._stop.wait(wait):
                break

    def _once(self) -> tuple[str, str]:
        """One connection: ``("up", why)`` after a live stream ended, else the failure."""
        http = getattr(self.client, "_http", None)
        params = {"types": ",".join(self.types)} if self.types else None
        try:
            conn, resp = http.open_stream("/events", params, timeout=CONNECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - any failure to connect is "down"
            return "down", f"cannot connect: {exc}"
        with self._mu:
            self._conn = conn
        try:
            if resp.status in (401, 403):
                body = resp.read(2048).decode("utf-8", "replace")
                return "refused", f"HTTP {resp.status}: {body.strip()[:200]}"
            if resp.status != 200:
                return "down", f"HTTP {resp.status}"
            sock = getattr(conn, "sock", None)
            if sock is not None:
                sock.settimeout(self._idle)
            self.connects += 1
            self._set("up", "streaming")
            if self._on_resync is not None:
                try:
                    self._on_resync()
                except Exception:  # noqa: BLE001
                    log.exception("hub stream resync failed")
            for event in parse_sse(self._lines(resp)):
                self._deliver(event)
            return "up", "the hub closed the stream"
        except Exception as exc:  # noqa: BLE001 - a read error is a drop
            return ("up" if self.state == "up" else "down"), f"stream dropped: {exc}"
        finally:
            self._drop()

    def _lines(self, resp: Any) -> Iterator[str]:
        # HTTPResponse.readline undoes the chunked framing uvicorn streams SSE with.
        while not self._stop.is_set():
            raw = resp.readline()
            if not raw:
                return
            yield raw.decode("utf-8", "replace")

    def _deliver(self, event: dict[str, Any]) -> None:
        relevant = getattr(self.client, "relevant", None)
        if self._only_relevant and relevant is not None and not relevant(event):
            return
        observe = getattr(self.client, "observe", None)
        if observe is not None:
            try:
                observe(event)
            except Exception:  # noqa: BLE001
                log.exception("hub client observe failed")
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - a consumer never stops the stream
            log.exception("hub event consumer failed for %s", event.get("type"))


def attach_bus(bus: EventBus, board_id: str, client: Any, *,
               on_resync: Callable[[], None] | None = None,
               backoff_s: Sequence[float] = BACKOFF_S, start: bool = True) -> HubEventStream:
    """A started stream publishing ``hub.event``/``hub.stream`` for ``board_id`` on ``bus``."""
    target = getattr(client, "target", "")

    def board() -> str:
        cfg_board = getattr(getattr(client, "config", None), "board", "")
        return getattr(client, "_board", "") or cfg_board

    def on_event(ev: dict[str, Any]) -> None:
        bus.publish(Event(EVENT_TOPIC, board_id, {
            "type": ev.get("type", ""), "ts": ev.get("ts", ""), "target": target,
            "board": board(), "data": ev.get("data") or {}}))

    def on_state(st: dict[str, Any]) -> None:
        bus.publish(Event(STREAM_TOPIC, board_id, st))

    stream = HubEventStream(client, on_event=on_event, on_state=on_state, on_resync=on_resync,
                            backoff_s=backoff_s)
    return stream.start() if start else stream
