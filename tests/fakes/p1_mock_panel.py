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

Lane P3 (the web UI) adds, additively: ``set_touch(bid, ok, bus_lost=, recoveries=)`` and
``set_rows(bid, rows, banner=)`` publish ``panel.state`` as the daemon does when they change;
``leases`` (the mock's ``LeaseRequestSim``) is told of a tap on the request banner, as
presence tells the lease service (CCR PANEL-1), and ``panel.tap`` then carries ``notify``
and ``request``; ``attach(engine)`` gives the REAL daemon's demo sessions these adapters,
so a browser test runs over both servers.
"""

from __future__ import annotations

import contextlib
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
    touch_health,
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
        stats = {"touch_ok": b["touch_ok"], "touch_bus_lost": b["touch_lost"],
                 "touch_recoveries": b["touch_rec"]}       # the additive stats keys
        if self.support().source == SOURCE_REBUILT:
            return PanelState(owner=b["owner"], touch=touch_health(stats),
                              source=SOURCE_REBUILT, observed_at=now,
                              note="rebuilt from what Harness Manager read, not read from "
                                   "the panel")
        events = tuple(PanelEvent(seq=s, on=on, ms_ago=int((now - at) * 1000), at=at)
                       for s, on, at in b["ring"])
        sessions = (PanelSession(SID, default_who(), "owner", 3.0, mine=True),
                    *b["others"])
        return PanelState(page=b["page"], owner=b["owner"], card="nanosoc [A]",
                          banner=b["banner"],
                          touch=touch_health(stats, {"present": True, "cal": True}),
                          sessions=sessions, count=len(sessions), seq=b["seq"], events=events,
                          source=SOURCE_PANEL, observed_at=now)

    def frame(self) -> PanelFrame:
        if self.support().source == SOURCE_REBUILT:
            ident = self.sim.engine.info(self.bid).identity
            return rebuilt_frame(name=getattr(self.sim.engine.session(self.bid).candidate,
                                              "name", ""), identity=ident,
                                 host=self.bid.split("@", 1)[-1].rsplit(":", 1)[0],
                                 owner=self.sim.boards[self.bid]["owner"], wall=time.time())
        b = self.sim.boards[self.bid]
        return PanelFrame(rows=b["rows"] or LINUX_STATUS_ROWS, roles=b["roles"] or "t" * 600,
                          source=SOURCE_PANEL, observed_at=time.time())

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
        #: the lease side of a request tap (``notify_holder(bid, seq=, at=)``), or None
        self.leases: Any = None

    def board(self, bid: str) -> dict[str, Any]:
        with self._mu:
            return self.boards.setdefault(bid, {"page": "status", "owner": "harness", "seq": 0,
                                                "ring": [], "others": (), "touch_ok": None,
                                                "touch_lost": None, "touch_rec": None,
                                                "banner": "", "rows": None, "roles": "",
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
        at = time.time()
        b["ring"] = [*b["ring"], (b["seq"], on, at)][-8:]
        data: dict[str, Any] = {"seq": b["seq"], "kind": "tap", "on": on, "ms_ago": 0,
                                "at": at, "notify": ""}
        if on == "request" and self.leases is not None:
            # presence._notify_holder: tell the lease side; it never releases (decision P2)
            out = self.leases.notify_holder(bid, seq=b["seq"], at=at) or {}
            data.update(notify="holder" if out.get("notified") else "",
                        request=out.get("request"))
        self.engine.bus.publish(Event("panel.tap", bid, data))
        return b["seq"]

    def _state_changed(self, bid: str) -> None:
        self.engine.bus.publish(Event("panel.state", bid,
                                      state_event_data(self.adapter(bid).state())))

    def set_owner(self, bid: str, owner: str) -> None:
        self.board(bid)["owner"] = owner
        self._state_changed(bid)

    def set_rows(self, bid: str, rows: tuple[str, ...] | None, *, banner: str = "",
                 roles: str = "") -> None:
        """What the glass shows now (None: the healthy status page) and its banner."""
        b = self.board(bid)
        b["rows"] = tuple(r.ljust(40)[:40] for r in rows) if rows else None
        b["roles"], b["banner"] = roles, banner
        self._state_changed(bid)

    def watcher(self, bid: str, who: str = "bob@srv03340", role: str = "watch") -> None:
        b = self.board(bid)
        b["others"] = (*b["others"], PanelSession("w" + str(len(b["others"])), who, role, 12.0))

    def set_touch(self, bid: str, ok: bool | None, *, bus_lost: int | None = None,
                  recoveries: int | None = None) -> None:
        b = self.board(bid)
        b["touch_ok"], b["touch_lost"], b["touch_rec"] = ok, bus_lost, recoveries
        self._state_changed(bid)

    def attach(self, engine: Any) -> None:
        """Give every board ``engine`` opens from now on this simulated panel adapter
        (``session.panel``), so the REAL daemon's front-panel routes answer for a demo board.
        The daemon's presence never tracks it (it looked before this ran), so it sends no
        hello: the tests drive the events through these knobs instead."""
        def opened(ev: Event) -> None:
            with contextlib.suppress(Exception):
                engine.session(ev.board_id).panel = self.adapter(ev.board_id)
        engine.bus.subscribe("session.opened", opened)


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
