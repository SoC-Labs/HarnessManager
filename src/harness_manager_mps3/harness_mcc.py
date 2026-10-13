"""The board controller (MCC) THROUGH THE HARNESS: net-protocol v0.18 (the gate) and v0.20 (the acts).

Today Harness Manager reaches the MCC over the Debug USB (``mcc.py``) or on the hub
(``hub_mcc.py``). The harness can reach it too, and says how:

- **features** (``version.features``): ``mccif`` (the in-fabric SCC route: the FPGA's own
  link to the MCC) and ``mcc_local`` (the USB loopback route: the harness's own cable to the
  MCC's USB console). ``mcc`` was this pack's name for the same idea before v0.18 (the J7
  mod); no image lists it, and it is recognised as an alias;
- **ONE verb**, ``{"op": "mcc", "act": ACT, ...}``, with the acts ``status``, ``temp``, ``osc``,
  ``osc_set`` (armed), ``probe``, ``mbox``, ``log`` and ``reboot`` (loopback only, two-phase
  with a caller token). v0.20 ``mcc status`` answers ``scc`` and ``loopback`` as an object or
  null (null: that route is not there); a v0.18 image answered ``route`` as the one route in
  use. Both are read (``parse_status``);
- ``reset`` target ``mcc_warm`` (feature ``mccif``): the MCC's warm board reset.

What this module does:

- ``recognise(features)``: which harness MCC names an image lists (``HarnessMccSupport``);
- ``HarnessMcc.status()``: the ``mcc status`` read behind the feature, cached;
- ``HarnessMcc`` acts: ``osc``/``oscillators``, ``set_osc`` (armed, claim-locked), ``probe``,
  ``mbox``, ``log``, ``temperatures``, ``reboot`` (two-phase, witnessed) and ``warm_reset``
  (confirmed by ``up_ms`` restarting). Every request is built by pyverify's ``mint4``
  (``mcc_request``), so a denied act or device (``MCC_DENY_ACTS``, ``MCC_OSC_PROTECTED``,
  ``MCC_PROBE_FUNCS``) never reaches the wire; ``policy()`` hands the same tables to the UI;
- ``HarnessMcc.capability_reasons``: ``reboot_board`` and ``clock_board`` through the harness
  need the feature AND a route that serves the act (``reboot``: loopback only);
- ``RoutedController``: the ONE selection policy (``prefer``): the harness route when the image
  has the feature and the route serves the act (no USB cable needed), else the Debug USB, else
  the hub. ``mcc.make_controller_adapter`` wraps its controller with it.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pyverify import mint4 as M4

from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import CapabilitySpec, satisfied_route
from harness_manager.core.errors import (
    ActionFailedError,
    ClaimLockedError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import Reading
from harness_manager.daemon.mcc_route import PREFER_HARNESS, prefer

FEATURE_SCC = "mccif"
FEATURE_LOOPBACK = "mcc_local"
LEGACY_FEATURE = "mcc"          # pre-v0.18 (the J7 mod); no image lists it
FEATURES = (FEATURE_SCC, FEATURE_LOOPBACK)
ALL_FEATURES = frozenset((*FEATURES, LEGACY_FEATURE))

ROUTE_SCC, ROUTE_LOOPBACK, ROUTE_NONE = "scc", "loopback", "none"
ROUTES = (ROUTE_SCC, ROUTE_LOOPBACK, ROUTE_NONE)
#: What each feature says the image can do (``mcc status`` says what it does now).
FEATURE_ROUTE = {FEATURE_SCC: ROUTE_SCC, FEATURE_LOOPBACK: ROUTE_LOOPBACK}

ACTS = ("status", "temp", "osc", "osc_set", "probe", "mbox", "log", "reboot")
#: The acts each route serves: ``reboot`` only over the USB loopback.
ROUTE_ACTS: dict[str, frozenset[str]] = {
    ROUTE_SCC: frozenset(ACTS) - {"reboot"},
    ROUTE_LOOPBACK: frozenset(ACTS),
    ROUTE_NONE: frozenset(),
}
#: The acts Harness Manager drives over the harness.
IMPLEMENTED = frozenset(ACTS)
#: What each capability needs from the harness's MCC route.
CAPABILITY_ACT = {C.REBOOT_BOARD: "reboot", C.CLOCK_BOARD: "osc"}
ACT_WHAT = {"reboot": "rebooting the board", "osc": "reading the board oscillators",
            "osc_set": "setting a board oscillator", "temp": "reading the MCC temperature"}

STATUS_TTL_S = 120.0
STATUS_FAIL_TTL_S = 30.0


@dataclass(frozen=True)
class HarnessMccSupport:
    """The harness MCC names an image lists, and the routes they promise."""

    features: tuple[str, ...]

    @property
    def routes(self) -> tuple[str, ...]:
        return tuple(FEATURE_ROUTE[f] for f in self.features if f in FEATURE_ROUTE)

    @property
    def legacy(self) -> bool:
        return self.features == (LEGACY_FEATURE,)


def recognise(features: Iterable[str]) -> HarnessMccSupport | None:
    """The seam: the harness MCC feature names among ``features`` (None: the image has none)."""
    have = tuple(dict.fromkeys(f for f in features if f in ALL_FEATURES))
    return HarnessMccSupport(have) if have else None


@dataclass(frozen=True)
class MccStatus:
    """``mcc status``. ``routes`` are the routes that are THERE now (v0.20: the non-null
    ``scc``/``loopback`` objects; v0.18: the one ``route`` it named). ``route`` is the one to
    show (``scc``, ``loopback``, ``none``, or ``""`` when the reply had no route this build
    knows); ``raw`` keeps what it said."""

    route: str
    raw: dict[str, Any] = field(default_factory=dict)
    routes: tuple[str, ...] = ()

    def serves(self, act: str) -> bool:
        return any(act in ROUTE_ACTS.get(r, frozenset()) for r in self.routes)

    @property
    def last_reboot(self) -> dict[str, Any] | None:
        """``loopback.last_reboot``: ``{token, state}`` or None."""
        lo = self.raw.get("loopback")
        lr = lo.get("last_reboot") if isinstance(lo, dict) else None
        return dict(lr) if isinstance(lr, dict) else None


def parse_status(reply: dict[str, Any]) -> MccStatus:
    """A ``mcc status`` reply as ``MccStatus``; ``HarnessError`` for a refusal."""
    if not reply.get("ok"):
        err = str(reply.get("err") or reply.get("code") or "no reason given")
        if "unknown op" in err:
            raise UnavailableError(C.REBOOT_BOARD, f"this image lists a harness MCC feature but "
                                                   f"does not answer `mcc status` ({err})")
        raise HarnessError(f"the harness refused `mcc status`: {err}")
    scc, lo = reply.get("scc"), reply.get("loopback")
    if "scc" in reply and "loopback" in reply and all(v is None or isinstance(v, dict)
                                                      for v in (scc, lo)):
        routes = tuple(r for r, v in ((ROUTE_SCC, scc), (ROUTE_LOOPBACK, lo)) if v is not None)
        return MccStatus(routes[0] if routes else ROUTE_NONE, dict(reply), routes)
    route = reply.get("route")
    route = route.strip().lower() if isinstance(route, str) else ""
    route = route if route in ROUTES else ""
    return MccStatus(route, dict(reply), (route,) if route in (ROUTE_SCC, ROUTE_LOOPBACK) else ())


def read_status(shell: Any) -> MccStatus:
    """``{"op": "mcc", "act": "status"}`` on 6900 (one connection, the board's control gate):
    the raw request the vendored pyverify does not model (``net_identity.request``'s way)."""
    reply = shell.call_raw(lambda c, _tap: c._request({"op": "mcc", "act": "status"}))
    return parse_status(dict(reply))


def _other_route(name: str, links: frozenset[Any], features: frozenset[str]) -> bool:
    """Does a route WITHOUT the harness's MCC give capability ``name``?"""
    from .capabilities import SPECS

    spec = next((s for s in SPECS if s.name == name), None)
    if spec is None:
        return False
    routes = tuple(r for r in spec.routes if not (r.features & ALL_FEATURES))
    return bool(routes) and satisfied_route(CapabilitySpec(name, spec.title, routes),
                                            links, features) is not None


class HarnessMcc:
    """``session.harness_mcc``: the MCC through the harness (module docstring)."""

    def __init__(self, session: Any, *, reader: Callable[[Any], MccStatus] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 3.0,
                 probe: Callable[[], str | None] | None = None,
                 uptime: Callable[[], int | None] | None = None) -> None:
        self._session = session
        self._reader = reader or read_status
        self._clock = clock
        self._sleep = sleep
        self.poll_s = poll_s
        self._probe_fn = probe
        self._uptime_fn = uptime
        self._mu = threading.Lock()
        self._last: tuple[MccStatus | None, str, float] | None = None   # (status, error, at)
        self._feats: tuple[str, ...] = ()        # the image's features, as last read
        self.reads = 0                                                  # how many (tests)

    def note_features(self, features: Iterable[str]) -> None:
        """The image's features as someone read them (the gate passes ``info``'s). A read
        with none (a held board's identify answer) keeps what was known."""
        feats = tuple(features)
        if feats:
            self._feats = feats

    # -- the route --------------------------------------------------------------------------

    def status(self, *, refresh: bool = False) -> MccStatus:
        """``mcc status`` (cached; ``refresh`` asks again). ``UnavailableError`` when the image
        has no harness MCC feature: the verb is behind it."""
        support = recognise(self._features())
        if support is None:
            raise UnavailableError(C.REBOOT_BOARD, "this release does not reach the MCC "
                                                   f"(no {' or '.join(repr(f) for f in FEATURES)}"
                                                   " in its features; net-protocol v0.18)")
        now = self._clock()
        with self._mu:
            last = self._last
        if last is not None and not refresh:
            st, err, at = last
            if now - at < (STATUS_FAIL_TTL_S if err else STATUS_TTL_S):
                if err:
                    raise HarnessError(err)
                assert st is not None
                return st
        shell = getattr(self._session, "shell", None)
        if shell is None:
            raise UnavailableError(C.REBOOT_BOARD, "no Ethernet link to the harness")
        self.reads += 1
        try:
            st = self._reader(shell)
        except HarnessError as exc:
            with self._mu:
                self._last = (None, exc.message, now)
            raise
        with self._mu:
            self._last = (st, "", now)
        return st

    def route(self) -> str:
        """The route ``mcc status`` reports (``""`` when unknown)."""
        return self.status().route

    def _features(self) -> tuple[str, ...]:
        """The features last noted, else the board's (one ``version`` read), else the probe's."""
        if self._feats:
            return self._feats
        try:
            ident = self._session.identity()
        except HarnessError:
            ident = getattr(getattr(self._session, "candidate", None), "identity", None)
        self.note_features(getattr(ident, "features", ()) or ())
        return self._feats

    # -- the gate -------------------------------------------------------------------------------

    def reason_for(self, act: str) -> str:
        """Why ``act`` cannot go through the harness's MCC now (``""``: it can)."""
        what = ACT_WHAT.get(act, act)
        try:
            st = self.status()
        except HarnessError as exc:
            return f"the harness's route to the MCC is unknown: `mcc status` said {exc.message}"
        if st.route == ROUTE_NONE:
            return ("the harness reports no route to the MCC (`mcc status`: route none: no "
                    "in-fabric SCC in this static, or no USB loopback cable)")
        if not st.route:
            return (f"the harness's `mcc status` names no route this build knows "
                    f"({st.raw.get('route')!r})")
        if not st.serves(act):
            return (f"{what} needs the harness's USB loopback route (feature "
                    f"'{FEATURE_LOOPBACK}'); `mcc status` says route {st.route} (the in-fabric "
                    "SCC)")
        return ""

    def capability_reasons(self, available: frozenset[str], features: Iterable[str],
                           links: Iterable[Any]) -> dict[str, str]:
        """``reboot_board``/``clock_board`` offered ONLY by the harness's MCC route: withdrawn
        with why unless the route in ``mcc status`` serves the act and HM drives it. A
        capability another route gives is left alone (and nothing is asked)."""
        feats = frozenset(features)
        if recognise(feats) is None:
            return {}
        self.note_features(sorted(feats))
        link_set = frozenset(links)
        out: dict[str, str] = {}
        for cap, act in CAPABILITY_ACT.items():
            if cap not in available or _other_route(cap, link_set, feats):
                continue
            why = self.reason_for(act)
            if why:
                out[cap] = why
        return out

    # -- the request path -----------------------------------------------------------------

    def _shell(self) -> Any:
        shell = getattr(self._session, "shell", None)
        if shell is None:
            raise UnavailableError(C.REBOOT_BOARD, "no Ethernet link to the harness")
        return shell

    def _request(self, ask: Callable[[Any], dict[str, Any]], *, locked: bool,
                 what: str) -> dict[str, Any]:
        """``ask(client)`` on the control port. A claim-locked act goes through the board's own
        SSH when the claim is this Harness Manager's, else directly, and a ``locked`` refusal
        met there is asked about again (``net_identity.HarnessdSetter``'s way)."""
        from .net_identity import _route
        from .slot_words import is_claim_lock, reply_code

        shell = self._shell()
        route = _route(self._session, what) if locked else ""
        if route == "board-ssh":
            return self._via_board(ask, what)
        reply = dict(shell.call_raw(lambda c, _tap: ask(c)))
        if locked and not reply.get("ok") and is_claim_lock(str(reply.get("err") or ""),
                                                            reply_code(reply)):
            _route(self._session, what, claimed=True)
            return self._via_board(ask, what)
        return reply

    def _via_board(self, ask: Callable[[Any], dict[str, Any]], what: str) -> dict[str, Any]:
        from .card import _forwarded, _gate_key
        from .os_slots import claim_forward
        from .shell import Mps3Shell

        shell = self._shell()
        with claim_forward(self._session, "mcc", what) as (host, ctl, _push):
            fwd = _forwarded(Mps3Shell(host, ctl, gate_key=_gate_key(shell)))
            return dict(fwd.call_raw(lambda c, _tap: ask(c)))

    def act(self, act: str, **fields: Any) -> dict[str, Any]:
        """One ``mcc`` act. The request is built by pyverify (``mint4.mcc_request``): a denied
        act, device or function is refused HERE, before anything is sent. A refusal from the
        board is raised in words (``explain``)."""
        try:
            msg = M4.mcc_request(act, **fields)
        except M4.MccDenied as exc:
            raise RefusedError(f"{exc}; Harness Manager never sends it") from exc
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        locked = act in ("osc_set", "reboot") or (
            act == "mbox" and any(k.startswith("w") for k in fields))
        reply = self._request(lambda c: c._request(dict(msg)), locked=locked,
                              what=ACT_WHAT.get(act, f"`mcc {act}`"))
        if not reply.get("ok"):
            raise explain(reply, act)
        return reply

    # -- the acts ---------------------------------------------------------------------------

    @staticmethod
    def policy() -> dict[str, Any]:
        """The deny list, as data from pyverify (never copied): the UI offers only what is
        not denied."""
        return {
            "deny_acts": sorted(M4.MCC_DENY_ACTS),
            "osc_allowed": sorted(M4.MCC_OSC_ALLOWED),
            "osc_protected": {str(d): M4.MCC_OSC_WHY[d] for d in sorted(M4.MCC_OSC_PROTECTED)},
            "probe_funcs": sorted(M4.MCC_PROBE_FUNCS),
            "osc_hz": [M4.OSC_HZ_MIN, M4.OSC_HZ_MAX],
        }

    def facts(self) -> dict[str, Any] | None:
        """What the Recover and clock pages may offer, in plain words (None: the image has no
        harness MCC feature, so none of this exists). Reads ``mcc status`` (cached)."""
        if recognise(self._features()) is None:
            return None
        try:
            st = self.status()
        except HarnessError as exc:
            why = f"the board did not say how it reaches its controller ({exc.message})"
            return {"route": "", "routes": [], "acts": [], "osc_devices": [],
                    "reboot": {"ok": False, "reason": why},
                    "warm_reset": {"ok": FEATURE_SCC in self._features(), "reason": ""},
                    "policy": self.policy()}
        denied = M4.MCC_DENY_ACTS
        acts = [a for a in ACTS if st.serves(a) and a not in denied]
        if not st.routes:
            gone = ("the board has no path to its controller right now (no USB loop cable, "
                    "and no fabric link)")
        else:
            gone = ""
        loop = ROUTE_LOOPBACK in st.routes
        reboot_why = "" if loop else (
            gone or "restarting the board needs the USB loop cable between the board's USB "
                    "host port and its Debug USB; the fabric link cannot do it")
        warm = FEATURE_SCC in self._features()
        return {"route": st.route, "routes": list(st.routes), "acts": acts,
                "osc_devices": sorted(M4.MCC_OSC_ALLOWED) if "osc_set" in acts else [],
                "reboot": {"ok": loop, "reason": reboot_why},
                "warm_reset": {"ok": warm, "reason": "" if warm else
                               "this board's design has no warm reset (it needs a newer design)"},
                "policy": self.policy()}

    def oscillators(self) -> Sequence[Reading]:
        source = "mcc-harness setpoint"
        out: list[Reading] = []
        for dev in range(8):
            try:
                r = self.act("osc", dev=dev)
                out.append(Reading(name=f"osc{dev}", value=float(r["hz"]) / 1e6, unit="MHz",
                                   source=source, reason="the set-point, not a measurement"))
            except (HarnessError, KeyError, TypeError, ValueError) as exc:
                out.append(Reading.unavailable(f"osc{dev}", "MHz", str(exc), source=source))
        return out

    def set_osc(self, dev: int, hz: int) -> dict[str, Any]:
        """``osc_set`` (armed, claim-locked) on an allowed device only."""
        return self.act("osc_set", dev=dev, hz=hz, arm=True)

    def temperatures(self) -> Sequence[Reading]:
        source = "mcc-harness"
        try:
            r = self.act("temp")
            return [Reading(name="mcc_temp", value=float(r["temp_c"]), unit="degC", source=source)]
        except (HarnessError, KeyError, TypeError, ValueError) as exc:
            return [Reading.unavailable("mcc_temp", "degC", str(exc), source=source)]

    # -- the two-phase reboot -------------------------------------------------------------

    def _probe(self) -> Callable[[], str | None]:
        if self._probe_fn is None:
            from .mcc import _shell_probe_for

            self._probe_fn = _shell_probe_for(self._shell(), 3.0)
        return self._probe_fn

    def _uptime_ms(self) -> int | None:
        if self._uptime_fn is not None:
            return self._uptime_fn()
        try:
            st = self._shell().call(lambda c: c.stats())
        except HarnessError:
            return None
        return int(st.up_ms) if getattr(st, "ok", False) else None

    def _reboot_state(self, token: str) -> str:
        """``loopback.last_reboot.state`` for OUR token (``""``: not ours, unreadable)."""
        try:
            st = self._reader(self._shell())
        except HarnessError:
            return ""
        lr = st.last_reboot
        if lr and str(lr.get("token", "")).lower() == token.lower():
            return str(lr.get("state") or "")
        return ""

    def reboot(self, progress: Callable[[str, int, int], None] | None = None,
               wait_s: float | None = None) -> dict[str, Any]:
        try:
            return self._reboot(progress, wait_s)
        finally:
            with self._mu:
                self._last = None        # the route is read again after a restart

    def _reboot(self, progress: Callable[[str, int, int], None] | None,
                wait_s: float | None) -> dict[str, Any]:
        """Power-cycle the board through its MCC (loopback route, claim-locked): TWO-PHASE.
        The reply is sent and the socket closes BEFORE the REBOOT is typed, so the answer
        proves nothing; the witness is that the board went away, came back with the same
        shell id, and ``mcc status`` says ``last_reboot`` = our token, state ``done``.
        ``failed`` = the MCC never acknowledged (the board never went down)."""
        emit = progress or (lambda phase, done, total: None)
        st = self.status(refresh=True)
        if ROUTE_LOOPBACK not in st.routes:
            raise UnavailableError(C.REBOOT_BOARD, self.reason_for("reboot")
                                   or "the harness has no USB loopback route to the MCC")
        probe = self._probe()
        before = probe()
        if before is None:
            raise UnreachableError("the board's harness is not answering, so nothing was sent",
                                   hint="check the board is on, then try again")
        token = secrets.token_hex(8)
        reply = self.act("reboot", token=token)
        sent_at = self._clock()
        emit("sent", 1, 3)
        eta_max = reply.get("eta_max_s")
        wait = float(wait_s) if wait_s else max(REBOOT_WAIT_S, (float(eta_max) if isinstance(
            eta_max, (int, float)) else 0.0) + 150.0)
        deadline = sent_at + wait
        down_at: float | None = None
        down_run = 0
        after = ""
        while self._clock() < deadline:
            self._sleep(self.poll_s)
            now = self._clock()
            sid = probe()
            if sid is None:
                down_run += 1
                if down_run >= DOWN_PINGS and down_at is None:
                    down_at = now
                    emit("down", 2, 3)
                continue
            down_run = 0
            state = self._reboot_state(token)
            if state == "failed":
                raise ActionFailedError(
                    "the board's controller never acknowledged the power-cycle request, so the "
                    "board was not restarted" + ("" if down_at is None else " (it stopped "
                                                  "answering, but did not restart through its "
                                                  "controller)"),
                    hint="nothing was changed; try Warm reset, or press the board's PB0 button")
            if state == "done":
                after = sid
                if before != SHELL_BUSY and sid != SHELL_BUSY and sid != before:
                    raise ActionFailedError(
                        f"the board came back running a different harness ({before} before, "
                        f"{sid} after), so this is not a clean restart",
                        hint="check which design the board runs now (Overview)")
                emit("up", 3, 3)
                took = now - sent_at
                return {"summary": "the board powered itself off and on through its controller "
                                   "and came back",
                        "via": "mcc", "route": ROUTE_LOOPBACK, "token": token,
                        "down_after_s": round((down_at - sent_at) if down_at else 0.0, 1),
                        "up_after_s": round(took, 1),
                        "shell_id_before": before, "shell_id_after": after,
                        "last_reboot": {"token": token, "state": "done"},
                        "down_evidence": ([] if down_at else
                                          ["the restart was too short to see from here"]),
                        "up_evidence": "the controller reports the restart as done"}
        raise ActionFailedError(
            (f"the board stopped answering but did not come back within {int(wait)} s"
             if down_at is not None else
             f"the board did not restart within {int(wait)} s of the request"),
            hint=("it may still be starting (a Linux board takes up to 3 minutes); wait, then "
                  "refresh. If it stays dark, power-cycle it at the plug or press PB0"
                  if down_at is not None else
                  "nothing seems to have happened; try Warm reset, or press the board's PB0"))

    def warm_reset(self, progress: Callable[[str, int, int], None] | None = None,
                   wait_s: float | None = None) -> dict[str, Any]:
        """``reset mcc_warm`` (feature ``mccif``, claim-locked): the board restarts like a
        press of its reset button. The reply comes FIRST (``in_ms``), then the connection
        drops; the witness is ``stats.up_ms`` starting again from small."""
        emit = progress or (lambda phase, done, total: None)
        if FEATURE_SCC not in self._features():
            raise UnavailableError(C.REBOOT_BOARD, "this harness has no warm board reset "
                                                   f"(no '{FEATURE_SCC}' feature)")
        before = self._uptime_ms()
        reply = self._request(lambda c: c.reset_mcc_warm(), locked=True,
                              what="the warm board reset")
        if not reply.get("ok"):
            raise explain(reply, "mcc_warm")
        sent_at = self._clock()
        emit("sent", 1, 3)
        wait = float(wait_s) if wait_s else WARM_WAIT_S
        deadline = sent_at + wait
        gone = False
        while self._clock() < deadline:
            self._sleep(self.poll_s)
            now = self._clock()
            up = self._uptime_ms()
            if up is None:
                if not gone:
                    gone = True
                    emit("down", 2, 3)
                continue
            if (before is not None and up < before) or (before is None and gone):
                emit("up", 3, 3)
                return {"summary": "the board restarted (warm reset) and came back",
                        "via": "mcc_warm", "in_ms": reply.get("in_ms"),
                        "up_ms_before": before, "up_ms_after": up,
                        "up_after_s": round(now - sent_at, 1)}
        raise ActionFailedError(
            f"no sign that the board restarted within {int(wait)} s of the warm reset",
            hint="the board's uptime kept counting; try again, or use the power-cycle step")


#: A reboot witness waits at least this long (ETA is 100-180 s; stage0 + SD + kernel).
REBOOT_WAIT_S = 330.0
WARM_WAIT_S = 240.0
#: Lost pings in a row that mean "down" (one lost ping is a glitch).
DOWN_PINGS = 2
SHELL_BUSY = "(busy)"


def explain(reply: dict[str, Any], act: str) -> HarnessError:
    """A failed ``mcc``/``reset`` reply as the error to show, by its ``code`` (words, not JSON)."""
    code = str(reply.get("code") or "")
    err = str(reply.get("err") or "").strip()
    tail = f" (the board said: {err})" if err else ""
    if code == "locked":
        return ClaimLockedError("the board is claimed: this needs the claiming key over the "
                                f"board's own SSH{tail}")
    if code == "denied":
        return RefusedError(f"the board refuses `{act}`: it is never allowed{tail}")
    if code == "busy":
        return HeldError("the board is busy (a card job runs, or another client holds its "
                         "controller); try again when it finishes")
    if code == "timeout":
        return ActionFailedError(f"the board's controller did not answer in time{tail}",
                                 hint="try again; if it keeps timing out, use the Debug USB")
    if code in ("no_route", "no_hw"):
        return ActionFailedError(f"the board has no path to its controller for this{tail}",
                                 hint="check the USB loopback cable, or the board's design")
    if code == "not_supported" or "unknown op" in err:
        return ActionFailedError(f"this board's harness does not support that{tail}",
                                 hint="update the board's harness")
    if code == "invalid":
        return UsageError(f"the board rejected the request{tail}")
    return HarnessError(f"the board refused `{act}`{tail or ': no reason given'}")


# -- the one selection policy ---------------------------------------------------------------

class RoutedController:
    """The session's ``controller``: reboot and the oscillators go through the harness's MCC
    route when ``prefer`` says so, everything else (and everything on an older image) to the
    Debug USB or hub controller it wraps."""

    def __init__(self, session: Any, fallback: Any = None) -> None:
        self._session = session
        self._fallback = fallback

    @property
    def has_fallback(self) -> bool:
        """Is there a Debug USB or hub controller behind this one?"""
        return self._fallback is not None

    @property
    def observer(self) -> Any:
        return getattr(self._fallback, "observer", None)

    @observer.setter
    def observer(self, value: Any) -> None:
        if self._fallback is not None:
            self._fallback.observer = value

    def __getattr__(self, name: str) -> Any:
        fb = self.__dict__.get("_fallback")
        if fb is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(fb, name)

    def _harness(self, act: str) -> HarnessMcc | None:
        hm = getattr(self._session, "harness_mcc", None)
        if hm is None or recognise(hm._features()) is None:
            return None
        try:
            serves = hm.status().serves(act)
        except HarnessError:
            serves = False
        kind = prefer(act, harness_serves=serves, has_usb=self._fallback is not None,
                      has_hub=False)
        return hm if kind == PREFER_HARNESS else None

    def _other(self, what: str) -> Any:
        if self._fallback is None:
            raise UnavailableError(C.REBOOT_BOARD, f"{what}: no route to the MCC (no Debug USB, "
                                                   "no hub, and the harness has none)")
        return self._fallback

    def command(self, line: str, *, arm: bool = False) -> str:
        return self._other("an MCC command").command(line, arm=arm)

    def reboot(self, progress: Any = None, wait_s: float | None = None) -> dict | None:
        hm = self._harness("reboot")
        if hm is not None:
            return hm.reboot(progress, wait_s)
        return self._other("rebooting the board").reboot(progress=progress, wait_s=wait_s)

    def temperatures(self) -> Sequence[Reading]:
        hm = self._harness("temp")
        return hm.temperatures() if hm is not None else self._other("temperatures").temperatures()

    def oscillators(self) -> Sequence[Reading]:
        hm = self._harness("osc")
        return hm.oscillators() if hm is not None else self._other("oscillators").oscillators()


def make_harness_mcc_adapter(session: Any) -> HarnessMcc | None:
    """The pack hook: an adapter for any session with an Ethernet shell (the gate asks the
    image's features before it asks the board anything)."""
    if getattr(session, "shell", None) is None:
        return None
    return HarnessMcc(session)
