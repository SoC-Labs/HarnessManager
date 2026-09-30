"""Hub leases as a service (lanes L1, LR-B): show, acquire (it may queue), heartbeat, release,
and asking the holder for the board (request, respond, force, leave).

A shared lab board sits behind a hub (fpgahub). Its lease is the convention
that keeps two people from driving it at once, and it has a TTL: a lease that
lapses lets the hub reset the board under whoever is still using it. So:

- **acquire** polls through a queue until the hub grants the lease (pyverify's
  ``LeaseClient.acquire``: re-acquiring as the same holder keeps the queue
  place). It can be cancelled; a cancelled wait removes its own queue entry,
  because an abandoned entry can later grant the board to nobody.
- **heartbeat** runs while the board is open in the Harness Manager service:
  every third of the TTL (at least every 10 minutes) the lease is extended.
  A heartbeat that finds the lease gone says whether it ``expired`` (nobody
  holds it) or was ``lost`` (someone else holds it now).
- **release** needs the token AND the holder (the hub answers "no lease to
  release" and keeps the board held otherwise; pyverify.lease).

The token is kept in ``<state_dir>/leases/`` (mode 0600), one file per hub
target, so the CLI and the service share one lease: ``harness-manager lease
acquire`` in a terminal, then the service heartbeats it once the board is open.

**Whose lease.** fpgahub 0.3.0 records a lease's holder as the CALLER's principal
(``name@host``) and ignores ``--holder``. So "mine" compares the hub's holder with
this client's principal (``client.principal()``, learnt once per hub), which is
also recorded on the stored lease at acquire time. Comparing with the holder name
we ASKED for (``david-hm``) was L1's bug: the hub said ``david@mapstone-dev``.

**Requests** (docs/LEASE_REQUESTS.md, frozen). Two sessions never talk; the hub is
the only meeting point:

=============  ==========================================================================
requester      ``request``: join the queue (an acquire that queues), write a request note
               (``created_at``, ``deadline_at`` = +120 s), then poll every 10 s: the answer
               note (``lease.answered``), the deadline (``lease.force_available``, only at
               the head of the queue with no answer or a keep that has run out), the grant
               (held). A keep answer does not end it (D1): it keeps waiting, and force can
               become available again when the keep runs out. It ends held (``{lease}``),
               left (``{left: true}``, D7), or when a force promotes us.
holder         while it holds a lease on a tracked board: list the request notes every 10 s,
               ``lease.wanted`` once per request. ``respond(release)`` releases (the hub
               promotes the head of the queue) and writes the answer; ``respond(keep)``
               writes the answer only.
force          re-reads the hub (status, notes, answer), checks every rule, revokes with
               the frozen reason, then takes the lease the hub promoted us to. A holder that
               has not answered may be a script (D12, ``holder_kind``): then the board's name
               must be typed (``confirm_board``).
leave          cancel the queue entry, delete our note, ``lease.left``.
victim         a heartbeat that says ``lost``, or a status naming another holder while we
               hold a stored lease: read ``lease_history``, find ``admin_revoked``, emit
               ``lease.taken {by, reason, at}`` and keep it for ``view()`` until dismissed.
=============  ==========================================================================

Hub mode (T8): ``on_hub_event`` hears fpgahub's event stream (``hub.event``), drops the
cached view, and settles a revoke of our lease at once, with ``lease.taken`` from the event
(over REST the only source of who and why). A REST client has no note store
(``notes_supported`` False: no keep, no answer notes) and may lack the right to revoke
(``can_revoke()``): force is then unavailable, and the view says why.

Every countdown is computed from the notes' UTC timestamps against the wall clock
(``wall_clock``), never from local elapsed time, so a second process (the CLI) or a
restarted daemon sees the same deadline. Clock, sleep and poll interval are injectable.

The service is board-agnostic. It drives a board's ``hub`` adapter, which
offers ``host``, ``target`` and ``client`` (the MPS3 pack's is
``harness_manager_mps3.hub.Mps3Hub``; the client's verbs are ``lease_show``,
``lease_acquire``, ``lease_heartbeat``, ``lease_release``, ``lease_cancel``, and for
requests ``principal``, ``lease_status``, ``board_id``, ``lease_revoke``,
``lease_history``, ``put_request``, ``list_requests``, ``delete_request``,
``put_answer``, ``get_answer``). A client without the request verbs (L1's) still
works for show/acquire/heartbeat/release; the request verbs then say what is missing.

**Fresh after an action, calm through a hiccup (LEASE-FRESH, 2026-09-29).** Our own acquire,
release and heartbeat answers ARE the lease state: ``lease.state`` carries it at once, with
``source`` (``acquire``, ``release``, ``heartbeat``), ``here`` and ``at`` (additive), and the
service keeps it as the last known state, without waiting for a ``lease show``. A read of the
hub that fails (the hub's sshd resets connections under load) then does not undo it:
``view()`` answers with the last known state, marked ``stale`` (``{confirmed_at, source,
misses, error}``, additive: absent on a fresh view), while ALL of these hold:

- a state is known here (our acquire, release, heartbeat, or a read that worked);
- it was confirmed at most ``KNOWN_MAX_AGE_S`` (5 min) ago;
- fewer than ``READ_MISSES_MAX`` (3) reads in a row failed (reads that fail within
  ``READ_RETRY_S`` of the last counted one are the same hiccup: one miss; at the page's 30 s
  cadence the third miss comes ~60-90 s after the last good read);
- a held lease has not passed its expiry (a lease held here with no readable expiry counts
  from when it was confirmed plus its TTL), and one held HERE still has its token stored here;
- our release is not carried as "not leased" when others were queued (the hub hands the
  board to the head of the queue at once).

Otherwise the read's error is raised as before ("lease unknown": not known is not free).
While a carried state stands the hub is not asked again sooner than ``READ_RETRY_S``.
Background contact (``services/quiet.py``) goes ahead on a carried lease held HERE (it is
ours until its expiry, and the heartbeat keeps running), but a carried FREE state is treated
as unknown (quiet): a board last seen free may have been taken meanwhile. The explicit gates
that need a fresh confirmation (a harness install, XVC, the SSH claim: ``forget`` then
``view``) refuse a carried state as they refuse an unanswered read (``not_fresh``).

Events: ``lease.state {target, state: held|queued|released|expired|lost, holder,
expires_at}``, and ``lease.wanted``, ``lease.answered``, ``lease.force_available``,
``lease.taken``, ``lease.left`` (docs/LEASE_REQUESTS.md), and ``lease.tapped {id, by, at}``
when someone at the board tapped the front panel's request banner (``notify_holder``, CCR
PANEL-1: a notice, never a release).
"""

from __future__ import annotations

import contextlib
import copy
import getpass
import json
import logging
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from harness_manager import naming
from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    AlreadyError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus

log = logging.getLogger(__name__)

TOPIC = "lease.state"
TOPIC_WANTED = "lease.wanted"
TOPIC_ANSWERED = "lease.answered"
TOPIC_FORCE_AVAILABLE = "lease.force_available"
TOPIC_TAKEN = "lease.taken"
TOPIC_LEFT = "lease.left"
TOPIC_TAPPED = "lease.tapped"
CAPABILITY = "lease"
REQUEST_CAPABILITY = "lease requests"
DEFAULT_TTL_S = 3600
DEFAULT_REQUEST_TTL_S = 7200
MIN_HEARTBEAT_S = 30.0
MAX_HEARTBEAT_S = 600.0
TICK_S = 5.0
POLL_S = 20.0
ACQUIRE_TIMEOUT_S = 3600.0
#: How long a ``lease show`` answer is reused. Each one is an ssh round trip to the hub,
#: and a UI chip may poll; our own acquire/release/heartbeat results replace it at once.
VIEW_TTL_S = 10.0
#: FIX-PACK-1 5b: how long a caller waits for the same hub read another caller has in
#: flight before it asks the hub itself (the read's own ssh timeout ends it well before).
SHARED_READ_WAIT_S = 120.0
#: LEASE-FRESH: a known lease state carries a failed hub read only if it was confirmed (our
#: acquire, release or heartbeat, or a read that worked) at most this long ago ...
KNOWN_MAX_AGE_S = 300.0
#: ... and only for fewer than this many failed reads in a row: the third says "unknown".
READ_MISSES_MAX = 3
#: Reads that fail within this long of the last counted miss are the same hiccup (one miss),
#: and while a known state carries the hub is not asked again sooner (its load stays low).
READ_RETRY_S = 20.0
#: The holder has this long to answer a request before the requester may force it.
REQUEST_WINDOW_S = 120
#: What a board without a hub cannot do (view()'s notes_reason and revoke_reason).
NO_HUB_REASON = "this board is not behind a hub"
#: Requester and holder both look at the hub this often.
REQUEST_POLL_S = 10.0
#: A waiting request survives this many unanswered polls in a row (ssh hiccups) minus one.
REQUEST_MAX_MISSES = 3
KEEP_MINUTES = (5, 15, 30, 60)
ANSWERS = ("release", "keep")
#: A note is at most 4 KiB on the hub; the message is the only free text in it.
MESSAGE_MAX = 1000
#: Request notes are pruned after an hour; what we remember of them is too.
NOTE_MAX_AGE_S = 3600.0
#: A hub that did not say who we are is asked again after this long (not on every view).
PRINCIPAL_RETRY_S = 60.0
#: A history entry this much older than our acquire (hub and local clocks differ) is not ours.
HISTORY_SLACK_S = 600.0
FORCE_REASON = "force-released by {principal} via Harness Manager: no answer to a request made at {created_at}"
_FORCER = re.compile(r"force-released by (\S+) via Harness Manager")
#: fpgahub appends `` (by <actor>)`` to a revoke reason (``unix:alice``, ``token:ci``).
_BY_SUFFIX = re.compile(r"\(by ([^()]+)\)\s*$")
#: fpgahub events that end a lease by force; ``lease.revoked`` names ``holder``, the
#: ``admin_revoked`` audit event ``prior_holder`` (T8: both arrive over the REST event stream).
REVOKE_EVENTS = ("lease.revoked", "lease.admin_revoked")
_NOTE_ID = re.compile(r"[A-Za-z0-9_.\-]{1,64}")

_QUEUED = re.compile(r"queued(?: at position (\d+))?")

Progress = Callable[[str, int, int], None]


class HubClientLike(Protocol):
    def lease_show(self) -> Any: ...
    def lease_acquire(self, holder: str, *, ttl: int, poll_s: float = ..., timeout_s: float = ...,
                      sleep: Callable[[float], None] | None = ...,
                      log_fn: Callable[[str], None] | None = ...) -> tuple[Any, str]: ...
    def lease_heartbeat(self, token: str, holder: str) -> str: ...
    def lease_release(self, token: str, holder: str) -> None: ...
    def lease_cancel(self, holder: str) -> bool: ...


class HubAdapter(Protocol):
    """``session.hub`` (CCR L1-3 proposes it for ``core.pack``): the board's hub."""

    host: str
    target: str
    client: HubClientLike


def default_holder() -> str:
    """``harness-manager-<user>@<host>``: says who and where, and stays the same across runs,
    so re-acquiring keeps the queue place (the hub keys its queue by holder)."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in a container
        user = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return f"harness-manager-{user}@{socket.gethostname().split('.')[0]}"


def _hub_setting(hub: Any, name: str, default: Any) -> Any:
    """A named hub's lease setting (``hub.config``: CCR SET-HUB-3); ``default`` when the hub
    has none (an inline table, a test stand-in)."""
    value = getattr(getattr(hub, "config", None), name, None)
    if name == "holder":
        return value if isinstance(value, str) and value else default
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    return type(default)(value) if ok else default


def hub_holder(hub: Any) -> str:
    """The holder to ask for: the hub's ``holder`` (``[hubs.<name>] holder``), else
    ``default_holder()``."""
    return _hub_setting(hub, "holder", "") or default_holder()


def hub_lease_ttl(hub: Any) -> int:
    """The lease time to ask for: the hub's ``lease_ttl``, else ``DEFAULT_TTL_S``."""
    return _hub_setting(hub, "lease_ttl", DEFAULT_TTL_S)


def hub_request_ttl(hub: Any) -> int:
    """The lease time asked for with a request: the hub's ``request_ttl``, else
    ``DEFAULT_REQUEST_TTL_S``."""
    return _hub_setting(hub, "request_ttl", DEFAULT_REQUEST_TTL_S)


def heartbeat_interval(ttl_s: int) -> float:
    return max(MIN_HEARTBEAT_S, min(MAX_HEARTBEAT_S, ttl_s / 3.0))


