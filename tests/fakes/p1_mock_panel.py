"""The front-panel routes (docs/API.md "Front panel", ``panel_api.py``) in the T14 mock daemon.

Lane P3 builds the web UI's Front panel card and the Board-tile line against this. The
mock's boards are ``DemoEngine``'s, and none of them has a panel adapter, so ``PanelSim``
gives each open board a simulated one that follows ``harness_manager_mps3.panel``:

- a board whose features include ``panel`` (``sim.linux(bid)`` adds ``presence``, ``panel``
  and ``locate``) answers like the Linux harness: page, owner, card, sessions, taps, the
  panel's own text grid, and Identify;
- any other board answers like bare metal (v0.11): the owner only, a REBUILT mirror, and
  Identify unavailable with the reason.

The body is built by the product's own ``services.presence.read_panel`` and the rebuilt
mirror by ``harness_manager_mps3.panel.rebuilt_frame``, so the mock and the daemon cannot
disagree on their shape. Knobs publish the events the daemon would: ``tap(bid, on)`` ->
``panel.tap``, ``set_owner(bid, owner)`` -> ``panel.state``; Identify -> ``panel.locate``.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from fastapi import Body, FastAPI

from harness_manager.cli.output import jsonable
from harness_manager.core import capabilities as C
from harness_manager.core.errors import UnavailableError
from harness_manager.core.events import Event
from harness_manager.core.panel import (
    SOURCE_PANEL,
    SOURCE_REBUILT,
    PanelEvent,
    PanelFrame,
    PanelSession,
    PanelState,
    PanelSupport,
    TouchHealth,
)
from harness_manager.services.presence import (
    NO_ADAPTER,
    check_seconds,
    default_who,
    read_panel,
    state_event_data,
)
from harness_manager_mps3.capabilities import NEEDS_LOCATE, NEEDS_PANEL, NEEDS_PRESENCE
from harness_manager_mps3.panel import rebuilt_frame

from .clcd_panel_shell import LINUX_STATUS_ROWS, PANEL_FEATURES

API = "/api/v1"
SID = "m0c4d00d"


class SimPanel:
    """One board's simulated panel adapter (``core.panel.PanelAdapter``)."""

    def __init__(self, sim: PanelSim, bid: str) -> None:
        self.sim = sim
        self.bid = bid

    def _features(self) -> frozenset[str]:
        return frozenset(self.sim.engine.info(self.bid).identity.features)

    def support(self) -> PanelSupport:
        f = self._features()
        panel = "panel" in f
        return PanelSupport(front_panel="" if panel or "clcd_kvm" in f else NEEDS_PANEL,
                            presence="" if "presence" in f else NEEDS_PRESENCE,
                            locate="" if "locate" in f else NEEDS_LOCATE,
                            source=SOURCE_PANEL if panel else SOURCE_REBUILT)

    def state(self) -> PanelState:
        b = self.sim.boards[self.bid]
        now = time.time()
        if self.support().source == SOURCE_REBUILT:
            return PanelState(owner=b["owner"], source=SOURCE_REBUILT, observed_at=now,
                              note="rebuilt from what Harness Manager read, not read from "
                                   "the panel")
        events = tuple(PanelEvent(seq=s, on=on, ms_ago=int((now - at) * 1000), at=at)
                       for s, on, at in b["ring"])
        sessions = (PanelSession(SID, default_who(), "owner", 3.0, mine=True),
                    *b["others"])
        return PanelState(page=b["page"], owner=b["owner"], card="nanosoc [A]",
                          touch=TouchHealth(present=True, cal=True, ok=b["touch_ok"]),
                          sessions=sessions, count=len(sessions), seq=b["seq"], events=events,
                          source=SOURCE_PANEL, observed_at=now)

    def frame(self) -> PanelFrame:
        if self.support().source == SOURCE_REBUILT:
            ident = self.sim.engine.info(self.bid).identity
            return rebuilt_frame(name=getattr(self.sim.engine.session(self.bid).candidate,
                                              "name", ""), identity=ident,
                                 host=self.bid.split("@", 1)[-1].rsplit(":", 1)[0],
                                 owner=self.sim.boards[self.bid]["owner"], wall=time.time())
        return PanelFrame(rows=LINUX_STATUS_ROWS, roles="t" * 600, source=SOURCE_PANEL,
                          observed_at=time.time())

    def locate(self, seconds: int, who: str) -> float:
        why = self.support().locate
        if why:
            raise UnavailableError(C.LOCATE, why)
        until = time.time() + seconds
        self.sim.boards[self.bid]["locate_until"] = until if seconds else 0.0
        return until


