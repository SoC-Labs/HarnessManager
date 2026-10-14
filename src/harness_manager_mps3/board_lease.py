"""The board as its own lease server (Release 3, single-user mode; net-protocol v0.21).

A desk board whose version features include ``board_lease`` keeps its own lease table. This
module is the Harness Manager side: ``BoardLeaseClient`` speaks the board's ``lease`` verb and
has the same shape as the hub client (``services.lease.HubClientLike`` plus the request notes
and ``principal``/``board_id``/``lease_revoke``/``lease_history``), so ``LeaseService`` drives a
board exactly as it drives a hub. ``Mps3BoardLease`` is the ``services.lease_backend.LeaseServer``
adapter (``session.lease``; ``Mps3Pack`` hook ``board_lease:make_lease_adapter``).

Wire: net-protocol v0.21 "Board lease and panel access" (frozen). The names are pyverify's
``board_lease.py`` (platform bdf73539) and are mirrored in the ONE block below ("the wire"),
not imported (the vendored pyverify does not carry them). Every other name in this module is
Harness Manager's own.

    request   ``{"op":"lease","act":ACT, ...}``; every time on the wire is RELATIVE seconds
              (``ttl_s``, ``expires_in_s``, ``age_s``): HM computes absolute times on its own clock.
    gated verbs carry a top-level ``"lease": "<32 hex>"`` while this HM holds the lease
              (``shell.board_lease_preamble``); heartbeat/release/respond/mode carry it too.
    refusal   ``{"ok":false,"err":"...","code":CODE}``. A QUEUED acquire is not a refusal: it is
              ``ok:true, granted:false, position, id, code:"lease_held"``.

Act -> transport (the claim gate comes first, then the lease: "claim first, then lease"):

    show, history                       6900 direct (reads stay open to every peer)
    acquire, heartbeat, release,
    request, respond, cancel,
    revoke, mode                        the claim route (``claim_route.routed_call``): direct on
                                        an unclaimed board, through the claim forward when this
                                        HM holds the claim, refused in plain words otherwise
                                        (a keyless peer never gets a write in)

Replies are read through ``dict.get`` so a board that adds fields keeps working.
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

# --- the wire: EVERY name from net-protocol v0.21 lives here (future drift = one edit) -------------

FEATURE = "board_lease"                  # engine feature name (hello / identify features)
FEATURE_PANEL_ACCESS = "panel_access"
VERB = "lease"
ACTS = ("show", "history", "acquire", "heartbeat", "release", "request", "respond", "cancel",
        "revoke", "mode")
#: Acts that read (6900 direct); every other act is a write (the claim route).
READ_ACTS = ("show", "history")
MODE_OPEN = "open"
MODE_SINGLE = "single_user"
CODES = ("leased", "lease_required", "lease_held", "bad_token", "queue_full", "local_hold",
         "not_holder", "busy_access")                  # pyverify's CODES
#: Codes the contract names beyond pyverify's tuple (verb refusals).
CODES_MORE = ("denied", "busy", "invalid", "not_supported", "locked")
CODE_QUEUED = "lease_held"               # on a queued acquire (ok:true, granted:false)
TOKEN_KEY = "lease"                      # top-level token key on gated verbs and holder acts
TOKEN_HEX = 32

#: Request fields, per act (besides ``op``/``act``/``lease``).
F_WHO, F_APP, F_TTL = "who", "app", "ttl_s"          # acquire (ttl 300-14400); heartbeat: ttl_s
F_N = "n"                                            # history, 1-8
F_MSG, F_ID = "msg", "id"                            # request (msg <= 48); respond/cancel (id)
F_ACCEPT, F_MINUTES = "accept", "minutes"            # respond (minutes 5-60 when accept is false)
F_CONFIRM = "confirm"                                # revoke: the board's label
F_MODE = "mode"                                      # mode (on also takes ttl_s 300-14400)
MSG_MAX = 48
WHO_MAX = 24                             # a longer `who` is refused (`invalid who`), never clipped
REQUEST_DEFAULT_MSG = "may I use the board?"   # `request` needs a non-empty msg (= LEASE_WORDS.requestDefault)
HISTORY_MAX = 8
TTL_MIN_S, TTL_MAX_S = 300, 14400
KEEP_MIN, KEEP_MAX = 5, 60

#: Reply fields.
R_MODE, R_PERSISTENT, R_OWNER = "mode", "persistent", "owner"
R_HOLDER, R_QUEUE = "holder", "queue"
R_LOCAL_HOLD, R_RECLAIM = "local_hold_s", "reclaim_s"
R_TOKEN, R_EXPIRES, R_GRANTED = "token", "expires_in_s", "granted"
R_POSITION, R_ID, R_EVENTS = "position", "id", "events"
H_WHO, H_APP, H_HELD, H_EXPIRES, H_YOU = "who", "app", "held_s", "expires_in_s", "you"
W_ID, W_WHO, W_MSG, W_AGE = "id", "who", "msg", "age_s"
E_EV, E_WHO, E_AGE, E_INFO = "ev", "who", "age_s", "info"

#: History events (``ev``) -> the hub-shaped event name HM's lease code reads.
HISTORY_EVENTS = {
    "grant": "lease.acquired", "queue": "lease.queued", "renew": "lease.renewed",
    "release": "lease.released", "expire": "lease.expired", "idle": "lease.expired",
    "cap": "lease.expired", "reserve": "lease.reserved", "request": "lease.requested",
    "give": "lease.given", "keep": "lease.kept", "cancel": "lease.cancelled",
    "revoke": "lease.admin_revoked", "mode_on": "lease.mode_on", "mode_off": "lease.mode_off",
    "break": "lease.broken_local", "suspend": "lease.suspended", "reclaim": "lease.reclaimed",
    "clear": "lease.cleared",
}
EV_GIVE, EV_KEEP, EV_BREAK = "give", "keep", "break"

#: Panel events (``panel`` / ``hello`` ``events``, ``k``) of the touchscreen override.
PANEL_EVENT_ARM = "access_arm"
PANEL_EVENT_BREAK = "lease_break"
#: Names that mean "the lease ended at the touchscreen" (history ``break``, panel ``lease_break``,
#: and HM's own hub-shaped ``lease.broken_local``).
BREAK_EVENTS = (EV_BREAK, PANEL_EVENT_BREAK, "lease.broken_local")

#: Timers (the board is the authority; these are the defaults HM plans around).
DEFAULT_TTL_S = 3600
REQUEST_WINDOW_S = 120                   # the holder's time to answer a request
RESERVE_S = 120                          # the queue head's reservation when the holder goes
POLL_HEAD_S = 5.0                        # acquire poll while first in the queue (inside RESERVE_S)

LAPSED_WORDS = "You were next but did not take it in time; ask again."
TOUCH_BY = "someone at the board"
TOUCH_REASON = "single-user mode was switched off on the touchscreen"

#: The words when this PC's key cannot write to the board (frozen with the guide).
NO_KEY_WORDS = ("Your key is not on this board. Ask its owner to add it "
                "(Board > Access > Board keys).")

APP = "harness-mgr"            # the board clips `app` to 12 characters
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


def board_who(name: str = "") -> str:
    """The ``who`` HM sends a board: at most ``WHO_MAX`` printable characters, stable across runs
    and recognisable: ``<user>@<host>`` (the hub name ``harness-manager-<user>@<host>`` without its
    prefix, from ``default_holder()`` when ``name`` is empty), cut to 24. The ONE place; hub leases
    keep ``default_holder()``."""
    if not name:
        from harness_manager.services.lease import default_holder

        name = default_holder()
    name = re.sub(r"^harness-manager-", "", name)
    name = "".join(ch for ch in name if ch.isprintable() and ch.isascii()).strip()
    return (name or "hm")[:WHO_MAX]


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
            "lease_held_mode": "single-user mode is already on and someone holds the board: "
                               "ask for it (Request)",
            "busy": "the holder still has time to answer, or nobody has an unanswered request: "
                    "ask for the board (Request) first",
            }.get(code, "see who holds the board: `harness-manager lease show`")
    exc: HarnessError
    if code == "locked":
        exc = ClaimLockedError(err, hint=hint)
    else:
        exc = HeldError(err, holder=_text(reply.get(R_HOLDER)), hint=hint)
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
        self._requested_at: dict[str, float] = {}    # our note id -> monotonic time it was asked
        self._queue_id = ""                      # our place in the queue (acquire's ``id``)
        self._request_who: dict[str, str] = {}   # our note id -> the ``who`` it was asked under
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
        return {"op": VERB, "act": act, **{k: v for k, v in fields.items() if v is not None}}

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
            msg[TOKEN_KEY] = lease
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
        return self._read("show", **{TOKEN_KEY: self.current_token() or None})

    def mode(self) -> LeaseMode:
        return self._mode_of(self.show())

    @staticmethod
    def _mode_of(reply: dict[str, Any]) -> LeaseMode:
        """The mode from a ``show``-keyed reply. ``owner`` (show/heartbeat/mode replies) says
        whether this key is the board's owner: ON is the owner's alone; OFF is the owner's or the
        holder's (``holder.you``). Absent ``owner``: the board decides (``denied``), so a change is
        offered unless the touchscreen hold bars it."""
        hold = _int(reply.get(R_LOCAL_HOLD))
        single = _text(reply.get(R_MODE)) == MODE_SINGLE
        owner = reply.get(R_OWNER)
        can, why = True, ""
        if not single and hold > 0:
            can, why = False, (f"single-user mode was switched off at the board; it can be "
                               f"switched on again in {hold} s")
        elif isinstance(owner, bool):
            you = _obj(reply.get(R_HOLDER)).get(H_YOU) is True
            if not single and not owner:
                can, why = False, "only the board's owner can turn single-user mode on"
            elif single and not (owner or you):
                can, why = False, "only the board's owner or the lease holder can turn it off"
        return LeaseMode(single_user=single, can_change=can, change_reason=why,
                         persisted=reply.get(R_PERSISTENT) is not False, local_hold_s=hold)

    def lease_show(self) -> LeaseView:
        reply = self.show()
        held = _obj(reply.get(R_HOLDER))
        who = _text(held.get(H_WHO))
        expires = iso(self._wall() + _int(held.get(H_EXPIRES))) if who else ""
        return LeaseView(target=self.board, held=bool(who), holder=who, user=who,
                         expires_at=expires, raw=str(reply))

    def lease_status(self) -> LeaseStatus:
        """The holder and the queue, head first (the queue is the board's waiting requesters)."""
        reply = self.show()
        held = _obj(reply.get(R_HOLDER))
        who = _text(held.get(H_WHO))
        self._note_you(held, reply)
        queue = tuple(QueueEntry(position=i + 1, holder=_text(w.get(W_WHO)), user=_text(w.get(W_WHO)))
                      for i, w in enumerate(w for w in reply.get(R_QUEUE) or [] if isinstance(w, dict)))
        return LeaseStatus(
            held=bool(who), holder=who, user=who,
            expires_at=iso(self._wall() + _int(held.get(H_EXPIRES))) if who else "",
            queue=queue)

    def lease_queues(self) -> LeaseStatus:
        return self.lease_status()

    def _note_you(self, held: dict[str, Any], _reply: dict[str, Any] | None = None) -> None:
        if held.get(H_YOU) is True and _text(held.get(H_WHO)):
            self._principal = _text(held.get(H_WHO))

    def principal(self) -> str:
        """Who the board records this client's lease under: its ``you`` in ``show``, else the
        holder name it was last granted under, else the name asked for."""
        if not self._principal:
            reply = self.show()
            self._note_you(_obj(reply.get(R_HOLDER)), reply)
        return self._principal or board_who(self.who)

    def board_id(self) -> str:
        return self.board

    def lease_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """The board's recent lease events, oldest first, shaped like the hub's (``event``
        ``lease.*`` via ``HISTORY_EVENTS``, ``holder``, ``ts`` absolute on HM's clock). The board
        keeps the last ``HISTORY_MAX`` (8). A ``revoke`` is a ``lease.admin_revoked`` with
        ``prior_holder``; a lease ended at the touchscreen is ``break`` -> ``lease.broken_local``."""
        reply = self._read("history", **{F_N: max(1, min(int(limit), HISTORY_MAX))})
        now = self._wall()
        out: list[dict[str, Any]] = []
        for e in reply.get(R_EVENTS) or []:
            if not isinstance(e, dict):
                continue
            kind = _text(e.get(E_EV))
            who = _text(e.get(E_WHO))
            info = _text(e.get(E_INFO))
            ev: dict[str, Any] = {"ts": iso(now - _int(e.get(E_AGE))), "holder": who,
                                  "reason": info, "by": "", "ev": kind}
            ev["event"] = HISTORY_EVENTS.get(kind) or f"lease.{kind}"
            if kind == "cancel" and info == "lapsed":      # a reserved head that did not take it
                ev["event"] = "lease.lapsed"
            if kind == "revoke":                      # who = the head that forced it
                m = re.match(r"from (.+)", info)
                ev.update(prior_holder=m.group(1).strip() if m else "", by=who)
            out.append(ev)
        out.sort(key=lambda e: e["ts"])           # ISO UTC sorts by time; stable keeps ties
        return out[-limit:]

    # -- the lease ------------------------------------------------------------------------------

    def lease_acquire(self, holder: str, *, ttl: int, poll_s: float = 20.0,
                      timeout_s: float = 3600.0, sleep: Callable[[float], None] | None = None,
                      log_fn: Callable[[str], None] | None = None) -> tuple[BoardGrant, str]:
        """Take the lease, polling ``acquire`` while queued (the board knows us by ``who`` and
        the SSH session, so the same ``who`` is sent each time). A queued reply is
        ``ok:true, granted:false`` and is not an error. The queue head is RESERVED for
        ``RESERVE_S`` once the holder goes, so the head polls every ``POLL_HEAD_S``. Logs
        ``queued at position N``. Returns ``(grant, expires_at)``."""
        wait = sleep or self._sleep
        say = log_fn or (lambda _t: None)
        deadline = self._mono() + timeout_s
        self.who = self.who or holder
        wire_who = board_who(holder)
        ttl_wire = max(TTL_MIN_S, min(int(ttl), TTL_MAX_S))
        while True:
            reply = self._write("acquire", raw=True, lease=self.current_token() or "",
                                **{F_WHO: wire_who, F_APP: APP, F_TTL: ttl_wire})
            token = _text(reply.get(R_TOKEN))
            if reply.get("ok") is not False and reply.get(R_GRANTED) is True \
                    and _TOKEN.fullmatch(token):
                self._set_token(token)
                self._ttl_s = ttl_wire
                self._principal = ""             # learned from `show` (holder.you), the board's name
                self._queue_id = ""
                left = _int(reply.get(R_EXPIRES), ttl_wire)
                return BoardGrant(token, left), iso(self._wall() + left)
            queued = reply.get("ok") is not False and reply.get(R_GRANTED) is False
            if reply.get("ok") is False and _text(reply.get("code")) == CODE_QUEUED:
                return self._already_ours(holder, ttl_wire)      # "lease already held by you"
            if not queued:
                raise refusal(reply, "board lease acquire")
            pos = _int(reply.get(R_POSITION))
            qid = _text(reply.get(R_ID))
            if self._queue_id and qid and qid != self._queue_id:
                # Our reserved place lapsed (dropped, then queued anew at the back): say so.
                self._queue_id = qid
                self.lease_cancel(holder)
                raise ActionFailedError(LAPSED_WORDS, hint="ask for the board again")
            self._queue_id = qid or self._queue_id
            say(f"queued at position {pos}" if pos else "queued")
            if self._mono() >= deadline:
                with_cancel = self.lease_cancel(holder)
                raise ActionFailedError(
                    f"{self.name} was not given back within {int(timeout_s)} s"
                    + ("; the queue place was removed" if with_cancel else ""),
                    hint="ask for the board (Request) or try again later")
            wait(min(poll_s, POLL_HEAD_S) if pos == 1 else poll_s)

    def _already_ours(self, holder: str, ttl_wire: int) -> tuple[BoardGrant, str]:
        """``lease_held`` on our own acquire (matched by ``who``, the reply has NO token): the
        lease is already ours, so keep using the STORED token. Without one (new state dir, wiped
        store) do not loop: say so in plain words."""
        token = self.current_token()
        view = _obj(self.show().get(R_HOLDER))
        left = _int(view.get(H_EXPIRES), ttl_wire)
        if _TOKEN.fullmatch(token):
            self._set_token(token)
            return BoardGrant(token, left), iso(self._wall() + left)
        raise ActionFailedError(
            "This board's lease is held under your name, but this Harness Manager no longer has "
            f"its key. Wait for it to run out (about {max(1, round(left / 60))} min) or ask the "
            "board's owner to switch single-user mode off.",
            hint="the owner can switch single-user mode off from Harness Manager or the touchscreen")

    def lease_heartbeat(self, token: str, holder: str) -> str:
        """Extend the lease (the acquire's TTL again). Returns the new ``expires_at``. A token
        the board no longer knows is ``LeaseLostError`` (``expired``: nobody holds the board
        any more; ``lost``: someone else does)."""
        reply = self._write("heartbeat", lease=token, raw=True, **{F_TTL: self._ttl_s})
        if reply.get("ok") is False:
            code = _text(reply.get("code"))
            if code in ("bad_token", "not_holder", "lease_required", "leased", "lease_held"):
                # The wire says `bad_token` for an expired, released and a taken token alike:
                # `show` tells them apart (nobody holds the board = expired).
                state = "lost"
                if code == "lease_required":
                    state = "expired"
                elif code == "bad_token":
                    try:
                        state = "lost" if _obj(self.show().get(R_HOLDER)) else "expired"
                    except HarnessError:
                        state = "lost"
                raise LeaseLostError(
                    f"lease heartbeat: the lease on {self.name} is gone "
                    f"({_text(reply.get('err')) or code})", state=state,
                    hint="see who holds it: harness-manager lease show")
            raise refusal(reply, "board lease heartbeat")
        # heartbeat replies with the `show` keys: the new deadline is holder.expires_in_s
        left = _int(_obj(reply.get(R_HOLDER)).get(H_EXPIRES), self._ttl_s)
        return iso(self._wall() + left)

    def lease_release(self, token: str, holder: str) -> None:
        self._write("release", lease=token)
        with self._mu:
            if self._token == token:
                self._token = ""

    def lease_cancel(self, holder: str) -> bool:
        """Leave the queue (by the ``id`` the queued ``acquire`` returned). True when a queue
        place was removed."""
        qid = self._queue_id
        if not qid:                                   # a fresh process (CLI poll): find our place
            try:
                qid = next((_text(w.get(W_ID)) for w in self._waiters()
                            if _text(w.get(W_WHO)) == board_who(holder)), "")
            except HarnessError:
                qid = ""
        if not qid:
            return False
        reply = self._write("cancel", raw=True, **{F_ID: qid, F_WHO: board_who(holder)})
        self._queue_id = ""
        return reply.get("ok") is not False

    def lease_revoke(self, reason: str) -> dict[str, Any]:
        """Force-release: the queue head, with the board's label typed (the service has checked
        it; the board only allows it after the head's request went unanswered for
        ``REQUEST_WINDOW_S``). The wire has no reason or actor: ``reason`` is HM's own note.
        Returns what the hub's does: ``{revoked, by, board, prior_holder, reason, principal}``;
        ``revoked`` is empty when nobody held the board."""
        before = _obj(self.show().get(R_HOLDER))
        prior = _text(before.get(H_WHO))
        me = self.principal()
        self._write("revoke", **{F_CONFIRM: self.name})
        return {"revoked": [self.board] if prior else [], "by": me,
                "board": self.board, "prior_holder": prior, "reason": reason, "principal": me}

    # -- request notes: the board's own requests ------------------------------------------------

    def put_request(self, note: RequestNote) -> None:
        who = board_who(note.by)
        msg = (note.message or "").strip() or REQUEST_DEFAULT_MSG   # the board refuses ""
        reply = self._write("request", **{F_WHO: who, F_MSG: msg[:MSG_MAX]})
        rid = _text(reply.get(R_ID))                  # a second request answers the same id
        if rid:
            with self._mu:
                self._requests[note.id] = rid
                self._request_who[note.id] = who
                self._requested_at[note.id] = self._mono()

    def _waiters(self) -> list[dict[str, Any]]:
        return [w for w in self.show().get(R_QUEUE) or [] if isinstance(w, dict)]

    def list_requests(self) -> list[RequestNote]:
        """The board's waiting requests, oldest first (notes as the hub's, absolute times). A
        waiter with ``msg`` ``""`` has no open request. The window is the board's
        ``REQUEST_WINDOW_S`` from the request."""
        now = self._wall()
        notes: list[RequestNote] = []
        for w in self._waiters():
            rid = _text(w.get(W_ID))
            if not rid or not _text(w.get(W_MSG)):
                continue
            made = now - _int(w.get(W_AGE))
            who = _text(w.get(W_WHO))
            notes.append(RequestNote(
                id=rid, by=who, user=who, host="", message=_text(w.get(W_MSG)),
                created_at=iso(made), deadline_at=iso(made + REQUEST_WINDOW_S), want_s=0))
        return notes

    def _board_request_id(self, note_id: str) -> str:
        with self._mu:
            return self._requests.get(note_id, note_id)

    def delete_request(self, request_id: str) -> None:
        with self._mu:
            who = self._request_who.get(request_id) or board_who(self.who)
        # `invalid` (id/who no longer match: already gone) is "nothing to cancel", not an error
        reply = self._write("cancel", raw=True, **{F_ID: self._board_request_id(request_id), F_WHO: who})
        if reply.get("ok") is False and _text(reply.get("code")) != "invalid":
            raise refusal(reply, "board lease cancel")
        with self._mu:
            self._request_who.pop(request_id, None)
            self._requests.pop(request_id, None)
            self._requested_at.pop(request_id, None)

    def put_answer(self, note: AnswerNote) -> None:
        """The holder answers a request: ``accept`` (the board hands over now), or keep for
        ``minutes`` (5-60, only sent when declining)."""
        accept = note.answer == "release"
        minutes = None if accept else max(KEEP_MIN, min(int(note.minutes or KEEP_MIN), KEEP_MAX))
        self._write("respond", lease=self.current_token(),
                    **{F_ID: self._board_request_id(note.id), F_ACCEPT: accept, F_MINUTES: minutes})

    def get_answer(self, request_id: str) -> AnswerNote | None:
        """The holder's answer to our request. The queue entry carries none: the answer is read
        from the board's history (``give`` = released, ``keep <minutes>`` in ``info``), newest
        event after we asked. None until there is one."""
        with self._mu:
            asked = self._requested_at.get(request_id)
        if asked is None:
            return None
        since = self._mono() - asked
        try:
            reply = self._read("history", **{F_N: HISTORY_MAX})
        except HarnessError:
            return None
        for e in reversed([e for e in reply.get(R_EVENTS) or [] if isinstance(e, dict)]):
            kind = _text(e.get(E_EV))
            age = _int(e.get(E_AGE))
            if kind not in (EV_GIVE, EV_KEEP) or age > since + 1:
                continue
            m = re.search(r"(\d+)", _text(e.get(E_INFO)))
            return AnswerNote(id=request_id, answer="release" if kind == EV_GIVE else "keep",
                              minutes=int(m.group(1)) if m and kind == EV_KEEP else 0, message="",
                              at=iso(self._wall() - age))
        return None

    # -- the mode -------------------------------------------------------------------------------

    def _holder(self) -> str:
        """The name this client takes the lease under (``harness-manager-<user>@<host>``)."""
        if not self.who:
            from harness_manager.services.lease import default_holder

            self.who = default_holder()
        return self.who

    def set_mode(self, on: bool, *, take: bool = False, confirm_board: str = "") -> ModeResult:
        """Switch single-user mode. The wire has one request key, ``mode``: turning it ON by the
        owner also grants the owner the lease (the reply carries ``token`` and ``expires_in_s``;
        ``take`` is therefore implicit; ``ttl_s`` is HM's board TTL); OFF is the owner's
        or the holder's (our token rides along). ``confirm_board`` has no wire field. Owner-only
        rules are the board's (``denied``)."""
        ttl = max(TTL_MIN_S, min(int(self._ttl_s), TTL_MAX_S)) if on else None
        reply = self._write("mode", lease=self.current_token(), raw=True,
                            **{F_MODE: MODE_SINGLE if on else MODE_OPEN, F_TTL: ttl})
        if reply.get("ok") is False and _text(reply.get("code")) == CODE_QUEUED:
            exc = refusal({**reply, "code": "lease_held_mode"}, "board lease mode")
            exc.code_text = CODE_QUEUED                      # type: ignore[attr-defined]
            exc.message = ("Single-user mode is already on, and someone else holds the board "
                           "now.")
            raise exc
        self._checked(reply, "board lease mode")
        token = _text(reply.get(R_TOKEN))
        granted = bool(on and _TOKEN.fullmatch(token))
        if granted:
            self._principal = ""
            self._set_token(token)
        elif not on:
            self._set_token("")
        left = _int(reply.get(R_EXPIRES), self._ttl_s)
        mode = self._mode_of({**reply, R_MODE: _text(reply.get(R_MODE)) or
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
        if kind in ("lease.broken_local",):
            return {"by": TOUCH_BY, "reason": TOUCH_REASON, "at": _text(e.get("ts"))}
        if e.get("holder") == holder and kind in ("lease.acquired", "lease.promoted",
                                                  "lease.expired", "lease.released",
                                                  "lease.given"):
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
