"""The board as its own lease server (Release 3, single-user mode; net-protocol v0.21).

A desk board whose version features include ``board_lease`` keeps its own lease table. This
module is the Harness Manager side: ``BoardLeaseClient`` speaks the board's ``lease`` verb and
has the same shape as the hub client (``services.lease.HubClientLike`` plus the request notes
and ``principal``/``board_id``/``lease_revoke``/``lease_history``), so ``LeaseService`` drives a
board exactly as it drives a hub. ``Mps3BoardLease`` is the ``services.lease_backend.LeaseServer``
adapter (``session.lease``; ``Mps3Pack`` hook ``board_lease:make_lease_adapter``).

Wire (names are pyverify's ``board_lease.py``, F0 stubs, ``mps3-r3-f0py``; vendored pyverify
does not have them yet, so they are mirrored here and NOT imported):

    request   ``{"op":"lease","act":ACT, ...}``; every time on the wire is RELATIVE seconds
              (``ttl_s``, ``expires_in_s``, ``age_s``): HM computes absolute times on its own clock.
    gated verbs carry a top-level ``"lease": "<32 hex>"`` while this HM holds the lease
              (``shell.board_lease_preamble``); heartbeat/release/respond carry it too.
    refusal   ``{"ok":false,"err":"...","code":CODE}``.

Act -> transport (the claim gate comes first, then the lease: "claim first, then lease"):

    show, history                       6900 direct (reads stay open to every peer)
    acquire, heartbeat, release,
    request, respond, cancel,
    revoke, mode                        the claim route (``claim_route.routed_call``): direct on
                                        an unclaimed board, through the claim forward when this
                                        HM holds the claim, refused in plain words otherwise
                                        (a keyless peer never gets a write in)

The request/reply field names beyond the act list are not in the F0 stubs (contract v0.21 text
is -f9's); the shapes used are the stubs' dataclasses (``LeaseView``, ``Holder``, ``Waiter``,
``AcquireResult``) and are all read through ``dict.get`` so a board that adds fields keeps working.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    ClaimLockedError,
    HarnessError,
    HeldError,
    UnreachableError,
    UsageError,
)
from harness_manager.services import lease_backend
from harness_manager.services.lease_backend import LeaseMode

from . import claim_route, ctlgate
from .hub import (
    AnswerNote,
    LeaseLostError,
    LeaseStatus,
    LeaseView,
    QueueEntry,
    RequestNote,
)
from .hub import taken_from_history as hub_taken_from_history
from .shell import set_lease_token_source

log = logging.getLogger(__name__)

# --- wire names, mirrored from pyverify board_lease.py (mps3-r3-f0py) ----------------------------

FEATURE = "board_lease"
ACTS = ("show", "history", "acquire", "heartbeat", "release", "request", "respond", "cancel",
        "revoke", "mode")
#: Acts that read (6900 direct); every other act is a write (the claim route).
READ_ACTS = ("show", "history")
MODE_OPEN = "open"
MODE_SINGLE = "single_user"
CODES = ("leased", "lease_required", "lease_held", "bad_token", "queue_full", "local_hold",
         "not_holder", "busy_access")
DEFAULT_TTL_S = 3600
REQUEST_WINDOW_S = 120
TOKEN_HEX = 32

#: What the board's history calls a lease ended at the touchscreen (panel event ``lease_break``).
BREAK_EVENTS = ("lease_break", "broken_local", "lease.broken_local")
TOUCH_BY = "someone at the board"
TOUCH_REASON = "single-user mode was switched off on the touchscreen"

#: The words when this PC's key cannot write to the board (frozen with the guide).
NO_KEY_WORDS = ("Your key is not on this board. Ask its owner to add it "
                "(Board > Access > Board keys).")

APP = "Harness Manager"
_TOKEN = re.compile(r"[0-9a-fA-F]{32}")


# --- small helpers ------------------------------------------------------------------------------


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0, int(value))


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def iso(t: float) -> str:
    """Epoch seconds as UTC ISO 8601 with ``+00:00`` (the notes' format)."""
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class BoardGrant:
    """What ``lease_acquire`` returns as its ``lease`` (``LeaseService`` reads ``.token``)."""

    token: str
    expires_in_s: int = 0


@dataclass(frozen=True)
class ModeResult:
    """``set_mode``: the mode now, and the lease taken in the same step (``take``)."""

    mode: LeaseMode
    granted: bool = False
    token: str = ""
    expires_at: str = ""


def refusal(reply: dict[str, Any], what: str) -> HarnessError:
    """A board refusal of a lease act as the error HM shows: the board's own plain ``err``
    (it names the cause), with the code on ``.code``."""
    code = _text(reply.get("code"))
    err = _text(reply.get("err")) or f"the board refused {what}"
    hint = {"lease_held": "ask for the board (Request), or wait for it to be given back",
            "queue_full": "the queue is full: try again when someone has been served",
            "local_hold": "someone switched single-user mode off at the board; it can be "
                          "switched on again from here after the hold ends",
            "not_holder": "only the person holding the lease (or the board's owner) can do this",
            "busy_access": "someone is using the board's touchscreen access page now",
            }.get(code, "see who holds the board: `harness-manager lease show`")
    exc: HarnessError
    if code == "locked":
        exc = ClaimLockedError(err, hint=hint)
    else:
        exc = HeldError(err, holder=_text(reply.get("holder")), hint=hint)
    exc.code_text = code          # type: ignore[attr-defined]  # ExitCode stays on .code
    exc.reply = reply             # type: ignore[attr-defined]
    return exc


# --- the client ---------------------------------------------------------------------------------


class BoardLeaseClient:
    """One board's lease acts. ``shell`` is the session's ``Mps3Shell`` (6900 direct); writes go
    through ``claim_route.routed_call(session, ...)``. ``clock`` (wall) and ``sleep`` are test
    seams; ``token_source`` reads a token kept elsewhere (the LeaseStore) when this process has
    none in memory (HM restarted while it held the lease)."""

    #: LeaseService reads these (a board has notes: requests are the board's own).
    transport = "board"
    notes_supported = True

    def __init__(self, session: Any = None, *, shell: Any = None, board: str = "", name: str = "",
                 who: str = "", clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 token_source: Callable[[], str] | None = None) -> None:
        self.session = session
        self.shell = shell if shell is not None else getattr(session, "shell", None)
        if self.shell is None:
            raise UsageError("a board lease needs the board's control channel (an Ethernet shell)")
        cand = getattr(session, "candidate", None)
        self.board = board or str(getattr(cand, "board_id", "") or "")
        self.name = name or str(getattr(cand, "name", "") or getattr(cand, "label", "") or self.board)
        self.who = who
        self._wall, self._mono, self._sleep = clock, monotonic, sleep
        self._token_source = token_source
        self._mu = threading.Lock()
        self._token = ""
        self._ttl_s = DEFAULT_TTL_S
        self._principal = ""
        self._requests: dict[str, str] = {}      # our note id -> the board's request id
        if token_source is not None:
            self._register()

    # -- the token ------------------------------------------------------------------------------

    def current_token(self) -> str:
        """The board lease token this Harness Manager holds ("" when none)."""
        with self._mu:
            if self._token:
                return self._token
        src = self._token_source
        try:
            return str(src() or "") if src is not None else ""
        except Exception:  # noqa: BLE001 - a store problem is "no token", not a crash
            return ""

    def set_ttl(self, ttl_s: int) -> None:
        """The TTL the next acquire / mode-with-take asks for (and each heartbeat repeats)."""
        if ttl_s > 0:
            self._ttl_s = int(ttl_s)

    def use_token_source(self, source: Callable[[], str] | None) -> None:
        """Where to read a token kept outside this process (``LeaseService`` hands over the
        LeaseStore, so the token outlives a restart of Harness Manager)."""
        self._token_source = source
        if source is not None:
            self._register()

    def _register(self) -> None:
        """Let this board's shells read our token. Done when we hold or can read one, not at
        construction: a second client for the same board (a session that learned the feature
        late) must not take the registry from the one that holds the lease."""
        set_lease_token_source(self.shell.gate_key, self.current_token)

    def _set_token(self, token: str) -> None:
        with self._mu:
            self._token = token
        if token:
            self._register()

    # -- transport ------------------------------------------------------------------------------

    def _msg(self, act: str, **fields: Any) -> dict[str, Any]:
        if act not in ACTS:
            raise UsageError(f"unknown board lease act {act!r}")
        return {"op": "lease", "act": act, **{k: v for k, v in fields.items() if v is not None}}

    def _read(self, act: str, **fields: Any) -> dict[str, Any]:
        """A read act over 6900 direct. The board's refusal is raised; the reply returned."""
        msg = self._msg(act, **fields)
        reply = self.shell.call(lambda c: c._request(msg))
        return self._checked(reply, f"lease {act}")

    def _write(self, act: str, *, lease: str = "", raw: bool = False, **fields: Any) -> dict[str, Any]:
        """A write act through the claim route (see the module docstring). ``lease`` adds the
        top-level ``"lease"`` (the holder's token). ``raw``: a refusal reply is returned, not raised."""
        msg = self._msg(act, **fields)
        if lease:
            msg["lease"] = lease
        what = f"board lease {act}"
        try:
            if self.session is None:
                reply = claim_route.locked_call(self.shell, lambda c: c._request(msg))
            else:
                reply = claim_route.routed_call(self.session, what, self.shell,
                                                lambda c: c._request(msg))
        except ClaimLockedError as exc:
            if getattr(exc, "code_text", ""):
                raise
            raise ClaimLockedError(NO_KEY_WORDS, hint=exc.hint) from exc
        except UnreachableError as exc:
            if "permission denied" in str(exc).lower():
                raise ClaimLockedError(
                    NO_KEY_WORDS, hint="`harness-manager board claim-status TARGET` shows "
                                       "whose key the board holds") from exc
            raise
        return reply if raw else self._checked(reply, what)

    @staticmethod
    def _checked(reply: Any, what: str) -> dict[str, Any]:
        if not isinstance(reply, dict):
            raise UnreachableError(f"the board answered {what} with something that is not a reply")
        if reply.get("ok") is False:
            raise refusal(reply, what)
        return reply

    # -- reads ----------------------------------------------------------------------------------

    def show(self) -> dict[str, Any]:
        """The board's lease table, raw (``lease show``)."""
        return self._read("show", lease=self.current_token() or None)

    def mode(self) -> LeaseMode:
        return self._mode_of(self.show())

    @staticmethod
    def _mode_of(reply: dict[str, Any]) -> LeaseMode:
        return LeaseMode(
            single_user=_text(reply.get("mode")) == MODE_SINGLE,
            can_change=bool(reply.get("can_change", False)),
            change_reason=_text(reply.get("change_reason")),
            since_s=_int(reply.get("since_s")), by=_text(reply.get("by")),
            persisted=bool(reply.get("persisted", True)),
            local_hold_s=_int(reply.get("local_hold_s")))

    def lease_show(self) -> LeaseView:
        reply = self.show()
        held = _obj(reply.get("holder"))
        who = _text(held.get("who"))
        expires = iso(self._wall() + _int(held.get("expires_in_s"))) if who else ""
        return LeaseView(target=self.board, held=bool(who), holder=who, user=who,
                         expires_at=expires, raw=str(reply))

    def lease_status(self) -> LeaseStatus:
        """The holder and the queue, head first (the queue is the board's waiting requesters)."""
        reply = self.show()
        held = _obj(reply.get("holder"))
        who = _text(held.get("who"))
        self._note_you(held, reply)
        queue = tuple(QueueEntry(position=i + 1, holder=_text(w.get("who")), user=_text(w.get("who")))
                      for i, w in enumerate(w for w in reply.get("queue") or [] if isinstance(w, dict)))
        return LeaseStatus(
            held=bool(who), holder=who, user=who,
            expires_at=iso(self._wall() + _int(held.get("expires_in_s"))) if who else "",
            queue=queue)

    def lease_queues(self) -> LeaseStatus:
        return self.lease_status()

    def _note_you(self, held: dict[str, Any], reply: dict[str, Any]) -> None:
        you = _text(reply.get("you"))
        if not you and held.get("you") is True:
            you = _text(held.get("who"))
        if you:
            self._principal = you

    def principal(self) -> str:
        """Who the board records this client's lease under: its ``you`` in ``show``, else the
        holder name it was last granted under, else the name asked for."""
        if not self._principal:
            reply = self.show()
            self._note_you(_obj(reply.get("holder")), reply)
        return self._principal or self.who

    def board_id(self) -> str:
        return self.board

    def lease_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """The board's recent lease events, oldest first, shaped like the hub's (``event``
        ``lease.*``, ``holder``, ``ts`` absolute on HM's clock). A revoke is a
        ``lease.admin_revoked`` with ``prior_holder``, ``by`` and ``reason``; a lease ended at
        the touchscreen is ``lease.broken_local``."""
        reply = self._read("history", n=max(1, min(int(limit), 500)))
        now = self._wall()
        out: list[dict[str, Any]] = []
        for e in reply.get("events") or []:
            if not isinstance(e, dict):
                continue
            kind = _text(e.get("event") or e.get("kind"))
            who = _text(e.get("who") or e.get("holder"))
            ev: dict[str, Any] = {"ts": iso(now - _int(e.get("age_s"))), "holder": who,
                                  "reason": _text(e.get("reason")), "by": _text(e.get("by"))}
            if kind in BREAK_EVENTS:
                ev["event"] = "lease.broken_local"
            elif kind in ("revoke", "revoked", "lease.revoked", "lease.admin_revoked"):
                ev.update(event="lease.admin_revoked", prior_holder=_text(e.get("prior") or who))
            else:
                ev["event"] = kind if kind.startswith("lease.") else f"lease.{kind}"
            out.append(ev)
        out.sort(key=lambda e: e["ts"])           # ISO UTC sorts by time; stable keeps ties
        return out[-limit:]

    # -- the lease ------------------------------------------------------------------------------

    def lease_acquire(self, holder: str, *, ttl: int, poll_s: float = 20.0,
                      timeout_s: float = 3600.0, sleep: Callable[[float], None] | None = None,
                      log_fn: Callable[[str], None] | None = None) -> tuple[BoardGrant, str]:
        """Take the lease, polling ``acquire`` (with the ticket the board gave) while queued.
        Logs ``queued at position N`` each time it is told. Returns ``(grant, expires_at)``."""
        wait = sleep or self._sleep
        say = log_fn or (lambda _t: None)
        deadline = self._mono() + timeout_s
        ticket = ""
        self.who = self.who or holder
        while True:
            reply = self._write("acquire", raw=True, who=holder, app=APP, ttl_s=int(ttl),
                                token=ticket or None)
            token = _text(reply.get("token"))
            if reply.get("ok") is not False and reply.get("granted", True) is not False \
                    and _TOKEN.fullmatch(token):
                self._set_token(token)
                self._ttl_s = int(ttl)
                self._principal = self._principal or holder
                left = _int(reply.get("expires_in_s"), int(ttl))
                return BoardGrant(token, left), iso(self._wall() + left)
            if _text(reply.get("code")) not in ("lease_held",) and reply.get("ok") is False:
                raise refusal(reply, "board lease acquire")
            pos = _int(reply.get("position") or reply.get("pos"))
            ticket = token or ticket
            say(f"queued at position {pos}" if pos else "queued")
            if self._mono() >= deadline:
                with_cancel = self.lease_cancel(holder)
                raise ActionFailedError(
                    f"{self.name} was not given back within {int(timeout_s)} s"
                    + ("; the queue place was removed" if with_cancel else ""),
                    hint="ask for the board (Request) or try again later")
            wait(poll_s)

    def lease_heartbeat(self, token: str, holder: str) -> str:
        """Extend the lease (the acquire's TTL again). Returns the new ``expires_at``. A token
        the board no longer knows is ``LeaseLostError`` (``expired``: nobody holds the board
        any more; ``lost``: someone else does)."""
        reply = self._write("heartbeat", lease=token, raw=True, ttl_s=self._ttl_s)
        if reply.get("ok") is False:
            code = _text(reply.get("code"))
            if code in ("bad_token", "not_holder", "lease_required", "leased", "lease_held"):
                state = "expired" if code == "lease_required" else "lost"
                raise LeaseLostError(
                    f"lease heartbeat: the lease on {self.name} is gone "
                    f"({_text(reply.get('err')) or code})", state=state,
                    hint="see who holds it: harness-manager lease show")
            raise refusal(reply, "board lease heartbeat")
        left = _int(reply.get("expires_in_s"), self._ttl_s)
        return iso(self._wall() + left)

    def lease_release(self, token: str, holder: str) -> None:
        self._write("release", lease=token)
        with self._mu:
            if self._token == token:
                self._token = ""

    def lease_cancel(self, holder: str) -> bool:
        """Leave the queue (by name). True when a queue place was removed."""
        reply = self._write("cancel", raw=True, who=holder)
        return reply.get("ok") is not False and reply.get("removed", True) is not False

    def lease_revoke(self, reason: str) -> dict[str, Any]:
        """Force-release: the board's owner, with the board's name typed (the service has
        checked it). Returns what the hub's does: ``{revoked, by, board, prior_holder,
        reason, principal}``; ``revoked`` is empty when nobody held the board."""
        before = _obj(self.show().get("holder"))
        prior = _text(before.get("who"))
        me = self.principal()
        reply = self._write("revoke", reason=reason, confirm_board=self.name)
        did = bool(prior) and reply.get("revoked", True) is not False
        return {"revoked": [self.board] if did else [], "by": _text(reply.get("by")) or me,
                "board": self.board, "prior_holder": prior, "reason": reason, "principal": me}

    # -- request notes: the board's own requests ------------------------------------------------

    def put_request(self, note: RequestNote) -> None:
        reply = self._write("request", who=note.by, msg=note.message)
        rid = _text(reply.get("id"))
        if rid:
            with self._mu:
                self._requests[note.id] = rid

    def _waiters(self) -> list[dict[str, Any]]:
        return [w for w in self.show().get("queue") or [] if isinstance(w, dict)]

    def list_requests(self) -> list[RequestNote]:
        """The board's waiting requests, oldest first (notes as the hub's, absolute times)."""
        now = self._wall()
        notes: list[RequestNote] = []
        for w in self._waiters():
            rid = _text(w.get("id"))
            if not rid:
                continue
            made = now - _int(w.get("age_s"))
            who = _text(w.get("who"))
            notes.append(RequestNote(
                id=rid, by=who, user=who, host="", message=_text(w.get("msg")),
                created_at=iso(made), deadline_at=iso(made + _int(w.get("window_s"), REQUEST_WINDOW_S)),
                want_s=_int(w.get("want_s"))))
        return notes

    def _board_request_id(self, note_id: str) -> str:
        with self._mu:
            return self._requests.get(note_id, note_id)

    def delete_request(self, request_id: str) -> None:
        self._write("cancel", id=self._board_request_id(request_id))
        with self._mu:
            self._requests.pop(request_id, None)

    def put_answer(self, note: AnswerNote) -> None:
        """The holder answers a request: ``release`` accepts (the board hands over), ``keep``
        declines for ``minutes``."""
        self._write("respond", lease=self.current_token(), id=self._board_request_id(note.id),
                    accept=note.answer == "release",
                    minutes=int(note.minutes) if note.answer == "keep" else None)

    def get_answer(self, request_id: str) -> AnswerNote | None:
        """The holder's answer to our request, as the board's queue entry says it; None
        until there is one."""
        rid = self._board_request_id(request_id)
        for w in self._waiters():
            if _text(w.get("id")) == rid and _text(w.get("answer")) in ("release", "keep"):
                return AnswerNote(id=request_id, answer=_text(w.get("answer")),
                                  minutes=_int(w.get("minutes")), message=_text(w.get("answer_msg")),
                                  at=iso(self._wall() - _int(w.get("answer_age_s"))))
        return None

    # -- the mode -------------------------------------------------------------------------------

    def _holder(self) -> str:
        """The name this client takes the lease under (``harness-manager-<user>@<host>``)."""
        if not self.who:
            from harness_manager.services.lease import default_holder

            self.who = default_holder()
        return self.who

    def set_mode(self, on: bool, *, take: bool = False, confirm_board: str = "") -> ModeResult:
        """Switch single-user mode. Turning it on may ``take`` the lease in the same step (the
        reply carries the token); turning it off by someone who is not the lease holder needs
        the board's name in ``confirm_board``. Owner-only rules are the board's."""
        reply = self._write("mode", lease=self.current_token(), mode=MODE_SINGLE if on else MODE_OPEN,
                            take=bool(take and on) or None, confirm_board=confirm_board or None,
                            who=self._holder() if take and on else None,
                            app=APP if take and on else None,
                            ttl_s=self._ttl_s if take and on else None)
        token = _text(reply.get("token"))
        granted = bool(on and take and _TOKEN.fullmatch(token))
        if granted:
            self._set_token(token)
        elif not on:
            self._set_token("")
        left = _int(reply.get("expires_in_s"), self._ttl_s)
        mode = self._mode_of({**reply, "mode": _text(reply.get("mode")) or
                              (MODE_SINGLE if on else MODE_OPEN)})
        return ModeResult(mode, granted, token if granted else "",
                          iso(self._wall() + left) if granted else "")


def taken_from_history(events: Any, holder: str) -> dict[str, str] | None:
    """Who took ``holder``'s lease: ``{by, reason, at}`` or None. As the hub's reading, plus
    the touchscreen: a ``lease.broken_local`` (``lease_break``) event after our tenure is
    ``{by: "someone at the board", reason: "single-user mode was switched off on the
    touchscreen"}``."""
    evs = [e for e in events if isinstance(e, dict)]
    for e in reversed(evs):
        kind = e.get("event")
        if kind == "lease.broken_local":
            return {"by": TOUCH_BY, "reason": TOUCH_REASON, "at": _text(e.get("ts"))}
        if e.get("holder") == holder and kind in ("lease.acquired", "lease.promoted",
                                                  "lease.expired", "lease.released"):
            break
        if kind == "lease.admin_revoked" and e.get("prior_holder") == holder:
            break
    return hub_taken_from_history(evs, holder)


# --- the adapter (session.lease) ----------------------------------------------------------------


class Mps3BoardLease:
    """``session.lease`` for a desk board that is its own lease server (``LeaseServer``)."""

    kind = lease_backend.KIND_BOARD
    closed = False                # True: made without a session (adapter_for_candidate)

    def __init__(self, session: Any, client: BoardLeaseClient | None = None) -> None:
        from .panel import board_address

        self.session = session
        shell = getattr(session, "shell", None)
        cand = getattr(session, "candidate", None)
        self.target = str(getattr(cand, "board_id", "") or "")
        self.name = str(getattr(cand, "name", "") or getattr(cand, "label", "") or self.target)
        self.host = board_address(session) or str(getattr(shell, "host", "") or "")
        self.client = client or BoardLeaseClient(session, board=self.target, name=self.name)
        self.words = dict(lease_backend.WORDS[lease_backend.KIND_BOARD])

    def mode(self) -> LeaseMode | None:
        """The board's mode from ``lease show``; None when the board cannot be asked now (the
        board stays the authority and refuses for itself)."""
        try:
            return self.client.mode()
        except HarnessError as exc:
            log.info("the lease mode of %s is unknown: %s", self.target, exc.message)
            return None


def adapter_for_candidate(candidate: Any, features: tuple[str, ...]) -> Mps3BoardLease | None:
    """The board lease adapter for a candidate without opening a session (``Mps3Pack.lease_for``;
    reads and the mode only: it has no claim adapter, so a write from it goes direct and a
    claimed board's lock is the answer). None when the board lacks ``board_lease`` or has no
    Ethernet control address."""
    from harness_manager.core.model import LinkKind

    from .constants import CONTROL_PORT
    from .shell import Mps3Shell, parse_endpoint

    if FEATURE not in features:
        return None
    link = next((lk for lk in candidate.links if lk.kind == LinkKind.ETHERNET), None)
    if link is None:
        return None
    host, port = parse_endpoint(link.address, CONTROL_PORT)

    class _Bare:                  # the few session attributes the adapter reads
        pass

    bare = _Bare()
    bare.candidate = candidate    # type: ignore[attr-defined]
    bare.shell = Mps3Shell(host, port, gate_key=ctlgate.key_of_address(link.address, CONTROL_PORT))  # type: ignore[attr-defined]
    bare.claim = None             # type: ignore[attr-defined]
    lease = Mps3BoardLease(bare)
    lease.closed = True           # no session behind it: a write should open the board first
    return lease


def upgrade_session(session: Any, features: tuple[str, ...]) -> None:
    """A session opened from a candidate that carried no features (a board added by address, a
    boards.toml row) learns them from its first identity read: the first one naming
    ``board_lease`` gives it the board's own lease server (``session.lease``). A hub, or a
    lease already there, is left alone."""
    if FEATURE not in features or getattr(session, "lease", None) is not None:
        return
    if getattr(session, "hub", None) is not None or getattr(session, "shell", None) is None:
        return
    session.lease = Mps3BoardLease(session)


def make_lease_adapter(session: Any) -> Any:
    """The pack hook ``board_lease:make_lease_adapter``: ``session.lease``. Behind a hub,
    ``session.hub`` itself (the hub's lease already does this); else ``Mps3BoardLease`` when the
    board's version features include ``board_lease``; else None."""
    hub = getattr(session, "hub", None)
    if hub is not None:
        return hub
    cand = getattr(session, "candidate", None)
    features = tuple(getattr(getattr(cand, "identity", None), "features", ()) or ())
    if FEATURE not in features or getattr(session, "shell", None) is None:
        return None
    return Mps3BoardLease(session)


__all__ = ["AnswerNote", "BoardGrant", "BoardLeaseClient", "FEATURE", "ModeResult",
           "Mps3BoardLease", "NO_KEY_WORDS", "RequestNote", "make_lease_adapter",
           "taken_from_history", "upgrade_session"]
