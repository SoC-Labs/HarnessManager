"""The live display compositor: one upstream per board, many viewers (lane LM1).

``docs/design/LCD_MIRROR.md`` §7.1-§7.3 is the design; ``harness_manager.core.display`` is
the model and ``core.display_wire`` every byte of the wire. Transport-agnostic: a board is
opened through a *source*, a ``DisplayAdapter`` (``display_connect()`` returns a connected
byte stream) or a bare connector callable. Lane LM2 wires the MPS3 source
(``claim.open_forward({"lcd_mirror": 6940})``) and the lease hooks, lane LM3 the daemon's
WebSocket; neither is needed here.

**The upstream (one per board, shared by every viewer).**

- Opened by the first viewer (``attach``: a WebSocket, a PNG, the CLI). It reads HELLO,
  sends ``RATE max(viewers)`` and ``KEY``, and presents nothing until the whole keyframe
  (``key_last``) is in.
- Every UPDATE is ACKed on receipt (H1: the board keeps at most 2 unacknowledged, so the
  backlog stays at the source, not in the SSH windows). A SNAP split across UPDATEs is
  staged and shown whole on ``snap_last`` (once the board has shown it sets that bit;
  before, each UPDATE is shown as it comes). A keyframe is staged and shown on ``key_last``.
- A ``seq`` gap: stop presenting, send ``KEY``, show the next whole keyframe.
- ``PING`` every 1 s is the liveness probe: the board sends no heartbeat UPDATEs (nothing
  changed = no UPDATE; the board's §6.1 correction), so a still screen is silent but for
  its PONGs. No PONG for 3 s: ``stale``; for 10 s, or the stream ends: ``reconnecting``
  over the last picture, with back-off; the new connection sends KEY.
- ``fps`` counts whole SNAPs that carried tiles (a header-only change is not a frame).
- The RATE clamp is the board's own ``0x11`` reply (``status()["rate"]``), never an UPDATE.
- The last viewer leaves: the upstream closes 30 s later (a returning viewer reuses it).
- ``close(board, reason)`` closes it at once. The lease hooks (lane LM2, D3: only the lease
  holder sees the picture) call it, wired as XVC's (``services.xvc``): on the bus,
  ``lease.state`` released/expired/lost closes that board's upstream ("closed: the lease was
  lost"), and ``session.closed`` closes it with the board. Either close releases the source
  (``display_release``: the MPS3 drops its SSH forward), and the next viewer's connect asks
  the lease again. ``leases`` (the daemon's ``LeaseService``, as for XVC) is handed to every
  source that takes it (``use_leases``), so the source's "is it mine" is the hub API's.
- The board's refusal line (a third client): ``refused``, try again in 10 s. A source that
  raises ``DisplayUnavailable``: ``down`` with its reason, again after its ``retry_s`` (None:
  not until a viewer asks again). Any other ``HarnessError`` (claim lost, key refused, host
  key changed): ``down``, and the upstream stops.

**Viewers: per-viewer dirty sets, drop-to-latest, never stale.** A viewer's dirty set is the
tiles whose latest record it has not been sent. ``next_message()`` sends exactly those, as
ONE UPDATE in the board's own layout (the records forwarded as the board encoded them),
and with ``ack=True`` then waits for ``ack()`` before it sends another: a slow viewer only
ever gets fewer, fresher messages, and never slows the others. Its first message is a
keyframe. The events ``Outbox`` is NOT used: it drops the OLDEST frames, which leaves tiles
stale for ever (§7.3).

Events: ``display.state`` ``{state, mode, owner, badges, reason}`` on every change.
States: ``down``, ``connecting``, ``syncing`` (waiting for a whole keyframe), ``live``,
``stale``, ``reconnecting``, ``refused``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harness_manager.core.display import (
    DISPLAY_MIRROR,
    NTILES,
    DimDebounce,
    DisplayFrame,
    DisplayInfo,
    DisplayPicture,
    DisplayStream,
    DisplayUnavailable,
    DisplayUpdate,
    Pong,
    RateEcho,
    Refusal,
    WireError,
    as_display_source,
    badges,
    decode_parts,
    owner_name,
)
from harness_manager.core.errors import HarnessError, UnavailableError, UnreachableError
from harness_manager.core.events import Event, EventBus

log = logging.getLogger(__name__)

__all__ = ["DisplayService", "DisplayViewer", "DisplayTimings", "TOPIC", "STATES",
           "DEFAULT_RATE_HZ"]

CAPABILITY = DISPLAY_MIRROR
TOPIC = "display.state"
STATES = ("down", "connecting", "syncing", "live", "stale", "reconnecting", "refused")
#: ``lease.state`` states that end the holder's view (D3), as ``services.xvc`` closes XVC.
LEASE_ENDS = ("released", "expired", "lost")
#: What a viewer asks for unless it says (the board clamps: 20-30 Hz is the panel's own pace).
DEFAULT_RATE_HZ = 20
_SEQ_MASK = 0xFFFFFFFF


@dataclass(frozen=True)
class DisplayTimings:
    """Every clock the compositor keeps (§7.2); tests shrink them."""

    grace_s: float = 30.0            # close the upstream this long after the last viewer
    ping_s: float = 1.0
    stale_s: float = 3.0             # no PONG: stale (PING is the liveness probe)
    dead_s: float = 10.0             # ... reconnect
    tick_s: float = 0.1              # the reader's poll: pings, clocks, rate changes
    hello_timeout_s: float = 10.0
    key_retry_s: float = 5.0         # KEY again if no keyframe has started this long after one
    backoff_s: tuple[float, ...] = (0.2, 0.5, 1.0, 2.0, 4.0, 8.0)
    refused_retry_s: float = 10.0    # the board's refusal line (its client slots are full)
    no_service_retry_s: float = 60.0  # a stream that closes before HELLO, with a known reason
    dim_persist_s: float = 1.0
    fps_window_s: float = 2.0


def _bus_of(engine: Any) -> EventBus | None:
    if engine is None:
        return None
    if isinstance(engine, EventBus):
        return engine
    return getattr(engine, "bus", None)


class _Refused(ConnectionError):
    def __init__(self, err: str) -> None:
        super().__init__(err)
        self.err = err


class _Dead(ConnectionError):
    """The board stopped answering PINGs for ``dead_s``."""


class _BeforeHello(ConnectionError):
    """The stream ended before HELLO (an SSH -L accepts, then closes, when the far end refuses)."""


# --- viewers -----------------------------------------------------------------------------------


class DisplayViewer:
    """One viewer of one board: a browser tab's WebSocket, a PNG request, the CLI.

    ``wake`` (any thread, must be quick) is called whenever ``next_message()`` or
    ``next_status()`` may have something new. ``next_message()`` returns ONE UPDATE (bytes,
    the board's layout) holding every tile whose latest record this viewer lacks, or None.
    With ``ack=True`` it then returns None until ``ack()``.
    """

    def __init__(self, board: _Board, *, ack: bool, rate: int,
                 wake: Callable[[], None] | None) -> None:
        self._board = board
        self.board_id = board.board_id
        self.ack_mode = ack
        self.rate = rate
        self.wake = wake
        self._sent: list[bytes | None] = [None] * NTILES   # the record last sent, per tile
        self._sent_sig: tuple[Any, ...] | None = None
        self._keyed = False
        self._inflight: int | None = None
        self._status_sig: tuple[Any, ...] | None = None
        self.closed = False
        self.messages = 0
        self.bytes = 0
        self.acks = 0

    # -- the picture ------------------------------------------------------------------------

    def dirty(self) -> list[int]:
        """The tiles whose latest record this viewer has not been sent (the board's lock held)."""
        f = self._board.frame
        recs, vl, sent = f.records, f.valid_list, self._sent
        return [t for t in range(NTILES) if vl[t] and recs[t] is not None and recs[t] != sent[t]]

    def next_message(self) -> bytes | None:
        b = self._board
        with b.lock:
            f = b.frame
            if self.closed or not f.presented:
                return None
            if self.ack_mode and self._inflight is not None:
                return None
            if not self._keyed:
                tiles = [t for t in range(NTILES) if f.held(t)]
                self._sent = [None] * NTILES
                key = True
            else:
                tiles = self.dirty()
                key = False
                if not tiles and self._sent_sig == f.header_sig():
                    return None
            msg = f.viewer_update(tiles, key=key)
            for t in tiles:
                self._sent[t] = f.records[t]
            self._sent_sig = f.header_sig()
            self._keyed = True
            if self.ack_mode:
                self._inflight = f.seq
            self.messages += 1
            self.bytes += len(msg)
            return msg

    def ack(self, seq: int | None = None) -> None:
        """The viewer drew the message in flight (``{"ack": seq}``). Only one is ever in
        flight, so any ack is for it."""
        with self._board.lock:
            self.acks += 1
            was = self._inflight
            self._inflight = None
        if was is not None:
            self._wake()

    @property
    def in_flight(self) -> int | None:
        return self._inflight

    def pending(self) -> bool:
        """``next_message()`` would return something now."""
        b = self._board
        with b.lock:
            f = b.frame
            if self.closed or not f.presented or (self.ack_mode and self._inflight is not None):
                return False
            return not self._keyed or bool(self.dirty()) or self._sent_sig != f.header_sig()

    # -- the rest ---------------------------------------------------------------------------

    def set_rate(self, hz: int) -> None:
        """This viewer's wish (``{"rate": hz}``); the upstream asks for the max of all."""
        self.rate = max(0, min(255, int(hz)))

    def status(self) -> dict[str, Any]:
        return self._board.status()

    def next_status(self) -> dict[str, Any] | None:
        """The board's status when what a viewer shows about it changed since the last call
        (state, reason, badges, owner, mode, rate), else None."""
        st = self._board.status()
        sig = (st["state"], st["reason"], tuple(b["key"] for b in st["badges"]), st["owner"],
               st["mode"], st["rate"])
        if sig == self._status_sig:
            return None
        self._status_sig = sig
        return st

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._board.viewer_closed(self)

    def _wake(self) -> None:
        if self.wake is not None and not self.closed:
            try:
                self.wake()
            except RuntimeError:                      # the viewer's event loop has gone
                pass
            except Exception:  # noqa: BLE001 - a viewer's callback never breaks the upstream
                log.exception("display viewer wake failed")

    def __enter__(self) -> DisplayViewer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- one board ---------------------------------------------------------------------------------


class _Board:
    def __init__(self, service: DisplayService, board_id: str, source: Any) -> None:
        self.svc = service
        self.t = service.timings
        self.clock = service.clock
        self.board_id = board_id
        self.source = source
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.frame = DisplayFrame()
        self.viewers: list[DisplayViewer] = []
        self.state, self.reason = "down", ""
        self.info: DisplayInfo | None = None
        self.stop = threading.Event()
        self.stream: DisplayStream | None = None
        self.idle_since: float | None = None
        self.finished = False
        self.end_reason = ""
        self.rate_asked: int | None = None
        self.rate_echo: int | None = None
        self.rtt_ms: float | None = None
        self.debounce = DimDebounce(self.t.dim_persist_s)
        self.dims: frozenset[str] = frozenset()
        self.presents: deque[float] = deque()
        self.rx: deque[tuple[float, int]] = deque()
        self.last_pong = self.clock()
        self._released = False
        self._last_event: tuple[Any, ...] | None = None
        self.stats = {"connects": 0, "updates": 0, "acks_sent": 0, "keys_sent": 0,
                      "keys_presented": 0, "deltas_presented": 0, "gaps": 0, "refusals": 0,
                      "protocol_errors": 0, "ignored": 0, "pongs": 0}
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"display-{board_id}")

    # -- viewers ----------------------------------------------------------------------------

    def viewer_closed(self, v: DisplayViewer) -> None:
        with self.lock:
            if v in self.viewers:
                self.viewers.remove(v)
            if not self.viewers and self.idle_since is None:
                self.idle_since = self.clock()

    def wanted_rate(self) -> int:
        with self.lock:
            rates = [v.rate for v in self.viewers]
        if rates:
            return max(rates)
        return self.rate_asked if self.rate_asked is not None else self.svc.default_rate

    def _wake_all(self) -> None:
        with self.lock:
            viewers = list(self.viewers)
        for v in viewers:
            v._wake()

    # -- state ------------------------------------------------------------------------------

    def set_state(self, state: str, reason: str = "") -> None:
        with self.lock:
            if self.finished or (state, reason) == (self.state, self.reason):
                return                               # a finished board keeps its last word
            self.state, self.reason = state, reason
            self.cond.notify_all()
        self.publish()
        self._wake_all()

    def badge_list(self) -> list[Any]:
        f = self.frame
        if not f.presented:
            return []
        return badges(f.flags(), f.owner, self.info.mode if self.info else "", f.panel_regs(),
                      dims=self.dims)

    def status(self) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            f = self.frame
            win = self.t.fps_window_s
            fps = sum(1 for t in self.presents if now - t <= win) / win
            bps = 0.0
            if len(self.rx) >= 2 and self.rx[-1][0] > self.rx[0][0]:
                bps = (self.rx[-1][1] - self.rx[0][1]) / (self.rx[-1][0] - self.rx[0][0])
            return {
                "board": self.board_id, "state": self.state, "reason": self.reason,
                "mode": self.info.mode if self.info else "",
                "hello": self.info.to_json() if self.info else None,
                "owner": owner_name(f.owner) if f.presented else "unknown",
                "flags": f.flags().to_json(), "regs": f.panel_regs().to_json(),
                "badges": [b.to_json() for b in self.badge_list()],
                "presented": f.presented, "hatched": f.hatched(), "seq": f.seq,
                "t_ms": f.t_ms, "frames": f.frames, "resets": f.resets,
                "rtt_ms": None if self.rtt_ms is None else round(self.rtt_ms, 1),
                "rate": self.rate_echo, "rate_asked": self.rate_asked, "fps": round(fps, 1),
                "bytes_per_s": round(bps), "viewers": len(self.viewers),
                "counters": dict(self.stats),
            }

    def publish(self) -> None:
        bus = self.svc.bus
        with self.lock:
            f = self.frame
            data = {"state": self.state, "mode": self.info.mode if self.info else "",
                    "owner": owner_name(f.owner) if f.presented else "unknown",
                    "badges": [b.to_json() for b in self.badge_list()], "reason": self.reason}
            sig = (data["state"], data["mode"], data["owner"],
                   tuple(b["key"] for b in data["badges"]), data["reason"])
            if sig == self._last_event:
                return
            self._last_event = sig
        if bus is not None:
            bus.publish(Event(TOPIC, self.board_id, data))

    # -- the upstream -----------------------------------------------------------------------

    def idle_done(self) -> bool:
        """The grace ran out with no viewer: finish now, atomically with ``attach``."""
        with self.svc._guard, self.lock:
            if self.viewers or self.idle_since is None:
                return False
            if self.clock() - self.idle_since < self.t.grace_s:
                return False
            self._finish_locked(f"closed: no viewer for {self.t.grace_s:g} s")
            return True

    def _finish_locked(self, reason: str) -> None:
        """svc._guard and self.lock held: the board's last word is ``down`` with ``reason``
        (published by whoever finishes it, once the locks are released)."""
        if self.finished:
            return
        self.state, self.reason = "down", reason
        self.finished = True
        if self.svc._boards.get(self.board_id) is self:
            del self.svc._boards[self.board_id]
        self.svc._last[self.board_id] = self
        self.cond.notify_all()

    def announce_end(self) -> None:
        self.publish()
        self._wake_all()

    def close_stream(self) -> None:
        with self.lock:
            ds = self.stream
        if ds is not None:
            ds.close()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        rel = getattr(self.source, "display_release", None)
        if rel is not None:
            try:
                rel()
            except Exception:  # noqa: BLE001 - releasing never raises into the service
                log.exception("display_release failed for %s", self.board_id)

    def _wait(self, seconds: float) -> bool:
        """Back off for ``seconds``; True when the upstream should end (stopped, or idle)."""
        deadline = self.clock() + seconds
        while True:
            if self.stop.is_set() or self.idle_done():
                return True
            left = deadline - self.clock()
            if left <= 0:
                return False
            self.stop.wait(min(left, max(self.t.tick_s, 0.05)))

    def _diagnose(self, since: float) -> str:
        fn = getattr(self.source, "display_diagnose", None)
        if fn is None:
            return ""
        try:
            return str(fn(since) or "")
        except Exception:  # noqa: BLE001 - a diagnosis is best effort
            log.exception("display_diagnose failed for %s", self.board_id)
            return ""

    def _run(self) -> None:
        attempt = 0
        try:
            while not self.stop.is_set():
                if self.idle_done():
                    return
                t0 = self.clock()
                wait = 0.0
                try:
                    outcome = self._session()
                    if outcome in ("stop", "idle"):
                        return
                    attempt = 0
                except _Refused as exc:
                    self.stats["refusals"] += 1
                    self.set_state("refused", f"the board refused this viewer: {exc.err}")
                    wait = self.t.refused_retry_s
                except DisplayUnavailable as exc:
                    if exc.retry_s is None:
                        self.end_reason = exc.reason
                        return
                    self.set_state("down", exc.reason)
                    wait = exc.retry_s
                except (OSError, EOFError, WireError, UnreachableError) as exc:
                    if self.stop.is_set():
                        return
                    if isinstance(exc, WireError):
                        self.stats["protocol_errors"] += 1
                    why = exc.message if isinstance(exc, HarnessError) else str(exc) or type(exc).__name__
                    diag = self._diagnose(t0) if isinstance(exc, (_BeforeHello, UnreachableError)) else ""
                    if diag:
                        self.set_state("down", diag)
                        wait = self.t.no_service_retry_s
                    else:
                        self.set_state("reconnecting", why)
                        wait = self.t.backoff_s[min(attempt, len(self.t.backoff_s) - 1)]
                        attempt += 1
                except HarnessError as exc:        # claim lost, key refused, host key changed
                    self.end_reason = exc.message
                    return
                finally:
                    self.close_stream()
                    with self.lock:
                        self.stream = None
                if self._wait(wait):
                    return
        except Exception as exc:  # noqa: BLE001 - never die silently
            log.exception("display upstream for %s failed", self.board_id)
            self.end_reason = f"internal error: {type(exc).__name__}: {exc}"
        finally:
            with self.svc._guard, self.lock:
                self._finish_locked(self.end_reason or "closed")
            self.release()
            self.announce_end()

    def _session(self) -> str:
        t = self.t
        self.set_state("connecting", self.reason if self.state == "reconnecting" else "")
        raw = self.source.display_connect()
        ds = DisplayStream(raw)
        with self.lock:
            self.stream = ds
            self.rx.clear()
        if self.stop.is_set():
            return "stop"
        self.stats["connects"] += 1
        # HELLO first
        deadline = self.clock() + t.hello_timeout_s
        info: DisplayInfo | None = None
        while info is None:
            if self.stop.is_set():
                return "stop"
            if self.clock() > deadline:
                raise _Dead(f"no HELLO from the board within {t.hello_timeout_s:g} s")
            try:
                msgs = ds.read(t.tick_s)
            except EOFError as exc:
                raise _BeforeHello(f"the board closed the stream before HELLO ({exc})") from exc
            for m in msgs:
                if isinstance(m, Refusal):
                    raise _Refused(m.err)
                if not isinstance(m, DisplayInfo):
                    raise WireError(f"the board's first message is {type(m).__name__}, not HELLO")
                info = m
                break
        why = info.supported()
        if why:
            raise DisplayUnavailable(f"this board's lcd_mirror serves {why}", retry_s=None)
        ds.reader.max_msg = info.max_msg
        with self.lock:
            self.info = info
            self.frame.info = info
            self.frame.new_connection()
        self.rate_asked = self.wanted_rate()
        ds.rate(self.rate_asked)
        ds.key()                                     # amendment 6: KEY on start
        self.stats["keys_sent"] += 1
        self.set_state("syncing", "")
        return self._pump(ds)

    def _pump(self, ds: DisplayStream) -> str:
        t = self.t
        last_seq: int | None = None
        resync = True                                # nothing is presented before a whole keyframe
        staging: list[DisplayUpdate] | None = None
        pending: list[DisplayUpdate] = []
        snap_last_seen = False
        pings: dict[int, float] = {}
        token = 0
        now = self.clock()
        self.last_pong = now                         # the clock starts at HELLO
        next_ping = now
        next_tick = now
        key_sent_at = now

        def resync_now(why: str) -> None:
            nonlocal resync, staging, pending, key_sent_at
            resync, staging, pending = True, None, []
            ds.key()                                 # amendment 6: KEY after a gap
            key_sent_at = self.clock()
            self.stats["keys_sent"] += 1
            self.set_state("syncing", why)

        while True:
            if self.stop.is_set():
                return "stop"
            msgs = ds.read(t.tick_s)
            now = self.clock()
            for m in msgs:
                if isinstance(m, DisplayUpdate):
                    u = m
                    self.stats["updates"] += 1
                    ds.ack(u.seq)                    # H1: every UPDATE, on receipt (cumulative)
                    self.stats["acks_sent"] += 1
                    if last_seq is not None and u.seq != (last_seq + 1) & _SEQ_MASK:
                        self.stats["gaps"] += 1
                        resync_now(f"seq gap ({last_seq} -> {u.seq}): waiting for a keyframe")
                    last_seq = u.seq
                    if u.key:
                        if u.key_first:
                            staging = [u]
                        elif staging is not None:
                            staging.append(u)
                        else:
                            self.stats["ignored"] += 1          # a key part without its first
                        if u.key_last and staging is not None:
                            self._present(staging, key=True)
                            staging, pending, resync = None, [], False
                    elif staging is not None:                    # H3: key parts are consecutive
                        self.stats["protocol_errors"] += 1
                        resync_now("a keyframe was interrupted: waiting for another")
                    elif resync:
                        self.stats["ignored"] += 1
                    else:
                        pending.append(u)
                        if u.snap_last or not snap_last_seen:    # H1: show a SNAP whole
                            self._present(pending, key=False)
                            pending = []
                    if u.snap_last:
                        snap_last_seen = True
                elif isinstance(m, Pong):                # the liveness probe (§6.1 correction 4)
                    self.stats["pongs"] += 1
                    self.last_pong = now
                    sent = pings.pop(m.token, None)
                    if sent is not None:
                        self.rtt_ms = (now - sent) * 1e3
                    if self.state == "stale" and not resync:
                        self.set_state("live", "")
                elif isinstance(m, RateEcho):
                    self.rate_echo = m.hz
                    self._wake_all()                 # a viewer's status shows the rate
                elif isinstance(m, Refusal):
                    raise _Refused(m.err)
                elif isinstance(m, DisplayInfo):
                    raise WireError("a second HELLO on one connection")
                # anything else: a type this reading does not know, skipped
            if now < next_tick:
                continue
            next_tick = now + t.tick_s
            # the clocks
            with self.lock:
                self.rx.append((now, ds.bytes_in))
                while len(self.rx) > 2 and now - self.rx[0][0] > t.fps_window_s:
                    self.rx.popleft()
            if now >= next_ping:
                token = (token + 1) & _SEQ_MASK
                pings[token] = now
                for old in [k for k, v in pings.items() if now - v > t.dead_s]:
                    del pings[old]
                ds.ping(token)
                next_ping = now + t.ping_s
            if resync and staging is None and now - key_sent_at >= t.key_retry_s:
                ds.key()                             # the board answers PING but sent no keyframe
                key_sent_at = now
                self.stats["keys_sent"] += 1
            silence = now - self.last_pong
            if silence >= t.dead_s:
                raise _Dead(f"the board has not answered a PING for {silence:.0f} s")
            if silence >= t.stale_s and self.state == "live":
                self.set_state("stale", f"no answer to PING for {silence:.0f} s")
            want = self.wanted_rate()
            if want != self.rate_asked:
                self.rate_asked = want
                ds.rate(want)
            if self.frame.presented:
                with self.lock:
                    dims = self.debounce.update(self.frame.flags(), now)
                    changed = dims != self.dims
                    self.dims = dims
                if changed:
                    self.publish()
                    self._wake_all()
            if self.idle_done():
                return "idle"

    def _present(self, parts: list[DisplayUpdate], *, key: bool) -> None:
        decoded = decode_parts(parts)            # WireError: the stream is untrusted, reconnect
        now = self.clock()
        with self.lock:
            self.frame.commit(parts, decoded, key=key)
            if decoded:                          # fps: whole SNAPs that carried tiles
                self.presents.append(now)
            while self.presents and now - self.presents[0] > self.t.fps_window_s:
                self.presents.popleft()
            self.stats["keys_presented" if key else "deltas_presented"] += 1
            self.dims = self.debounce.update(self.frame.flags(), now)
            self.cond.notify_all()
        if self.state not in ("live", "stale"):       # stale ends on a PONG, not an UPDATE
            self.set_state("live", "")
        self.publish()
        self._wake_all()


# --- the service -------------------------------------------------------------------------------


class DisplayService:
    """Live display mirrors, one upstream per board (``docs/design/LCD_MIRROR.md`` §7.1).

    ``engine``: the Engine (its ``bus``), an ``EventBus`` or None; with a bus the lease and
    session hooks are wired (the module docstring). ``timings``: the compositor's clocks
    (``DisplayTimings``). ``clock``: monotonic seconds (tests). ``leases``: the lease service
    handed to sources (``use_leases``); the daemon sets the hub API's, as it does for XVC.
    """

    def __init__(self, engine: Any = None, *, timings: DisplayTimings | None = None,
                 default_rate: int = DEFAULT_RATE_HZ,
                 clock: Callable[[], float] = time.monotonic, leases: Any = None) -> None:
        self.bus = _bus_of(engine)
        self.timings = timings or DisplayTimings()
        self.default_rate = default_rate
        self.clock = clock
        self.leases = leases
        self._boards: dict[str, _Board] = {}
        self._guard = threading.Lock()
        self._last: dict[str, _Board] = {}           # the board as it ended, per board
        self.threads: list[threading.Thread] = []    # lease/session close workers (tests join)
        self._unsubs: list[Callable[[], None]] = []
        if self.bus is not None:
            self._unsubs += [
                self.bus.subscribe("lease.state", self._on_lease_state),
                self.bus.subscribe("session.closed", self._on_session_closed),
            ]

    # -- viewers ----------------------------------------------------------------------------

    def attach(self, board_id: str, source: Any, *, ack: bool = True, rate: int | None = None,
               wake: Callable[[], None] | None = None) -> DisplayViewer:
        """A new viewer of ``board_id``; opens the board's upstream through ``source`` (a
        ``DisplayAdapter`` or a connector callable) when none is open."""
        src = as_display_source(source)
        use_leases = getattr(src, "use_leases", None)
        if self.leases is not None and callable(use_leases):
            use_leases(self.leases)              # one view of "mine" with the hub API (D3)
        start = False
        with self._guard:
            b = self._boards.get(board_id)
            if b is None or b.finished:
                b = _Board(self, board_id, src)
                self._boards[board_id] = b
                start = True
            else:
                b.source = src                   # the next connect uses the newest source
            with b.lock:
                v = DisplayViewer(b, ack=ack, rate=self.default_rate if rate is None else rate,
                                  wake=wake)
                b.viewers.append(v)
                b.idle_since = None
                ready = b.frame.presented
        if start:
            b.set_state("connecting")
            b.thread.start()
        if ready:
            v._wake()
        return v

    def detach(self, viewer: DisplayViewer) -> None:
        viewer.close()

    def picture(self, board_id: str, source: Any = None, *, wait_s: float = 10.0) -> DisplayPicture:
        """The presented picture. Opens the upstream if needed (through ``source``) and waits
        up to ``wait_s`` for a keyframe; the upstream then closes after the grace."""
        with self._guard:
            b = self._boards.get(board_id)
        if b is not None:
            with b.lock:
                if b.frame.presented:
                    return b.frame.picture(badge_list=b.badge_list())
        if source is None and b is None:
            raise UnavailableError(CAPABILITY, "no live display is open for this board")
        v = self.attach(board_id, source if source is not None else b.source, ack=False)
        try:
            vb = v._board
            deadline = self.clock() + wait_s
            with vb.cond:
                while not vb.frame.presented and not vb.finished:
                    left = deadline - self.clock()
                    if left <= 0:
                        break
                    vb.cond.wait(min(left, 0.25))
                if vb.frame.presented:
                    return vb.frame.picture(badge_list=vb.badge_list())
                state, reason = vb.state, vb.reason
            raise DisplayUnavailable(
                f"no picture from the board within {wait_s:g} s ({state}"
                + (f": {reason})" if reason else ")"), retry_s=None)
        finally:
            v.close()

    # -- status -----------------------------------------------------------------------------

    def status(self, board_id: str) -> dict[str, Any]:
        with self._guard:
            b = self._boards.get(board_id)
        if b is not None:
            return b.status()
        last = self._last.get(board_id)
        if last is not None:
            return last.status()
        return {"board": board_id, "state": "down", "reason": "", "mode": "", "hello": None,
                "owner": "unknown", "badges": [], "presented": False, "hatched": NTILES,
                "viewers": 0}

    def boards(self) -> list[str]:
        with self._guard:
            return sorted(self._boards)

    # -- closing ----------------------------------------------------------------------------

    def close(self, board_id: str, reason: str = "closed") -> dict[str, Any]:
        """Close the board's upstream at once (lease released/expired/lost, unclaim). Its
        viewers see ``down`` with ``reason``; a later ``attach`` opens a new one."""
        with self._guard:
            b = self._boards.get(board_id)
            if b is not None:
                with b.lock:
                    b._finish_locked(reason)
        if b is None:
            return self.status(board_id)
        b.stop.set()
        b.close_stream()
        if b.thread.is_alive() and b.thread is not threading.current_thread():
            b.thread.join(timeout=5)
        b.release()
        b.announce_end()
        return self.status(board_id)

    def close_all(self, reason: str = "closed") -> None:
        for bid in self.boards():
            self.close(bid, reason)

    # -- the lease and session hooks (lane LM2) -----------------------------------------------

    def _on_lease_state(self, event: Event) -> None:
        state = str(event.data.get("state") or "")
        if state in LEASE_ENDS:
            self._close_soon(event.board_id, f"closed: the lease was {state}")

    def _on_session_closed(self, event: Event) -> None:
        self._close_soon(event.board_id, "closed: the board was closed")

    def _close_soon(self, board_id: str, reason: str) -> None:
        """Close at once, on a worker: ``close`` joins the reader, and the publisher must not
        wait for it (as ``services.xvc`` does on a lease change)."""
        with self._guard:
            if board_id not in self._boards:
                return
        worker = threading.Thread(target=self.close, args=(board_id, reason), daemon=True,
                                  name=f"display-close-{board_id}")
        self.threads.append(worker)
        worker.start()

    def shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.close_all("Harness Manager is shutting down")
