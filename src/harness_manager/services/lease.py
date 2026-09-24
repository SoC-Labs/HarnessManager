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
               the frozen reason, then takes the lease the hub promoted us to.
leave          cancel the queue entry, delete our note, ``lease.left``.
victim         a heartbeat that says ``lost``, or a status naming another holder while we
               hold a stored lease: read ``lease_history``, find ``admin_revoked``, emit
               ``lease.taken {by, reason, at}`` and keep it for ``view()`` until dismissed.
=============  ==========================================================================

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

Events: ``lease.state {target, state: held|queued|released|expired|lost, holder,
expires_at}``, and ``lease.wanted``, ``lease.answered``, ``lease.force_available``,
``lease.taken``, ``lease.left`` (docs/LEASE_REQUESTS.md).
"""

from __future__ import annotations

import contextlib
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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

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
#: The holder has this long to answer a request before the requester may force it.
REQUEST_WINDOW_S = 120
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

    def public(self, *, mine: bool = True) -> dict[str, Any]:
        """The API's ``lease`` object. The token never leaves this process."""
        return {"target": self.target, "holder": self.principal or self.holder,
                "expires_at": self.expires_at, "mine": mine}


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
            return StoredLease(**data)
        except (OSError, ValueError, TypeError):
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
    cancel: threading.Event | None = None
    left: bool = False


@dataclass
class _Incoming:
    """Requests for a lease we hold, as the holder's poll last saw them."""

    announced: dict[str, float] = field(default_factory=dict)   # id -> its created_at (wall)