class PanelSim:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.boards: dict[str, dict[str, Any]] = {}
        self._mu = threading.Lock()

    def board(self, bid: str) -> dict[str, Any]:
        with self._mu:
            return self.boards.setdefault(bid, {"page": "status", "owner": "harness", "seq": 0,
                                                "ring": [], "others": (), "touch_ok": None,
                                                "locate_until": 0.0})

    def adapter(self, bid: str) -> SimPanel:
        self.board(bid)
        return SimPanel(self, bid)

    # -- knobs ----------------------------------------------------------------------------

    def linux(self, bid: str, on: bool = True) -> None:
        """Make a demo board a Linux harness with the front-panel verbs (or take them away)."""
        feats = [f for f in self.engine.info(bid).identity.features if f not in PANEL_FEATURES]
        self.engine.set_features(bid, feats + (list(PANEL_FEATURES) if on else []))

    def tap(self, bid: str, on: str = "request") -> int:
        b = self.board(bid)
        b["seq"] += 1
        b["ring"] = [*b["ring"], (b["seq"], on, time.time())][-8:]
        self.engine.bus.publish(Event("panel.tap", bid, {
            "seq": b["seq"], "kind": "tap", "on": on, "ms_ago": 0, "at": time.time(),
            "notify": ""}))
        return b["seq"]

    def set_owner(self, bid: str, owner: str) -> None:
        self.board(bid)["owner"] = owner
        self.engine.bus.publish(Event("panel.state", bid,
                                      state_event_data(self.adapter(bid).state())))

    def watcher(self, bid: str, who: str = "bob@srv03340", role: str = "watch") -> None:
        b = self.board(bid)
        b["others"] = (*b["others"], PanelSession("w" + str(len(b["others"])), who, role, 12.0))

    def set_touch(self, bid: str, ok: bool | None) -> None:
        self.board(bid)["touch_ok"] = ok


def register(app: FastAPI, state: Any, sim: PanelSim, ok: Any) -> None:
    """Add the three front-panel routes to the mock app."""

    def presence(bid: str, adapter: SimPanel) -> dict[str, Any]:
        why = adapter.support().presence
        return {"active": not why, "reason": why, "sid": "" if why else SID,
                "last_hello_at": None if why else time.time() - 3, "sent": 0 if why else 1,
                "ridden": 0, "skipped": 0, "last_error": "", "interval_s": 30.0}

    @app.get(f"{API}/boards/{{bid}}/panel/frame")
    def panel_frame(bid: str) -> dict[str, Any]:
        state.session(bid)
        state.jobs.gate(bid)
        adapter = sim.adapter(bid)
        why = adapter.support().front_panel
        if why:
            raise UnavailableError(C.FRONT_PANEL, why or NO_ADAPTER)
        return ok(board_id=bid, **jsonable(adapter.frame()))

    @app.get(f"{API}/boards/{{bid}}/panel")
    def panel_state(bid: str) -> dict[str, Any]:
        state.session(bid)
        state.jobs.gate(bid)
        adapter = sim.adapter(bid)
        body = read_panel(SimpleNamespace(panel=adapter))
        body["presence"] = presence(bid, adapter)
        until = sim.board(bid)["locate_until"]
        if until > time.time():
            body["identify"]["until"] = until
        return ok(board_id=bid, **jsonable(body))

    @app.post(f"{API}/boards/{{bid}}/identify")
    def identify(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:  # noqa: B008
        state.session(bid)
        seconds = check_seconds((body or {}).get("seconds"))
        state.jobs.gate(bid)
        until = sim.adapter(bid).locate(seconds, default_who())
        state.engine.bus.publish(Event("panel.locate", bid, {
            "state": "on" if seconds else "off", "until": until, "seconds": seconds,
            "who": default_who()}))
        return ok(board_id=bid, until=until if seconds else time.time(), seconds=seconds)
