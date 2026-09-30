"""Consoles for ``screen`` and baud (lane L2): the docs/API.md "Week-plan additions → Consoles" routes.

| Method and path | Broker call | Gate |
|---|---|---|
| ``POST /boards/{bid}/consoles/{name}/pty`` | ``pty`` (idempotent) | none: it subscribes the console, like the console WebSocket |
| ``GET /boards/{bid}/consoles/{name}/pty`` | ``pty_info`` | none |
| ``DELETE /boards/{bid}/consoles/{name}/pty`` | ``close_pty`` | none |
| ``GET /boards/{bid}/consoles/{name}/baud`` | ``baud`` | the op gate; while a job runs, the last report (never HELD) |
| ``POST /boards/{bid}/consoles/{name}/baud`` ``{baud}`` | ``set_baud`` | the op gate (409 HELD while a job runs) |
| ``GET /boards/{bid}/consoles`` | ``names`` + ``consoles`` | as ``GET .../baud``; ``?rates=0`` never asks the board |

Reading an Ethernet console's rate asks the harness (one control connection,
cached for a few seconds by the broker), so it goes through the op gate like
every other read that talks to the board. The console list is polled by the
UI and never went through the gate, so while a job runs it answers from the
broker's last report instead of refusing. A rate change is a write: HELD while
a job runs, as the core routes do.

``create_app`` registers these before the core ``/boards/{bid:path}`` routes, so
this module's ``GET /boards/{bid}/consoles`` replaces the core one (it still
returns ``names``).

A console service without its own PTYs and rates (the demo engine behind
``harness-manager daemon start --demo``; a hub broker later) still gets PTYs:
``_FallbackPtys`` bridges its ``subscribe`` streams the same way, and its rates
are reported as unknown. So ``screen`` works on the demo boards too.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from harness_manager.core.errors import AbsentError, HeldError, UnavailableError, UsageError
from harness_manager.core.events import Event
from harness_manager.services import pty as _pty

from . import console_access
from .app import _JSON, JsonBody, RouteContext, _obj, ok

log = logging.getLogger(__name__)

NO_RATES = "this console service does not report rates"


class _FallbackPtys:
    """PTYs for a console service that has ``subscribe`` but no ``pty`` of its own."""

    def __init__(self, daemon: Any) -> None:
        self._d = daemon
        self._lock = threading.Lock()
        self._mgr: _pty.PtyManager | None = None
        daemon.bus.subscribe("session.closed", lambda ev: self.close_board(ev.board_id))

    def _manager(self) -> _pty.PtyManager:
        with self._lock:
            if self._mgr is None:
                self._mgr = _pty.PtyManager(
                    lambda topic, bid, data: self._d.bus.publish(Event(topic, bid, data)),
                    lambda port, baud: log.debug("console %s of %s: PTY speed %d ignored (%s)",
                                                 port.name, port.board_id, baud, NO_RATES))
            return self._mgr

    def pty(self, session: Any, name: str) -> dict[str, Any]:
        if not _pty.supported():
            raise _pty.unavailable()
        broker = self._d.engine.consoles
        names = list(broker.names(session))
        if name not in names:
            raise AbsentError(f"no console named {name!r}",
                              hint=f"consoles: {', '.join(names) or 'none'}")
        port = self._manager().open(session, name, lambda: broker.subscribe(session, name),
                                    kind="ethernet", baud=None)
        return {**port.info(), "command": _pty.screen_command(port.link)}

    def pty_info(self, board_id: str, name: str) -> dict[str, Any] | None:
        mgr = self._mgr
        port = mgr.get(board_id, name) if mgr is not None else None
        return {**port.info(), "command": _pty.screen_command(port.link)} if port else None

    def close_pty(self, board_id: str, name: str) -> bool:
        mgr = self._mgr
        return mgr.close(board_id, name) if mgr is not None else False

    def close_board(self, board_id: str) -> None:
        mgr = self._mgr
        if mgr is not None:
            mgr.close_board(board_id)

    def open_ptys(self) -> list[dict[str, Any]]:
        """As ``ConsoleBroker.open_ptys`` (lane OTA-D: the restart records them)."""
        mgr = self._mgr
        return [{"board_id": p.board_id, **p.info(), "command": _pty.screen_command(p.link)}
                for p in (mgr.ptys() if mgr is not None else [])]

    def announce_ptys(self, text_for: Any) -> list[str]:
        mgr = self._mgr
        done = []
        for p in (mgr.ptys() if mgr is not None else []):
            view = {"board_id": p.board_id, **p.info(), "command": _pty.screen_command(p.link)}
            if p.announce(text_for(view)):
                done.append(str(p.link))
        return done

    def baud(self, session: Any, name: str, **_kw: Any) -> dict[str, Any]:
        names = list(self._d.engine.consoles.names(session))
        if name not in names:
            raise AbsentError(f"no console named {name!r}",
                              hint=f"consoles: {', '.join(names) or 'none'}")
        return {"name": name, "kind": _kind(name), "baud": None, "source": "unknown",
                "settable": False, "reason": NO_RATES, "choices": []}

    def set_baud(self, session: Any, name: str, baud: int) -> dict[str, Any]:
        raise UnavailableError("console_baud", self.baud(session, name)["reason"])

    def consoles(self, session: Any, **_kw: Any) -> list[dict[str, Any]]:
        bid = session.candidate.board_id
        rows = []
        for name in self._d.engine.consoles.names(session):
            port = self.pty_info(bid, name)
            rows.append({"name": name, "kind": _kind(name), "baud": None, "settable": False,
                         "source": "unknown", "reason": NO_RATES,
                         "pty": port["path"] if port else None})
        return rows


def _kind(name: str) -> str:
    """A console service that says nothing: the board's USB serial lanes by their names."""
    return "serial" if name.startswith(("fpga_uart", "mcc", "shell")) else "ethernet"


