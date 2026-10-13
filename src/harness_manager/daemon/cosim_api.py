"""The DUT clock in the daemon API (lane L5c COSIM), loaded through ``app.EXTENSIONS``.

| Method and path | Body | Returns |
|---|---|---|
| ``GET /boards/{bid}/cosim`` | none | ``{board_id, offered, reason, limitation, mode, running, cycles, remaining}``; when the board has no clock control ``offered`` is false, ``reason`` says why and the clock fields are absent |
| ``POST /boards/{bid}/cosim`` | ``{act: halt}``, ``{act: run}``, ``{act: step, n}`` or ``{act: cycles}`` | ``{board_id, act, mode, running, cycles, remaining}`` (``step`` adds ``n``) |

Rules:

- ``cycles`` (and the GET) are open. ``halt``, ``step`` and ``run`` are claim-locked: the
  service sends them through the board's claim SSH when the claim is this Harness Manager's,
  else 409 REFUSED "Claim the board to stop its clock".
- ``n`` is a whole number from 1 to 100,000,000 (400 USAGE before the board is asked).
  ``step`` answers when the step has finished; one that does not finish in 5 s is 500
  ACTION_FAILED "The step did not finish in 5 seconds; the clock was left as it is".
- A board whose image has no ``cosim`` is 422 UNAVAILABLE (capability ``mps3.cosim``) on
  POST; the GET answers ``offered: false`` so a page can hide the card.
- A swap, ``reset dut`` and ``reset rp`` put the clock back to running.
"""

from __future__ import annotations

from harness_manager.core.errors import UsageError
from harness_manager.services import cosim as CS

from .app import _JSON, JsonBody, RouteContext, _obj, ok


def register(ctx: RouteContext) -> None:
    d = ctx.daemon

    @ctx.api.get("/boards/{bid:path}/cosim")
    def cosim_state(bid: str) -> _JSON:
        s = ctx.board(bid)
        with d.gates.op(bid):
            offered, why = CS.offered(s)
            state = CS.cycles(s) if offered else {}
        return _JSON(ok(board_id=bid, offered=offered, reason=why, limitation=CS.LIMITATION,
                        **state))

    @ctx.api.post("/boards/{bid:path}/cosim")
    def cosim_act(bid: str, body: JsonBody = None) -> _JSON:
        b = _obj(body)
        act = b.get("act")
        if act not in CS.ACTS:
            raise UsageError(f"act must be one of {', '.join(CS.ACTS)}, not {act!r}")
        n = CS.check_steps(b.get("n")) if act == "step" else None     # 400 before the board
        s = ctx.board(bid)
        with d.gates.op(bid):
            out = CS.step(s, n) if act == "step" else CS.ACTIONS[act](s)
        return _JSON(ok(board_id=bid, act=act, **out))
