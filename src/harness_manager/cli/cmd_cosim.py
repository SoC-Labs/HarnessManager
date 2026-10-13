"""``harness-manager cosim``: stop, step and restart the DUT's clock (lane L5c COSIM).

Verbs::

    harness-manager cosim cycles TARGET        the DUT clock's state and cycle count
    harness-manager cosim halt   TARGET        stop the DUT clock
    harness-manager cosim step   TARGET N      run exactly N cycles (1 to 100,000,000), then stop
    harness-manager cosim run    TARGET        let the clock run again

``cycles`` is open. ``halt``, ``step`` and ``run`` need the board claimed (they go through the
claim SSH): exit 15 says "Claim the board to stop its clock". A board whose image has no
clock control exits 12 ("This board's image has no clock control"). A swap, ``reset dut`` and
``reset rp`` start the clock again.

Serial input and the network receive are not held while the clock is stopped, so a stopped or
stepped run is exact inside the DUT and on its buses, but not for those pins.

Exit codes: 0 done; 2 bad arguments (N out of range); 7 the board did not answer; 12 this
board has no clock control; a step that did not finish in 5 s is a failed action (the clock is
left as it is).

``cli/main.py`` registers it with ``cmd_cosim.register(sub)``.
"""

from __future__ import annotations

import argparse
from typing import Any

from harness_manager.core.errors import ExitCode, UsageError
from harness_manager.services import cosim as CS

from .context import VIA_HELP, VIA_METAVAR, Ctx
from .output import TSV_COLUMNS, Result

COSIM_TSV: dict[str, tuple[str, ...]] = {
    "cosim": ("BOARD_ID", "ACT", "MODE", "RUNNING", "CYCLES", "REMAINING", "N"),
}
TARGET_HELP = "shell address host[:port] (the board's harness)"
WHAT = "the DUT's clock: read the cycle count, halt, step N cycles, run (claim needed to stop it)"


def _parents() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    fmt = argparse.ArgumentParser(add_help=False)
    g = fmt.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    via = argparse.ArgumentParser(add_help=False)
    via.add_argument("--via", metavar=VIA_METAVAR, default=argparse.SUPPRESS, help=VIA_HELP)
    return fmt, via


def _count(text: str) -> int:
    try:
        return CS.check_steps(int(text.replace("_", "").replace(",", ""), 10))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a whole number of cycles (1 to {CS.STEP_MAX:,})") from None
    except UsageError as exc:
        raise argparse.ArgumentTypeError(exc.message) from None


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``cosim`` and its actions to the top-level subparsers. Returns the parser."""
    for layout, cols in COSIM_TSV.items():
        TSV_COLUMNS.setdefault(layout, cols)
    fmt, via = _parents()
    epilog = f"--tsv columns: {' '.join(COSIM_TSV['cosim'])}\n{CS.LIMITATION}"
    cp = subparsers.add_parser(
        "cosim", help=WHAT, parents=[fmt, via], epilog=epilog,
        description="Stop, step and restart the DUT's clock (boards whose harness reports "
                    "'cosim'). Reading the count is open; halt, step and run need the board "
                    "claimed. A swap or a DUT/RP reset starts the clock again.")
    sub = cp.add_subparsers(dest="cosim_cmd", required=True, metavar="ACTION")
    for name, text in (("cycles", "the clock's state and its cycle count"),
                       ("halt", "stop the DUT clock (claimed board)"),
                       ("run", "let the DUT clock run again (claimed board)")):
        ap = sub.add_parser(name, help=text, description=text, parents=[fmt, via],
                            epilog=epilog)
        ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    sp = sub.add_parser("step", help="run exactly N cycles, then stop (claimed board)",
                        description="Run exactly N DUT clock cycles, then stop the clock. The "
                                    "answer comes when the step is done; one that does not "
                                    "finish in 5 seconds is reported and the clock is left "
                                    "as it is.", parents=[fmt, via], epilog=epilog)
    sp.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    sp.add_argument("n", metavar="N", type=_count,
                    help=f"cycles to run, 1 to {CS.STEP_MAX:,}")
    cp.set_defaults(fn=cmd_cosim)
    return cp


def _human(board_id: str, act: str, out: dict[str, Any]) -> list[str]:
    cycles = f"{out['cycles']:,}"
    if act == "step":
        head = f"cosim   {board_id} ran {out['n']:,} cycles and stopped at {cycles}"
    elif out["running"]:
        head = f"cosim   {board_id} clock running; {cycles} cycles so far"
    else:
        head = f"cosim   {board_id} clock stopped at {cycles} cycles"
    return [head, f"        {CS.LIMITATION}"] if act in ("halt", "step") else [head]


def cmd_cosim(ctx: Ctx) -> int:
    a = ctx.args
    act = a.cosim_cmd
    with ctx.board(note=f"cosim {act}") as (cand, session):
        out = CS.step(session, a.n) if act == "step" else CS.ACTIONS[act](session)
    ctx.emit(Result("cosim", {"board_id": cand.board_id, "act": act, **out},
                    rows=[[cand.board_id, act, out["mode"], out["running"], out["cycles"],
                           out["remaining"], out.get("n", "")]],
                    human=_human(cand.board_id, act, out)))
    return ExitCode.OK
