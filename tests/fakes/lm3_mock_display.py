"""The Live display routes (docs/API.md "Live display", ``display_api.py``) in the T14 mock daemon.

Lane LM4 builds the web canvas against this. The mock runs the REAL routes: it calls
``daemon/display_api.register`` with a ``RouteContext`` whose daemon side is the mock's (its
sessions, its token, its bus), so the mock and the daemon cannot disagree on a display
route, a frame or a refusal. The board side is ``DisplaySim``: the pack's
``display_adapter`` hook (lane LM2's shape) hands each open demo board an adapter over its
own LM1 ``FakeLcdMirror`` on 127.0.0.1:0, started on first use:

- every demo board: live, the clcd_demo test card repainting about three times a second,
  in the snooper's ``hw`` mode (exact);
- behind a hub (``WeekPlanSim.behind_hub``): the lease holder only (D3). Another holder, or
  none, is refused with the reason.

Knobs: ``refuse(bid, reason)`` / ``allow(bid)`` (``display_reason``), ``no_display(bid)``
(the hook returns None: a pack with no live display for that board), ``mirror(bid)`` (the
board's ``FakeLcdMirror``, to edit its picture or ``handover`` the panel), ``freeze(bid)``
(the card stops counting), ``close()`` (every fake board and upstream).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, FastAPI

from harness_manager.daemon import display_api
from harness_manager.daemon.app import RouteContext
from harness_manager.services.display import DisplayService

from .lm1_fake_lcd_mirror import CardAnimator, FakeLcdMirror, FakePanel

API = "/api/v1"
LEASE_ONLY = "the live display is for the lease holder only"


class SimDisplayAdapter:
    """One board's ``DisplayAdapter`` (LM2's shape) over the sim's fake board."""

    def __init__(self, sim: DisplaySim, bid: str) -> None:
        self.sim = sim
        self.bid = bid

    def display_reason(self) -> str:
        return self.sim.reason(self.bid)

    def display_connect(self) -> Any:
        return self.sim.mirror(self.bid).connect()

    def display_release(self) -> None:
        pass


class _EveryPack:
    """``engine.packs()`` for the routes: every pack name has the sim's hook."""

    def __init__(self, sim: DisplaySim) -> None:
        self.sim = sim

    def get(self, _name: str, _default: Any = None) -> Any:
        return self.sim


class DisplaySim:
    """The mock's boards' lcd_mirror services, and the hook that reaches them."""

    def __init__(self, week_plan: Any) -> None:
        self.week_plan = week_plan                     # l3_week_plan.WeekPlanSim (its hubs)
        self._lock = threading.Lock()
        self._mirrors: dict[str, FakeLcdMirror] = {}
        self._anims: dict[str, CardAnimator] = {}
        self._reasons: dict[str, str] = {}
        self._none: set[str] = set()
        self.service: DisplayService | None = None

    # -- the pack hook (lane LM2's name) --------------------------------------------------------

    def display_adapter(self, session: Any) -> SimDisplayAdapter | None:
        bid = session.candidate.board_id
        with self._lock:
            if bid in self._none:
                return None
        return SimDisplayAdapter(self, bid)

    def reason(self, bid: str) -> str:
        with self._lock:
            forced = self._reasons.get(bid)
        if forced:
            return forced
        hub = getattr(self.week_plan, "hubs", {}).get(bid)
        if hub is not None:
            lease = hub.get("lease")
            if not lease:
                return f"{LEASE_ONLY}, and nobody holds {hub['target']}"
            if not lease.get("mine"):
                return f"{LEASE_ONLY}: {lease['holder']} holds {hub['target']}"
        return ""

    # -- knobs ------------------------------------------------------------------------------------

    def refuse(self, bid: str, reason: str) -> None:
        with self._lock:
            self._reasons[bid] = reason

    def allow(self, bid: str) -> None:
        with self._lock:
            self._reasons.pop(bid, None)
            self._none.discard(bid)

    def no_display(self, bid: str) -> None:
        with self._lock:
            self._none.add(bid)

    def mirror(self, bid: str) -> FakeLcdMirror:
        with self._lock:
            board = self._mirrors.get(bid)
            if board is None:
                anim = CardAnimator(period_s=1 / 3)
                board = FakeLcdMirror(FakePanel(), mode="hw", animate=anim).start()
                self._mirrors[bid], self._anims[bid] = board, anim
            return board

    def freeze(self, bid: str, frozen: bool = True) -> None:
        self.mirror(bid)
        self._anims[bid].frozen = frozen

    def close(self) -> None:
        if self.service is not None:
            self.service.shutdown()
        with self._lock:
            boards = list(self._mirrors.values())
            self._mirrors.clear()
        for board in boards:
            board.close()


def register(app: FastAPI, state: Any, week_plan: Any) -> DisplaySim:
    """Add the three Live display routes to the mock app; returns the sim (its knobs)."""
    sim = DisplaySim(week_plan)
    sim.service = DisplayService(state.engine.bus)
    daemon = SimpleNamespace(
        engine=SimpleNamespace(packs=lambda: _EveryPack(sim)), bus=state.engine.bus,
        check_token=lambda presented: bool(presented) and presented == state.token,
        display=sim.service, close=lambda: None)
    api, wsr = APIRouter(prefix=API), APIRouter(prefix=API)
    display_api.register(RouteContext(daemon=daemon, api=api, wsr=wsr, board=state.session,
                                      require=None, accepted=None))
    app.include_router(wsr)
    app.include_router(api)
    return sim
