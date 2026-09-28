"""The board's network identity in the daemon API (lane BOARD-ID), loaded through
``app.EXTENSIONS``.

docs/API.md "Board identity" (bearer auth and the error envelope as everywhere):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/identity?refresh=`` | ``{board_id, identity}``: ``BoardInfo.net_identity`` read now (the board: one control-port read, or identify; the hub record once per session). ``refresh=true`` asks the hub again, and for its other targets |
| ``POST /boards/{bid}/identity`` ``{confirm, from_hub?, label?, ip?, mac?, hostname?, clear?, wait_s?}`` | 202 job ``identity``; the result is ``{board_id, action, changes, set, reboot, verified, identity, notes}`` |

Rules:

- **Never automatic.** ``confirm`` is the typed phrase (``identity.fix.phrase``: the new
  label, or ``IDENTITY <board_id>``); without it 409 REFUSED before anything is sent, and a
  phrase that does not match the plan fails the job with REFUSED, nothing sent.
- **Refusals before the job:** 409 HELD while another job runs; 409 HELD naming the holder
  when the board is behind a hub and the lease is not this client's; 422 UNAVAILABLE on bare
  metal or an image without the identity verbs; 409 REFUSED on a netbooted board (its
  identity is the stage0 bake) or a claim that is not this Harness Manager's; 400 USAGE for
  a value the board would refuse. In the job: 409 HELD while the card is written or read
  back (the reset guard).
- **The reboot is the harness's own ``reboot`` verb** (warm), never an MCC REBOOT.

Events: ``board.net_identity`` ``{status, reported, hub}`` when what the board reports, or
its verdict, changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import RefusedError, UnavailableError, UsageError
from harness_manager.services import board_identity as BI

from .app import _JSON, JsonBody, RouteContext, _bool, _obj, ok
from .xvc_api import _flag


def _opt_str(b: dict[str, Any], key: str) -> str | None:
    value = b.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise UsageError(f"{key} must be a string")
    return value


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api

    def service() -> Any:
        svc = getattr(d.engine, "board_identity", None)
        from harness_manager.services._unavailable import is_unavailable

        if svc is None or is_unavailable(svc):
            raise UnavailableError(BI.CAPABILITY, "this engine has no board identity service")
        if getattr(svc, "leases", "absent") is None and getattr(d, "leases", None) is not None:
            svc.leases = d.leases                           # hub_api's: one view of "mine"
        return svc

    @api.get("/boards/{bid:path}/identity")
    def identity_status(bid: str, refresh: str | None = None) -> Any:
        s = ctx.board(bid)
        st = service().status(s, refresh=_flag(refresh, "refresh"))
        return _JSON(ok(board_id=bid, identity=st))

    @api.post("/boards/{bid:path}/identity")
    def identity_fix(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        confirm = b.get("confirm")
        if not isinstance(confirm, str) or not confirm.strip():
            raise RefusedError("changing a board's identity needs the typed phrase: nothing was "
                               "changed", hint='send {"confirm": "<identity.fix.phrase>"}')
        want = {k: v for k in BI.FIELDS if (v := _opt_str(b, k)) is not None}
        from_hub = _bool(b, "from_hub", False)
        clear = _bool(b, "clear", False)
        if clear and (want or from_hub):
            raise UsageError("clear goes alone", hint="clear first, then set what you want")
        if not (want or from_hub or clear):
            raise UsageError("nothing to change", hint="send from_hub, or label/ip/mac/hostname, "
                                                      "or clear")
        BI.validate_want(want)                              # 400 before the job
        wait = b.get("wait_s")
        if wait is not None and (isinstance(wait, bool) or not isinstance(wait, (int, float))
                                 or wait <= 0):
            raise UsageError("wait_s must be a positive number of seconds")
        svc = service()
        with d.gates.op(bid):                               # 409 HELD while a job runs
            st = svc.status(s, cheap=True)
            if st is None:
                raise UnavailableError(BI.CAPABILITY, BI.NO_ADAPTER)
            ref = (st.get("fix") or {}).get("refusal")
            if ref:                                         # bare metal, netboot, the claim
                raise BI.refusal_error(ref["name"], ref["message"], ref.get("hint") or "")
            svc.check_lease(s)                              # 409 HELD naming the holder

        def run(progress: Callable[[str, int, int], None]) -> Any:
            return svc.fix(s, confirm=confirm, want=want or None, from_hub=from_hub,
                           clear=clear, wait_s=float(wait) if wait is not None else None,
                           progress=lambda text: progress(text, 0, 0))

        return ctx.accepted(d.jobs.submit("identity", bid, run))
