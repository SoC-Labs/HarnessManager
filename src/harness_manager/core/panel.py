"""The front panel: presence (``hello``), what the panel shows, and Identify (lane P1).

docs/design/CLCD_ALIGNMENT.md §2, §5 is the design; david's decisions of 2026-09-24:

- **Presence** is a ``hello`` on the board's existing control channel, every 30 s, on
  connections Harness Manager already makes, with a 90 s TTL on the board and at most 4
  sessions there. The reply carries the panel's state (page, KVM owner, banner, card,
  ``seq``) and a ring of tap events.
- **A tap on a lease-request banner notifies the holder.** It never releases.
- **Only the Linux harness changes its panel.** Bare metal stays byte-identical, and Harness
  Manager falls back by feature bit: the owner from ``display``, a mirror rebuilt from what
  it read, and Identify greyed out with the reason.

This module is board-agnostic: the model, the ``hello`` message with its field caps, and
the session adapter ``PanelAdapter``: ``BoardSession.panel`` (CCR PANEL-5), None when the
board has no front panel Harness Manager can reach. A board pack provides the adapter
(MPS3: ``harness_manager_mps3.panel``), a daemon session proxies it
(``harness_manager.client.remote``), and ``harness_manager.services.presence`` drives it.

The field caps. The MPS3 harness reads a request line of at most ``LINE_MAX`` (256) bytes
(``MPS3_NET_LINE_MAX``), and the panel draws printable ASCII only. So every text field is
printable ASCII, clipped per field, and a lease principal is sent as its user part only.
The worst case is 251 bytes (``tests/unit/test_p1_presence.py`` pins it, as the spike's
``tools/clcd_mock.py`` did). Times are RELATIVE seconds: the board has no wall clock it
can trust, so it ages them on its own monotonic clock from the moment the hello arrived.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: The harness's request line limit (platform ``firmware/common/net_if.h``, MPS3_NET_LINE_MAX).
LINE_MAX = 256
WHO_MAX = 20        # user@host (the panel shows 16)
USER_MAX = 12       # a lease holder or requester: the user part only
NAME_MAX = 16       # the board's N1 name
APP_MAX = 12        # "hm/<version>"
JOB_MAX = 8         # a job kind
SID_LEN = 8         # hex characters, random per (daemon, board open)
#: Numeric caps: the lease's time left, the queue length, and an open request's time to answer.
LIMITS = {"left": 86_400, "q": 99, "rl": 600}
TTL_S = 90          # the board lists a session this long after its last hello
TTL_RANGE = (30, 300)
MAX_SESSIONS = 4    # the board keeps at most this many; the oldest is dropped
BEAT_S = 30.0       # a hello this often
#: ... and this often while a lease request or an Identify is open. docs/design §5.3 caps
#: the host at one hello per 10 s per session.
FAST_BEAT_S = 10.0

ROLES = ("holder", "owner", "watch")
#: The panel's text grid.
ROWS, COLS = 15, 40
#: Per-cell role codes of a frame (``PanelFrame.roles``, ROWS*COLS characters). A rebuilt
#: frame uses only ``t`` and ``i``; the Linux harness's renderer (R2/R4) defines the rest.
ROLE_TEXT, ROLE_INVERTED = "t", "i"

SOURCE_PANEL = "panel"       # read from the panel (the Linux harness's `panel`/`hello`)
SOURCE_REBUILT = "rebuilt"   # rebuilt by Harness Manager from what it read (bare metal)
REBUILT_NOTE = "rebuilt from what Harness Manager read, not read from the panel"


#: LOCATE: ``locate``'s ``who`` (the banner "IDENTIFY: <who>" on a 40-column row: 30 left).
LOCATE_WHO_MAX = COLS - len("IDENTIFY: ")
LOCATE_VIA = " via Harness Manager"
LOCATE_VIA_SHORT = " via HM"


def locate_who(who: str) -> str:
    """``who`` for the board's IDENTIFY banner (lane LOCATE, the Linux lead's R3):
    ``"<user>@<host> via Harness Manager"``, else ``"... via HM"`` when that does not fit in
    ``LOCATE_WHO_MAX``, else the ``user@host`` clipped. Printable ASCII only."""
    base = ascii_field(who, LOCATE_WHO_MAX)
    for tail in (LOCATE_VIA, LOCATE_VIA_SHORT):
        if len(base) + len(tail) <= LOCATE_WHO_MAX:
            return base + tail
    return base


def ascii_field(value: Any, limit: int) -> str:
    """Printable ASCII only (the panel font's range, one byte per character, so the line
    budget holds), clipped to ``limit``. Anything else becomes ``?``."""
    return "".join(ch if " " <= ch <= "~" else "?" for ch in str(value))[:limit]


def user_part(principal: Any) -> str:
    """``david@mapstone-dev`` -> ``david``: the panel names people, not hosts."""
    return str(principal or "").split("@", 1)[0]


# --- hello (host -> board) ---------------------------------------------------------------


@dataclass(frozen=True)
class HelloLease:
    """The hub lease as this host last read it. The board cannot see the hub.

    ``left`` and ``rl`` are seconds from now (None: unknown). ``by``/``req`` are principals;
    only their user part is sent. A lease with no ``by`` says "behind a hub, nobody holds it".
    """

    by: str = ""
    left: float | None = None
    q: int = 0
    req: str = ""
    rl: float | None = None


@dataclass(frozen=True)
class HelloJob:
    kind: str
    percent: int = 0


@dataclass(frozen=True)
class Hello:
    sid: str
    who: str
    app: str
    name: str = ""
    role: str = "watch"                   # holder | owner | watch
    lease: HelloLease | None = None
    job: HelloJob | None = None
    ttl: int = TTL_S


def hello_message(h: Hello) -> dict[str, Any]:
    """The ``hello`` request as a JSON object, every field capped (the order is the wire's)."""
    msg: dict[str, Any] = {"op": "hello", "v": 1, "sid": ascii_field(h.sid, SID_LEN),
                           "who": ascii_field(h.who, WHO_MAX), "app": ascii_field(h.app, APP_MAX)}
    if h.name:
        msg["name"] = ascii_field(h.name, NAME_MAX)
    msg["role"] = h.role if h.role in ROLES else "watch"
    if h.lease is not None:
        lease: dict[str, Any] = {}
        if h.lease.by:
            lease["by"] = ascii_field(user_part(h.lease.by), USER_MAX)
        if h.lease.left is not None:
            lease["left"] = _clamp(h.lease.left, LIMITS["left"])
        lease["q"] = _clamp(h.lease.q, LIMITS["q"])
        if h.lease.req:
            lease["req"] = ascii_field(user_part(h.lease.req), USER_MAX)
            if h.lease.rl is not None:
                lease["rl"] = _clamp(h.lease.rl, LIMITS["rl"])
        msg["lease"] = lease
    if h.job is not None:
        msg["job"] = {"k": ascii_field(h.job.kind, JOB_MAX), "p": _clamp(h.job.percent, 100)}
    msg["ttl"] = max(TTL_RANGE[0], min(TTL_RANGE[1], int(h.ttl)))
    return msg


def _clamp(value: Any, top: int) -> int:
    try:
        return max(0, min(top, int(value)))
    except (TypeError, ValueError):
        return 0


def encode_hello(h: Hello) -> bytes:
    """The request line as sent (compact JSON + newline); ``ValueError`` over ``LINE_MAX``."""
    line = (json.dumps(hello_message(h), separators=(",", ":")) + "\n").encode("ascii")
    if len(line) > LINE_MAX:
        raise ValueError(f"hello is {len(line)} B; the harness reads at most {LINE_MAX}")
    return line


# --- what the panel shows (board -> host) -----------------------------------------------


@dataclass(frozen=True)
class PanelSession:
    """A Harness Manager session the board lists (its ``panel.sessions``)."""

    sid: str
    who: str
    role: str = "watch"
    age_s: float = 0.0          # since its last hello, on the board's clock
    mine: bool = False          # this Harness Manager's own session


@dataclass(frozen=True)
class PanelEvent:
    """One entry of the board's tap ring. ``seq`` rises; nothing is acknowledged or deleted,
    so every Harness Manager that watches the board sees every tap."""

    seq: int
    kind: str = "tap"
    on: str = ""                # identify | request | nav | ...
    ms_ago: int = 0
    at: float = 0.0             # epoch seconds on THIS host (read time minus ms_ago)


@dataclass(frozen=True)
class TouchHealth:
    """The touch controller, when the harness says. ``None`` means it did not say."""

    present: bool | None = None
    cal: bool | None = None
    ok: bool | None = None                 # stats.touch_ok
    bus_lost: int | None = None            # stats.touch_bus_lost
    recoveries: int | None = None          # stats.touch_recoveries
    reason: str = ""                       # "touch unavailable: ..." when ok is False


@dataclass(frozen=True)
class PanelState:
    page: str = ""                          # status | apps | "" (not known)
    owner: str = ""                         # harness | dut | "" (not known)
    pending: bool = False                   # an owner flip has not landed yet
    banner: str = ""
    card: str = ""
    touch: TouchHealth = field(default_factory=TouchHealth)
    sessions: tuple[PanelSession, ...] = ()
    count: int = 0                          # live sessions the board counts
    seq: int = 0
    events: tuple[PanelEvent, ...] = ()
    source: str = SOURCE_PANEL              # panel | rebuilt
    observed_at: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class PanelFrame:
    rows: tuple[str, ...]                   # ROWS strings of COLS characters
    roles: str = ""                         # ROWS*COLS role codes, or "" when not known
    source: str = SOURCE_PANEL
    observed_at: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class PanelSupport:
    """What this board's panel can do now: "" when it can, else the reason it cannot."""

    front_panel: str = ""
    presence: str = ""
    locate: str = ""
    source: str = SOURCE_PANEL              # where state() comes from on this board


_ROLE_RANK = {r: i for i, r in enumerate(ROLES)}


def order_sessions(sessions: Iterable[PanelSession]) -> tuple[PanelSession, ...]:
    """The board's order, enforced here too: holder > owner > watch, most recent first."""
    return tuple(sorted(sessions, key=lambda s: (_ROLE_RANK.get(s.role, len(ROLES)), s.age_s)))


def touch_health(stats: dict[str, Any] | None, panel_touch: Any = None) -> TouchHealth:
    """Touch facts from a ``panel`` reply's ``touch`` and the additive ``stats`` keys
    ``touch_ok``, ``touch_bus_lost``, ``touch_recoveries``. An absent key is unknown."""
    pt = panel_touch if isinstance(panel_touch, dict) else {}
    st = stats if isinstance(stats, dict) else {}

    def flag(src: dict[str, Any], key: str) -> bool | None:
        v = src.get(key)
        return v if isinstance(v, bool) else None

    def count(key: str) -> int | None:
        v = st.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    ok, lost, rec = flag(st, "touch_ok"), count("touch_bus_lost"), count("touch_recoveries")
    reason = ""
    if ok is False:
        extra = []
        if lost:
            extra.append(f"bus lost {lost}x")
        if rec is not None:
            extra.append(f"{rec} {'recovery' if rec == 1 else 'recoveries'}")
        reason = "touch unavailable" + (f" ({', '.join(extra)})" if extra else "")
    return TouchHealth(present=flag(pt, "present"), cal=flag(pt, "cal"), ok=ok, bus_lost=lost,
                       recoveries=rec, reason=reason)


# --- the optional session adapter ---------------------------------------------------------


@runtime_checkable
class PanelAdapter(Protocol):
    """``session.panel``: a board's front panel (``BoardSession.panel``; None: it has none).

    Every method may raise ``UnavailableError`` (with the capability and the reason) when
    this board cannot do it now, and the usual ``HarnessError``s when the board does not
    answer (``HeldError`` for a busy control channel).
    """

    def support(self) -> PanelSupport: ...

    def state(self) -> PanelState:
        """What the panel shows now (a ``panel`` read, or the bare-metal fallback)."""
        ...

    def frame(self) -> PanelFrame:
        """The panel's text grid, read from it or rebuilt (``source`` says which)."""
        ...

    def hello(self, hello: Hello) -> PanelState:
        """Send one hello on its own connection; the reply's state (``sessions`` is empty:
        a hello reply carries the count only)."""
        ...

    def locate(self, seconds: int, who: str) -> float:
        """Blink the board for ``seconds`` (0 stops); returns the epoch time it stops."""
        ...


#: Optional on an adapter: ``offer(hello, on_reply)`` arms a hello that the next control
#: connection Harness Manager opens for any reason carries first (riding); ``on_reply``
#: gets the reply's ``PanelState``. ``withdraw()`` disarms it; returns True if it was armed.
#:
#: Optional on an adapter that is a view of a Harness Manager service (a daemon session's
#: proxy, CCR PANEL-5): ``presence()`` returns that service's presence for the board (the
#: ``presence`` object of ``GET /boards/{bid}/panel``), and ``identify_until()`` the epoch
#: time a running Identify stops, or None. An in-process adapter has neither: nothing beats.
OnReply = Callable[[PanelState], None]
