"""Presence on the front panel, and what the panel shows (lane P1).

docs/design/CLCD_ALIGNMENT.md §2 and §5.1; ``harness_manager.core.panel`` is the model and
the wire's field caps. The board-side half (the Linux harness's ``hello``, ``panel`` and
``locate`` verbs, lanes R1-R3) is not built yet; the MPS3 adapter and its FakeShell profile
follow the design's wire.

**The beat.** For each board open in the Harness Manager service, a ``hello`` goes to the
board every ``BEAT_S`` (30 s), every ``FAST_BEAT_S`` (10 s) while a lease request or an
Identify is open. The board lists the session for 90 s after its last hello, so two missed
beats are survived. A due hello is first OFFERED to the adapter (``offer``): the next
control connection Harness Manager opens for any reason carries it (decision P1: presence
rides connections HM already makes). If nothing did within ``RIDE_WAIT_S``, the service
sends it itself, through the board's gate: while a job holds the board (a swap parks the
control port) the beat is SKIPPED, never queued, and the hello stays offered so the job's
own connections can carry it. A board whose harness lacks ``presence`` (bare metal) is
never sent a hello; its features are looked at again every beat.

**The hello** says who (``user@host``), the app, the board's N1 name, this session's role
(``holder`` of the hub lease, ``owner`` when standalone, else ``watch``), the lease as this
host last read it (the lease service's cached view: no extra hub calls) in RELATIVE seconds,
and a running job. ``core.panel.hello_message`` caps every field; the worst case is 251 B.

**The reply** is the panel's state and its ring of tap events. ``panel.state`` is published
when the state changes; each tap is published once as ``panel.tap`` (de-duplicated by
``seq``: the ring is never acknowledged, so every Harness Manager sees every tap). On first
contact only taps from the last beat are news. A ``seq`` that goes backwards means the
harness restarted its ring, and counting starts again.

**A tap on the lease-request banner notifies the holder; it never releases** (decision P2).
The lease service is asked through ``notify_holder(board_id, hub, *, seq, at)`` when it has
it (CCR PANEL-1). Until then the tap event itself is the notice: in the holder's own
Harness Manager (its lease view says ``mine``) ``panel.tap`` carries ``notify: "holder"`` and
the open request, so the holder's UI can say "someone at the board tapped the request".
Nothing here calls release, respond, force or leave.

**Quiet on a shared board** (lane QUIET-POLL, ``services/quiet.py``). A beat is background
contact: before each one the service asks ``allow(board_id)`` (the daemon's
``BackgroundGate.check``). While it says no (nobody views the board, the lease is someone
else's, the policy is ``off``, or the board just turned a connection away) nothing is sent
and nothing is offered to ride; the board is looked at again at the next beat, or when the
back-off ends. A beat the BOARD turned away (another client holds the control port: refused,
reset, timed out) is reported to ``noted(board_id, exc)`` and waits a whole beat; only a beat
skipped because one of our own jobs holds the board is retried in ``RETRY_S``.

Events: ``panel.state {page, owner, pending, banner, card, count, seq, source, touch,
sessions}``, ``panel.tap {seq, kind, on, ms_ago, at, notify, request?}``, ``panel.locate
{state: on|off, until, seconds, who}`` (docs/CONTRACTS.md).
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from typing import Any

from harness_manager import __version__
from harness_manager.cli.output import jsonable
from harness_manager.core import capabilities as C
from harness_manager.core.errors import HarnessError, HeldError, UnavailableError
from harness_manager.core.events import Event, EventBus
from harness_manager.core.panel import (
    BEAT_S,
    FAST_BEAT_S,
    SID_LEN,
    TTL_S,
    Hello,
    HelloJob,
    HelloLease,
    PanelEvent,
    PanelFrame,
    PanelState,
)

log = logging.getLogger(__name__)

TOPIC_STATE = "panel.state"
TOPIC_TAP = "panel.tap"
TOPIC_LOCATE = "panel.locate"
#: How long an offered hello waits for another connection to carry it.
RIDE_WAIT_S = 5.0
#: After a beat skipped because a job held the board, try again this soon.
RETRY_S = 10.0
TICK_S = 1.0
#: Reads are rate-limited on the board (docs/design §5.3): the state at most once a second,
#: a frame at most once every 3 s. Within that, the last answer is reused.
STATE_CACHE_S = 1.0
FRAME_CACHE_S = 3.0
IDENTIFY_DEFAULT_S = 10
IDENTIFY_MAX_S = 30
NO_ADAPTER = "this board has no front panel Harness Manager can reach"
NOT_BEATING = "presence runs in the Harness Manager service (harness-manager-daemon)"

#: Where a tap was, and what it means. Only a lease-request tap notifies anyone.
TAP_REQUEST = "request"


def default_who() -> str:
    """``user@host`` (short host name): the person and the machine, as the panel shows them."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in a container
        user = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return f"{user}@{socket.gethostname().split('.')[0]}"


