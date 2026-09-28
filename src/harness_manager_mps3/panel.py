"""The MPS3 front panel (CLCD) as ``session.panel`` (lane P2). ``make_panel_adapter(session)``
is the hook ``pack.py`` calls.

docs/design/CLCD_ALIGNMENT.md §2, §5.2, §5.3 is the wire; ``harness_manager.core.panel`` is
the model. What a board can do is told apart by FEATURE (``version.features``), never by a
harness type guessed from a missing one (PANEL-TRUTH: the Linux harness rc2_v6 has
``clcd_kvm`` but not ``panel``/``presence``/``locate``, like bare metal v0.11):

| Features | ``state()`` | ``frame()`` | ``hello()`` | ``locate()`` |
|---|---|---|---|---|
| ``panel`` (R2) | the ``panel`` verb | ``panel`` ``frame:"a"`` + ``"b"`` | ``presence`` (R1): the ``hello`` verb | ``locate`` (R3) |
| ``clcd_kvm`` without ``panel`` (bare metal v0.11, Linux before R1-R3) | ``display`` query: the owner only | REBUILT from what HM read | UNAVAILABLE, with the reason | UNAVAILABLE, with the reason |
| neither | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |

``locate()`` gates on its own bit: the Linux harness images rc2_v7/v7n report ``locate``
without ``presence``/``panel`` (R1/R2 are not in them), so Identify works there while the
state and the mirror follow the ``clcd_kvm`` row (lane LOCATE).

``support().impl`` carries the harness's own ``version.impl`` so a view can name the type
when (and only when) the harness said it.

Bare metal keeps its panel exactly as it is (decision P3): nothing here sends it a new verb.
A board without ``locate`` never gets a ``display`` toggle as a stand-in: that would disturb
a DUT that owns the panel.

**Touch health** comes from the additive ``stats`` keys ``touch_ok``, ``touch_bus_lost`` and
``touch_recoveries`` (both engines, Linux lead 2026-09-24), read on the same connection as
the state when the harness reports ``stats``. An absent key is unknown, never "ok".

**Riding.** A hello goes on connections Harness Manager makes anyway (decision P1):
``offer(hello, on_reply)`` arms it, and the next ``Mps3Shell.call_raw`` on this session, for
whatever reason, sends it first on the same connection. When nothing else talks to the board
the presence service sends it itself (``hello``). Arming is the only way a ride happens, and
only a board that reports ``presence`` is ever armed, so every other board's connections are
untouched. The ride is the shell's explicit hook (CCR PANEL-3): ``make_panel_adapter`` sets
``Mps3Shell.preamble`` to ``ride``, which ``call_raw`` runs on each connection before its
own requests; with nothing armed it sends nothing.

**One codec, until R1-R3 land in pyverify.** ``hello``, ``panel`` and ``locate`` have no
``ShellClient`` method yet, so they go through pyverify's own request framing
(``ShellClient._request``: its transport, its reply validation) with the op this module
builds. The FakeShell profile in ``tests/fakes/clcd_panel_shell.py`` answers them exactly as
the design's wire says. ``stats`` uses ``ShellClient.stats()`` when the installed pyverify has
it. CCR PANEL-4 (to the Linux lead): ``ShellClient.hello/panel/locate`` + FakeShell ``_op_*``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from harness_manager.core import capabilities as C
from harness_manager.core.errors import UnavailableError, UsageError
from harness_manager.core.model import BoardIdentity, LinkKind
from harness_manager.core.panel import (
    COLS,
    LINE_MAX,
    LOCATE_MAX_MS,
    LOCATE_WHO_WIRE_MAX,
    REBUILT_NOTE,
    ROLE_INVERTED,
    ROLE_TEXT,
    ROWS,
    SOURCE_PANEL,
    SOURCE_REBUILT,
    UNKNOWN,
    Hello,
    OnReply,
    PanelEvent,
    PanelFrame,
    PanelSession,
    PanelState,
    PanelSupport,
    ascii_field,
    encode_hello,
    hello_message,
    order_sessions,
    touch_health,
)

from .capabilities import NEEDS_LOCATE, NEEDS_PANEL, NEEDS_PRESENCE
from .shell import _ShellBusy

log = logging.getLogger(__name__)

#: How long the harness's feature list is trusted before it is read again.
FEATURES_TTL_S = 300.0
#: A frame comes in two halves (rows 0-7, then 8-14): a whole frame with its roles is about
#: 1.25 KB, at the harness's 1280 B reply limit (docs/design §2.5).
FRAME_HALVES = (("a", 0, 8), ("b", 8, ROWS))
LOCATE_MAX_S = 30


def _request(client: Any, msg: dict[str, Any]) -> dict[str, Any]:
    """One request through pyverify's framing (see the module docstring)."""
    return client._request(msg)


