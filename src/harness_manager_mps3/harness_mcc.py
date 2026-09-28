"""The board controller (MCC) THROUGH THE HARNESS: net-protocol v0.18 (mint 4). FIX-PACK-2 item 2.

Today Harness Manager reaches the MCC over the Debug USB (``mcc.py``) or on the hub
(``hub_mcc.py``). From v0.18 the harness can reach it too, and says how:

- **features** (``version.features``): ``mccif`` (the in-fabric SCC route: the FPGA's own
  link to the MCC) and ``mcc_local`` (the USB loopback route: the harness's own cable to the
  MCC's USB console). ``mcc`` was this pack's name for the same idea before v0.18 (the J7
  mod); no image lists it, and it is recognised as an alias;
- **ONE verb**, ``{"op": "mcc", "act": ACT, ...}``, replying ``{"ok", "op": "mcc", "act",
  "route": "scc" | "loopback" | "none", ...}``, with the acts ``status``, ``temp``, ``osc``,
  ``osc_set`` (armed), ``probe``, ``mbox``, ``log`` and ``reboot`` (loopback only, a two-phase
  token);
- **the route in use is what ``mcc status`` says**, not the feature: a feature says the
  firmware has the verb; the route says whether the hardware path is there now (``none``: no
  SCC in this static, or no loopback cable).

What this module does now (the gate and the seam, lead's scope 2026-09-28):

- ``recognise(features)``: which harness MCC names an image lists (``HarnessMccSupport``);
- ``HarnessMcc.status()``: the ``mcc status`` read behind the feature, cached (``STATUS_TTL_S``;
  a failure ``STATUS_FAIL_TTL_S``), so ``info`` asks at most that often;
- ``HarnessMcc.capability_reasons``: ``reboot_board`` and ``clock_board`` through the harness
  need the feature AND a route that serves the act (``reboot``: loopback only). A capability
  another route gives (the Debug USB, the hub, a power plug) is never touched.

PENDING v0.18 (not implemented, by the lead's scope): ``osc_set`` and ``reboot`` over the
harness, and the ``temp``/``osc`` reads. They raise ``UnavailableError(..., "... pending
v0.18")``, and a capability that would need them says so. When one lands, add its act to
``IMPLEMENTED`` and the capability lights up on a board whose route serves it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import CapabilitySpec, satisfied_route
from harness_manager.core.errors import HarnessError, UnavailableError

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
#: The acts Harness Manager drives over the harness today. Everything else is PENDING.
IMPLEMENTED = frozenset({"status"})
PENDING = "pending v0.18"
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
    """``mcc status``: ``route`` is ``scc``, ``loopback``, ``none``, or ``""`` when the reply had
    no route this build knows (``raw`` keeps what it said)."""

    route: str
    raw: dict[str, Any] = field(default_factory=dict)

    def serves(self, act: str) -> bool:
        return act in ROUTE_ACTS.get(self.route, frozenset())


def parse_status(reply: dict[str, Any]) -> MccStatus:
    """A ``mcc status`` reply as ``MccStatus``; ``HarnessError`` for a refusal."""
    if not reply.get("ok"):
        err = str(reply.get("err") or reply.get("code") or "no reason given")
        if "unknown op" in err:
            raise UnavailableError(C.REBOOT_BOARD, f"this image lists a harness MCC feature but "
                                                   f"does not answer `mcc status` ({err})")
        raise HarnessError(f"the harness refused `mcc status`: {err}")
    route = reply.get("route")
    route = route.strip().lower() if isinstance(route, str) else ""
    return MccStatus(route if route in ROUTES else "", dict(reply))


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
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._session = session
        self._reader = reader or read_status
        self._clock = clock
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
            raise UnavailableError(C.REBOOT_BOARD, "this harness image does not reach the MCC "
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
        if act not in IMPLEMENTED:
            return (f"{what} through the harness's MCC route ({st.route}) is {PENDING} in "
                    f"Harness Manager (net-protocol v0.18 `mcc {{act: {act}}}`)")
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

    # -- the acts that are PENDING v0.18 -----------------------------------------------------

    def _pending(self, cap: str, act: str) -> UnavailableError:
        return UnavailableError(cap, f"{ACT_WHAT.get(act, act)} through the harness is {PENDING}"
                                     " in Harness Manager (net-protocol v0.18 "
                                     f"`mcc {{act: {act}}}`); use the Debug USB or the hub")

    def reboot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise self._pending(C.REBOOT_BOARD, "reboot")

    def set_osc(self, *args: Any, **kwargs: Any) -> Any:
        raise self._pending(C.CLOCK_BOARD, "osc_set")

    def oscillators(self) -> Any:
        raise self._pending(C.CLOCK_BOARD, "osc")

    def temperatures(self) -> Any:
        raise self._pending(C.TELEMETRY_TEMP, "temp")


def make_harness_mcc_adapter(session: Any) -> HarnessMcc | None:
    """The pack hook: an adapter for any session with an Ethernet shell (the gate asks the
    image's features before it asks the board anything)."""
    if getattr(session, "shell", None) is None:
        return None
    return HarnessMcc(session)