# --- notes and time ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestNote:
    """``req-<id>.json`` on the hub. Same fields as ``harness_manager_mps3.hub.RequestNote``
    (the frozen interface); the service reads any object with these attributes."""

    id: str
    by: str
    user: str
    host: str
    message: str
    created_at: str
    deadline_at: str


@dataclass(frozen=True)
class AnswerNote:
    """``ans-<id>.json`` on the hub: ``answer`` is ``release`` or ``keep``."""

    id: str
    answer: str
    minutes: int
    message: str
    at: str


def iso_utc(t: float) -> str:
    """Epoch seconds as UTC ISO 8601 (``2026-09-24T10:00:00+00:00``), the notes' format."""
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def parse_utc(text: Any) -> float | None:
    """A note's ISO 8601 timestamp as epoch seconds; None when it cannot be read."""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso_norm(text: Any) -> str:
    """A note's time as ISO 8601 UTC with ``+00:00`` (D8); unreadable text is passed on."""
    t = parse_utc(text)
    return iso_utc(t) if t is not None else (text if isinstance(text, str) else "")


def force_reason(principal: str, created_at: str) -> str:
    """The revoke reason (frozen): the audit log and the victim's banner show it."""
    return FORCE_REASON.format(principal=principal, created_at=created_at)


@dataclass(frozen=True)
class ForceCheck:
    """Whether a force-release is allowed now, and if not, why.

    ``kind`` is ``""`` (available), ``"early"`` (the holder still has time to answer:
    a USAGE refusal with the time left) or ``"refused"`` (answered, not at the head,
    no request: a REFUSED refusal). ``time_left_s`` is set for a running countdown.
    """

    available: bool
    reason: str = ""
    kind: str = ""
    time_left_s: int = 0


def keep_until(answer: Any) -> float | None:
    """When a ``keep`` answer runs out (epoch seconds, from the answer note's ``at``)."""
    if answer is None or getattr(answer, "answer", "") != "keep":
        return None
    at = parse_utc(getattr(answer, "at", ""))
    if at is None:
        return None
    return at + 60 * int(getattr(answer, "minutes", 0) or 0)


def force_check(note: Any, answer: Any, position: int, now: float) -> ForceCheck:
    """The frozen rule: the deadline has passed; no answer, or a keep whose minutes have
    run out; and we are at the head of the queue. Times come from the notes."""
    if note is None:
        return ForceCheck(False, "you have no request for this board on the hub; request it first",
                          "refused")
    if answer is not None:
        kind = getattr(answer, "answer", "")
        if kind == "release":
            return ForceCheck(False, "the holder answered release: the hub hands the board to the "
                                     "head of the queue", "refused")
        if kind == "keep":
            until = keep_until(answer)
            minutes = int(getattr(answer, "minutes", 0) or 0)
            if until is None:
                return ForceCheck(False, "the holder answered keep, at a time that cannot be read",
                                  "refused")
            if now < until:
                left = max(1, math.ceil(until - now))
                return ForceCheck(False, f"the holder answered keep for {minutes} min, until "
                                         f"{iso_utc(until)} ({left} s left)", "refused", left)
        else:
            return ForceCheck(False, f"the holder's answer {kind!r} is not release or keep", "refused")
    deadline = parse_utc(getattr(note, "deadline_at", ""))
    if deadline is None:
        return ForceCheck(False, f"the request note's deadline {getattr(note, 'deadline_at', '')!r} "
                                 "cannot be read; leave the queue and request again", "refused")
    if now < deadline:
        left = max(1, math.ceil(deadline - now))
        return ForceCheck(False, f"the holder has {left} s left to answer (until {note.deadline_at})",
                          "early", left)
    if position != 1:
        if position <= 0:
            return ForceCheck(False, "you are not in the queue for this board; request it again",
                              "refused")
        return ForceCheck(False, f"you are at position {position} in the queue; a force-release "
                                 "hands the board to the head of the queue, not to you", "refused")
    return ForceCheck(True)


class ForceTooEarlyError(UnavailableError):
    """Force refused because the holder still has time to answer: UNAVAILABLE, the spec's 422
    "with the time left". ``data`` carries ``time_left_s`` (and the request id) for the API."""

    def __init__(self, reason: str, *, time_left_s: int, request_id: str = "",
                 deadline_at: str = "", hint: str = "") -> None:
        super().__init__("force-release", reason)
        self.hint = hint
        self.time_left_s = time_left_s
        self.data = {"time_left_s": time_left_s, "request_id": request_id,
                     "deadline_at": iso_norm(deadline_at)}


class ForceRefusedError(RefusedError):
    """Force refused: answered, not at the head of the queue, or no request (409 REFUSED).
    ``data`` carries ``time_left_s`` (a keep's) and the request id."""

    def __init__(self, message: str, *, time_left_s: int = 0, request_id: str = "",
                 hint: str = "") -> None:
        super().__init__(message, hint=hint)
        self.time_left_s = time_left_s
        self.data = {"time_left_s": time_left_s, "request_id": request_id}


# --- who holds it: a Harness Manager session, or maybe a script (D12) ----------------------------

HOLDER_HM = "hm"
HOLDER_UNKNOWN = "unknown"
HOLDER_KINDS = (HOLDER_HM, HOLDER_UNKNOWN)


def _field(obj: Any, name: str, default: Any = "") -> Any:
    """``name`` of an answer note, or of its public dict (``view()['request']['answer']``)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def holder_kind(answer: Any = None, *, asked: bool = True, here: bool = False, mine: bool = False,
                notes_ok: bool = True) -> tuple[str, str]:
    """D12: is the lease held by a Harness Manager session (``"hm"``), or can nobody say
    (``"unknown"``: likely a script, such as a soak or a runner)? Returns ``(kind, reason)``.

    The one evidence that cannot come from a script: **an answer to our current request**.
    ``respond()`` writes one only from the session that holds the lease's token, and D9
    re-sends the request (a new note, no answer) when the holder changes. Evidence about the
    PRINCIPAL is not enough: scripts run under the same ``name@host`` as their owner's
    Harness Manager (pyverify leases for the B1 runner and the soaks), and fpgahub records
    no ``--holder`` or client kind. So a holder that has not answered is ``unknown``: a
    script, or a person away from Harness Manager; forcing it then needs the board's name.
    """
    if here:
        return HOLDER_HM, "this Harness Manager session holds it"
    if mine:
        return HOLDER_UNKNOWN, ("it is held under your hub name, but not by this Harness Manager "
                                "session: another session of yours, or a script you run")
    kind = str(_field(answer, "answer", "") or "") if answer is not None else ""
    if kind in ANSWERS:
        minutes = int(_field(answer, "minutes", 0) or 0)
        what = f"keep {minutes} min" if kind == "keep" else "release"
        return HOLDER_HM, (f"the holder answered your request from Harness Manager ({what}, "
                           f"at {iso_norm(_field(answer, 'at', ''))})")
    if not notes_ok:
        return HOLDER_UNKNOWN, ("this hub connection carries no request notes, so Harness Manager "
                                "cannot tell a session from a script (a soak or runner)")
    if not asked:
        return HOLDER_UNKNOWN, ("you have not asked for it, and only an answer to your request "
                                "shows that a Harness Manager session holds it")
    return HOLDER_UNKNOWN, ("no Harness Manager session has answered your request for it; the "
                            "holder may be a script (a soak or runner), or someone away from "
                            "Harness Manager")


def typed_names(name: str, board: str | None, target: str, *also: str) -> list[str]:
    """D12: what may be typed to confirm force-releasing a board no Harness Manager session is
    known to hold. The first is the one to ask for: the board's name (N1: ``mps3-01``), else
    the hub's board as people write it, else the hub target. The rest are accepted too: the
    hub's board id (``mps3_01``), ``also`` (the address) and the target."""
    board = board or ""
    out: list[str] = []
    for n in (name, naming.hub_display(board) if board else "", board, *also, target):
        n = (n or "").strip()
        if n and n.casefold() not in {o.casefold() for o in out}:
            out.append(n)
    return out


def confirm_board_error(kind: str, reason: str, confirm_board: Any, names: list[str],
                        target: str) -> HarnessError | None:
    """D12: a force-release of a board whose holder is not known to be a Harness Manager
    session needs the board's name typed. None when it may go ahead; else USAGE (400) when
    ``confirm_board`` is missing, REFUSED (409) when it is not one of ``names``. A name given
    for a Harness Manager holder must be right too (a wrong one means the wrong board)."""
    if kind == HOLDER_HM and confirm_board is None:
        return None
    ask = names[0] if names else target
    data = {"holder_kind": kind, "holder_kind_reason": reason, "confirm_board": ask}
    err: HarnessError
    if kind == HOLDER_HM and isinstance(confirm_board, str) and not confirm_board.strip():
        return None
    if confirm_board is None or (isinstance(confirm_board, str) and not confirm_board.strip()):
        err = UsageError(f"force-release of {ask} needs the board's name typed: {reason}",
                         hint=f"type {ask} to confirm it (API: \"confirm_board\": \"{ask}\"; "
                              f"CLI: --confirm-board {ask})")
    elif not isinstance(confirm_board, str):
        err = UsageError(f"confirm_board must be the board's name as text, not "
                         f"{type(confirm_board).__name__}")
    elif confirm_board.strip().casefold() not in {n.casefold() for n in names}:
        err = RefusedError(f"force-release of {ask} is refused: {confirm_board.strip()!r} is not "
                           "this board's name", hint=f"type {ask} to confirm it")
    else:
        return None
    err.data = data  # type: ignore[attr-defined]
    return err


def view_confirm_error(view: dict[str, Any], confirm_board: Any, names: list[str],
                       target: str) -> HarnessError | None:
    """``confirm_board_error`` from a lease view (the daemon's route and the CLI check it
    before the job or the revoke; ``force()`` checks again against the hub). A view without
    ``holder_kind`` (an older service) counts as ``unknown``."""
    lease = view.get("lease") or {}
    kind = lease.get("holder_kind") if lease.get("holder_kind") in HOLDER_KINDS else HOLDER_UNKNOWN
    reason = lease.get("holder_kind_reason") or holder_kind(None)[1]
    return confirm_board_error(kind, reason, confirm_board, names, target)


def lease_name(board: str | None, target: str) -> str:
    """LEASE-BOARD: what lease text calls the leased thing. fpgahub's physical board
    (``mps3_01``, its "chassis") when it is known, else the hub target (``mps3_01_pl``) as
    before. The lease itself is still taken on the target (docs/HUB_MODE.md "Boards and
    targets"): this is only the name people read."""
    return (board or "").strip() or target


def lease_detail(board: str | None, target: str) -> str:
    """``mps3_01 (target mps3_01_pl)`` when the board is known and is not the target itself;
    else just the name ``lease_name`` gives (an unknown board, or a single-target board whose
    board id is its target's name)."""
    name = lease_name(board, target)
    return f"{name} (target {target})" if name != target else name


def _answer_public(answer: Any) -> dict[str, Any] | None:
    if answer is None:
        return None
    return {"answer": getattr(answer, "answer", ""), "minutes": int(getattr(answer, "minutes", 0) or 0),
            "message": getattr(answer, "message", ""), "at": iso_norm(getattr(answer, "at", ""))}


def _answer_sig(answer: Any) -> tuple[str, int, str, str] | None:
    if answer is None:
        return None
    return (getattr(answer, "answer", ""), int(getattr(answer, "minutes", 0) or 0),
            getattr(answer, "at", ""), getattr(answer, "message", ""))


def _incoming_public(note: Any, answer: Any = None) -> dict[str, Any]:
    out = {k: getattr(note, k, "") for k in ("id", "by", "user", "host", "message")}
    out["created_at"] = iso_norm(getattr(note, "created_at", ""))
    out["deadline_at"] = iso_norm(getattr(note, "deadline_at", ""))
    out["answer"] = _answer_public(answer)                    # D5
    return out


# --- the token store -------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredLease:
    hub: str
    target: str
    holder: str               # the holder name we asked for (the hub may ignore it)
    token: str
    ttl_s: int
    expires_at: str = ""
    acquired_at: float = 0.0
    principal: str = ""       # the holder the hub recorded (``name@host``), "" if unknown
    # LEASE-BOARD: no ``board`` field here on purpose. This file is shared with every other
    # Harness Manager on the machine (the CLI and the service), and an older one reads it with
    # ``StoredLease(**data)``: an extra key would make it drop the lease (and its token). The
    # board is looked up when the lease is shown (``LeaseService.board_of``), never stored.

    def public(self, *, mine: bool = True, board: str | None = None) -> dict[str, Any]:
        """The API's ``lease`` object. The token never leaves this process. ``board``
        (LEASE-BOARD, additive): the physical board the target belongs to, or None when this
        process does not know it."""
        return {"target": self.target, "board": board or None,
                "holder": self.principal or self.holder, "expires_at": self.expires_at,
                "mine": mine}