def _flag(request: Request, key: str, default: bool) -> bool:
    raw = request.query_params.get(key)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise UsageError(f"{key} must be true or false, not {raw!r}")


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api

    fallback = _FallbackPtys(d)
    d.fallback_ptys = fallback            # lane OTA-D: the restart records and reopens PTYs

    def broker_call(name: str) -> Any:
        """The console service's own call, or the fallback's (a service stub is refused)."""
        broker = d.engine.consoles
        reason = getattr(broker, "reason", None)
        if reason is not None:                       # a stub: the service did not load
            raise UnavailableError("console_dut", reason)
        fn = getattr(broker, name, None)
        return fn if callable(fn) else getattr(fallback, name)

    def offline_reason(bid: str) -> str:
        job = d.gates.busy(bid)
        return (f"{job.describe()} is running on the board; the rate is read when it finishes"
                if job is not None else "")

    def rates(bid: str, fn: Any, *args: Any) -> Any:
        """``fn(*args, live=...)``: live through the op gate, or the last report during a job."""
        why = offline_reason(bid)
        if not why:
            try:
                with d.gates.op(bid):
                    return fn(*args, live=True)
            except HeldError:
                why = offline_reason(bid)
                if not why:
                    raise
        return fn(*args, live=False, offline_reason=why)

    # -- PTYs --------------------------------------------------------------------------------

    @api.post("/boards/{bid:path}/consoles/{name}/pty")
    def pty_open(bid: str, name: str) -> JSONResponse:
        session = ctx.board(bid)
        return _JSON(ok(board_id=bid, **broker_call("pty")(session, name)))

    @api.get("/boards/{bid:path}/consoles/{name}/pty")
    def pty_get(bid: str, name: str) -> JSONResponse:
        session = ctx.board(bid)
        key = d.engine.consoles.resolve(session, name)[0] \
            if callable(getattr(d.engine.consoles, "resolve", None)) else name
        return _JSON(ok(board_id=bid, pty=broker_call("pty_info")(bid, key)))

    @api.delete("/boards/{bid:path}/consoles/{name}/pty")
    def pty_close(bid: str, name: str) -> JSONResponse:
        session = ctx.board(bid)
        key = d.engine.consoles.resolve(session, name)[0] \
            if callable(getattr(d.engine.consoles, "resolve", None)) else name
        closed = broker_call("close_pty")(bid, key)
        return _JSON(ok(board_id=bid, name=key, closed=closed))

    # -- baud -----------------------------------------------------------------------------------

    @api.get("/boards/{bid:path}/consoles/{name}/baud")
    def baud_get(bid: str, name: str) -> JSONResponse:
        session = ctx.board(bid)
        row = rates(bid, broker_call("baud"), session, name)
        return _JSON(ok(board_id=bid, **row))

    @api.post("/boards/{bid:path}/consoles/{name}/baud")
    def baud_set(bid: str, name: str, body: JsonBody = None) -> JSONResponse:
        session = ctx.board(bid)
        b = _obj(body)
        if "baud" not in b:
            raise UsageError("the request needs 'baud'", hint='e.g. {"baud": 115200}; 0 goes back '
                                                              "to the console's default rate")
        baud = b["baud"]
        if isinstance(baud, bool) or not isinstance(baud, int):
            raise UsageError(f"baud must be a whole number, not {baud!r}")
        setter = broker_call("set_baud")
        with d.gates.op(bid):
            result = setter(session, name, baud)
        return _JSON(ok(board_id=bid, **result))

    # -- the richer console list --------------------------------------------------------------------

    @api.get("/boards/{bid:path}/consoles")
    def console_list(bid: str, request: Request) -> JSONResponse:
        session = ctx.board(bid)
        names = list(d.engine.consoles.names(session))
        rows_fn = broker_call("consoles")
        if _flag(request, "rates", True):
            rows = rates(bid, rows_fn, session)
        else:
            rows = rows_fn(session, live=False,
                           offline_reason="not read: the request asked for names only (?rates=0)")
        # --- ui2 api-hub (G1b, additive): who may type here (console_access) ---
        rows = console_access.with_access(d, bid, rows)
        return _JSON(ok(board_id=bid, names=names, consoles=rows))