def _stats(client: Any, tap: Any) -> dict[str, Any] | None:
    """The ``stats`` reply as a dict (the additive touch keys are read from it)."""
    ask = getattr(client, "stats", None)
    try:
        if callable(ask):
            resp = ask()
            raw = getattr(resp, "raw", None)
            reply = dict(raw) if isinstance(raw, dict) and raw else dict(getattr(tap, "last", {}))
        else:
            reply = _request(client, {"op": "stats"})
    except (TimeoutError, OSError):
        raise
    except Exception as exc:  # noqa: BLE001 - touch health is optional; never fail the state
        log.debug("stats for touch health failed: %s", exc)
        return None
    return reply if reply.get("ok") else None


def _declined(reply: dict[str, Any], capability: str, what: str) -> UnavailableError | None:
    """The harness said no. "unknown op" means it lacks the verb despite the feature bit."""
    if reply.get("ok"):
        return None
    err = str(reply.get("err") or "no reason given")
    return UnavailableError(capability, f"the harness declined {what}: {err}")


def _events(raw: Any, wall: float) -> tuple[PanelEvent, ...]:
    out = []
    for e in raw if isinstance(raw, list) else ():
        if not isinstance(e, dict):
            continue
        seq, ms = e.get("seq"), e.get("ms_ago", 0)
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue
        ms = ms if isinstance(ms, int) and not isinstance(ms, bool) and ms >= 0 else 0
        out.append(PanelEvent(seq=seq, kind=str(e.get("k") or "tap"), on=str(e.get("on") or ""),
                              ms_ago=ms, at=wall - ms / 1000.0))
    return tuple(sorted(out, key=lambda ev: ev.seq))


def _sessions(raw: Any, mine: str = "") -> tuple[PanelSession, ...]:
    out = []
    for s in raw if isinstance(raw, list) else ():
        if not isinstance(s, dict) or not s.get("sid"):
            continue
        age = s.get("age_s", 0)
        age = float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else 0.0
        sid = str(s["sid"])
        out.append(PanelSession(sid=sid, who=str(s.get("who") or ""),
                                role=str(s.get("role") or "watch"), age_s=age,
                                mine=bool(mine) and sid == mine))
    return order_sessions(out)


def parse_state(reply: dict[str, Any], *, wall: float, stats: dict[str, Any] | None = None,
                mine: str = "") -> PanelState:
    """A ``panel`` reply (or a ``hello`` reply's ``panel`` object merged with its events)."""
    sessions = _sessions(reply.get("sessions"), mine)
    count = reply.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        count = len(sessions)
    seq = reply.get("seq", 0)
    return PanelState(
        page=str(reply.get("page") or ""), owner=str(reply.get("owner") or ""),
        pending=bool(reply.get("pending", False)), banner=str(reply.get("banner") or ""),
        card=str(reply.get("card") or ""), touch=touch_health(stats, reply.get("touch")),
        sessions=sessions, count=count,
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
        events=_events(reply.get("events"), wall), source=SOURCE_PANEL, observed_at=wall)


def parse_hello_reply(reply: dict[str, Any], *, wall: float) -> PanelState:
    """``{ok, sessions: N, panel: {...}, events: [...]}``: the count, the panel, the ring."""
    panel = reply.get("panel") if isinstance(reply.get("panel"), dict) else {}
    count = reply.get("sessions")
    merged = {**panel, "events": reply.get("events"),
              "count": count if isinstance(count, int) and not isinstance(count, bool) else 0}
    merged.pop("sessions", None)
    return parse_state(merged, wall=wall)


# --- the rebuilt mirror (an image without `panel`) ----------------------------------------


def _row(text: str) -> str:
    return text[:COLS].ljust(COLS)