class LeaseStore:
    """One JSON file per hub target, mode 0600: the token releases the board, so it is a secret."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, hub: str, target: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", f"{hub}__{target}")
        return self.root / f"{safe}.json"

    def get(self, hub: str, target: str) -> StoredLease | None:
        try:
            data = json.loads(self._path(hub, target).read_text(encoding="utf-8"))
            # Keys a newer Harness Manager added are ignored, not a reason to lose the token.
            known = {f.name for f in fields(StoredLease)}
            return StoredLease(**{k: v for k, v in data.items() if k in known})
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def put(self, lease: StoredLease) -> None:
        self._write(self._path(lease.hub, lease.target), asdict(lease))

    def drop(self, hub: str, target: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(hub, target).unlink()

    # -- the last forced release of our lease (kept until dismissed) ----------------------------

    def _taken_path(self, hub: str, target: str) -> Path:
        return self.root / "taken" / self._path(hub, target).name

    def get_taken(self, hub: str, target: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._taken_path(hub, target).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return {k: str(data.get(k, "")) for k in ("by", "reason", "at")}

    def put_taken(self, hub: str, target: str, taken: dict[str, Any]) -> None:
        self._write(self._taken_path(hub, target), taken)

    def drop_taken(self, hub: str, target: str) -> bool:
        try:
            self._taken_path(hub, target).unlink()
        except FileNotFoundError:
            return False
        return True

    def _write(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        for d in {self.root, path.parent}:
            with contextlib.suppress(OSError):
                os.chmod(d, 0o700)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)


# --- the service -----------------------------------------------------------------------------


class _Cancelled(Exception):
    pass


class _OneShot(Exception):
    """Stops the client's acquire loop after its first answer (``_acquire_once``)."""


@dataclass
class _Tracked:
    hub: Any
    last_beat: float
    announced: bool = False
    last_watch: float = float("-inf")


@dataclass
class _Outgoing:
    """Our request for a board: the note, what we last saw, what we announced."""

    board_id: str
    hub: Any
    holder: str
    ttl_s: int
    note: Any
    heartbeat: bool = True
    answer: Any = None
    answer_sig: Any = None
    position: int = 0
    force_announced: Any = False       # the answer_sig it was announced for, or False
    asked_holder: str = ""             # who held the board when the note was written
    reasked_at: str = ""               # D9: when the note was re-sent to a new holder
    cancel: threading.Event | None = None
    left: bool = False


@dataclass
class _Incoming:
    """Requests for a lease we hold, as the holder's poll last saw them."""

    announced: dict[str, float] = field(default_factory=dict)   # id -> its created_at (wall)


@dataclass
class _Known:
    """LEASE-FRESH: the last lease state this process knows for a hub, and where it came from."""

    at: float                 # monotonic: when it was confirmed
    wall: float               # epoch seconds, the same moment (what people read)
    source: str               # acquire | release | heartbeat | show
    view: dict[str, Any]      # the view as it stood then (``lease`` None: nobody held it)


@dataclass
class _Misses:
    """LEASE-FRESH: the failed hub reads in a row for one hub (a read that works clears it)."""

    count: int = 0
    last: float = float("-inf")    # monotonic: the last counted miss
    error: str = ""


class _Flight:
    """One hub read in flight (``LeaseService._cached``): its answer or error, once done."""

    __slots__ = ("done", "error", "gen", "value")

    def __init__(self, gen: int) -> None:
        self.done = threading.Event()
        self.gen = gen
        self.value: Any = None
        self.error: BaseException | None = None


def _hk(hub: Any) -> tuple[str, str]:
    return (hub.host, hub.target)


def _configured_board(hub: Any) -> str:
    """boards.toml ``hub.board`` (the MPS3 pack's ``hub.config.board``), with no hub call;
    ``""`` when the adapter has none."""
    board = getattr(getattr(hub, "config", None), "board", "")
    return board.strip() if isinstance(board, str) else ""


def _pack_attr(client: Any, name: str, default: Any = None) -> Any:
    """``name`` from the module that defines the hub client (the MPS3 pack's ``hub``), so the
    notes the service writes are the client's own classes (it checks them) and the victim is
    told by the pack's reading of its hub's history; ``default`` for any other client."""
    mod = sys.modules.get(type(client).__module__)
    return getattr(mod, name, default) if mod is not None else default