def default_app() -> str:
    return f"hm/{__version__}"


def _parse_utc(text: Any) -> float | None:
    from harness_manager.services.lease import parse_utc

    return parse_utc(text)


def hello_lease(view: dict[str, Any] | None, now: float,
                me: str = "") -> tuple[str, HelloLease | None, bool]:
    """``(role, lease, request_open)`` from the lease service's view at wall time ``now``.

    No view, or no hub: standalone, so this session is the board's ``owner`` and there is
    no lease to relay. Behind a hub: ``holder`` when the lease is ours, else ``watch``; the
    lease's time left and an open request's time to answer become seconds from ``now``.
    ``request_open`` asks for the fast beat.
    """
    if not view or not view.get("hub"):
        return "owner", None, False
    lease = view.get("lease") or None
    by, left = "", None
    if lease:
        by = str(lease.get("holder") or lease.get("user") or "")
        expires = _parse_utc(lease.get("expires_at"))
        left = expires - now if expires is not None else None
    req, rl = "", None
    incoming = [i for i in view.get("incoming") or () if not i.get("answer")]
    mine = view.get("request") or None
    if incoming:                               # the holder: someone asked us
        req = str(incoming[0].get("by") or incoming[0].get("user") or "")
        deadline = _parse_utc(incoming[0].get("deadline_at"))
    elif mine and not mine.get("answer"):      # the requester: we asked
        req, deadline = me, _parse_utc(mine.get("deadline_at"))
    else:
        deadline = None
    if req and deadline is not None:
        rl = deadline - now
    role = "holder" if lease and lease.get("mine") else "watch"
    return role, HelloLease(by=by, left=left, q=len(view.get("queue") or ()), req=req, rl=rl), \
        bool(req)


def state_event_data(state: PanelState) -> dict[str, Any]:
    data = jsonable(state)
    data.pop("events", None)
    data.pop("observed_at", None)
    return data


def _changed(a: PanelState | None, b: PanelState) -> bool:
    if a is None:
        return True
    keep = ("page", "owner", "pending", "banner", "card", "count", "touch", "source")
    return any(getattr(a, k) != getattr(b, k) for k in keep) or \
        [(s.sid, s.role) for s in a.sessions] != [(s.sid, s.role) for s in b.sessions]


# --- the per-board record ------------------------------------------------------------------


@dataclass
class _Board:
    board_id: str
    session: Any
    sid: str
    next_due: float
    armed_at: float | None = None
    hello: Hello | None = None
    last_hello_at: float | None = None         # wall time of the last reply
    state: PanelState | None = None
    last_seq: int | None = None                # None: no contact yet
    unsupported: str = ""                      # why this board gets no hello
    last_error: str = ""
    quiet: str = ""                            # QUIET-POLL: why no background beat now
    sent: int = 0
    ridden: int = 0
    skipped: int = 0
    quieted: int = 0                           # beats the background gate held back
    fast: bool = False
    view: dict[str, Any] | None = None
    locate_until: float = 0.0
    cache: dict[str, tuple[float, Any]] = field(default_factory=dict)


@contextlib.contextmanager
def _no_gate(_board_id: str) -> Iterator[None]:
    yield


