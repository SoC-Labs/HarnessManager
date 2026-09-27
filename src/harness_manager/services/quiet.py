"""Background activity on a shared lab board, polite by construction (lane QUIET-POLL).

**Why.** A lab board's control port (the MPS3's 6900) takes ONE client at a time: while one
connection is open, every other client is turned away. On 2026-09-27 a 24 h soak on the lab
MPS3 died 17 minutes in: one of its control calls was reset because another client held the
port at that instant, and the prime suspect was a Harness Manager service polling the board
in the background, with nobody looking at it, while someone else was using the board.

**The rule.** Harness Manager contacts a board in the background (a poll, a presence beat, a
refresh nobody clicked) only when ALL of these hold; ``BackgroundGate.check`` says which one
does not:

1. ``off``: the policy allows it. ``general.background_poll`` (``on-view``, the default, or
   ``off``), overridden per board by ``boards.<name>.poll`` in ``boards.toml``. ``off``: only
   explicit actions ever touch the board.
2. ``no_viewer``: a UI is actually viewing the board: a page that shows it registers a viewer
   (``PUT /boards/{bid}/viewers/{vid}``, refreshed while it stays visible, dropped when it
   closes or looks elsewhere; a viewer that stops refreshing lapses after ``VIEWER_TTL_S``).
3. ``lease``: the board's hub lease is not held by someone else. "Someone else" is exactly
   what the lease service decides (``LeaseService.view``: ``lease.mine`` false); fpgahub 0.3.0
   records one principal per box, and this module invents no rule of its own.
4. ``busy``: the board did not just turn one of our connections away. A connect that was
   refused, reset or timed out means "someone else is using it": background contact backs
   off exponentially (``BACKOFF_FIRST_S`` 30 s, doubling to ``BACKOFF_MAX_S`` 10 min) and the
   UI says "busy (another client)", never a red error. Any answered call ends the back-off.

Explicit actions (a click, a CLI verb, a job the user started) are never gated: they keep
today's behaviour, and when the lease is someone else's the answer names the holder
(``state``). The gate only ever says no to background work.

**Single-client etiquette** is the board pack's (the MPS3 shell opens 6900, asks, and closes
at once: one call per connection, never an idle connection parked between polls). The pack
reports each connection's outcome here (``observe``), so a refusal met by ANY caller, a
presence beat, a background read or a user's own click, starts the back-off.

The demo (``harness-manager app --demo``) has scripted boards only: its gate is disabled
(``enabled=False``) and says yes to everything.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from harness_manager.core.errors import HarnessError, HeldError, UnreachableError

log = logging.getLogger(__name__)

#: ``general.background_poll`` and the ``boards.toml`` key that overrides it per board.
POLICY_KEY = "general.background_poll"
BOARD_KEY = "poll"
ON_VIEW = "on-view"
OFF = "off"
POLICIES = (ON_VIEW, OFF)

#: A viewer that is not refreshed within this long has gone (a closed tab, a sleeping laptop).
VIEWER_TTL_S = 45.0
VIEWER_TTL_MAX_S = 300.0
#: The back-off after a background connect was refused, reset or timed out.
BACKOFF_FIRST_S = 30.0
BACKOFF_MAX_S = 600.0

KIND_OFF = "off"
KIND_NO_VIEWER = "no_viewer"
KIND_LEASE = "lease"
KIND_BUSY = "busy"

#: The request header that marks a read nobody clicked (``X-HM-Background: 1``), and the
#: one a viewing page adds so its own background read also says it is looking.
BACKGROUND_HEADER = "x-hm-background"
VIEWER_HEADER = "x-hm-viewer"


@dataclass(frozen=True)
class Quiet:
    """Why a background contact is not made now (``BackgroundGate.check``)."""

    kind: str                       # off | no_viewer | lease | busy
    text: str                       # one line for the UI and the log
    holder: str = ""                # the lease holder (kind lease; or known beside another kind)
    retry_in_s: float | None = None  # busy: when background contact may resume
    policy: str = ON_VIEW
    detail: str = ""                # busy: what the last refusal said

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "holder": self.holder,
                "retry_in_s": None if self.retry_in_s is None else round(self.retry_in_s, 1),
                "policy": self.policy, "detail": self.detail}


def is_contention(exc: BaseException | None) -> bool:
    """A connect refused, reset or timed out, or a port another client holds: the board is
    being used by someone else (or is not answering), so background contact backs off."""
    return isinstance(exc, (HeldError, UnreachableError, ConnectionError, TimeoutError))


def lease_elsewhere(view: dict[str, Any] | None) -> str:
    """The holder when the lease service's view says the lease is someone else's, else ""."""
    lease = (view or {}).get("lease") or None
    if not lease or lease.get("mine"):
        return ""
    return str(lease.get("holder") or lease.get("user") or "someone else")


def policy_value(raw: Any) -> str:
    """``raw`` when it is a policy word, else "" (the next layer speaks)."""
    text = str(raw or "").strip().lower()
    return text if text in POLICIES else ""


_warned: set[str] = set()


def policy_for(board_id: str, links: Iterable[Any] = (), *, boards_path: Any = None) -> str:
    """The background policy for a board: ``boards.<name>.poll`` in ``boards.toml`` (its
    table, or ``[boards.defaults]``), else ``general.background_poll`` (the settings), else
    ``on-view``. Both are read where every other setting is (``settings.files.config_dir``).
    A bad value is logged once and skipped; a file that cannot be read never stops a board
    (the next layer speaks)."""
    from harness_manager.power.config import load_boards

    try:
        cfg = load_boards(boards_path).for_board(board_id, links)
    except (HarnessError, OSError) as exc:
        _warn_once(f"boards.toml: {getattr(exc, 'message', exc)}; {POLICY_KEY} decides")
        cfg = None
    if cfg is not None and BOARD_KEY in cfg.tables:
        raw = cfg.tables.get(BOARD_KEY)
        got = policy_value(raw)
        if got:
            return got
        if str(raw or "") != "":
            _warn_once(f"boards.{cfg.key}.{BOARD_KEY} = {raw!r} is not one of "
                       f"{', '.join(POLICIES)}; {POLICY_KEY} decides")
    try:
        from harness_manager.settings import runtime

        return policy_value(runtime.value(POLICY_KEY)) or ON_VIEW
    except (HarnessError, OSError, ValueError) as exc:
        _warn_once(f"{POLICY_KEY} could not be read ({exc}); {ON_VIEW} is used")
        return ON_VIEW


def _warn_once(message: str) -> None:
    if message in _warned:
        return
    _warned.add(message)
    log.warning("background polls: %s", message)


class _Board:
    __slots__ = ("viewers", "until", "delay", "detail", "refusals")

    def __init__(self) -> None:
        self.viewers: dict[str, float] = {}      # viewer id -> monotonic expiry
        self.until = 0.0                          # back-off: no background contact before
        self.delay = 0.0                          # the back-off's current step (0: none)
        self.detail = ""
        self.refusals = 0


class BackgroundGate:
    """Viewers, the back-off and the verdict for background contact, per board.

    ``policy_of(board_id) -> "on-view" | "off"`` (the settings and boards.toml: ``policy_for``)
    and ``lease_of(board_id) -> holder`` (the lease holder when the lease service says it is
    someone else's, else "": ``lease_elsewhere``) are the daemon's. None: always ``on-view``;
    no lease. ``enabled=False`` (the demo): ``check`` always says go ahead.
    """

    def __init__(self, *, policy_of: Callable[[str], str] | None = None,
                 lease_of: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 viewer_ttl_s: float = VIEWER_TTL_S,
                 backoff: tuple[float, float] = (BACKOFF_FIRST_S, BACKOFF_MAX_S),
                 enabled: bool = True) -> None:
        self.policy_of = policy_of
        self.lease_of = lease_of
        self.clock = clock
        self.viewer_ttl_s = viewer_ttl_s
        self.backoff = backoff
        self.enabled = enabled
        self._mu = threading.Lock()
        self._boards: dict[str, _Board] = {}

    def _get(self, board_id: str) -> _Board:
        rec = self._boards.get(board_id)
        if rec is None:
            rec = self._boards[board_id] = _Board()
        return rec

    # -- viewers ----------------------------------------------------------------------------

    def view(self, board_id: str, viewer: str, ttl_s: float | None = None) -> None:
        """A UI shows this board now (again): it counts as viewed for ``ttl_s``."""
        ttl = self.viewer_ttl_s if ttl_s is None else max(1.0, min(float(ttl_s),
                                                                   VIEWER_TTL_MAX_S))
        with self._mu:
            self._get(board_id).viewers[str(viewer)] = self.clock() + ttl

    def unview(self, board_id: str, viewer: str | None = None) -> bool:
        """That viewer looks elsewhere or closed (None: every viewer). True if it was there."""
        with self._mu:
            rec = self._boards.get(board_id)
            if rec is None:
                return False
            if viewer is None:
                had = bool(rec.viewers)
                rec.viewers.clear()
                return had
            return rec.viewers.pop(str(viewer), None) is not None

    def viewers(self, board_id: str) -> int:
        now = self.clock()
        with self._mu:
            rec = self._boards.get(board_id)
            if rec is None:
                return 0
            for vid in [v for v, until in rec.viewers.items() if until <= now]:
                del rec.viewers[vid]
            return len(rec.viewers)

    def forget(self, board_id: str) -> None:
        """The board closed: its viewers and its back-off go with it."""
        with self._mu:
            self._boards.pop(board_id, None)

    # -- contention -------------------------------------------------------------------------

    def observe(self, board_id: str, exc: BaseException | None) -> None:
        """A connection's outcome, from the board pack (any caller): None answered, a
        contention error backs off, anything else says nothing about other clients."""
        if exc is None:
            self.note_ok(board_id)
        elif is_contention(exc):
            self.note_busy(board_id, getattr(exc, "message", "") or str(exc)
                           or type(exc).__name__)

    def note_busy(self, board_id: str, why: str = "") -> float:
        """The board turned a connection away. Returns the back-off now in force (s).

        Refusals met while a back-off runs (a user's own click, the rest of one read) do not
        lengthen it; the first one after it has run out doubles the step, up to the cap."""
        first, cap = self.backoff
        now = self.clock()
        with self._mu:
            rec = self._get(board_id)
            rec.refusals += 1
            rec.detail = why or rec.detail
            if now < rec.until:
                return rec.until - now
            rec.delay = first if rec.delay <= 0 else min(rec.delay * 2, cap)
            rec.until = now + rec.delay
            delay = rec.delay
        log.info("background contact with %s backs off %.0f s: %s", board_id, delay,
                 why or "the board turned a connection away")
        return delay

    def note_ok(self, board_id: str) -> None:
        """A call was answered: the port was free, so the back-off ends."""
        with self._mu:
            rec = self._boards.get(board_id)
            if rec is not None:
                rec.until, rec.delay, rec.detail = 0.0, 0.0, ""

    def backing_off(self, board_id: str) -> tuple[float, float, str] | None:
        """``(remaining_s, step_s, why)`` while a back-off runs, else None."""
        now = self.clock()
        with self._mu:
            rec = self._boards.get(board_id)
            if rec is None or now >= rec.until:
                return None
            return rec.until - now, rec.delay, rec.detail

    # -- the verdict --------------------------------------------------------------------------

    def _policy(self, board_id: str) -> str:
        if self.policy_of is None:
            return ON_VIEW
        try:
            return policy_value(self.policy_of(board_id)) or ON_VIEW
        except Exception:  # noqa: BLE001 - a policy that cannot be read is the default
            log.exception("reading the background policy of %s failed", board_id)
            return ON_VIEW

    def _holder(self, board_id: str) -> str:
        if self.lease_of is None:
            return ""
        try:
            return str(self.lease_of(board_id) or "")
        except HarnessError as exc:        # the hub did not answer: no lease known
            log.debug("lease of %s: %s", board_id, exc.message)
            return ""
        except Exception:  # noqa: BLE001 - never a crash in the gate
            log.exception("reading the lease of %s failed", board_id)
            return ""

    def check(self, board_id: str) -> Quiet | None:
        """None: a background contact may go ahead now. Otherwise why not.

        Cheapest first: the policy, the viewers (no hub call for a board nobody views), then
        the lease (the lease service's view, cached), then the back-off."""
        if not self.enabled:
            return None
        policy = self._policy(board_id)
        if policy == OFF:
            return Quiet(KIND_OFF, "background reads are off (general.background_poll or "
                                   "boards.<name>.poll): Harness Manager contacts this board "
                                   "only when you ask", policy=policy)
        if not self.viewers(board_id):
            return Quiet(KIND_NO_VIEWER, "nobody is viewing this board: no background reads",
                         policy=policy)
        holder = self._holder(board_id)
        if holder:
            return Quiet(KIND_LEASE, f"the hub lease is held by {holder}: Harness Manager "
                                     "contacts this board only when you ask", holder=holder,
                         policy=policy)
        return self._busy(board_id, policy)

    def _busy(self, board_id: str, policy: str, holder: str = "") -> Quiet | None:
        off = self.backing_off(board_id)
        if off is None:
            return None
        left, _step, why = off
        return Quiet(KIND_BUSY, f"busy (another client): background reads resume in "
                                f"{max(1, round(left))} s", holder=holder, retry_in_s=left,
                     policy=policy, detail=why)

    def state(self, board_id: str) -> dict[str, Any]:
        """What the API says about background contact with this board (never touches it):
        ``{allowed, kind, text, holder, retry_in_s, policy, viewers, refusals}``. Unlike
        ``check`` it always asks who holds the lease, so an explicit read can name them."""
        if not self.enabled:
            return {"allowed": True, "kind": "", "text": "", "holder": "", "retry_in_s": None,
                    "policy": "demo", "viewers": self.viewers(board_id), "refusals": 0,
                    "detail": ""}
        policy = self._policy(board_id)
        viewers = self.viewers(board_id)
        holder = self._holder(board_id)
        q: Quiet | None
        if policy == OFF:
            q = Quiet(KIND_OFF, "background reads are off (general.background_poll or "
                                "boards.<name>.poll): Harness Manager contacts this board only "
                                "when you ask", holder=holder, policy=policy)
        elif holder:
            q = Quiet(KIND_LEASE, f"the hub lease is held by {holder}: Harness Manager "
                                  "contacts this board only when you ask", holder=holder,
                      policy=policy)
        elif not viewers:
            q = Quiet(KIND_NO_VIEWER, "nobody is viewing this board: no background reads",
                      policy=policy)
        else:
            q = self._busy(board_id, policy)
        with self._mu:
            rec = self._boards.get(board_id)
            refusals = rec.refusals if rec is not None else 0
        out = q.public() if q is not None else {
            "kind": "", "text": "", "holder": holder, "retry_in_s": None, "policy": policy,
            "detail": ""}
        return {"allowed": q is None, **out, "viewers": viewers, "refusals": refusals}


def request_is_background(headers: Any) -> bool:
    """``X-HM-Background: 1`` (or true/yes): a read nobody clicked."""
    value = str(headers.get(BACKGROUND_HEADER, "") or "").strip().lower()
    return value in ("1", "true", "yes", "on")
