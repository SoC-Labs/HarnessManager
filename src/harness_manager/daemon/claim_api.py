"""The Linux harness's SSH claim in the daemon API (lane LINUX-CLAIM), loaded through
``app.EXTENSIONS``.

docs/API.md "SSH claim (Linux harness)" (bearer auth and the error envelope as everywhere):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/claim?refresh=`` | ``{board_id, claim}``: ``claim`` is ``BoardInfo.claim`` (null on bare metal). ``refresh=true`` asks the board now, through the hub when there is one |
| ``POST /boards/{bid}/claim`` ``{confirm: true, key?, adopt?, replace_host_key?}`` | 202 job ``claim``; the result is ``{board_id, claim}`` with ``claim.action`` ``claimed`` or ``adopted`` |
| ``GET /boards/{bid}/ssh?command=`` | ``{board_id, argv}``: the pinned ``ssh [-J HUB] -l root BOARD`` (nothing is run) |

Rules:

- **Never automatic, always confirmed.** ``POST`` without ``"confirm": true`` is 409 REFUSED
  before anything reaches the board.
- **The lease holder only**, on a board behind a hub (hub_api's lease service, shared): 409
  HELD naming the holder, before the job starts.
- ``POST`` is a board job (409 HELD while another job runs). ``GET`` never takes the board's
  control port: identify is UDP, and the hub round trip happens only with ``refresh``.

Events: ``board.claim`` ``{state, claimed, host_key, route, action?}``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import RefusedError, UnavailableError

from .app import _JSON, JsonBody, RouteContext, _bool, _obj, ok
from .xvc_api import _flag

CAPABILITY = "ssh_claim"


def _opt_str(b: dict[str, Any], key: str) -> str | None:
    value = b.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        from harness_manager.core.errors import UsageError

        raise UsageError(f"{key} must be a string")
    return value


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api

    def service() -> Any:
        svc = getattr(d.engine, "board_claim", None)
        if svc is None:                                     # an engine without one (the demo)
            raise UnavailableError(CAPABILITY, "this engine has no SSH claim service")
        if getattr(svc, "leases", "absent") is None and getattr(d, "leases", None) is not None:
            svc.leases = d.leases                           # hub_api's: one view of "mine"
        return svc

    @api.get("/boards/{bid:path}/claim")
    def claim_status(bid: str, refresh: str | None = None) -> Any:
        s = ctx.board(bid)
        svc = service()
        st = svc.refresh(s) if _flag(refresh, "refresh") else svc.status(s)
        return _JSON(ok(board_id=bid, claim=st))

    @api.post("/boards/{bid:path}/claim")
    def claim(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        if not _bool(b, "confirm", False):
            raise RefusedError("a claim needs a confirmation: it gives your key root on the "
                               "board and locks its slots to that key",
                               hint="send {\"confirm\": true}")
        key = _opt_str(b, "key")
        adopt = _bool(b, "adopt", False)
        replace = _bool(b, "replace_host_key", False)
        svc = service()
        with d.gates.op(bid):                               # 409 HELD while a job runs
            svc.check_claimable(s)                          # 422 on a board with no claim
            svc.check_lease(s)                              # 409 HELD naming the holder

        def run(progress: Callable[[str, int, int], None]) -> Any:
            st = svc.claim(s, confirm=True, key=key, adopt=adopt, replace_host_key=replace,
                           progress=lambda text: progress(text, 0, 0))
            return {"board_id": bid, "claim": st}

        return ctx.accepted(d.jobs.submit("claim", bid, run))

    @api.get("/boards/{bid:path}/ssh")
    def ssh_argv(bid: str, command: str | None = None) -> Any:
        import shlex

        s = ctx.board(bid)
        argv = service().ssh_argv(s, shlex.split(command) if command else ())
        return _JSON(ok(board_id=bid, argv=argv))