class LeaseService:
    """Leases for boards behind a hub. ``bus`` gets ``lease.*`` events; None is fine (CLI).

    ``clock`` is monotonic (caches, heartbeats); ``wall_clock`` is epoch seconds (the notes'
    countdowns); ``sleep`` replaces the request poll's wait (tests); ``request_poll_s`` is the
    request and answer poll interval (10 s).
    """

    def __init__(self, state_dir: Path, bus: EventBus | None = None, *,
                 clock: Callable[[], float] = time.monotonic, tick_s: float = TICK_S,
                 heartbeat_s: float | None = None,
                 wall_clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] | None = None,
                 request_poll_s: float = REQUEST_POLL_S) -> None:
        self.store = LeaseStore(Path(state_dir) / "leases")
        self.bus = bus
        self._clock = clock
        self._wall = wall_clock
        self._sleep = sleep
        self._poll_s = request_poll_s
        self._tick_s = tick_s
        self._heartbeat_s = heartbeat_s          # None: a third of the lease's TTL
        self._mu = threading.Lock()
        self._tracked: dict[str, _Tracked] = {}
        self._acquiring: dict[str, threading.Event] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
        # FIX-PACK-1 5b: the hub read in flight per cache key (single-flight), and each hub's
        # generation (``_forget`` moves it: a read begun before is neither shared nor kept).
        self._inflight: dict[tuple[str, str, str], _Flight] = {}
        self._gen: dict[tuple[str, str], int] = {}
        self.hub_reads = 0                       # hub calls made through the cache (tests)
        self.shared_reads = 0                    # callers that shared one in flight (tests)
        self._principals: dict[tuple[str, str], str] = {}
        self._principal_failed: dict[tuple[str, str], float] = {}
        self._outgoing: dict[tuple[str, str], _Outgoing] = {}
        self._incoming: dict[tuple[str, str], _Incoming] = {}
        self._boards: dict[tuple[str, str], str] = {}     # hub key -> the board id last seen
        self._chassis: dict[tuple[str, str], str] = {}    # hub key -> hub.board_id() (D4)
        self._chassis_failed: dict[tuple[str, str], float] = {}
        self._viewed: dict[tuple[str, str], float] = {}   # hub key -> wall time of the last view
        self._hubs: dict[str, Any] = {}                   # board id -> its hub (hub events)
        # PANEL-2: the last view built per hub, (monotonic time, view), for view(cached_only)
        self._views: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        # PANEL-1: taps on the front panel's request banner, request id -> (seq, tapped_at)
        self._taps: dict[tuple[str, str], dict[str, tuple[int, str]]] = {}
        # PANEL-TRUTH: when the hub last confirmed that THIS process holds each hub's lease (a
        # view that said ``here``, a heartbeat it took, our acquire): (monotonic time, the
        # lease). A forget (a hub event, a note) keeps it; only a lease that ended here, or a
        # view that says otherwise, drops it. ``held_here`` reads it.
        self._here: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        # LEASE-FRESH: the last known state per hub, and the failed reads since it
        self._known: dict[tuple[str, str], _Known] = {}
        self._misses: dict[tuple[str, str], _Misses] = {}

    # -- hub reads (cached) -----------------------------------------------------------------------

    @staticmethod
    def require_hub(hub: Any, board_id: str = "") -> Any:
        if hub is None:
            raise UnavailableError(CAPABILITY, f"{board_id or 'this board'} is not behind a hub; "
                                               "add a hub table to boards.toml (docs/HIL_B0.md step 0.2)")
        return hub

    def _cached(self, hub: Any, what: str, fn: Callable[[], Any], *, fresh: bool = False) -> Any:
        """``fn()`` (a hub read), cached ``VIEW_TTL_S``. FIX-PACK-1 5b, single-flight:
        callers that miss the cache while the same read is in flight share its answer (or its
        error) instead of each opening an ssh to the hub (the hub's sshd reset them under
        load). A ``fresh`` caller never joins a read begun before it asked, and a read begun
        before a ``forget`` is neither joined nor cached after it."""
        key = (hub.host, hub.target, what)
        now = self._clock()
        with self._mu:
            hit = self._cache.get(key)
            if not fresh and hit is not None and now - hit[0] < VIEW_TTL_S:
                return hit[1]
            gen = self._gen.get(key[:2], 0)
            flight = self._inflight.get(key)
            if fresh or flight is None or flight.gen != gen:
                flight, lead = _Flight(gen), True
                self._inflight[key] = flight
            else:
                lead = False
                self.shared_reads += 1
        if not lead:
            if flight.done.wait(SHARED_READ_WAIT_S):
                if flight.error is not None:
                    raise flight.error
                return flight.value
            return fn()                  # the read in flight never ended: ask on our own
        with self._mu:
            self.hub_reads += 1
        try:
            flight.value = fn()
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self._mu:
                if self._inflight.get(key) is flight:
                    del self._inflight[key]
                if flight.error is None and self._gen.get(key[:2], 0) == flight.gen:
                    self._cache[key] = (now, flight.value)
            flight.done.set()
        return flight.value

    def _remember(self, hub: Any, what: str, value: Any) -> None:
        with self._mu:
            self._cache[(hub.host, hub.target, what)] = (self._clock(), value)

    def _show(self, hub: Any, *, fresh: bool = False) -> Any:
        """The hub's status: ``lease_status()`` (with the queue) when the client has it,
        else L1's ``lease_show()``."""
        client = hub.client
        fn = getattr(client, "lease_status", None)
        return self._cached(hub, "status", fn if callable(fn) else client.lease_show, fresh=fresh)

    def _notes(self, hub: Any, *, fresh: bool = False) -> list[Any]:
        fn = getattr(hub.client, "list_requests", None)
        if not callable(fn):
            return []
        return list(self._cached(hub, "notes", fn, fresh=fresh) or [])

    def _answer(self, hub: Any, request_id: str, *, fresh: bool = False) -> Any:
        fn = getattr(hub.client, "get_answer", None)
        if not callable(fn):
            return None
        return self._cached(hub, f"answer:{request_id}", lambda: fn(request_id), fresh=fresh)

    def _drop_at_edges(self, hub: Any) -> None:
        """D6: a cached view is dropped when a request's ``deadline_at`` or a keep's end has
        passed since the last view (``force_available`` may have flipped)."""
        key, now = _hk(hub), self._wall()
        with self._mu:
            last = self._viewed.get(key)
            self._viewed[key] = now
            cached = [(k[2], v[1]) for k, v in self._cache.items() if k[:2] == key]
        if last is None:
            return
        edges: list[float | None] = []
        for what, value in cached:
            if what == "notes":
                edges += [parse_utc(getattr(n, "deadline_at", "")) for n in value or []]
            elif what.startswith("answer:"):
                edges.append(keep_until(value))
        if any(e is not None and last < e <= now for e in edges):
            self._forget(hub)

    def _board_id(self, hub: Any) -> str | None:
        """D4: the physical board that owns the target (``hub.board_id()``: ``mps3_01``).
        Asked once per hub and kept; boards.toml ``hub.board`` answers with no hub call."""
        key = _hk(hub)
        with self._mu:
            known = self._chassis.get(key)
            failed = self._chassis_failed.get(key)
        if known:
            return known
        configured = _configured_board(hub)
        if configured:
            with self._mu:
                self._chassis[key] = configured
            return configured
        fn = getattr(hub.client, "board_id", None)
        if not callable(fn) or (failed is not None and self._clock() - failed < PRINCIPAL_RETRY_S):
            return None
        try:
            board = str(fn() or "")
        except HarnessError as exc:
            log.warning("the hub %s did not say which board owns %s: %s", hub.host, hub.target,
                        exc.message)
            board = ""
        with self._mu:
            if board:
                self._chassis[key] = board
            else:
                self._chassis_failed[key] = self._clock()
        return board or None

    def board_of(self, hub: Any, *, ask: bool = True) -> str:
        """LEASE-BOARD: the physical board (fpgahub's chassis, ``mps3_01``) the lease's target
        belongs to, for what people read; ``""`` when unknown. The lease is still taken on the
        target (docs/HUB_MODE.md "Boards and targets"). ``ask=False`` never calls the hub: only
        boards.toml ``hub.board`` or an answer this process already has (events, every
        render); ``ask=True`` asks the hub at most once per hub per process (``_board_id``)."""
        if hub is None:
            return ""
        if not ask:
            with self._mu:
                known = self._chassis.get(_hk(hub))
            return known or _configured_board(hub)
        return self._board_id(hub) or ""

    def _named(self, hub: Any) -> str:
        """The name lease messages use: the board when this process knows it, else the target
        (never a hub call: a message is not a reason to ask)."""
        return lease_name(self.board_of(hub, ask=False), hub.target)

    def forget(self, hub: Any) -> None:
        """Drop the cached view for ``hub`` (the hub said something changed; T8). LEASE-FRESH:
        the next view asks the hub even while a failed read is recent (a caller that forgets
        wants a fresh answer); if that read fails too, it counts as another miss."""
        self._forget(hub)
        with self._mu:
            miss = self._misses.get(_hk(hub))
            if miss is not None:
                miss.last = float("-inf")

    def on_hub_event(self, ev: Event) -> None:
        """``hub.event`` (T8, fpgahub's event stream): a lease change seconds before a poll.

        Drops the board's cached view. A revoke of the lease this service holds for the
        board (``lease.revoked``/``lease.admin_revoked``) settles at once: ``lease.state``
        lost, the token dropped, and ``lease.taken {by, reason, at}`` from the event itself
        (over REST it is the only place that says who and why). An expiry of it heartbeats
        now, which settles it the way a scheduled heartbeat would. Never raises.
        """
        try:
            self._on_hub_event(ev)
        except Exception:  # noqa: BLE001 - an event handler must not break the bus
            log.exception("hub event %s for %s", (ev.data or {}).get("type"), ev.board_id)

    def _on_hub_event(self, ev: Event) -> None:
        with self._mu:
            tr = self._tracked.get(ev.board_id)
            hub = tr.hub if tr is not None else self._hubs.get(ev.board_id)
        if hub is None:
            return
        self._forget(hub)
        etype = str((ev.data or {}).get("type", ""))
        data = dict((ev.data or {}).get("data") or {})
        if etype not in (*REVOKE_EVENTS, "lease.expired"):
            return
        gone = str(data.get("prior_holder") or data.get("holder") or "")
        stored = self.store.get(hub.host, hub.target)
        mine = {self._principal(hub)} | ({stored.principal, stored.holder} if stored else set())
        if not gone or gone not in mine - {""}:
            return
        if etype == "lease.expired":
            if stored is not None and tr is not None:
                tr.last_beat = self._clock() - 10 * MAX_HEARTBEAT_S
                self.beat_due()
            return
        taken = self._taken_from_event(data, (ev.data or {}).get("ts"))
        if stored is not None:
            self.store.drop(hub.host, hub.target)
            self.untrack(ev.board_id)
            log.warning("the lease on %s was revoked: %s", hub.target, taken["reason"])
            self._emit(ev.board_id, hub, "lost", stored.principal or stored.holder)
        else:
            # Already settled (the other revoke event, or a heartbeat that read the history):
            # only fill in what that could not say (fpgahub 0.3.0's history has no reason).
            before = self.store.get_taken(hub.host, hub.target)
            if before is None or before.get("reason") not in ("", taken["reason"]) or \
                    (before.get("by") and before.get("reason")):
                return                             # not this revoke, or nothing to add
            taken = {k: before.get(k) or taken[k] for k in ("by", "reason", "at")}
        self.store.put_taken(hub.host, hub.target, taken)
        self._publish(TOPIC_TAKEN, ev.board_id, dict(taken))

    def _taken_from_event(self, data: dict[str, Any], ts: Any) -> dict[str, str]:
        reason = str(data.get("reason") or "")
        m = _FORCER.search(reason)
        suffix = _BY_SUFFIX.search(reason)
        by = m.group(1) if m else str(data.get("by") or (suffix.group(1) if suffix else ""))
        return {"by": by, "reason": reason, "at": iso_norm(ts) or iso_utc(self._wall())}

    def _forget(self, hub: Any) -> None:
        with self._mu:
            self._gen[_hk(hub)] = self._gen.get(_hk(hub), 0) + 1   # FIX-PACK-1 5b
            for k in [k for k in self._cache if k[:2] == _hk(hub)]:
                del self._cache[k]
            self._views.pop(_hk(hub), None)           # PANEL-2: never hand out a stale view

    def _principal(self, hub: Any, *, required: bool = False) -> str:
        """This client's principal on the hub (``name@host``), learnt once per hub."""
        key = _hk(hub)
        with self._mu:
            known = self._principals.get(key, "")
            failed = self._principal_failed.get(key)
        if known:
            return known
        if not required and failed is not None and self._clock() - failed < PRINCIPAL_RETRY_S:
            return ""                              # asked lately and it failed: not on every view
        fn = getattr(hub.client, "principal", None)
        who = ""
        if callable(fn):
            try:
                who = str(fn() or "")
            except HarnessError as exc:
                log.warning("the hub %s did not say who this client is: %s", hub.host, exc.message)
                with self._mu:
                    self._principal_failed[key] = self._clock()
                if required:
                    raise
        if who:
            with self._mu:
                self._principals[key] = who
                self._principal_failed.pop(key, None)
        elif required:
            raise UnavailableError(REQUEST_CAPABILITY,
                                   f"the hub {hub.host} does not say who this client is "
                                   "(fpgahub whoami); lease requests need fpgahub 0.3.0")
        return who

    def _need(self, hub: Any, *verbs: str) -> None:
        missing = [v for v in verbs if not callable(getattr(hub.client, v, None))]
        if missing:
            raise UnavailableError(REQUEST_CAPABILITY,
                                   f"this hub client has no {', '.join(missing)}; lease requests "
                                   "need the fpgahub 0.3.0 client (harness_manager_mps3.hub)")

    @staticmethod
    def _notes_supported(hub: Any) -> tuple[bool, str]:
        """T8: over fpgahub's REST API there is no note store (no messages, no keep)."""
        client = hub.client
        if getattr(client, "notes_supported", True):
            return True, ""
        return False, str(getattr(client, "notes_reason", "")
                          or "this hub client cannot carry request notes")

    @staticmethod
    def _can_revoke(hub: Any) -> tuple[bool, str]:
        """T8: force-release needs a credential the hub lets revoke (REST: an admin token)."""
        fn = getattr(hub.client, "can_revoke", None)
        if not callable(fn):
            return True, ""
        try:
            ok, why = fn()
        except HarnessError as exc:
            return False, exc.message
        return bool(ok), str(why or "")

    def _my_ids(self, hub: Any, stored: StoredLease | None, principal: str) -> set[str]:
        ids = {principal}
        if stored is not None:
            ids |= {stored.principal, stored.holder}
        with self._mu:
            out = self._outgoing.get(_hk(hub))
        if out is not None:
            ids.add(out.holder)
        return ids - {""}

    # -- events -------------------------------------------------------------------------------------

    def _publish(self, topic: str, board_id: str, data: dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.publish(Event(topic, board_id, data))

    def _emit(self, board_id: str, hub: Any, state: str, holder: str = "", expires_at: str = "",
              *, warning: str = "", source: str = "") -> None:
        """``lease.state``. ``source`` (LEASE-FRESH): the hub's own answer to OUR ``acquire``,
        ``release`` or ``heartbeat`` says this state: it becomes the last known state at once
        (``_know``), and the event says so (``source``, ``here``, ``at``, additive), so a page
        shows it without waiting for a ``lease show``."""
        self._forget(hub)
        if state == "held" and not warning:
            self._confirm_here(hub, holder, expires_at)      # we hold it: the hub said so
        elif state != "held":
            with self._mu:
                self._here.pop(_hk(hub), None)                # queued, released, expired, lost
        if state in ("expired", "lost"):
            with self._mu:
                self._known.pop(_hk(hub), None)               # ours ended: nothing to carry
        # LEASE-BOARD: ``board`` (additive) is the physical board when this process knows it
        # (never a hub call from an event), else None; ``target`` stays what was leased.
        data: dict[str, Any] = {"target": hub.target, "board": self.board_of(hub, ask=False) or None,
                                "state": state, "holder": holder, "expires_at": expires_at}
        if warning:
            data["warning"] = warning             # additive: the state stands, something failed
        elif source and state in ("held", "released"):
            known = self._know(hub, source, holder if state == "held" else None, expires_at)
            data.update(source=source, here=state == "held", at=iso_utc(known.wall))
        self._publish(TOPIC, board_id, data)

    # -- LEASE-FRESH: the last known state ------------------------------------------------------

    def _know(self, hub: Any, source: str, holder: str | None, expires_at: str = "") -> _Known:
        """Our own action's answer as the last known state: held HERE by ``holder`` until
        ``expires_at``, or (``holder`` None) not leased. Never a hub call: what is not in the
        answer (the queue, the notes) is kept from the last view, or empty."""
        key = _hk(hub)
        with self._mu:
            prior = self._known.get(key)
            last = self._views.get(key)
        base = copy.deepcopy(prior.view if prior is not None else last[1] if last is not None
                             else {"lease": None, "hub": hub.host, "board": None, "queue": [],
                                   "request": None, "incoming": [], "taken": None,
                                   "notes_supported": self._notes_supported(hub)[0],
                                   "notes_reason": self._notes_supported(hub)[1],
                                   "can_revoke": True, "revoke_reason": ""})
        board = self.board_of(hub, ask=False) or base.get("board") or None
        base.update(hub=hub.host, board=board, request=None)
        if holder is None:
            base.update(lease=None, incoming=[])
        else:
            kind, why = holder_kind(here=True, mine=True)
            base["lease"] = {"target": hub.target, "board": board, "holder": holder,
                             "expires_at": expires_at, "mine": True, "here": True, "user": "",
                             "holder_kind": kind, "holder_kind_reason": why}
            base["queue"] = [e for e in base.get("queue") or [] if not e.get("mine")]
        known = _Known(self._clock(), self._wall(), source, base)
        # A release with others queued hands the board to the head of the queue at once:
        # "not leased" is then not a state to carry (a failed read says unknown instead).
        handed_on = holder is None and any(not e.get("mine") for e in base.get("queue") or [])
        with self._mu:
            if handed_on:
                self._known.pop(key, None)
            else:
                self._known[key] = known
            self._misses.pop(key, None)            # our own answer is a hub answer
        return known

    def _read_failed(self, hub: Any, exc: HarnessError) -> tuple[int, _Known | None]:
        """Count a failed read (one per ``READ_RETRY_S``: one hiccup, many callers)."""
        key, now = _hk(hub), self._clock()
        with self._mu:
            miss = self._misses.setdefault(key, _Misses())
            if miss.count == 0 or now - miss.last >= READ_RETRY_S:
                miss.count += 1
                miss.last = now
            miss.error = exc.message or type(exc).__name__
            return miss.count, self._known.get(key)

    def _why_not_carried(self, hub: Any, known: _Known | None, misses: int) -> str:
        """"" when ``known`` may stand in for a failed read, else why not (plain words)."""
        if known is None:
            return "no lease state is known here yet"
        if misses >= READ_MISSES_MAX:
            return f"the hub did not answer {misses} lease reads in a row"
        if self._clock() - known.at > KNOWN_MAX_AGE_S:
            return (f"the lease was last confirmed at {_hhmmss(known.wall)}, over "
                    f"{int(KNOWN_MAX_AGE_S // 60)} minutes ago")
        lease = known.view.get("lease")
        if not lease:
            return ""
        until = parse_utc(lease.get("expires_at"))
        stored = self.store.get(hub.host, hub.target) if lease.get("here") else None
        if lease.get("here"):
            if stored is None:
                return "this Harness Manager no longer holds the lease's token"
            if until is None:
                until = known.wall + stored.ttl_s
        if until is not None and until <= self._wall():
            return f"the lease it last knew ended at {_hhmmss(until)}"
        return ""

    def _stale_view(self, hub: Any, known: _Known, misses: int, error: str) -> dict[str, Any]:
        out = copy.deepcopy(known.view)
        taken = self.store.get_taken(hub.host, hub.target)
        out["taken"] = {**taken, "at": iso_norm(taken.get("at"))} if taken else None
        out["stale"] = {"confirmed_at": iso_utc(known.wall), "source": known.source,
                        "misses": misses, "error": error}
        return out

    def _carried(self, hub: Any, exc: HarnessError | None) -> dict[str, Any] | None:
        """LEASE-FRESH. ``exc`` None (before a read): the last known state while a failed read
        is recent (``READ_RETRY_S``: the hub is not asked again sooner), else None (read).
        ``exc`` (a read failed): the last known state, marked ``stale``, when it may stand in
        (``_why_not_carried``); otherwise ``exc`` is raised, saying why when a state was known."""
        key = _hk(hub)
        if exc is None:
            with self._mu:
                miss = self._misses.get(key)
                known = self._known.get(key)
                if miss is None or miss.count == 0 or self._clock() - miss.last >= READ_RETRY_S:
                    return None
                misses, error = miss.count, miss.error
            if known is None or self._why_not_carried(hub, known, misses):
                return None
            return self._stale_view(hub, known, misses, error)
        misses, known = self._read_failed(hub, exc)
        why = self._why_not_carried(hub, known, misses)
        if why:
            if known is None:
                raise exc
            log.info("the lease on %s is unknown: %s (%s)", hub.target, why, exc.message)
            raise UnreachableError(f"{why}; the last: {exc.message}", hint=exc.hint) from exc
        log.info("the hub %s did not answer a lease read (%d in a row): the state confirmed at "
                 "%s (%s) stands: %s", hub.host, misses, _hhmmss(known.wall), known.source,
                 exc.message)
        assert known is not None
        return self._stale_view(hub, known, misses, exc.message or type(exc).__name__)

    def _board_for(self, hub: Any, board_id: str = "") -> str:
        key = _hk(hub)
        with self._mu:
            if board_id:
                self._boards[key] = board_id
                return board_id
            return self._boards.get(key, "")

    # -- the view -------------------------------------------------------------------------------

    def view(self, hub: Any, *, cached_only: bool = False,
             max_age_s: float | None = None) -> dict[str, Any] | None:
        """``GET /boards/{bid}/lease`` (docs/LEASE_REQUESTS.md, API)::

            lease:    {target, holder, user, expires_at, mine, here,
                       holder_kind: "hm" | "unknown", holder_kind_reason} | null   # D12
            hub:      HOST | null
            board:    the physical board, hub.board_id() (D4) | null
            queue:    [{position, holder, user, mine}]
            request:  {id, message, created_at, deadline_at, position,
                       answer: {answer, minutes, message, at} | null,
                       force_available, force_reason,
                       reasked, reasked_at,             # my outgoing request; reasked: D9
                       tapped_at} | null                # PANEL-1
            incoming: [{id, by, user, host, message, created_at, deadline_at,
                        answer: {answer, minutes, message, at} | null,       # D5
                        tapped_at}]                                         # PANEL-1
            taken:    {by, reason, at} | null
            notes_supported, notes_reason    # messages and keep answers (False over REST)
            can_revoke, revoke_reason        # force-release (REST: an admin token only)

        ``reasked`` is known to the process that is waiting (D9's limit): another process
        (the CLI's ``lease show``) says False.

        ``mine`` is by principal: also true when ANOTHER session of the same principal holds
        it (every lab session is david@mapstone-dev). ``here`` (REVIEW-W5 1, additive) is true
        only when this process holds the lease token. Background contact goes by ``here``
        (``services/quiet.py``); explicit actions by ``mine``.

        ``holder_kind`` (D12, ``holder_kind()``): ``"hm"`` when a Harness Manager session is
        known to hold the lease (this one, or one that answered our request), else
        ``"unknown"`` (maybe a script): force-release then needs the board's name typed.

        ``tapped_at`` (CCR PANEL-1, ``notify_holder``): when someone at the board last tapped
        the front panel's banner for that request, as this process saw it, else null.

        ``cached_only=True`` (CCR PANEL-2, the presence beat): the last view this service
        built for the hub, copied, with no hub call at all; None when there is none, when it
        is older than ``max_age_s``, or since a lease change dropped it (every ``lease.state``,
        a hub event, a request's deadline). Without a hub: the empty view, as always.
        """
        empty: dict[str, Any] = {"lease": None, "hub": None, "board": None, "queue": [],
                                 "request": None, "incoming": [], "taken": None,
                                 "notes_supported": False, "notes_reason": NO_HUB_REASON,
                                 "can_revoke": False, "revoke_reason": NO_HUB_REASON}
        if hub is None:
            return empty
        if cached_only:
            with self._mu:
                hit = self._views.get(_hk(hub))
            if hit is None or (max_age_s is not None and self._clock() - hit[0] > max_age_s):
                return None
            return copy.deepcopy(hit[1])
        notes_ok, notes_why = self._notes_supported(hub)
        can, why = self._can_revoke(hub)
        out = {**empty, "hub": hub.host, "board": self._board_id(hub),
               "notes_supported": notes_ok, "notes_reason": "" if notes_ok else notes_why,
               "can_revoke": can, "revoke_reason": "" if can else why}
        self._drop_at_edges(hub)
        carried = self._carried(hub, None)          # LEASE-FRESH: a hiccup is recent
        if carried is not None:
            return carried
        try:
            shown = self._show(hub)
        except UnreachableError as exc:             # the hub did not answer (not "free")
            stale = self._carried(hub, exc)         # the last known state, or it raises
            assert stale is not None
            return stale
        stored = self.store.get(hub.host, hub.target)
        principal = self._principal(hub)
        ids = self._my_ids(hub, stored, principal)
        holder = getattr(shown, "holder", "") or ""
        held = bool(getattr(shown, "held", False))
        if held and stored is not None and principal and holder not in ids:
            # A stored lease, and the hub names someone else: ours is gone. Check once more,
            # fresh, so a status read just before our own acquire cannot drop a new lease.
            shown = self._show(hub, fresh=True)
            holder, held = getattr(shown, "holder", "") or "", bool(getattr(shown, "held", False))
            stored = self.store.get(hub.host, hub.target)
            if held and stored is not None and holder not in ids:
                self._lost(self._board_for(hub), hub, stored, holder)
                stored = None
        # "mine" is by principal (docs/LEASE_REQUESTS.md "Who am I"): also true when another
        # session of ours holds it. Answering requests needs the token, so "incoming" is only
        # for the session that holds it here.
        here = held and stored is not None and holder in ids
        mine = held and (here or (bool(principal) and holder == principal))
        if held:
            out["lease"] = {"target": hub.target, "board": out["board"], "holder": holder,
                            "expires_at": getattr(shown, "expires_at", "")
                            or (stored.expires_at if here and stored else ""),
                            "mine": mine, "here": here,
                            "user": getattr(shown, "user", "") or ""}
        queue = list(getattr(shown, "queue", ()) or ())
        out["queue"] = [{"position": int(getattr(e, "position", 0) or 0),
                         "holder": getattr(e, "holder", ""), "user": getattr(e, "user", ""),
                         "mine": bool(principal) and getattr(e, "holder", "") in ids}
                        for e in queue]
        notes = self._safe_notes(hub)
        if principal:
            ours = self._latest_of(notes, principal)
            if ours is not None:
                out["request"] = self._request_public(hub, ours, queue, principal, (can, why))
            if here:
                out["incoming"] = self._incoming_list(hub, notes, principal, queue,
                                                      has_queue=hasattr(shown, "queue"))
        if out["lease"] is not None:
            req = out["request"]
            kind, why = holder_kind(req.get("answer") if req else None, asked=req is not None,
                                    here=here, mine=mine, notes_ok=notes_ok)
            out["lease"].update(holder_kind=kind, holder_kind_reason=why)
        taps = self._taps_for(hub, {getattr(n, "id", "") for n in notes})
        if out["request"] is not None:
            out["request"]["tapped_at"] = taps.get(out["request"]["id"])
        for inc in out["incoming"]:
            inc["tapped_at"] = taps.get(inc["id"])
        taken = self.store.get_taken(hub.host, hub.target)
        out["taken"] = {**taken, "at": iso_norm(taken.get("at"))} if taken else None
        with self._mu:
            self._views[_hk(hub)] = (self._clock(), copy.deepcopy(out))
            self._known[_hk(hub)] = _Known(self._clock(), self._wall(), "show", copy.deepcopy(out))
            self._misses.pop(_hk(hub), None)           # LEASE-FRESH: the hub answered
            if here:
                self._here[_hk(hub)] = (self._clock(), copy.deepcopy(out["lease"]))
            else:
                self._here.pop(_hk(hub), None)
        return out

    def _confirm_here(self, hub: Any, holder: str, expires_at: str) -> None:
        board = self.board_of(hub, ask=False) or None      # before the lock: it takes it too
        with self._mu:
            self._here[_hk(hub)] = (self._clock(), {
                "target": hub.target, "board": board, "holder": holder,
                "expires_at": expires_at, "mine": True, "here": True, "user": ""})

    def held_here(self, hub: Any, *, max_age_s: float) -> tuple[float, dict[str, Any]] | None:
        """PANEL-TRUTH: when the hub last confirmed that this process holds ``hub``'s lease (a
        view that said ``here``, a heartbeat it took, our acquire), as ``(age_s, lease)``: at
        most ``max_age_s`` old, and only while the lease's token is still stored here. None
        otherwise. Never a hub call: the fallback for a hub read that failed transiently (the
        hub's sshd reset one ssh), so one hub hiccup does not undo what the hub just said."""
        if hub is None:
            return None
        with self._mu:
            hit = self._here.get(_hk(hub))
        if hit is None:
            return None
        age = self._clock() - hit[0]
        if age > max_age_s or self.store.get(hub.host, hub.target) is None:
            return None
        return age, copy.deepcopy(hit[1])

    def _taps_for(self, hub: Any, live: set[str]) -> dict[str, str]:
        """PANEL-1: request id -> tapped_at, for the notes still on the hub (the rest go)."""
        with self._mu:
            taps = self._taps.get(_hk(hub), {})
            for rid in [r for r in taps if r not in live]:
                del taps[rid]
            return {rid: at for rid, (_seq, at) in taps.items()}

    # -- the front panel (CCR PANEL-1) ------------------------------------------------------------

    def notify_holder(self, board_id: str, hub: Any, *, seq: int, at: float) -> dict[str, Any]:
        """Someone at the board tapped the lease-request banner on its front panel (decision
        P2: a tap notifies the holder; it never releases).

        The banner shows the open request: the oldest request note for the board with no
        answer yet. Its ``tapped_at`` is recorded here (``view()``: ``incoming[].tapped_at``
        in the holder's Harness Manager, ``request.tapped_at`` in the requester's) and
        ``lease.tapped {id, by, at}`` is published, once per tap (``seq``; presence calls this
        in every Harness Manager that watches the board, since the tap ring is never
        acknowledged). ``notified`` is True where the lease is held (this process has its
        token): there the holder is told. Elsewhere the tap is still recorded.

        Reads the request notes and their answers (cached for 10 s); writes nothing to the
        hub, and never releases, answers, forces or leaves. Returns
        ``{"notified": bool, "request": {"id", "by"} | None}``.
        """
        nothing: dict[str, Any] = {"notified": False, "request": None}
        if hub is None:
            return nothing
        self._board_for(hub, board_id)
        notes = sorted(self._safe_notes(hub),
                       key=lambda n: (parse_utc(getattr(n, "created_at", "")) or 0.0,
                                      getattr(n, "id", "")))
        note = None
        for n in notes:
            try:
                answered = self._answer(hub, n.id) is not None
            except HarnessError as exc:
                log.warning("reading the answer to %s on %s: %s", n.id, hub.host, exc.message)
                answered = False
            if not answered:
                note = n
                break
        if note is None:
            return nothing                          # no open request: the banner was stale
        when = iso_utc(at) if isinstance(at, (int, float)) and not isinstance(at, bool) \
            and at > 0 else iso_utc(self._wall())
        request = {"id": note.id, "by": getattr(note, "by", "")}
        key = _hk(hub)
        with self._mu:
            taps = self._taps.setdefault(key, {})
            seen = taps.get(note.id)
            fresh = seen is None or seen[0] != seq
            if fresh:
                taps[note.id] = (seq, when)
                hit = self._views.get(key)
                if hit is not None:                 # the cached view says so too (PANEL-2)
                    view = hit[1]
                    for entry in [view.get("request") or {}, *(view.get("incoming") or [])]:
                        if entry.get("id") == note.id:
                            entry["tapped_at"] = when
        if fresh:
            self._publish(TOPIC_TAPPED, board_id, {**request, "at": when})
        here = self.store.get(hub.host, hub.target) is not None
        return {"notified": here, "request": request}

    def _safe_notes(self, hub: Any) -> list[Any]:
        try:
            return self._notes(hub)
        except HarnessError as exc:
            log.warning("listing lease requests on %s: %s", hub.host, exc.message)
            return []

    @staticmethod
    def _latest_of(notes: list[Any], principal: str) -> Any:
        ours = [n for n in notes if getattr(n, "by", "") == principal]
        if not ours:
            return None
        return max(ours, key=lambda n: parse_utc(getattr(n, "created_at", "")) or 0.0)

    @staticmethod
    def _position(queue: list[Any], ids: set[str] | str) -> int:
        want = {ids} if isinstance(ids, str) else ids
        for e in queue:
            if getattr(e, "holder", "") in want:
                return int(getattr(e, "position", 0) or 0)
        return 0

    def _request_public(self, hub: Any, note: Any, queue: list[Any], principal: str,
                        revoke: tuple[bool, str]) -> dict[str, Any]:
        try:
            answer = self._answer(hub, note.id)
        except HarnessError as exc:
            log.warning("reading the answer to %s on %s: %s", note.id, hub.host, exc.message)
            answer = None
        position = self._position(queue, principal)
        check = force_check(note, answer, position, self._wall())
        if check.available and not revoke[0]:
            check = ForceCheck(False, revoke[1], "refused")
        with self._mu:
            out = self._outgoing.get(_hk(hub))
        reasked_at = out.reasked_at if out is not None and out.note.id == note.id else ""
        return {"id": note.id, "message": getattr(note, "message", ""),
                "created_at": iso_norm(note.created_at), "deadline_at": iso_norm(note.deadline_at),
                "position": position, "answer": _answer_public(answer),
                "force_available": check.available, "force_reason": check.reason,
                "reasked": bool(reasked_at), "reasked_at": reasked_at or None}

    def _incoming_list(self, hub: Any, notes: list[Any], principal: str, queue: list[Any], *,
                       has_queue: bool) -> list[dict[str, Any]]:
        waiting = {getattr(e, "holder", "") for e in queue}
        out = []
        for n in notes:
            if getattr(n, "by", "") == principal or (has_queue and getattr(n, "by", "") not in waiting):
                continue
            try:
                answer = self._answer(hub, n.id)
            except HarnessError as exc:
                log.warning("reading the answer to %s on %s: %s", n.id, hub.host, exc.message)
                answer = None
            out.append(_incoming_public(n, answer))
        return out

    def dismiss_taken(self, hub: Any) -> bool:
        """Forget the last forced release of our lease (the victim's banner was dismissed)."""
        if hub is None:
            return False
        return self.store.drop_taken(hub.host, hub.target)

    # -- acquire / release ------------------------------------------------------------------------

    def acquire(self, hub: Any, *, board_id: str = "", ttl_s: int | None = None,
                holder: str | None = None, progress: Progress | None = None,
                cancel: threading.Event | None = None, poll_s: float | None = None,
                timeout_s: float | None = None, heartbeat: bool = True) -> dict[str, Any]:
        """Block until the lease is held (it may queue); store it; heartbeat it while tracked.

        ``ttl_s``, ``holder`` and ``timeout_s`` not given: the hub's ``lease_ttl``, ``holder``
        and ``queue_timeout`` (a named hub, SET-HUB-3), else 3600 s, ``default_holder()``
        and 3600 s."""
        hub = self.require_hub(hub, board_id)
        self._board_for(hub, board_id)
        if ttl_s is None:
            ttl_s = hub_lease_ttl(hub)
        if timeout_s is None:
            timeout_s = _hub_setting(hub, "queue_timeout", ACQUIRE_TIMEOUT_S)
        if not isinstance(ttl_s, int) or ttl_s <= 0:
            raise UsageError(f"ttl_s must be a positive whole number of seconds, not {ttl_s!r}")
        holder = holder or hub_holder(hub)
        stored = self.store.get(hub.host, hub.target)
        if stored is not None:
            shown = self._show(hub, fresh=True)
            ids = {stored.principal, holder} - {""}
            if shown.held and shown.holder in ids and stored.holder == holder:
                # Already ours: say so rather than queue behind ourselves.
                if heartbeat:
                    self.track(board_id, hub)
                return {"lease": stored.public(board=self.board_of(hub, ask=False)),
                        "already": True}
        cancel = cancel or threading.Event()
        key = board_id or f"{hub.host}/{hub.target}"
        with self._mu:
            self._acquiring[key] = cancel
        report = progress or (lambda *_: None)

        def sleep(seconds: float) -> None:
            if cancel.wait(seconds):
                raise _Cancelled()

        def on_log(message: str) -> None:
            m = _QUEUED.search(message)
            if m:
                pos = int(m.group(1)) if m.group(1) else 0
                report("queued", pos, 0)
                self._emit(board_id, hub, "queued", holder)

        report("acquire", 0, 1)
        try:
            lease, expires_at = hub.client.lease_acquire(holder, ttl=ttl_s,
                                                          poll_s=poll_s or POLL_S,
                                                          timeout_s=timeout_s, sleep=sleep,
                                                          log_fn=on_log)
        except _Cancelled:
            removed = False
            with contextlib.suppress(HarnessError):
                # By PRINCIPAL (CCR-A3): over the hub's unix socket we are an admin, whose
                # --holder is taken literally, so the name we asked for cancels nothing.
                removed = hub.client.lease_cancel(self._principal(hub) or holder)
            raise ActionFailedError(
                f"the lease request for {self._named(hub)} was cancelled"
                + ("; its queue entry was removed" if removed else ""),
                hint="acquire again when you want the board") from None
        finally:
            with self._mu:
                self._acquiring.pop(key, None)
        record = self._store_grant(hub, holder, lease, expires_at, ttl_s)
        report("held", 1, 1)
        self._emit(board_id, hub, "held", record.principal or holder, expires_at, source="acquire")
        if heartbeat:
            self.track(board_id, hub, announced=True)
        return {"lease": record.public(board=self.board_of(hub, ask=False))}

    def _store_grant(self, hub: Any, holder: str, lease: Any, expires_at: str, ttl_s: int) -> StoredLease:
        """Keep a granted lease, with the principal the hub recorded it under."""
        record = StoredLease(hub=hub.host, target=hub.target, holder=holder, token=lease.token,
                             ttl_s=ttl_s, expires_at=expires_at, acquired_at=self._wall(),
                             principal=self._principal(hub))
        self.store.put(record)
        return record

    def cancel_acquire(self, board_id: str) -> bool:
        """Stop a queued acquire or request for this board (its job then removes the queue
        entry and withdraws the request note)."""
        with self._mu:
            ev = self._acquiring.get(board_id)
        if ev is None:
            return False
        ev.set()
        return True

    def release(self, hub: Any, *, board_id: str = "") -> dict[str, Any]:
        hub = self.require_hub(hub, board_id)
        if self.cancel_acquire(board_id):
            return {"ok": True, "cancelled": True}
        stored = self.store.get(hub.host, hub.target)
        if stored is None:
            shown = self._show(hub, fresh=True)
            who = f"held by {shown.holder}" if shown.held else "not leased"
            raise AbsentError(f"this Harness Manager holds no lease on {self._named(hub)} ({who})",
                              hint="a lease taken outside Harness Manager is released where it was "
                                   "taken (fpgahub lease release --token …)")
        return self._release_stored(board_id, hub, stored)

    def _release_stored(self, board_id: str, hub: Any, stored: StoredLease) -> dict[str, Any]:
        hub.client.lease_release(stored.token, stored.holder)
        self.store.drop(hub.host, hub.target)
        self.untrack(board_id)
        self._emit(board_id, hub, "released", stored.principal or stored.holder, source="release")
        return {"ok": True,
                "released": stored.public(mine=True, board=self.board_of(hub, ask=False))}

    # -- requests: the requester --------------------------------------------------------------------

    def request(self, board_id: str, hub: Any, *, message: str = "", ttl_s: int | None = None,
                progress: Progress | None = None, cancel: threading.Event | None = None,
                heartbeat: bool = True) -> dict[str, Any]:
        """Ask the holder for the board: queue, write a request note, and wait.

        Returns ``{lease}`` when the hub grants it (the holder released, a force promoted
        us, or the lease lapsed to us), or ``{left: true}`` when we leave (``leave()``, the
        cancel event, the board closing: D7). A keep answer does NOT end it (D1): it emits
        ``lease.answered`` and keeps waiting; force can become available again when the keep
        runs out. Phases: ``queued``, ``notified``, ``answered``, ``force-available``,
        ``held``. ``heartbeat=False`` (the CLI) does not heartbeat the lease it gets.
        """
        hub = self.require_hub(hub, board_id)
        self._board_for(hub, board_id)
        if ttl_s is None:
            ttl_s = hub_request_ttl(hub)                 # SET-HUB-3: the hub's request_ttl
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or ttl_s <= 0:
            raise UsageError(f"ttl_s must be a positive whole number of seconds, not {ttl_s!r}")
        message = self._check_message(message)
        self._need(hub, "lease_status", "put_request", "list_requests", "get_answer",
                   "delete_request")
        principal = self._principal(hub, required=True)
        status = self._show(hub, fresh=True)
        if status.held and status.holder == principal:
            # CCR-A2: fpgahub keys leases on the principal, so an acquire would hand this
            # session the lease another session of ours holds, token and all. Refuse.
            ours = self.store.get(hub.host, hub.target)
            raise AlreadyError(f"you already hold {self._named(hub)}"
                               + ("" if ours is not None else " (another session)"),
                               hint="use it there, or release it there first")
        asked = status.holder if status.held else ""
        key = _hk(hub)
        cancel = cancel or threading.Event()
        gate = board_id or f"{hub.host}/{hub.target}"
        named = self._named(hub)                  # before the lock: it takes the lock itself
        with self._mu:
            prior = self._outgoing.get(key)
            if prior is not None:
                raise AlreadyError(f"a request for {named} is already waiting",
                                   hint="watch it, or leave the queue to withdraw it")
            if gate in self._acquiring:
                raise AlreadyError(f"an acquire for {named} is already waiting",
                                   hint="cancel it first (DELETE the lease)")
            self._acquiring[gate] = cancel
        report = progress or (lambda *_: None)
        holder = hub_holder(hub)
        out: _Outgoing | None = None
        misses = 0
        try:
            while True:
                if cancel.is_set():
                    raise _Cancelled()
                try:
                    lease, expires_at, position = self._acquire_once(hub, holder, ttl_s)
                    if lease is not None:
                        return self._granted(board_id, hub, holder, lease, expires_at, ttl_s,
                                             report, heartbeat=heartbeat)
                    if out is None:
                        report("queued", position, 0)
                        self._emit(board_id, hub, "queued", principal)
                        out = self._open_request(board_id, hub, holder, ttl_s, message, principal,
                                                 cancel, heartbeat, asked)
                        report("notified", 0, 0)
                    self._moved(out, position, principal, report)
                    self._poll_answer(out, report)
                    misses = 0
                except UnreachableError as exc:
                    misses += 1                  # an ssh hiccup: the queue place is still ours
                    if misses >= REQUEST_MAX_MISSES:
                        raise
                    log.warning("request for %s: poll %d missed (will retry): %s", hub.target,
                                misses, exc.message)
                self._nap(cancel)
        except _Cancelled:
            if out is None or not out.left:        # leave() has already done it otherwise
                self._leave_hub(board_id, hub, principal)
            return {"left": True}                  # D7: leaving is not a failure
        except HarnessError:
            # The wait failed: do not leave a queue place behind that nobody watches (it can
            # later hand the board to nobody). Best effort: the hub may be the thing that failed.
            with contextlib.suppress(HarnessError):
                self._leave_hub(board_id, hub, principal)
            raise
        finally:
            with self._mu:
                if self._acquiring.get(gate) is cancel:
                    del self._acquiring[gate]
                if out is not None and self._outgoing.get(key) is out:
                    del self._outgoing[key]      # e.g. Ctrl-C: the caller leaves or keeps it

    @staticmethod
    def _check_message(message: Any) -> str:
        if message is None:
            return ""
        if not isinstance(message, str):
            raise UsageError(f"message must be text, not {type(message).__name__}")
        if len(message) > MESSAGE_MAX:
            raise UsageError(f"message must be at most {MESSAGE_MAX} characters, not {len(message)}")
        return message

    def _nap(self, cancel: threading.Event) -> None:
        if self._sleep is None:
            if cancel.wait(self._poll_s):
                raise _Cancelled()
            return
        self._sleep(self._poll_s)
        if cancel.is_set():
            raise _Cancelled()

    def _acquire_once(self, hub: Any, holder: str, ttl_s: int) -> tuple[Any, str, int]:
        """One acquire: ``(lease, expires_at, 0)`` when granted, else ``(None, "", position)``.
        Re-acquiring keeps our queue place (the hub keys the queue by principal)."""
        position = [0]

        def on_log(message: str) -> None:
            m = _QUEUED.search(message)
            if m and m.group(1):
                position[0] = int(m.group(1))

        def stop(_seconds: float) -> None:
            raise _OneShot()

        try:
            lease, expires_at = hub.client.lease_acquire(holder, ttl=ttl_s, poll_s=self._poll_s,
                                                          timeout_s=ACQUIRE_TIMEOUT_S, sleep=stop,
                                                          log_fn=on_log)
        except _OneShot:
            return None, "", position[0]
        return lease, expires_at, 0

    def _open_request(self, board_id: str, hub: Any, holder: str, ttl_s: int, message: str,
                      principal: str, cancel: threading.Event, heartbeat: bool,
                      asked: str) -> _Outgoing:
        """Our note on the hub: the one already there (its countdown stands), else a new one.
        ``asked`` is who held the board when we queued."""
        ours = self._latest_of(list(hub.client.list_requests() or []), principal)
        if ours is None:
            ours = self._new_note(hub, principal, message)
            hub.client.put_request(ours)
        out = _Outgoing(board_id, hub, holder, ttl_s, ours, heartbeat=heartbeat, cancel=cancel,
                        asked_holder=asked)
        with self._mu:
            self._outgoing[_hk(hub)] = out
        self._forget(hub)
        return out

    def _new_note(self, hub: Any, principal: str, message: str) -> Any:
        now = self._wall()
        cls = _pack_attr(hub.client, "RequestNote", RequestNote)
        return cls(id=f"{int(now)}-{secrets.token_hex(4)}", by=principal,
                           user=_local_user(), host=socket.gethostname().split(".")[0],
                           message=message, created_at=iso_utc(now),
                           deadline_at=iso_utc(now + REQUEST_WINDOW_S))

    def _moved(self, out: _Outgoing, position: int, principal: str,
               report: Progress | None = None) -> None:
        """We moved up the queue. If that is because someone ahead of us got the board, its
        new holder was never asked: send the request to them, with a fresh deadline."""
        moved_up = 0 < position < out.position
        out.position = position
        if not moved_up:
            return
        status = self._show(out.hub, fresh=True)
        holder = (getattr(status, "holder", "") or "") if getattr(status, "held", False) else ""
        if holder and holder not in (out.asked_holder, principal):
            self._reissue(out, principal, holder)
            if report is not None:
                report("notified", 0, 0)

    def _reissue(self, out: _Outgoing, principal: str, holder: str) -> None:
        hub = out.hub
        with contextlib.suppress(HarnessError):
            hub.client.delete_request(out.note.id)
        note = self._new_note(hub, principal, getattr(out.note, "message", ""))
        hub.client.put_request(note)
        log.info("%s now holds %s; the request was sent to them (deadline %s)", holder, hub.target,
                 note.deadline_at)
        out.note, out.asked_holder = note, holder
        out.reasked_at = iso_norm(note.created_at)
        out.answer = out.answer_sig = None
        out.force_announced = False
        self._forget(hub)

    def _poll_answer(self, out: _Outgoing, report: Progress | None = None) -> None:
        """Read the answer; announce a new one, and a force that became available (again,
        after a keep runs out)."""
        hub = out.hub
        answer = hub.client.get_answer(out.note.id)
        self._remember(hub, f"answer:{out.note.id}", answer)
        sig = _answer_sig(answer)
        if sig is not None and sig != out.answer_sig:
            out.answer, out.answer_sig = answer, sig
            self._publish(TOPIC_ANSWERED, out.board_id,
                          {"id": out.note.id, "answer": sig[0], "minutes": sig[1],
                           "message": getattr(answer, "message", "")})
            if report is not None:
                report("answered", 0, 0)
        check = force_check(out.note, out.answer, out.position, self._wall())
        if check.available and out.force_announced != ("sig", out.answer_sig) and \
                self._can_revoke(hub)[0]:
            out.force_announced = ("sig", out.answer_sig)
            self._publish(TOPIC_FORCE_AVAILABLE, out.board_id, {"id": out.note.id})
            if report is not None:
                report("force-available", 0, 0)

    def _granted(self, board_id: str, hub: Any, holder: str, lease: Any, expires_at: str, ttl_s: int,
                 report: Progress | None = None, *, heartbeat: bool = True,
                 already: bool = False) -> dict[str, Any]:
        """The hub gave us the board: keep the token, withdraw our note, heartbeat it."""
        record = self._store_grant(hub, holder, lease, expires_at, ttl_s)
        with self._mu:
            self._outgoing.pop(_hk(hub), None)
        self._delete_our_notes(hub, record.principal)
        if report is not None:
            report("held", 1, 1)
        self._emit(board_id, hub, "held", record.principal or holder, expires_at, source="acquire")
        if heartbeat:
            self.track(board_id, hub, announced=True)
        out: dict[str, Any] = {"lease": record.public(board=self.board_of(hub, ask=False))}
        if already:
            out["already"] = True
        return out

    def _delete_our_notes(self, hub: Any, principal: str) -> bool:
        if not principal:
            return False
        deleted = False
        try:
            for note in list(hub.client.list_requests() or []):
                if getattr(note, "by", "") == principal:
                    hub.client.delete_request(note.id)
                    deleted = True
        except HarnessError as exc:
            log.warning("withdrawing our request note on %s: %s", hub.host, exc.message)
        self._forget(hub)
        return deleted

    # -- requests: leave ---------------------------------------------------------------------------

    def leave(self, board_id: str, hub: Any) -> dict[str, Any]:
        """Leave the queue and withdraw our request: ``{left: bool}``."""
        hub = self.require_hub(hub, board_id)
        self._board_for(hub, board_id)
        self._need(hub, "list_requests", "delete_request")
        principal = self._principal(hub, required=True)
        with self._mu:
            out = self._outgoing.get(_hk(hub))
            if out is not None:
                out.left = True
                if out.cancel is not None:
                    out.cancel.set()               # a blocking request() stops quietly
        return self._leave_hub(board_id, hub, principal)

    def _leave_hub(self, board_id: str, hub: Any, principal: str) -> dict[str, Any]:
        removed = bool(hub.client.lease_cancel(principal))
        deleted = self._delete_our_notes(hub, principal)
        with self._mu:
            had = self._outgoing.pop(_hk(hub), None) is not None
        left = removed or deleted or had
        if left:
            self._publish(TOPIC_LEFT, board_id, {})
        return {"left": left}

    # -- requests: the holder --------------------------------------------------------------------

    def respond(self, board_id: str, hub: Any, request_id: str, answer: str, *, minutes: int = 0,
                message: str = "") -> dict[str, Any]:
        """Answer a request for our lease: ``release`` (now) or ``keep`` (5/15/30/60 min)."""
        hub = self.require_hub(hub, board_id)
        self._board_for(hub, board_id)
        if not isinstance(request_id, str) or not _NOTE_ID.fullmatch(request_id):
            raise UsageError(f"request id {request_id!r} is not a request id ([A-Za-z0-9_.-])")
        if answer not in ANSWERS:
            raise UsageError(f"answer must be release or keep, not {answer!r}")
        if answer == "keep":
            if isinstance(minutes, bool) or minutes not in KEEP_MINUTES:
                raise UsageError(f"keep takes minutes = 5, 15, 30 or 60, not {minutes!r}")
        else:
            minutes = 0
        message = self._check_message(message)
        self._need(hub, "lease_status", "list_requests", "put_answer")
        notes_ok, notes_why = self._notes_supported(hub)
        if answer == "keep" and not notes_ok:
            raise UnavailableError("keep", notes_why)          # T8: REST has no answer notes
        principal = self._principal(hub, required=True)
        stored = self.store.get(hub.host, hub.target)
        shown = self._show(hub, fresh=True)
        ids = self._my_ids(hub, stored, principal)
        if stored is None or not shown.held or shown.holder not in ids:
            who = f"{shown.holder} holds it" if shown.held else "nobody holds it"
            raise RefusedError(f"only the holder answers requests for {self._named(hub)}, and this "
                               f"Harness Manager does not hold it ({who})")
        notes = self._notes(hub, fresh=True)
        note = next((n for n in notes if n.id == request_id), None)
        if note is None:
            raise AbsentError(f"no request {request_id} for {self._named(hub)} (withdrawn, or it expired)",
                              hint="list the requests again")
        now = self._wall()
        reply = _pack_attr(hub.client, "AnswerNote", AnswerNote)(
            id=request_id, answer=answer, minutes=minutes, message=message, at=iso_utc(now))
        if answer == "release":
            released = self._release_stored(board_id, hub, stored)   # the hub promotes the head
            try:
                if notes_ok:
                    hub.client.put_answer(reply)
            except HarnessError as exc:
                log.warning("released %s, but the answer note was not written: %s", hub.target,
                            exc.message)
            return {"ok": True, "answer": _answer_public(reply), "released": released["released"]}
        hub.client.put_answer(reply)
        self._forget(hub)
        return {"ok": True, "answer": _answer_public(reply)}

    # -- requests: force -----------------------------------------------------------------------------

    def force(self, board_id: str, hub: Any, *, confirm: bool, confirm_board: str | None = None,
              board_names: tuple[str, ...] | list[str] = (), heartbeat: bool = True) -> dict[str, Any]:
        """Force-release the board to us: every rule re-checked against the hub NOW, then
        revoke (the hub promotes the head of the queue, which is us) and take the lease.

        D12: unless the holder answered our request (a Harness Manager session), it may be a
        script, and ``confirm_board`` must be the board's name: one of ``typed_names()`` of
        ``board_names`` (the names the caller showed, the one it asked for first: the N1
        name, then e.g. the address), the hub's board and the target. Missing: USAGE;
        another name: REFUSED; both before any revoke."""
        hub = self.require_hub(hub, board_id)
        self._board_for(hub, board_id)
        if confirm is not True:
            raise UsageError("force-release needs confirm: true",
                             hint=f"are you sure? This kicks the holder off {self._named(hub)} now; "
                                  "anything they are running on the board is interrupted")
        self._need(hub, "lease_status", "list_requests", "get_answer", "lease_revoke",
                   "delete_request")
        principal = self._principal(hub, required=True)
        status = self._show(hub, fresh=True)
        holder = hub_holder(hub)
        if status.held and status.holder == principal:
            ours = self.store.get(hub.host, hub.target)
            raise AlreadyError(f"{self._named(hub)} is already yours"
                               + ("" if ours is not None else " (another session holds it)")
                               + "; there is nothing to force",
                               hint="release it there first if this session should have it")
        with self._mu:
            out = self._outgoing.get(_hk(hub))
        ttl_s = out.ttl_s if out is not None else hub_request_ttl(hub)
        if out is not None and out.asked_holder and status.held and \
                status.holder not in (out.asked_holder, principal):
            # Someone ahead of us got the board since we asked: its holder was never asked.
            self._reissue(out, principal, status.holder)
            raise ForceRefusedError(
                f"force-release of {self._named(hub)} is refused: {status.holder} holds it now and was "
                f"not asked; the request was sent to them (deadline {out.note.deadline_at})",
                time_left_s=REQUEST_WINDOW_S, request_id=out.note.id)
        notes = self._notes(hub, fresh=True)
        note = self._latest_of(notes, principal)
        answer = self._answer(hub, note.id, fresh=True) if note is not None else None
        queue = list(getattr(status, "queue", ()) or ())
        position = self._position(queue, principal)
        check = force_check(note, answer, position, self._wall())
        if check.available and not status.held:
            check = ForceCheck(False, f"nobody holds {self._named(hub)} now: there is nothing to force; "
                                      "your queued request is granted at its next poll", "refused")
        if not check.available:
            rid = getattr(note, "id", "") if note is not None else ""
            if check.kind == "early":
                raise ForceTooEarlyError(f"{self._named(hub)}: {check.reason}",
                                         time_left_s=check.time_left_s, request_id=rid,
                                         deadline_at=getattr(note, "deadline_at", ""),
                                         hint="wait for the answer or the deadline")
            raise ForceRefusedError(f"force-release of {self._named(hub)} is refused: {check.reason}",
                                    time_left_s=check.time_left_s, request_id=rid)
        can, why = self._can_revoke(hub)
        if not can:
            raise ForceRefusedError(f"force-release of {self._named(hub)} is refused: {why}",
                                    request_id=note.id,
                                    hint="an admin credential can force-release; or wait")
        kind, kind_why = holder_kind(answer, notes_ok=self._notes_supported(hub)[0])
        names = list(board_names or ())
        typed = typed_names(names[0] if names else "", self._board_id(hub), hub.target, *names[1:])
        refusal = confirm_board_error(kind, kind_why, confirm_board, typed, hub.target)
        if refusal is not None:
            raise refusal
        victim = status.holder
        reason = force_reason(principal, note.created_at)
        revoked = hub.client.lease_revoke(reason)
        self._forget(hub)
        log.warning("force-released %s from %s: %s", hub.target, victim, reason)
        lease, expires_at, pos = self._acquire_once(hub, holder, ttl_s)
        if lease is None:
            raise ActionFailedError(f"{self._named(hub)} was revoked from {victim}, but the hub did not "
                                    f"promote this client (queue position {pos or '?'})",
                                    hint="see the queue: harness-manager lease show")
        result = self._granted(board_id, hub, holder, lease, expires_at, ttl_s, heartbeat=heartbeat)
        result["forced"] = {"holder": victim, "reason": reason,
                            "revoked": list((revoked or {}).get("revoked", []))
                            if isinstance(revoked, dict) else []}
        return result

    # -- the victim ----------------------------------------------------------------------------------

    def _lost(self, board_id: str, hub: Any, stored: StoredLease, new_holder: str) -> None:
        """Our stored lease is gone and someone else holds the board."""
        self.store.drop(hub.host, hub.target)
        self.untrack(board_id)
        self._emit(board_id, hub, "lost", stored.principal or stored.holder)
        self._check_taken(board_id, hub, stored, new_holder)

    def _check_taken(self, board_id: str, hub: Any, stored: StoredLease, new_holder: str) -> None:
        """Was it a forced release? Then say who, why and when (``lease.taken``). Never raises."""
        try:
            taken = self._find_taken(hub, stored, new_holder)
        except Exception:  # noqa: BLE001 - the heartbeat thread must survive
            log.exception("reading the lease history for %s", hub.target)
            return
        if taken is None:
            return
        self.store.put_taken(hub.host, hub.target, taken)
        self._publish(TOPIC_TAKEN, board_id, dict(taken))

    def _find_taken(self, hub: Any, stored: StoredLease, new_holder: str) -> dict[str, str] | None:
        """``{by, reason, at}`` for a forced release of ``stored``, or None (it expired, or it
        went some other way). The pack reads its own hub's history (``taken_from_history``:
        fpgahub 0.3.0 keeps ``admin_revoked`` out of a target's history, so LR-A's client
        merges its revoke notes in); any other client's history is read here."""
        history: list[Any] = []
        fn = getattr(hub.client, "lease_history", None)
        if callable(fn):
            try:
                history = list(fn() or [])
            except HarnessError as exc:
                log.warning("lease history for %s: %s", hub.target, exc.message)
                return None
        holder = stored.principal or self._principal(hub) or stored.holder
        reader = _pack_attr(hub.client, "taken_from_history")
        if callable(reader):
            got = reader(history, holder)
            if not got:
                return None
            out = {k: str(got.get(k, "") or "") for k in ("by", "reason", "at")}
            out["at"] = iso_norm(out["at"]) or iso_utc(self._wall())
            return out
        mine = {stored.principal, stored.holder, holder} - {""}
        since = stored.acquired_at or 0.0
        best: dict[str, Any] | None = None
        for entry in history:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("event", "")).rsplit(".", 1)[-1] != "admin_revoked":
                continue
            prior = entry.get("prior_holder")
            if prior and prior not in mine:
                continue
            at = parse_utc(entry.get("ts") or entry.get("at"))
            if at is not None and since and at < since - HISTORY_SLACK_S:
                continue                           # an older revoke, before this lease
            best = entry                           # history is oldest first: keep the last
        if best is None:
            return None
        reason = str(best.get("reason", "") or "")
        m = _FORCER.search(reason)
        by = m.group(1) if m else str(best.get("by") or new_holder or "")
        return {"by": by, "reason": reason,
                "at": iso_norm(best.get("ts") or best.get("at")) or iso_utc(self._wall())}

    # -- heartbeat --------------------------------------------------------------------------------

    def track(self, board_id: str, hub: Any, *, announced: bool = False) -> None:
        """Heartbeat this board's stored lease (if any, now or later) while it stays tracked,
        and watch the requests for it while we hold it."""
        if hub is None:
            return
        self._board_for(hub, board_id)
        with self._mu:
            self._hubs[board_id] = hub
            prev = self._tracked.get(board_id)
            self._tracked[board_id] = _Tracked(hub, self._clock(), announced=announced,
                                               last_watch=prev.last_watch if prev else float("-inf"))
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        with self._mu:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="lease-heartbeat",
                                                daemon=True)
                self._thread.start()

    def untrack(self, board_id: str) -> None:
        with self._mu:
            self._tracked.pop(board_id, None)

    def tracked(self) -> list[str]:
        with self._mu:
            return list(self._tracked)

    def _run(self) -> None:
        # Nothing may end this loop but close(): a dead heartbeat lets the lease lapse, and
        # the hub then resets the board under whoever is using it (Q2).
        while not self._stop.wait(self._tick_s):
            for step in (self.beat_due, self.watch_due):
                try:
                    step()
                except Exception:  # noqa: BLE001 - both say they never raise; make it so
                    log.exception("lease service round %s", step.__name__)

    def beat_due(self, *, force: bool = False) -> None:
        """One heartbeat round: every tracked board whose stored lease is due. Never raises:
        a round that fails in any way is logged, said (``lease.state`` with ``warning``) and
        retried within a minute; the TTL still covers the lease meanwhile."""
        with self._mu:
            items = list(self._tracked.items())
        for board_id, tr in items:
            try:
                self._beat_one(board_id, tr, force)
            except Exception as exc:  # noqa: BLE001 - e.g. OSError: a full disk on store.put
                log.exception("lease heartbeat for %s failed (will retry)", board_id)
                self._beat_failed(board_id, tr, exc)

    def _beat_failed(self, board_id: str, tr: _Tracked, exc: BaseException) -> None:
        stored = None
        with contextlib.suppress(Exception):
            stored = self.store.get(tr.hub.host, tr.hub.target)
        every = self._heartbeat_s or heartbeat_interval(stored.ttl_s if stored else DEFAULT_TTL_S)
        tr.last_beat = self._clock() - every + min(every, 60.0)
        with contextlib.suppress(Exception):
            self._emit(board_id, tr.hub, "held",
                       (stored.principal or stored.holder) if stored else "",
                       stored.expires_at if stored else "",
                       warning=f"the lease heartbeat failed ({type(exc).__name__}: {exc}); "
                               "retrying within a minute")

    def _beat_one(self, board_id: str, tr: _Tracked, force: bool) -> None:
        stored = self.store.get(tr.hub.host, tr.hub.target)
        if stored is None:
            return                             # nothing of ours to keep alive (yet)
        every = self._heartbeat_s or heartbeat_interval(stored.ttl_s)
        now = self._clock()
        if not force and now - tr.last_beat < every:
            return
        tr.last_beat = now
        try:
            expires_at = tr.hub.client.lease_heartbeat(stored.token, stored.holder)
        except HeldError as exc:
            state = getattr(exc, "state", "lost")
            self.store.drop(tr.hub.host, tr.hub.target)
            self.untrack(board_id)
            log.warning("lease heartbeat for %s: %s", board_id, exc.message)
            self._emit(board_id, tr.hub, state if state in ("expired", "lost") else "lost",
                       stored.principal or stored.holder)
            if state != "expired":
                self._check_taken(board_id, tr.hub, stored, self._holder_now(tr.hub))
            return
        except HarnessError as exc:
            # The hub did not answer this time: the TTL still covers us; try next tick.
            log.warning("lease heartbeat for %s failed (will retry): %s", board_id, exc.message)
            tr.last_beat = now - every + min(every, 60.0)
            return
        if expires_at:
            self.store.put(StoredLease(**{**asdict(stored), "expires_at": expires_at}))
        # PANEL-TRUTH: the hub took our token: it is ours, here, as of now
        self._confirm_here(tr.hub, stored.principal or stored.holder,
                           expires_at or stored.expires_at)
        if not tr.announced or expires_at != stored.expires_at:
            tr.announced = True
            self._emit(board_id, tr.hub, "held", stored.principal or stored.holder,
                       expires_at or stored.expires_at, source="heartbeat")
        else:
            # LEASE-FRESH: the same expiry, no event; still a fresh answer from the hub
            self._know(tr.hub, "heartbeat", stored.principal or stored.holder,
                       expires_at or stored.expires_at)

    def _holder_now(self, hub: Any) -> str:
        try:
            shown = self._show(hub, fresh=True)
        except HarnessError:
            return ""
        return (getattr(shown, "holder", "") or "") if getattr(shown, "held", False) else ""

    def watch_due(self, *, force: bool = False) -> None:
        """One holder's-watch round: every tracked board whose lease we hold, every
        ``request_poll_s``: list the requests (one call), ``lease.wanted`` once per new one.
        Never raises. (A requester's ``request()`` polls its own request.)"""
        now = self._clock()
        with self._mu:
            tracked = list(self._tracked.items())
        for board_id, tr in tracked:
            if not force and now - tr.last_watch < self._poll_s:
                continue
            tr.last_watch = now
            try:
                self._watch_incoming(board_id, tr.hub)
            except Exception:  # noqa: BLE001 - the heartbeat thread must survive
                log.exception("watching lease requests for %s", board_id)

    def _watch_incoming(self, board_id: str, hub: Any) -> None:
        stored = self.store.get(hub.host, hub.target)
        if stored is None or not callable(getattr(hub.client, "list_requests", None)):
            return
        principal = self._principal(hub)
        if not principal:
            return
        notes = self._notes(hub, fresh=True)
        wall = self._wall()
        with self._mu:
            inc = self._incoming.setdefault(_hk(hub), _Incoming())
            new = []
            for note in notes:
                if getattr(note, "by", "") in ("", principal):
                    continue
                if note.id not in inc.announced:
                    inc.announced[note.id] = parse_utc(getattr(note, "created_at", "")) or wall
                    new.append(note)
            for nid in [k for k, t in inc.announced.items() if wall - t > NOTE_MAX_AGE_S]:
                inc.announced.pop(nid, None)
        for note in new:
            data = {k: getattr(note, k, "") for k in ("id", "by", "user", "host", "message")}
            data["deadline_at"] = iso_norm(getattr(note, "deadline_at", ""))
            self._publish(TOPIC_WANTED, board_id, data)

    def close(self) -> None:
        """Stop: waiting acquires and requests leave the queue (their threads do it: an
        abandoned queue entry can later grant the board to nobody)."""
        self._stop.set()
        with self._mu:
            waits = list(self._acquiring.values())
            self._tracked.clear()
        for ev in waits:
            ev.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)


