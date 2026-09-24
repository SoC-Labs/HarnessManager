"""Fabric debug over XVC in the daemon API (lane XVC-CORE, X3), loaded through ``app.EXTENSIONS``.

docs/API.md "Fabric debug over XVC" (the routes; bearer auth and the error envelope as
everywhere):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/xvc`` | ``XvcStatus``: ``{state, open, mode, relay_port, hw_server_port, hw_server_pid, hw_server, url, attached, board_slot, reach, ltx, warnings, scope, rm_id, rm_name, reason, detail}`` |
| ``POST /boards/{bid}/xvc/open`` ``{byo?}`` | 202 job ``xvc_open``; the result is the ``XvcStatus`` |
| ``POST /boards/{bid}/xvc/close`` | ``XvcStatus`` (``down``) |
| ``GET /boards/{bid}/xvc/tcl?byo=`` | ``{tcl, url, ltx, which, mode, scope, open}`` |
| ``GET /boards/{bid}/xvc/ltx?which=auto|rm|static|full&format=file|json`` | the probes file (``application/octet-stream``, a download), or with ``format=json`` ``{which, name, path, crc_ok, source, vivado}`` |

Rules:

- **Scope.** Every status carries ``scope``: XVC here is the reconfigurable
  partition's debug chain only, never whole-device JTAG.
- **The lease holder only.** ``open`` of a board behind a hub asks the hub (the lease
  service of ``hub_api``, shared) and refuses anyone but the holder: 409 HELD naming
  the holder, before any job starts.
- **Gates.** ``open`` is a board job (409 HELD while another job runs, and it holds the
  board while hw_server starts). ``close`` takes the board gate. ``GET`` routes never
  touch the board while a session is open (the session keeps the loaded design's
  probes files); with none open, ``tcl``/``ltx`` read the board's identity once under
  the gate.
- **Swaps.** The engine's XVC service drops the board's slot on ``deploy.started`` and
  re-attaches on ``deploy.done`` (a fresh hw_server); ``xvc.state`` events say so.

Events: ``xvc.state`` (docs/CONTRACTS.md).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Response

from harness_manager.core import capabilities as C
from harness_manager.core.errors import UsageError

from .app import _JSON, JsonBody, RouteContext, _bool, _obj, ok

WHICH = ("auto", "rm", "static", "full")
FORMATS = ("file", "json")


def _flag(value: str | None, name: str) -> bool | None:
    if value is None or value == "":
        return None
    low = value.strip().lower()
    if low in ("1", "true", "yes"):
        return True
    if low in ("0", "false", "no"):
        return False
    raise UsageError(f"{name} must be true or false, not {value!r}")


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api

    def service() -> Any:
        svc = d.engine.xvc
        # Share hub_api's lease service (one cache, one view of "mine"): X6.
        if getattr(svc, "leases", "absent") is None and getattr(d, "leases", None) is not None:
            svc.leases = d.leases
        return svc

    def status_body(bid: str, st: Any) -> dict[str, Any]:
        data = st.to_json() if hasattr(st, "to_json") else dict(st)
        return {"board_id": bid, **data}

    def is_open(bid: str, s: Any) -> bool:
        return bool(getattr(service().status(s), "open", False))

    @api.post("/boards/{bid:path}/xvc/open")
    def xvc_open(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        byo = _bool(_obj(body), "byo", False)
        svc = service()
        with d.gates.op(bid):                               # 409 HELD while a job runs
            if getattr(s, "xvc", None) is None:
                ctx.require(s, "xvc", C.DEBUG_FABRIC)       # 422 with the pack's reason
            # Every refusal before any job: the lease (409 HELD naming the holder), no XVC
            # on this image or no hw_server (422), already open (409 ALREADY).
            svc.check_open(s, byo=byo)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress("starting", 0, 0)
            st = svc.open(s, byo=byo)
            progress(st.state, 0, 0)
            return status_body(bid, st)

        return ctx.accepted(d.jobs.submit("xvc_open", bid, run))

    @api.post("/boards/{bid:path}/xvc/close")
    def xvc_close(bid: str) -> Any:
        s = ctx.board(bid)
        with d.gates.op(bid):
            st = service().close(s, reason="closed")
        return _JSON(ok(**status_body(bid, st)))

    @api.get("/boards/{bid:path}/xvc/tcl")
    def xvc_tcl(bid: str, byo: str | None = None) -> Any:
        s = ctx.board(bid)
        want = _flag(byo, "byo")
        svc = service()
        if is_open(bid, s):
            out = svc.tcl(s, byo=want)
        else:
            with d.gates.op(bid):                           # read the loaded design once
                out = svc.tcl(s, byo=want, refresh=True)
        return _JSON(ok(board_id=bid, **out))

    @api.get("/boards/{bid:path}/xvc/ltx")
    def xvc_ltx(bid: str, which: str = "auto", format: str = "file") -> Any:  # noqa: A002
        if which not in WHICH:
            raise UsageError(f"which must be one of {', '.join(WHICH)}, not {which!r}")
        if format not in FORMATS:
            raise UsageError(f"format must be one of {', '.join(FORMATS)}, not {format!r}")
        s = ctx.board(bid)
        svc = service()
        if is_open(bid, s):
            item = svc.ltx(s, which)
        else:
            with d.gates.op(bid):
                item = svc.ltx(s, which, refresh=True)
        if format == "json":
            return _JSON(ok(board_id=bid, **item))
        from pathlib import Path

        path = Path(item["path"])
        data = path.read_bytes()
        return Response(data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{path.name}"',
                                 "X-Ltx-Which": str(item.get("which") or which)})

    @api.get("/boards/{bid:path}/xvc")
    def xvc_status(bid: str) -> Any:
        s = ctx.board(bid)
        svc = service()
        if is_open(bid, s) or svc.identity_known(s):
            return _JSON(ok(**status_body(bid, svc.status(s))))
        with d.gates.op(bid):                               # the first look: read identity once
            return _JSON(ok(**status_body(bid, svc.status(s, refresh=True))))