def _hk(hub: Any) -> tuple[str, str]:
    return (hub.host, hub.target)


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
        self._principals: dict[tuple[str, str], str] = {}
        self._principal_failed: dict[tuple[str, str], float] = {}
        self._outgoing: dict[tuple[str, str], _Outgoing] = {}
        self._incoming: dict[tuple[str, str], _Incoming] = {}
        self._boards: dict[tuple[str, str], str] = {}     # hub key -> the board id last seen
        self._chassis: dict[tuple[str, str], str] = {}    # hub key -> hub.board_id() (D4)
        self._chassis_failed: dict[tuple[str, str], float] = {}
        self._viewed: dict[tuple[str, str], float] = {}   # hub key -> wall time of the last view

    # -- hub reads (cached) -----------------------------------------------------------------------

    @staticmethod
    def require_hub(hub: Any, board_id: str = "") -> Any:
        if hub is None:
            raise UnavailableError(CAPABILITY, f"{board_id or 'this board'} is not behind a hub; "
                                               "add a hub table to boards.toml (docs/HIL_B0.md step 0.2)")
        return hub

    def _cached(self, hub: Any, what: str, fn: Callable[[], Any], *, fresh: bool = False) -> Any:
        key = (hub.host, hub.target, what)
        now = self._clock()
        with self._mu:
            hit = self._cache.get(key)
        if not fresh and hit is not None and now - hit[0] < VIEW_TTL_S:
            return hit[1]
        value = fn()
        with self._mu:
            self._cache[key] = (now, value)
        return value

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
        """D4: the physical board that owns the target (``hub.board_id()``: ``mps3_01``)."""
        key = _hk(hub)
        with self._mu:
            known = self._chassis.get(key)
            failed = self._chassis_failed.get(key)
        if known:
            return known
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

    def _forget(self, hub: Any) -> None:
        with self._mu:
            for k in [k for k in self._cache if k[:2] == _hk(hub)]:
                del self._cache[k]

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
              *, warning: str = "") -> None:
        self._forget(hub)
        data = {"target": hub.target, "state": state, "holder": holder, "expires_at": expires_at}
        if warning:
            data["warning"] = warning             # additive: the state stands, something failed
        self._publish(TOPIC, board_id, data)

    def _board_for(self, hub: Any, board_id: str = "") -> str:
        key = _hk(hub)
        with self._mu:
            if board_id:
                self._boards[key] = board_id
                return board_id
            return self._boards.get(key, "")

    # -- the view -------------------------------------------------------------------------------

    def view(self, hub: Any) -> dict[str, Any]:
        """``GET /boards/{bid}/lease`` (docs/LEASE_REQUESTS.md, API)::

            lease:    {target, holder, user, expires_at, mine} | null
            hub:      HOST | null
            board:    the physical board, hub.board_id() (D4) | null
            queue:    [{position, holder, user, mine}]
            request:  {id, message, created_at, deadline_at, position,
                       answer: {answer, minutes, message, at} | null,
                       force_available, force_reason} | null     # my outgoing request
            incoming: [{id, by, user, host, message, created_at, deadline_at,
                        answer: {answer, minutes, message, at} | null}]      # D5
            taken:    {by, reason, at} | null
        """
        empty: dict[str, Any] = {"lease": None, "hub": None, "board": None, "queue": [],
                                 "request": None, "incoming": [], "taken": None}
        if hub is None:
            return empty
        out = {**empty, "hub": hub.host, "board": self._board_id(hub)}
        self._drop_at_edges(hub)
        shown = self._show(hub)
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
            out["lease"] = {"target": hub.target, "holder": holder,
                            "expires_at": getattr(shown, "expires_at", "")
                            or (stored.expires_at if here and stored else ""),
                            "mine": mine, "user": getattr(shown, "user", "") or ""}
        queue = list(getattr(shown, "queue", ()) or ())
        out["queue"] = [{"position": int(getattr(e, "position", 0) or 0),
                         "holder": getattr(e, "holder", ""), "user": getattr(e, "user", ""),
                         "mine": bool(principal) and getattr(e, "holder", "") in ids}
                        for e in queue]
        notes = self._safe_notes(hub)
        if principal:
            ours = self._latest_of(notes, principal)
            if ours is not None:
                out["request"] = self._request_public(hub, ours, queue, principal)
            if here:
                out["incoming"] = self._incoming_list(hub, notes, principal, queue,
                                                      has_queue=hasattr(shown, "queue"))
        taken = self.store.get_taken(hub.host, hub.target)
        out["taken"] = {**taken, "at": iso_norm(taken.get("at"))} if taken else None
        return out

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

    def _request_public(self, hub: Any, note: Any, queue: list[Any], principal: str) -> dict[str, Any]:
        try:
            answer = self._answer(hub, note.id)
        except HarnessError as exc:
            log.warning("reading the answer to %s on %s: %s", note.id, hub.host, exc.message)
            answer = None
        position = self._position(queue, principal)
        check = force_check(note, answer, position, self._wall())
        return {"id": note.id, "message": getattr(note, "message", ""),
                "created_at": iso_norm(note.created_at), "deadline_at": iso_norm(note.deadline_at),
                "position": position, "answer": _answer_public(answer),
                "force_available": check.available, "force_reason": check.reason}

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

    def acquire(self, hub: Any, *, board_id: str = "", ttl_s: int = DEFAULT_TTL_S,
                holder: str | None = None, progress: Progress | None = None,
                cancel: threading.Event | None = None, poll_s: float | None = None,
                timeout_s: float = ACQUIRE_TIMEOUT_S, heartbeat: bool = True) -> dict[str, Any]:
        """Block until the lease is held (it may queue); store it; heartbeat it while tracked."""
        hub = self.require_hub(hub, board_id)
        self._board_for(hub, board_id)
        if not isinstance(ttl_s, int) or ttl_s <= 0:
            raise UsageError(f"ttl_s must be a positive whole number of seconds, not {ttl_s!r}")
        holder = holder or default_holder()
        stored = self.store.get(hub.host, hub.target)
        if stored is not None:
            shown = self._show(hub, fresh=True)
            ids = {stored.principal, holder} - {""}
            if shown.held and shown.holder in ids and stored.holder == holder:
                # Already ours: say so rather than queue behind ourselves.
                if heartbeat:
                    self.track(board_id, hub)
                return {"lease": stored.public(), "already": True}
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
                f"the lease request for {hub.target} was cancelled"
                + ("; its queue entry was removed" if removed else ""),
                hint="acquire again when you want the board") from None
        finally:
            with self._mu:
                self._acquiring.pop(key, None)
        record = self._store_grant(hub, holder, lease, expires_at, ttl_s)
        report("held", 1, 1)
        self._emit(board_id, hub, "held", record.principal or holder, expires_at)
        if heartbeat:
            self.track(board_id, hub, announced=True)
        return {"lease": record.public()}

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
            raise AbsentError(f"this Harness Manager holds no lease on {hub.target} ({who})",
                              hint="a lease taken outside Harness Manager is released where it was "
                                   "taken (fpgahub lease release --token …)")
        return self._release_stored(board_id, hub, stored)

    def _release_stored(self, board_id: str, hub: Any, stored: StoredLease) -> dict[str, Any]:
        hub.client.lease_release(stored.token, stored.holder)
        self.store.drop(hub.host, hub.target)
        self.untrack(board_id)
        self._emit(board_id, hub, "released", stored.principal or stored.holder)
        return {"ok": True, "released": stored.public(mine=True)}

    # -- requests: the requester --------------------------------------------------------------------

    def request(self, board_id: str, hub: Any, *, message: str = "", ttl_s: int = DEFAULT_REQUEST_TTL_S,
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
            raise AlreadyError(f"you already hold {hub.target}"
                               + ("" if ours is not None else " (another session)"),
                               hint="use it there, or release it there first")
        asked = status.holder if status.held else ""
        key = _hk(hub)
        cancel = cancel or threading.Event()
        gate = board_id or f"{hub.host}/{hub.target}"
        with self._mu:
            prior = self._outgoing.get(key)
            if prior is not None:
                raise AlreadyError(f"a request for {hub.target} is already waiting",
                                   hint="watch it, or leave the queue to withdraw it")
            if gate in self._acquiring:
                raise AlreadyError(f"an acquire for {hub.target} is already waiting",
                                   hint="cancel it first (DELETE the lease)")
            self._acquiring[gate] = cancel
        report = progress or (lambda *_: None)
        holder = default_holder()
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
        if check.available and out.force_announced != ("sig", out.answer_sig):
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
        self._emit(board_id, hub, "held", record.principal or holder, expires_at)
        if heartbeat:
            self.track(board_id, hub, announced=True)
        out: dict[str, Any] = {"lease": record.public()}
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
        principal = self._principal(hub, required=True)
        stored = self.store.get(hub.host, hub.target)
        shown = self._show(hub, fresh=True)
        ids = self._my_ids(hub, stored, principal)
        if stored is None or not shown.held or shown.holder not in ids:
            who = f"{shown.holder} holds it" if shown.held else "nobody holds it"
            raise RefusedError(f"only the holder answers requests for {hub.target}, and this "
                               f"Harness Manager does not hold it ({who})")
        notes = self._notes(hub, fresh=True)
        note = next((n for n in notes if n.id == request_id), None)
        if note is None:
            raise AbsentError(f"no request {request_id} for {hub.target} (withdrawn, or it expired)",
                              hint="list the requests again")
        now = self._wall()
        reply = _pack_attr(hub.client, "AnswerNote", AnswerNote)(
            id=request_id, answer=answer, minutes=minutes, message=message, at=iso_utc(now))
        if answer == "release":
            released = self._release_stored(board_id, hub, stored)   # the hub promotes the head
            try:
                hub.client.put_answer(reply)
            except HarnessError as exc:
                log.warning("released %s, but the answer note was not written: %s", hub.target,
                            exc.message)
            return {"ok": True, "answer": _answer_public(reply), "released": released["released"]}
        hub.client.put_answer(reply)
        self._forget(hub)
        return {"ok": True, "answer": _answer_public(reply)}

    # -- requests: force -----------------------------------------------------------------------------

    def force(self, board_id: str, hub: Any, *, confirm: bool, heartbeat: bool = True) -> dict[str, Any]:
        """Force-release the board to us: every rule re-checked against the hub NOW, then
        revoke (the hub promotes the head of the queue, which is us) and take the lease."""
        hub = self.require_hub(hub, board_id)
        self._board_for(hub, board_id)
        if confirm is not True:
            raise UsageError("force-release needs confirm: true",
                             hint=f"are you sure? This kicks the holder off {hub.target} now; "
                                  "anything they are running on the board is interrupted")
        self._need(hub, "lease_status", "list_requests", "get_answer", "lease_revoke",
                   "delete_request")
        principal = self._principal(hub, required=True)
        status = self._show(hub, fresh=True)
        holder = default_holder()
        if status.held and status.holder == principal:
            ours = self.store.get(hub.host, hub.target)
            raise AlreadyError(f"{hub.target} is already yours"
                               + ("" if ours is not None else " (another session holds it)")
                               + "; there is nothing to force",
                               hint="release it there first if this session should have it")
        with self._mu:
            out = self._outgoing.get(_hk(hub))
        ttl_s = out.ttl_s if out is not None else DEFAULT_REQUEST_TTL_S
        if out is not None and out.asked_holder and status.held and \
                status.holder not in (out.asked_holder, principal):
            # Someone ahead of us got the board since we asked: its holder was never asked.
            self._reissue(out, principal, status.holder)
            raise ForceRefusedError(
                f"force-release of {hub.target} is refused: {status.holder} holds it now and was "
                f"not asked; the request was sent to them (deadline {out.note.deadline_at})",
                time_left_s=REQUEST_WINDOW_S, request_id=out.note.id)
        notes = self._notes(hub, fresh=True)
        note = self._latest_of(notes, principal)
        answer = self._answer(hub, note.id, fresh=True) if note is not None else None
        queue = list(getattr(status, "queue", ()) or ())
        position = self._position(queue, principal)
        check = force_check(note, answer, position, self._wall())
        if check.available and not status.held:
            check = ForceCheck(False, f"nobody holds {hub.target} now: there is nothing to force; "
                                      "your queued request is granted at its next poll", "refused")
        if not check.available:
            rid = getattr(note, "id", "") if note is not None else ""
            if check.kind == "early":
                raise ForceTooEarlyError(f"{hub.target}: {check.reason}",
                                         time_left_s=check.time_left_s, request_id=rid,
                                         deadline_at=getattr(note, "deadline_at", ""),
                                         hint="wait for the answer or the deadline")
            raise ForceRefusedError(f"force-release of {hub.target} is refused: {check.reason}",
                                    time_left_s=check.time_left_s, request_id=rid)
        victim = status.holder
        reason = force_reason(principal, note.created_at)
        revoked = hub.client.lease_revoke(reason)
        self._forget(hub)
        log.warning("force-released %s from %s: %s", hub.target, victim, reason)
        lease, expires_at, pos = self._acquire_once(hub, holder, ttl_s)
        if lease is None:
            raise ActionFailedError(f"{hub.target} was revoked from {victim}, but the hub did not "
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
        if not tr.announced or expires_at != stored.expires_at:
            tr.announced = True
            self._emit(board_id, tr.hub, "held", stored.principal or stored.holder,
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


def _local_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in a container
        return str(os.getuid()) if hasattr(os, "getuid") else "user"