def held_here(lease: Any) -> bool:
    """FIX-PACK-4: the ONE rule every lease gate uses (XVC, harness installs; the app's every
    gated button): THIS Harness Manager holds the lease, it has the token (``here``). ``mine``
    (by principal) is also true for another session of the same hub name (every lab session
    is david@mapstone-dev), which must not drive the board from here. A lease without
    ``here`` (a view from before REVIEW-W5, a test's stand-in) reads ``mine``, as the app's
    ``leaseWho`` does."""
    if not isinstance(lease, dict) or not lease:
        return False
    here = lease.get("here")
    return bool(lease.get("mine")) if here is None else bool(here)


def elsewhere_text(lease: dict[str, Any], target: str) -> str:
    """"<holder> holds <target> in another session, not this Harness Manager" when ``lease``
    is ``mine`` without ``here``, else "<holder> holds <target>"."""
    who = str(lease.get("holder") or "someone else")
    if lease.get("mine") and not held_here(lease):
        return f"{who} holds {target} in another session, not this Harness Manager"
    return f"{who} holds {target}"


def not_fresh(view: Any) -> str:
    """LEASE-FRESH: why ``view`` is not a fresh hub answer (it is the last known state, carried
    over a failed read: ``stale``), in plain words; "" when it is fresh. For the gates that
    must confirm the lease with the hub (an install, XVC, the claim)."""
    stale = (view or {}).get("stale") if isinstance(view, dict) else None
    if not stale:
        return ""
    at = parse_utc(stale.get("confirmed_at"))
    when = f"; it was last confirmed at {_hhmmss(at)}" if at is not None else ""
    return f"the hub did not answer ({stale.get('error') or 'no answer'}){when}"


def _hhmmss(t: float) -> str:
    """Epoch seconds as local ``HH:MM:SS`` (what a person reads in a note)."""
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _local_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in a container
        return str(os.getuid()) if hasattr(os, "getuid") else "user"