class PresenceService:
    """Presence for the boards open in this process, and the panel reads behind the API.

    ``lease_view(session)`` returns the lease service's view (``LeaseService.view``) or None;
    ``leases`` is the lease service itself (only its optional ``notify_holder``, CCR
    PANEL-1); ``job_of(board_id)`` returns a running ``HelloJob`` or None; ``gate(board_id)``
    is a context manager that raises ``HeldError`` at once while a job holds the board.
    ``allow(board_id)`` (QUIET-POLL) returns None when a background beat may go, else why
    not (``services.quiet.Quiet``, or any object with ``text``); None: always.
    ``noted(board_id, exc)`` hears a beat the board turned away.
    """

    def __init__(self, bus: EventBus | None = None, *,
                 lease_view: Callable[[Any], dict[str, Any] | None] | None = None,
                 leases: Any = None,
                 job_of: Callable[[str], HelloJob | None] | None = None,
                 gate: Callable[[str], AbstractContextManager[Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 beat_s: float = BEAT_S, fast_s: float = FAST_BEAT_S,
                 ride_wait_s: float = RIDE_WAIT_S, tick_s: float = TICK_S,
                 who: str | None = None, app: str | None = None,
                 allow: Callable[[str], Any] | None = None,
                 noted: Callable[[str, BaseException], None] | None = None) -> None:
        self.bus = bus
        self._allow = allow
        self._noted = noted
        self._lease_view = lease_view
        self._leases = leases
        self._job_of = job_of
        self._gate = gate or _no_gate
        self._clock = clock
        self._wall = wall
        self.beat_s = beat_s
        self.fast_s = fast_s
        self.ride_wait_s = ride_wait_s
        self.tick_s = tick_s
        self.who = who or default_who()
        self.app = app or default_app()
        self._mu = threading.RLock()
        self._boards: dict[str, _Board] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- tracking -------------------------------------------------------------------------

    def track(self, board_id: str, session: Any) -> None:
        """Start beating for an open board (its first hello is due at once)."""
        with self._mu:
            if board_id in self._boards:
                return
            self._boards[board_id] = _Board(board_id, session, secrets.token_hex(SID_LEN // 2),
                                            next_due=self._clock())

    def untrack(self, board_id: str) -> None:
        """The board closed: no more hellos. Its board-side session simply expires (TTL)."""
        with self._mu:
            rec = self._boards.pop(board_id, None)
        if rec is not None:
            withdraw = getattr(getattr(rec.session, "panel", None), "withdraw", None)
            if callable(withdraw):
                withdraw()

    def tracked(self) -> list[str]:
        with self._mu:
            return list(self._boards)

    def start(self) -> None:
        with self._mu:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="harness-manager-presence")
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.beat_due()
            except Exception:  # noqa: BLE001 - the beat must outlive any one board's bug
                log.exception("presence beat failed")

    def close(self) -> None:
        self._stop.set()
        for board_id in self.tracked():
            self.untrack(board_id)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    # -- the beat --------------------------------------------------------------------------

    def beat_due(self, now: float | None = None) -> None:
        """Send (or offer) every hello that is due. The thread calls this; tests call it."""
        now = self._clock() if now is None else now
        with self._mu:
            boards = list(self._boards.values())
        for rec in boards:
            try:
                self._beat(rec, now)
            except Exception:  # noqa: BLE001 - one board's failure never stops the others
                log.exception("presence beat for %s failed", rec.board_id)

    def _interval(self, rec: _Board) -> float:
        return self.fast_s if rec.fast or rec.locate_until > self._wall() else self.beat_s

    def _quiet(self, rec: _Board, panel: Any, now: float) -> bool:
        """QUIET-POLL: True when the background gate holds this beat back (nothing is sent,
        and an offered hello is withdrawn so no other connection carries it)."""
        if self._allow is None:
            return False
        try:
            why = self._allow(rec.board_id)
        except Exception:  # noqa: BLE001 - a gate that fails holds the beat back
            log.exception("the background gate for %s failed", rec.board_id)
            why = "the background gate could not decide"
        if why is None:
            rec.quiet = ""
            return False
        withdraw = getattr(panel, "withdraw", None)
        if callable(withdraw):
            withdraw()
        rec.quiet = str(getattr(why, "text", why))
        rec.quieted += 1
        retry = getattr(why, "retry_in_s", None)
        after = self.beat_s if not retry else min(self.beat_s, max(self.tick_s, float(retry)))
        self._later(rec, now, after)
        return True

    def _beat(self, rec: _Board, now: float) -> None:
        panel = getattr(rec.session, "panel", None)
        if panel is None:
            rec.unsupported = NO_ADAPTER
            return
        with self._mu:
            armed_at, due = rec.armed_at, rec.next_due
        if armed_at is not None:
            if now - armed_at < self.ride_wait_s:
                return
            withdraw = getattr(panel, "withdraw", None)
            if callable(withdraw) and not withdraw():
                with self._mu:                 # it rode meanwhile; the reply is handled
                    if rec.armed_at == armed_at:
                        rec.armed_at = None
                return
            if self._quiet(rec, panel, now):
                return
            self._send(rec, panel, now)
            return
        if now < due:
            return
        if self._quiet(rec, panel, now):
            return
        try:
            why = panel.support().presence
        except HarnessError as exc:        # not answering: look again at the next beat
            self._later(rec, now, self.beat_s,
                        error=f"cannot read the harness's features: {exc.message}")
            return
        if why:
            rec.unsupported = why
            self._later(rec, now, self.beat_s)
            return
        rec.unsupported = ""
        rec.hello = self.build_hello(rec)
        offer = getattr(panel, "offer", None)
        if callable(offer) and self.ride_wait_s > 0:
            try:
                offer(rec.hello, lambda st, r=rec: self._on_reply(r, st, ridden=True))
            except UnavailableError as exc:
                rec.unsupported = exc.reason
                self._later(rec, now, self.beat_s)
                return
            with self._mu:
                rec.armed_at = now
            return
        self._send(rec, panel, now)

    def _later(self, rec: _Board, now: float, after: float, *, error: str = "") -> None:
        with self._mu:
            rec.armed_at = None
            rec.next_due = now + after
            if error:
                rec.last_error = error

    def _send(self, rec: _Board, panel: Any, now: float) -> None:
        hello = rec.hello or self.build_hello(rec)
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(self._gate(rec.board_id))
            except HeldError as exc:
                # One of our jobs holds the board (a swap parks the control port): skip this
                # beat. Keep the hello offered, so the job's own connections may carry it.
                rec.skipped += 1
                self._later(rec, now, RETRY_S, error=f"skipped: {exc.message}")
                offer = getattr(panel, "offer", None)
                if callable(offer) and self.ride_wait_s > 0:
                    with contextlib.suppress(HarnessError):
                        offer(hello, lambda st, r=rec: self._on_reply(r, st, ridden=True))
                        with self._mu:
                            rec.armed_at = now
                return
            try:
                state = panel.hello(hello)
            except UnavailableError as exc:
                rec.unsupported = exc.reason
                self._later(rec, now, self.beat_s)
                return
            except HarnessError as exc:
                # The BOARD turned the beat away (another client holds the port: refused,
                # reset, timed out) or did not answer. Never retried sooner than a beat
                # (QUIET-POLL): the TTL survives two misses, and the gate backs off.
                if self._noted is not None:
                    with contextlib.suppress(Exception):
                        self._noted(rec.board_id, exc)
                self._later(rec, now, self.beat_s, error=exc.message)
                return
        self._on_reply(rec, state, ridden=False)

    def build_hello(self, rec: _Board) -> Hello:
        view = rec.view
        if self._lease_view is not None:
            try:
                view = self._lease_view(rec.session)
            except HarnessError as exc:        # keep the last view; the hub may be slow
                log.debug("lease view for %s: %s", rec.board_id, exc.message)
            rec.view = view
        role, lease, open_request = hello_lease(view, self._wall(), self.who)
        rec.fast = open_request
        job = None
        if self._job_of is not None:
            try:
                job = self._job_of(rec.board_id)
            except Exception:  # noqa: BLE001 - a job is a nicety in the hello
                job = None
        name = str(getattr(rec.session.candidate, "name", "") or "")
        return Hello(sid=rec.sid, who=self.who, app=self.app, name=name, role=role,
                     lease=lease, job=job, ttl=TTL_S)

    # -- replies and events ---------------------------------------------------------------

    def _on_reply(self, rec: _Board, state: PanelState, *, ridden: bool) -> None:
        with self._mu:
            if self._boards.get(rec.board_id) is not rec:
                return                           # closed meanwhile
            rec.armed_at = None
            rec.next_due = self._clock() + self._interval(rec)
            rec.last_hello_at = self._wall()
            rec.last_error = ""
            if ridden:
                rec.ridden += 1
            else:
                rec.sent += 1
            prev, rec.state = rec.state, state
            news = self._news(rec, state)
        if _changed(prev, state):
            self._publish(TOPIC_STATE, rec.board_id, state_event_data(state))
        for ev in news:
            self._tap(rec, ev)

    def _news(self, rec: _Board, state: PanelState) -> list[PanelEvent]:
        """The taps not seen before (by ``seq``). Called under the lock."""
        top = max([state.seq, *(e.seq for e in state.events)])
        fresh_ms = self.beat_s * 1000
        if rec.last_seq is None or top < rec.last_seq:
            # First contact, or the harness restarted its ring: only recent taps are news.
            rec.last_seq = top
            return [e for e in state.events if e.ms_ago <= fresh_ms]
        news = [e for e in state.events if e.seq > rec.last_seq]
        rec.last_seq = top
        return news

    def _tap(self, rec: _Board, ev: PanelEvent) -> None:
        data: dict[str, Any] = {"seq": ev.seq, "kind": ev.kind, "on": ev.on, "ms_ago": ev.ms_ago,
                                "at": ev.at, "notify": ""}
        if ev.on == TAP_REQUEST:
            data.update(self._notify_holder(rec, ev))
        self._publish(TOPIC_TAP, rec.board_id, data)

    def _notify_holder(self, rec: _Board, ev: PanelEvent) -> dict[str, Any]:
        """Decision P2: tell the holder someone at the board tapped. Never a release."""
        hub = getattr(rec.session, "hub", None)
        hook = getattr(self._leases, "notify_holder", None)
        if callable(hook) and hub is not None:
            try:
                out = hook(rec.board_id, hub, seq=ev.seq, at=ev.at) or {}
            except HarnessError as exc:
                log.warning("notifying the holder of %s: %s", rec.board_id, exc.message)
                out = {}
            return {"notify": "holder" if out.get("notified") else "",
                    "request": out.get("request")}
        lease = (rec.view or {}).get("lease") or {}
        if not lease.get("mine"):
            return {"notify": ""}                # this Harness Manager is not the holder's
        waiting = [i for i in (rec.view or {}).get("incoming") or () if not i.get("answer")]
        request = {"id": waiting[0].get("id"), "by": waiting[0].get("by", "")} if waiting else None
        return {"notify": "holder", "request": request}

    def _publish(self, topic: str, board_id: str, data: dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.publish(Event(topic, board_id, data))

    # -- what the API reads ------------------------------------------------------------------

    def presence(self, board_id: str, session: Any = None) -> dict[str, Any]:
        """This service's side of presence for a board: is it beating, and why not."""
        with self._mu:
            rec = self._boards.get(board_id)
            if rec is None:
                return {"active": False, "reason": NOT_BEATING, "sid": "", "last_hello_at": None,
                        "sent": 0, "ridden": 0, "skipped": 0, "last_error": "",
                        "interval_s": self.beat_s, "quiet": "", "quieted": 0}
            return {"active": not rec.unsupported and rec.last_hello_at is not None,
                    "reason": rec.unsupported, "sid": rec.sid,
                    "last_hello_at": rec.last_hello_at, "sent": rec.sent, "ridden": rec.ridden,
                    "skipped": rec.skipped, "last_error": rec.last_error,
                    "interval_s": self._interval(rec), "quiet": rec.quiet,
                    "quieted": rec.quieted}

    def _cached(self, board_id: str, what: str, max_age: float,
                fn: Callable[[], Any]) -> Any:
        with self._mu:
            rec = self._boards.get(board_id)
            hit = rec.cache.get(what) if rec is not None else None
        now = self._clock()
        if hit is not None and now - hit[0] < max_age:
            return hit[1]
        value = fn()
        with self._mu:
            if rec is not None:
                rec.cache[what] = (now, value)
        return value

    def read(self, board_id: str, session: Any, *,
             reason_for: Callable[[str], str] | None = None,
             with_state: bool = True) -> dict[str, Any]:
        """``GET /boards/{bid}/panel``: the panel, what it can do, and presence.
        ``with_state=False`` (``?state=0``) leaves the board's panel unread."""
        with self._mu:
            rec = self._boards.get(board_id)
            sid = rec.sid if rec is not None else ""
            until = rec.locate_until if rec is not None else 0.0

        def state(panel: Any) -> PanelState:
            got = self._cached(board_id, "state", STATE_CACHE_S, panel.state)
            if not sid or not got.sessions:
                return got
            return replace(got, sessions=tuple(replace(s, mine=s.sid == sid)
                                               for s in got.sessions))

        body = read_panel(session, reason_for=reason_for, state=state, with_state=with_state)
        body["presence"] = self.presence(board_id, session)
        if until > self._wall():
            body["identify"]["until"] = until
        return body

    def frame(self, board_id: str, session: Any) -> PanelFrame:
        panel = require_panel(session, C.FRONT_PANEL)
        return self._cached(board_id, "frame", FRAME_CACHE_S, panel.frame)

    def identify(self, board_id: str, session: Any, seconds: int,
                 who: str | None = None) -> dict[str, Any]:
        """Blink the board for ``seconds`` (0 stops). Publishes ``panel.locate``."""
        out = identify(session, seconds, who or self.who, wall=self._wall)
        with self._mu:
            rec = self._boards.get(board_id)
            if rec is not None:
                rec.locate_until = out["until"] if seconds else 0.0
                if seconds:                      # the fast beat starts with the next hello
                    rec.next_due = min(rec.next_due, self._clock() + self.fast_s)
        self._publish(TOPIC_LOCATE, board_id, {"state": "on" if seconds else "off",
                                               "until": out["until"], "seconds": seconds,
                                               "who": who or self.who})
        return out


# --- one-shot reads (the CLI in-process, and the service) ---------------------------------------


def require_panel(session: Any, capability: str, reason: str = "") -> Any:
    panel = getattr(session, "panel", None)
    if panel is None:
        raise UnavailableError(capability, reason or NO_ADAPTER)
    return panel


def read_panel(session: Any, *, reason_for: Callable[[str], str] | None = None,
               state: Callable[[Any], PanelState] | None = None,
               with_state: bool = True) -> dict[str, Any]:
    """``{panel: PanelState | None, reason, identify: {available, reason, until}, support}``.

    ``panel`` is None, with the reason, when this board has no front panel HM can read. A
    bare-metal board answers with ``source: "rebuilt"`` and Identify unavailable.
    ``with_state=False``: what the board can do only; ``panel`` is None and the panel is not
    read (``reason`` is set only when it could not be read at all). ``identify.until`` is
    the adapter's ``identify_until()`` when it has one (a daemon session's proxy).
    """
    panel = getattr(session, "panel", None)
    if panel is None:
        why = (reason_for(C.FRONT_PANEL) if reason_for else "") or NO_ADAPTER
        locate_why = (reason_for(C.LOCATE) if reason_for else "") or why
        return {"panel": None, "reason": why,
                "identify": {"available": False, "reason": locate_why, "until": None},
                "support": {"front_panel": why, "presence": why, "locate": locate_why,
                            "source": ""}}
    support = panel.support()
    reason, got = "", None
    if support.front_panel:
        reason = support.front_panel
    elif with_state:
        try:
            got = state(panel) if state is not None else panel.state()
        except UnavailableError as exc:
            reason = exc.reason
    until = getattr(panel, "identify_until", None)
    return {"panel": got, "reason": reason,
            "identify": {"available": not support.locate, "reason": support.locate,
                         "until": until() if callable(until) else None},
            "support": jsonable(support)}


def identify(session: Any, seconds: int, who: str, *,
             wall: Callable[[], float] = time.time) -> dict[str, Any]:
    panel = require_panel(session, C.LOCATE)
    why = panel.support().locate
    if why:
        raise UnavailableError(C.LOCATE, why)
    until = panel.locate(int(seconds), who)
    return {"until": until if seconds else wall(), "seconds": int(seconds)}


def check_seconds(value: Any, default: int = IDENTIFY_DEFAULT_S) -> int:
    """Identify's duration: whole seconds 0-30 (0 stops a blink)."""
    from harness_manager.core.errors import UsageError

    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= IDENTIFY_MAX_S:
        raise UsageError(f"seconds must be whole seconds from 0 to {IDENTIFY_MAX_S}, not {value!r}",
                         hint="0 stops a blink that is running")
    return value