def _centred(text: str) -> str:
    return _row(" " * ((COLS - len(text)) // 2) + text)


def board_address(session: Any) -> str:
    """The board's OWN address for the rebuilt NET row (PANEL-TRUTH): where the hub (or this
    LAN) reaches it (``session.reach.remote_host``), else the configured Ethernet link's
    host, else the shell's. Never a loopback address: through a hub the shell connects to
    its SSH tunnel's local end (127.0.0.1), which is this computer, not the board. ""
    when nothing names the board's own address."""
    from .identify import is_loopback
    from .shell import parse_endpoint

    seen = [str(getattr(getattr(session, "reach", None), "remote_host", "") or "")]
    cand = getattr(session, "candidate", None)
    for lk in getattr(cand, "links", ()) or ():
        if lk.kind == LinkKind.ETHERNET:
            seen.append(parse_endpoint(lk.address, 0)[0])
            break
    seen.append(str(getattr(getattr(session, "shell", None), "host", "") or ""))
    return next((h for h in seen if h and not is_loopback(h)), "")


def rebuilt_frame(*, name: str, identity: BoardIdentity | None, host: str, owner: str,
                  wall: float) -> PanelFrame:
    """Today's (v0.11) panel layout with only the facts Harness Manager read, and ``UNKNOWN``
    ("\u2014") where it did not read one (never "?", which reads as broken, and never a
    guess). ``host`` is the board's own address (``board_address``); a loopback one (a
    tunnel's end) is not the board's and shows as unknown. It is labelled as rebuilt: it
    was not read from the glass."""
    from .identify import is_loopback

    roles = [ROLE_TEXT * COLS for _ in range(ROWS)]
    if owner == "dut":
        # The KVM notice the harness leaves on the glass (clcd.c:543-553), rows 6-8 inverted.
        rows = [" " * COLS] * ROWS
        rows[0] = "-" * 11 + " nanoSoC harness " + "-" * 12
        rows[6], rows[8] = _centred("DUT HAS THE DISPLAY"), _centred("PRESS  PB1  TO RETURN")
        rows[14] = "-" * COLS
        for r in (6, 7, 8):
            roles[r] = ROLE_INVERTED * COLS
    else:
        ident = identity or BoardIdentity(board_type="mps3")
        design = ident.rm_name or ident.rm_id or UNKNOWN
        shell = ident.shell_id.upper().replace("0X", "0x") if ident.shell_id else UNKNOWN
        net = host if host and not is_loopback(host) else UNKNOWN
        title = str(getattr(ident, "label", "") or "") or name or "MPS3"
        rows = [
            _row(f"{title.upper()[:19]:<20}nanoSoC harness"),
            "-" * COLS,
            _row(f"DUT : {design}"),
            _row(f"SWAP: {UNKNOWN}"),
            _row(f"SID : {shell}"),
            _row(f"NET : {net}"),
            _row(f"UP  : {UNKNOWN}"),
            _row(f"DUT : {UNKNOWN}"),
            _row(f"ICAP: {UNKNOWN}"),
            _row(f"CFG : {UNKNOWN}"),
            " " * COLS, " " * COLS, " " * COLS,
            "-" * COLS,
            _row(f"MAC {UNKNOWN}"),
        ]
    return PanelFrame(rows=tuple(rows), roles="".join(roles), source=SOURCE_REBUILT,
                      observed_at=wall, note=REBUILT_NOTE)


# --- the adapter ---------------------------------------------------------------------------


class Mps3Panel:
    """``session.panel`` for an MPS3 with an Ethernet link to its shell."""

    def __init__(self, session: Any, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 features_ttl_s: float = FEATURES_TTL_S) -> None:
        self._session = session
        self._clock = clock
        self._wall = wall
        self._ttl = features_ttl_s
        self._mu = threading.Lock()
        self._ident: tuple[float, BoardIdentity] | None = None
        self._forgot = False                 # the next identity is read live, not seeded
        self._armed: tuple[Hello, OnReply] | None = None
        self.rides = 0                       # hellos that rode another connection

    # -- what this board can do -------------------------------------------------------------

    @property
    def _shell(self) -> Any:
        return self._session.shell

    def identity(self, *, fresh: bool = False) -> BoardIdentity:
        """The harness's identity, cached ``features_ttl_s`` (features change only with the
        firmware). The probe's identity seeds it, so opening a board costs no extra read."""
        now = self._clock()
        with self._mu:
            cached = self._ident
        if not fresh and cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        if cached is None and not fresh and not self._forgot:
            seed = getattr(self._session.candidate, "identity", None)
            if seed is not None and seed.features:
                with self._mu:
                    self._ident = (now, seed)
                return seed
        ident = self._session.identity()
        with self._mu:
            self._ident = (now, ident)
            self._forgot = False
        return ident

    def forget(self) -> None:
        """Read the features again next time (the harness declined a verb it announced)."""
        with self._mu:
            self._ident = None
            self._forgot = True

    def note_identity(self, identity: BoardIdentity | None) -> None:
        """PANEL-TRUTH: someone read the board's identity (``engine.info``): the rebuilt
        mirror's DUT and SID rows follow a swap at once, not 5 minutes later. An identity
        without features says nothing about them (FIX-PACK-1 item 3): it is ignored."""
        if identity is None or not tuple(identity.features or ()):
            return
        with self._mu:
            self._ident = (self._clock(), identity)
            self._forgot = False

    def _features(self) -> frozenset[str]:
        return frozenset(self.identity().features)

    def support(self) -> PanelSupport:
        f = self._features()
        panel = "panel" in f
        return PanelSupport(
            front_panel="" if panel or "clcd_kvm" in f else NEEDS_PANEL,
            presence="" if "presence" in f else NEEDS_PRESENCE,
            locate="" if "locate" in f else NEEDS_LOCATE,
            source=SOURCE_PANEL if panel else SOURCE_REBUILT,
            impl=str(self.identity().harness_impl or ""))

    # -- reads ------------------------------------------------------------------------------

    def state(self) -> PanelState:
        f = self._features()
        stats_too = "stats" in f
        if "panel" in f:
            def ask(c: Any, tap: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
                reply = _request(c, {"op": "panel"})
                return reply, (_stats(c, tap) if stats_too else None)

            reply, stats = self._shell.call_raw(ask)
            refusal = _declined(reply, C.FRONT_PANEL, "the panel read")
            if refusal is not None:
                self.forget()
                raise refusal
            return parse_state(reply, wall=self._wall(), stats=stats)
        if "clcd_kvm" in f:
            def ask_owner(c: Any, tap: Any) -> tuple[Any, dict[str, Any] | None]:
                resp = c.display_owner()
                return resp, (_stats(c, tap) if stats_too else None)

            resp, stats = self._shell.call_raw(ask_owner)
            if not resp.ok:
                raise UnavailableError(C.FRONT_PANEL, f"the shell has no CLCD KVM ({resp.err})")
            return PanelState(owner=str(resp.owner or ""), touch=touch_health(stats),
                              source=SOURCE_REBUILT, observed_at=self._wall(), note=REBUILT_NOTE)
        raise UnavailableError(C.FRONT_PANEL, NEEDS_PANEL)

    def frame(self) -> PanelFrame:
        f = self._features()
        if "panel" in f:
            return self._read_frame()
        if "clcd_kvm" not in f:
            raise UnavailableError(C.FRONT_PANEL, NEEDS_PANEL)
        owner = self.state().owner
        cand = self._session.candidate
        return rebuilt_frame(name=getattr(cand, "name", ""), identity=self.identity(),
                             host=board_address(self._session), owner=owner, wall=self._wall())

    def _read_frame(self) -> PanelFrame:
        def ask(c: Any, _tap: Any) -> list[dict[str, Any]]:
            out = []
            for part, _r0, _r1 in FRAME_HALVES:
                reply = _request(c, {"op": "panel", "frame": part})
                out.append(reply)
                if not reply.get("ok") or len(reply.get("rows") or ()) >= ROWS:
                    break          # refused, or the harness sent the whole frame at once
            return out

        replies = self._shell.call_raw(ask)
        for reply in replies:
            refusal = _declined(reply, C.FRONT_PANEL, "the frame read")
            if refusal is not None:
                raise refusal
        rows: list[str] = []
        roles = ""
        for reply in replies:
            rows += [_row(str(r)) for r in reply.get("rows") or ()]
            roles += str(reply.get("roles") or "")
        if len(rows) != ROWS:
            raise UnavailableError(C.FRONT_PANEL, f"the harness sent {len(rows)} rows, "
                                                  f"not {ROWS}")
        return PanelFrame(rows=tuple(rows), roles=roles if len(roles) == ROWS * COLS else "",
                          source=SOURCE_PANEL, observed_at=self._wall())

    # -- presence ---------------------------------------------------------------------------

    def _presence(self) -> None:
        if "presence" not in self._features():
            raise UnavailableError(C.PRESENCE, NEEDS_PRESENCE)

    def _send_hello(self, c: Any, hello: Hello) -> PanelState:
        encode_hello(hello)                     # ValueError over LINE_MAX: never on the wire
        reply = _request(c, hello_message(hello))
        refusal = _declined(reply, C.PRESENCE, "the hello")
        if refusal is not None:
            self.forget()
            raise refusal
        return parse_hello_reply(reply, wall=self._wall())

    def hello(self, hello: Hello) -> PanelState:
        self._presence()
        self.withdraw()                          # sent now: nothing left to ride
        return self._shell.call_raw(lambda c, _tap: self._send_hello(c, hello))

    def offer(self, hello: Hello, on_reply: OnReply) -> None:
        """Arm ``hello`` for the next connection this session opens (see the docstring)."""
        self._presence()
        with self._mu:
            self._armed = (hello, on_reply)

    def withdraw(self) -> bool:
        with self._mu:
            armed, self._armed = self._armed, None
        return armed is not None

    def _take(self) -> tuple[Hello, OnReply] | None:
        with self._mu:
            armed, self._armed = self._armed, None
        return armed

    def ride(self, client: Any, _tap: Any) -> None:
        """Send the armed hello, if any, on ``client``'s connection before its own requests.

        Connection-level failures (busy, timeout, reset) are re-armed and re-raised: the
        caller's own request would meet them too. Anything else is logged and the caller
        goes on; the presence service sends the hello itself later.
        """
        armed = self._take()
        if armed is None:
            return
        hello, on_reply = armed
        try:
            state = self._send_hello(client, hello)
        except (TimeoutError, OSError, _ShellBusy):
            self._rearm(armed)
            raise
        except Exception as exc:  # noqa: BLE001 - a hello never fails someone else's call
            log.debug("a riding hello failed: %s", exc)
            return
        self.rides += 1
        try:
            on_reply(state)
        except Exception:  # noqa: BLE001 - the listener's bug is not the caller's
            log.exception("presence reply handler failed")

    def _rearm(self, armed: tuple[Hello, OnReply]) -> None:
        with self._mu:
            if self._armed is None:
                self._armed = armed

    # -- Identify ---------------------------------------------------------------------------

    def locate(self, seconds: int, who: str) -> float:
        """The Linux harness's ``locate`` (docs/design/BOARD_LOCATE.md §2; net-protocol v0.16 as
        shipped, platform 18622e5): ``{op, s, who}`` -> ``{ok, op, until_ms}``; ``s: 0`` stops.
        The board blinks the panel's backlight at 2 Hz, with an "IDENTIFY: <who>" banner while
        the harness owns the panel; a tap on the glass stops it early.

        V7-ALIGN: ``who`` is at most 32 printable ASCII characters on the wire
        (``LOCATE_WHO_WIRE_MAX``; the board refuses more with ``invalid``); ``until_ms`` is
        RELATIVE (ms from the reply to the end), so a value past ``s`` seconds (an absolute
        epoch, say) is not believed: ``s`` seconds instead. ``invalid`` is a ``UsageError``
        (HM sent something wrong; the features are not forgotten); ``not_supported`` (a build
        without the panel) and anything else are ``UnavailableError``."""
        if "locate" not in self._features():
            raise UnavailableError(C.LOCATE, NEEDS_LOCATE)
        s = max(0, min(LOCATE_MAX_S, int(seconds)))
        msg: dict[str, Any] = {"op": "locate", "s": s}
        if s and who:
            msg["who"] = ascii_field(who, LOCATE_WHO_WIRE_MAX)
        reply = self._shell.call_raw(lambda c, _tap: _request(c, msg))
        if not reply.get("ok") and reply.get("code") == "invalid":
            raise UsageError(f"the harness refused Identify: {reply.get('err') or 'invalid'}",
                             hint=f"s 0-{LOCATE_MAX_S}; who at most {LOCATE_WHO_WIRE_MAX} "
                                  "printable ASCII characters")
        refusal = _declined(reply, C.LOCATE, "Identify")
        if refusal is not None:
            self.forget()
            raise refusal
        return self._wall() + locate_ms(reply, s) / 1000.0


def locate_ms(reply: dict[str, Any], s: int) -> float:
    """``locate``'s ``until_ms`` as ms from now (V7-ALIGN: RELATIVE, 0 = stopped). Missing, not
    a number, negative, or past ``s`` seconds (and ``LOCATE_MAX_MS``): ``s`` seconds."""
    until_ms = reply.get("until_ms", s * 1000)
    if not isinstance(until_ms, (int, float)) or isinstance(until_ms, bool) or until_ms < 0 \
            or until_ms > min(LOCATE_MAX_MS, s * 1000):
        return float(s * 1000)
    return float(until_ms)


def make_panel_adapter(session: Any) -> Mps3Panel | None:
    """The pack hook: a panel adapter for a session with an Ethernet shell, else None.

    It takes the shell's ``preamble`` hook (CCR PANEL-3) for the ride: an armed hello goes
    first on the next connection. A shell without the hook gets no ride; the presence
    service then sends every hello on its own connection.
    """
    shell = getattr(session, "shell", None)
    if shell is None:
        return None
    panel = Mps3Panel(session)
    if hasattr(shell, "preamble"):
        shell.preamble = panel.ride
    return panel


__all__ = ["LINE_MAX", "Mps3Panel", "board_address", "locate_ms", "make_panel_adapter",
           "parse_hello_reply", "parse_state", "rebuilt_frame"]
