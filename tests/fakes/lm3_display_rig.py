"""Lane LM3 test rig: the daemon's Live display routes, under a REAL uvicorn, over a fake board.

- ``FakeDisplayAdapter``: lane LM2's ``DisplayAdapter`` shape (LM1's ``TunnelSource``): a
  NEW connection to LM1's ``FakeLcdMirror`` per ``display_connect``; ``reason`` is what
  ``display_reason()`` says (D3's lease rule, the feature gate); counters for every call.
- ``DisplayPack``: a scriptable pack with the ``display_adapter(session)`` pack hook
  (LM2's name), returning ``adapter`` (None: no live display on this board).
- ``DisplayDaemon``: ``create_app`` under the daemon's own ``uvicorn_config`` (so
  permessage-deflate is off exactly as in the product), on 127.0.0.1:0.
- ``display_rig``: the three together, with the board open and a ``DisplayService`` of
  the test's timings and clock installed as the daemon's.
- ``Tab``: a browser tab over the real WebSocket: a ``ViewerModel`` it draws into, the
  status frames it saw, and whether it acks.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from harness_manager.core.display import DisplayUnavailable
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.server import uvicorn_config
from harness_manager.engine import Engine
from harness_manager.services.display import DisplayService, DisplayTimings
from tests.fakes.lm1_fake_lcd_mirror import FakeLcdMirror, ViewerModel
from tests.fakes.t1_fakes import FakePack, candidate
from tests.fakes.t13_daemon import LiveDaemon, state_dir, wait_for

BOARD = "fake@lcd-01"
#: Quick clocks for the tests that do not inject one (LM1's tunnel test's values; a grace
#: long enough that a test's next request finds the upstream still open).
FAST = DisplayTimings(grace_s=10.0, ping_s=0.1, stale_s=2.0, dead_s=5.0, tick_s=0.02,
                      backoff_s=(0.1, 0.2, 0.4), no_service_retry_s=0.5)


def bid_path(board_id: str = BOARD) -> str:
    return f"/api/v1/boards/{quote(board_id, safe='')}"


class FakeDisplayAdapter:
    """LM2's adapter shape (``harness_manager_mps3.display.Mps3Display``) over a
    ``FakeLcdMirror``: ``display_reason``, ``display_connect`` (a NEW connection each time;
    None board: refused for good; ``connect_error``: raised instead, as the MPS3 adapter's
    ``HeldError`` naming the holder), ``display_release``, ``use_leases``."""

    def __init__(self, board: FakeLcdMirror | None = None, reason: str = "") -> None:
        self.board = board
        self.reason = reason
        self.connect_error: BaseException | None = None
        self.leases_used: list[Any] = []
        self.leases_before_reason: bool | None = None
        self.reasons_asked = 0
        self.connects = 0
        self.released = 0

    def use_leases(self, leases: Any) -> None:
        self.leases_used.append(leases)

    def display_reason(self) -> str:
        if self.leases_before_reason is None:
            self.leases_before_reason = bool(self.leases_used)
        self.reasons_asked += 1
        return self.reason

    def display_connect(self) -> Any:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        if self.board is None:
            raise DisplayUnavailable("no lcd_mirror service (test)", retry_s=None)
        return self.board.connect()

    def display_release(self) -> None:
        self.released += 1


@dataclass
class DisplayPack(FakePack):
    """A fake pack with lane LM2's ``display_adapter`` hook."""

    adapter: Any = None
    hook_calls: int = 0
    sessions: list[Any] = field(default_factory=list)

    def display_adapter(self, session: Any) -> Any:
        self.hook_calls += 1
        self.sessions.append(session)
        return self.adapter


class DisplayDaemon(LiveDaemon):
    """T13's live daemon, served with the product's ``uvicorn_config`` (deflate off)."""

    def __init__(self, engine: Any, **app_kw: Any) -> None:
        import uvicorn

        super().__init__(engine, write_json=False, static_dir=None, **app_kw)
        self.server = uvicorn.Server(uvicorn_config(self.app, log_level="warning",
                                                    timeout_graceful_shutdown=2))

    @property
    def daemon(self) -> Any:
        return self.app.state.daemon

    def display_ws(self, board_id: str = BOARD, token: str | None = None, **query: str) -> str:
        qs = urlencode({"token": self.token if token is None else token, **query})
        return f"ws://127.0.0.1:{self.port}{bid_path(board_id)}/display/ws?{qs}"

    def events_ws(self, topics: str = "display.*") -> str:
        return f"ws://127.0.0.1:{self.port}/api/v1/events?" + urlencode(
            {"token": self.token, "topics": topics})


