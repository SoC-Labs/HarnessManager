"""An open board nobody uses gives its hub lease back (lane IDLE-LEASE, HM v1.1.0).

**Why.** The service heartbeats an open board's hub lease for as long as the board stays
open here, even after the app window has closed. On 2026-10-08 a board stayed leased for
about three hours with nobody using it, and another team's soak waited for it.

**The rule.** ``lease.idle_release_min`` (minutes, default 30; 0: never). A board is IDLE when
none of these has happened for that long:

- a request on the board that is use of it: anything the CLI or a script sends (reads too),
  and anything the page sends but its own reads (``X-HM-Client: page`` on a GET or HEAD) and
  its viewer registration. A background read (``X-HM-Background``) is never use;
- the page SHOWING the board (a registered viewer, QUIET-POLL: dropped when the window closes
  or looks at another board, lapsed 45 s after the last refresh);
- and, checked on every round, something still running or attached (``holds``): a job on the
  board (a deploy, a slot write, an SD install, a build ...), a checks run, an attached
  console (web, PTY or TCP), a debug session, an XVC session, a live display viewer. These
  keep the idle clock at zero for as long as they last: a write is never interrupted.

Only a board whose lease is held HERE (its token in this service's store) is watched. Two
minutes before the limit (``WARN_S``; half the limit when that is shorter) ``lease.idle``
says ``warning`` with the time it goes; the page shows a banner with "Keep it"
(``POST /boards/{bid}/lease/keep``), which, like any use, puts the clock back to zero and
says ``kept``. At the limit, and never sooner than the warning's promise, the board is
CLOSED through the user's own Close (``Daemon.close_board``: a guard or a job refuses it,
the release goes first and a failed release leaves the board open), with the release only
when the lease is one THIS service took (``LeaseService.acquired_here``). A lease someone
took in a terminal (``harness-manager lease acquire``) is theirs to give back: the board is
closed without it, so the heartbeat stops and the lease lapses at its expiry. Then
``lease.idle`` says ``released`` (or ``closed``), and the log says "lease released after N
min idle". A close that fails says ``failed`` once and is tried again after ``RETRY_S``.

Event ``lease.idle {state: warning|kept|released|closed|failed, board, idle_min, limit_min,
release_at, text}`` (``release_at`` ISO UTC, with ``warning``). ``GET
/boards/{bid}/lease/idle`` gives the same state for a page that was not listening.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from harness_manager import naming
from harness_manager.core.errors import HarnessError
from harness_manager.core.events import Event
from harness_manager.services.quiet import request_is_background

log = logging.getLogger(__name__)

TOPIC = "lease.idle"
SETTING = "lease.idle_release_min"
DEFAULT_MIN = 30
#: The warning comes this long before the release (half the limit when that is shorter).
WARN_S = 120.0
#: How often the service looks (a round is local: no board, no hub).
TICK_S = 15.0
#: A close that failed (the hub did not answer the release) is tried again after this.
RETRY_S = 60.0
#: The page marks its own requests with this header (web/static/js/api.js).
CLIENT_HEADER = "x-hm-client"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mins(seconds: float) -> int:
    return max(0, int(seconds // 60))


def _plural(n: int) -> str:
    return f"{n} min"


def warning_text(name: str, left_s: float, idle_s: float, own: bool) -> str:
    left = max(1, round(left_s / 60))
    if own:
        return (f"{name}'s lease is released in {_plural(left)}: nothing has used it for "
                f"{_plural(_mins(idle_s))}. Use the board to keep it.")
    return (f"{name} is closed in {_plural(left)}: nothing has used it for "
            f"{_plural(_mins(idle_s))}. Use the board to keep it. Its lease was taken outside "
            "the app, so it is not released here: it runs out at its expiry.")


def released_text(name: str, idle_s: float) -> str:
    return (f"{name}'s lease was released after {_plural(_mins(idle_s))} with nothing using it, "
            "and the board was closed. Open it again to take the lease again.")


def closed_text(name: str, idle_s: float) -> str:
    return (f"{name} was closed after {_plural(_mins(idle_s))} with nothing using it. Its lease "
            "was taken outside the app, so it was not released: it runs out at its expiry.")


def page_request_is_use(method: str, path: str, headers: Mapping[str, str] | Any) -> bool:
    """Whether a request on a board counts as use of it (see the module's rule)."""
    if request_is_background(headers):
        return False
    page = str(headers.get(CLIENT_HEADER, "") or "").strip().lower() == "page"
    if path.rstrip("/").endswith("/lease/idle") or "/viewers/" in path:
        return False                  # reading the idle state, or the page saying it looks
    if page and method in ("GET", "HEAD"):
        return False                  # the page's own reads: it is a viewer while it shows it
    return True


@dataclass
class _Board:
    last: float                       # monotonic: the last use
    warned_at: float | None = None    # monotonic: when the warning went out
    release_at: float | None = None   # monotonic: when the warning said it goes
    retry_at: float = 0.0             # monotonic: a failed close is not tried before this
    failed: bool = False


class IdleLeases:
    """The idle clock of every open board, and the round that releases.

    ``clock``/``wall`` are injectable (tests use a fake clock and call ``tick()``);
    ``limit_min`` returns the setting (None: read ``lease.idle_release_min``)."""

    def __init__(self, d: Any, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 limit_min: Callable[[], int] | None = None, tick_s: float = TICK_S) -> None:
        self.d = d
        self.clock = clock
        self.wall = wall
        self._limit_min = limit_min
        self.tick_s = tick_s
        self._mu = threading.Lock()
        self._boards: dict[str, _Board] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the setting --------------------------------------------------------------------------

    def limit_s(self) -> float:
        """The limit in seconds; 0 means never."""
        try:
            if self._limit_min is not None:
                minutes = self._limit_min()
            else:
                from harness_manager.settings import runtime

                minutes = runtime.value(SETTING, state_dir=self.d.state_dir, strict=False)
        except (HarnessError, OSError, ValueError) as exc:
            log.warning("%s: %s; using %d", SETTING, exc, DEFAULT_MIN)
            minutes = DEFAULT_MIN
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = DEFAULT_MIN
        return max(0, minutes) * 60.0

    @staticmethod
    def warn_lead_s(limit_s: float) -> float:
        return min(WARN_S, limit_s / 2)

    # -- use ---------------------------------------------------------------------------------

    def touch(self, board_id: str | None, why: str = "") -> None:
        """The board was used now: its idle clock starts again (a warning is withdrawn)."""
        if not board_id:
            return
        now = self.clock()
        with self._mu:
            b = self._boards.get(board_id)
            if b is None:
                self._boards[board_id] = _Board(now)
                return
            warned = b.warned_at is not None
            b.last, b.warned_at, b.release_at, b.failed, b.retry_at = now, None, None, False, 0.0
        if warned:
            log.info("%s: used (%s); the idle release is off again", board_id, why or "use")
            self._publish(board_id, "kept", 0.0, self.limit_s(), why=why)

    def request(self, board_id: str | None, method: str, path: str, headers: Any) -> None:
        """An API request (``background_gate``): use of its board, unless the rule says not."""
        if board_id and page_request_is_use(method, path, headers):
            self.touch(board_id, f"{method} {path}")

    def forget(self, board_id: str) -> None:
        with self._mu:
            self._boards.pop(board_id, None)

    # -- what holds a board -------------------------------------------------------------------

    def holds(self, board_id: str) -> list[str]:
        """What uses the board right now (empty: nothing). Local state only: never the board,
        never the hub. A probe that fails counts as a hold: when in doubt, keep the lease."""
        out: list[str] = []
        d = self.d

        def probe(name: str, fn: Callable[[], Any]) -> None:
            try:
                if fn():
                    out.append(name)
            except Exception as exc:  # noqa: BLE001 - a broken probe must not release a board
                log.warning("idle check of %s (%s) failed: %s", board_id, name, exc)
                out.append(name)

        job = d.gates.busy(board_id)
        if job is not None:
            out.append(f"job {job.kind}")
        for guard in list(getattr(d, "close_guards", ())):
            try:
                guard(board_id)
            except HarnessError:
                out.append("checks run")
        quiet = getattr(d, "quiet", None)
        if quiet is not None:
            probe("viewer", lambda: quiet.viewers(board_id) > 0)
        services = getattr(d.engine, "_services", None) or {}
        consoles = services.get("consoles")
        if consoles is not None and callable(getattr(consoles, "attached", None)):
            probe("console", lambda: consoles.attached(board_id) > 0)
        debug = services.get("debug")
        if debug is not None and callable(getattr(debug, "running_here", None)):
            probe("debug", lambda: debug.running_here(board_id))
        xvc = services.get("xvc")
        if xvc is not None and callable(getattr(xvc, "open_boards", None)):
            probe("xvc", lambda: board_id in xvc.open_boards())
        display = getattr(d, "display", None)
        if display is not None and callable(getattr(display, "status", None)):
            probe("display", lambda: int((display.status(board_id) or {}).get("viewers") or 0) > 0)
        return out

    # -- the round ----------------------------------------------------------------------------

    def _hub(self, board_id: str) -> Any:
        try:
            return getattr(self.d.engine.session(board_id), "hub", None)
        except HarnessError:
            return None

    def _held_here(self, board_id: str) -> tuple[Any, bool] | None:
        """(hub, own) when this service holds the board's lease (its token is stored here)."""
        leases = getattr(self.d, "leases", None)
        hub = self._hub(board_id)
        if leases is None or hub is None or leases.store.get(hub.host, hub.target) is None:
            return None
        return hub, bool(leases.acquired_here(hub))

    def _name(self, board_id: str) -> str:
        try:
            return naming.display_name(self.d.engine.session(board_id).candidate) or board_id
        except Exception:  # noqa: BLE001 - a name is never a reason to fail
            return board_id

    def tick(self) -> None:
        """One round over the open boards. Never raises."""
        try:
            open_ids = list(self.d.engine.open_boards())
        except Exception:  # noqa: BLE001
            log.exception("idle lease round: the open boards")
            return
        with self._mu:
            for gone in [b for b in self._boards if b not in open_ids]:
                del self._boards[gone]
        limit = self.limit_s()
        for board_id in open_ids:
            try:
                self._one(board_id, limit)
            except Exception:  # noqa: BLE001 - the round must survive one board
                log.exception("idle lease round for %s", board_id)

    def _one(self, board_id: str, limit: float) -> None:
        now = self.clock()
        with self._mu:
            b = self._boards.setdefault(board_id, _Board(now))
        held = self._held_here(board_id)
        if limit <= 0 or held is None:
            # Off, or no lease of ours to give back: no clock runs (a lease taken later
            # starts it from then, never from when the board opened).
            self.touch(board_id, "no lease held here" if held is None else "idle release off")
            return
        holds = self.holds(board_id)
        if holds:
            self.touch(board_id, ", ".join(holds))
            return
        _, own = held
        idle = now - b.last
        lead = self.warn_lead_s(limit)
        if b.warned_at is None:
            if idle >= limit - lead:
                with self._mu:
                    b.warned_at = now
                    b.release_at = max(b.last + limit, now + lead)
                self._publish(board_id, "warning", idle, limit, own=own,
                              release_at=self.wall() + (b.release_at - now))
            return
        if now < (b.release_at or 0.0) or now < b.retry_at:
            return
        self._release(board_id, b, idle, limit, own)

    def _release(self, board_id: str, b: _Board, idle: float, limit: float, own: bool) -> None:
        name = self._name(board_id)
        try:
            out = self.d.close_board(board_id, release=own, only_ours=True)
        except HarnessError as exc:
            # A job or a checks run started after the round looked (it holds the board now),
            # or the hub did not answer the release: the board stays open; try again later.
            with self._mu:
                b.retry_at = self.clock() + RETRY_S
                first = not b.failed
                b.failed = True
            log.warning("%s: the idle release did not happen: %s", board_id, exc.message)
            if first:
                self._publish(board_id, "failed", idle, limit, own=own,
                              text=f"{name} was not released after {_plural(_mins(idle))} "
                                   f"idle: {exc.message}. Harness Manager tries again in a "
                                   "minute.")
            return
        self.forget(board_id)
        released = (out or {}).get("released")
        if released:
            log.info("%s: lease released after %d min idle", name, _mins(idle))
            self._publish(board_id, "released", idle, limit, own=True,
                          text=released_text(name, idle))
        else:
            log.info("%s: closed after %d min idle; its lease was taken outside the service "
                     "and is not released", name, _mins(idle))
            self._publish(board_id, "closed", idle, limit, own=False,
                          text=closed_text(name, idle))

    # -- what the page reads ------------------------------------------------------------------

    def state(self, board_id: str) -> dict[str, Any]:
        limit = self.limit_s()
        now = self.clock()
        with self._mu:
            b = self._boards.get(board_id)
            idle = (now - b.last) if b is not None else 0.0
            warned = b is not None and b.warned_at is not None
            release_at = (self.wall() + (b.release_at - now)) if warned and b.release_at else None
        held = self._held_here(board_id)
        return {"board_id": board_id, "limit_min": int(limit // 60), "watched": held is not None
                and limit > 0, "own": bool(held and held[1]), "idle_s": round(idle, 1),
                "warning": warned, "release_at": _iso(release_at) if release_at else None}

    def _publish(self, board_id: str, state: str, idle: float, limit: float, *,
                 own: bool = True, release_at: float | None = None, text: str = "",
                 why: str = "") -> None:
        name = self._name(board_id)
        if not text:
            if state == "warning":
                left = (release_at - self.wall()) if release_at is not None else WARN_S
                text = warning_text(name, left, idle, own)
            elif state == "kept":
                text = f"{name} is in use again: its lease is kept."
        data: dict[str, Any] = {"state": state, "board": name, "idle_min": _mins(idle),
                                "limit_min": int(limit // 60), "own": own, "text": text}
        if release_at is not None:
            data["release_at"] = _iso(release_at)
        if state == "warning":
            log.info("%s", text)
        self.d.bus.publish(Event(TOPIC, board_id, data))

    # -- the thread ---------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="idle-lease", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            self.tick()

    def close(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)


def register(ctx: Any) -> None:
    from .app import _JSON, ok

    d = ctx.daemon
    idle = IdleLeases(d)
    d.idle = idle

    def opened(ev: Event) -> None:
        idle.touch(ev.board_id, "opened")

    def closed(ev: Event) -> None:
        idle.forget(ev.board_id)

    def job_ended(ev: Event) -> None:
        idle.touch(ev.board_id, "a job ended")       # the clock starts after the job, not at it

    d.bus.subscribe("session.opened", opened)
    d.bus.subscribe("session.closed", closed)
    for topic in ("job.done", "job.failed"):
        d.bus.subscribe(topic, job_ended)

    original_close = d.close

    def close() -> None:
        idle.close()
        original_close()

    d.close = close
    if not getattr(d.engine, "fake_boards", False) or getattr(d, "idle_in_demo", False):
        idle.start()

    @ctx.api.post("/boards/{bid:path}/lease/keep")
    def lease_keep(bid: str) -> Any:
        ctx.board(bid)                                   # 404 for a board that is not open
        idle.touch(bid, "Keep it")
        return _JSON(ok(**idle.state(bid)))

    @ctx.api.get("/boards/{bid:path}/lease/idle")
    def lease_idle(bid: str) -> Any:
        ctx.board(bid)
        return _JSON(ok(**idle.state(bid)))