@dataclass
class Rig:
    daemon: DisplayDaemon
    engine: Engine
    pack: DisplayPack
    svc: DisplayService
    board_id: str = BOARD

    def status(self) -> dict[str, Any]:
        return self.svc.status(self.board_id)


@contextmanager
def display_rig(adapter: Any = None, *, timings: DisplayTimings = FAST,
                clock: Callable[[], float] | None = None) -> Iterator[Rig]:
    pack = DisplayPack(adapter=adapter)
    engine = Engine(EngineConfig(state_dir=state_dir()), packs={"fake": pack})
    svc = DisplayService(engine, timings=timings, clock=clock or time.monotonic)
    try:
        engine.open(candidate(BOARD), note="lm3")
        with DisplayDaemon(engine) as dm:
            dm.daemon.display = svc                  # the test's clocks (the route reads it)
            yield Rig(dm, engine, pack, svc)
    finally:
        svc.shutdown()
        engine.close_all()


def connect(url: str, **kw: Any) -> Any:
    """A websockets sync client: no proxy; permessage-deflate OFFERED, as every browser does."""
    import inspect

    from websockets.sync.client import connect as ws_connect

    if "legacy" in inspect.signature(ws_connect).parameters:
        kw.setdefault("legacy", True)            # a plain object, closed by the test
    kw.setdefault("open_timeout", 10)
    kw.setdefault("max_size", None)
    return ws_connect(url, proxy=None, **kw)


class Tab:
    """A browser tab on ``WS .../display/ws``: it draws each binary frame into a
    ``ViewerModel`` and, when ``ack``, sends ``{"ack": seq}`` after drawing."""

    def __init__(self, url: str, *, ack: bool = True) -> None:
        self.ws = connect(url)
        self.ack = ack
        self.vm = ViewerModel()
        self.statuses: list[dict[str, Any]] = []
        self.binaries: list[bytes] = []
        self.closed: tuple[int, str] | None = None

    def pump(self, timeout: float = 0.2, max_s: float = 1.0) -> int:
        """Take every frame that arrives within ``timeout`` of the last (for at most
        ``max_s``: a live board never goes quiet); binary frames drawn."""
        from websockets.exceptions import ConnectionClosed

        n = 0
        end = time.monotonic() + max_s
        while time.monotonic() < end:
            try:
                msg = self.ws.recv(timeout=timeout)
            except TimeoutError:
                return n
            except ConnectionClosed as exc:
                rcvd = exc.rcvd
                self.closed = (rcvd.code, rcvd.reason) if rcvd is not None else (1006, "")
                return n
            if isinstance(msg, str):
                self.statuses.append(json.loads(msg))
                continue
            self.binaries.append(bytes(msg))
            seq = self.vm.apply(bytes(msg))
            n += 1
            if self.ack:
                try:
                    self.send({"ack": seq})
                except ConnectionClosed:              # the server closed after this frame
                    pass
        return n

    def pump_until(self, pred: Callable[[Tab], Any], timeout: float = 20.0,
                   what: str = "the tab") -> Any:
        return wait_for(lambda: (self.pump(0.05), pred(self))[1], timeout=timeout, what=what)

    def send(self, obj: Any) -> None:
        self.ws.send(obj if isinstance(obj, str) else json.dumps(obj))

    @property
    def state(self) -> str:
        return self.statuses[-1]["state"] if self.statuses else ""

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001 - a test's cleanup
            pass

    def __enter__(self) -> Tab:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeLeases:
    """The hub API's lease service as the display reads it (``view(hub)``, ``LeaseService``)."""

    def __init__(self, holder: str = "", *, mine: bool = False, held: bool = True) -> None:
        self.holder, self.mine, self.held = holder, mine, held
        self.views = 0

    def view(self, hub: Any, **_kw: Any) -> dict[str, Any]:
        self.views += 1
        if not self.held:
            return {"lease": None}
        return {"lease": {"target": getattr(hub, "target", ""), "holder": self.holder,
                          "mine": self.mine}}


class FakeClock:
    """Monotonic seconds that move only when the test says (the grace test)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
