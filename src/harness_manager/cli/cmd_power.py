"""``harness-manager power``: the board's power meter, and a cold power cycle (lane L4).

Verbs::

    harness-manager power show  TARGET              W, V and A from the meter; the device; can it cycle?
    harness-manager power cycle TARGET [--off S]    cut the supply for S seconds (default 5)

The meter is an add-on configured per board in ``boards.toml`` (``[boards.<id>.power]``:
a Shelly, Tasmota or NETIO outlet, or an INA260 on the 12 V input; see
``harness_manager/power/config.py``). ``show`` always answers: a board with no meter
gives three unavailable rows with the reason, never a 0.

``cycle`` asks ``[y/N]`` unless ``--yes``. The DEVICE switches the power back on after
``--off`` seconds, so the board comes back even if this process dies mid-cycle. It
is refused with exit 12 when the device cannot switch the power (a meter-only
INA260, ``power.cycle = false``, no meter), with the reason.

Exit codes: 0 done; 2 a bad ``--off``; 6 the outlet never reported OFF, or never ON
again (the board may be unpowered: the message says so); 7 the outlet did not
answer; 12 the device cannot cycle.

The lead wires this module into ``cli/main.py`` with ``cmd_power.register(sub)``. Over a
running harness-manager-daemon it needs the ``RemoteSession`` power proxy (CCR L4-3);
until then it says so and names ``HARNESS_MANAGER_NO_DAEMON=1``.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from harness_manager.core import capabilities as C
from harness_manager.core.errors import ExitCode, UnavailableError
from harness_manager.core.model import Reading

from .context import Ctx
from .output import (
    READING_COLUMNS,
    TSV_COLUMNS,
    Result,
    StderrProgress,
    reading_human,
    reading_json,
    reading_row,
)

#: TSV layouts for the power verbs (append-only). Registered into the shared table at
#: ``register()`` time until the lead folds them into ``output.TSV_COLUMNS`` (CCR L4-4).
POWER_TSV: dict[str, tuple[str, ...]] = {
    "power show": READING_COLUMNS,
    "power cycle": ("BOARD_ID", "DEVICE", "OFF_S", "CONFIRMED_OFF", "CONFIRMED_ON", "SECONDS"),
}
#: The rows every meter answers, in order (``harness_manager.power.READINGS``).
POWER_ROWS = (("board_power", "W"), ("supply_voltage", "V"), ("supply_current", "A"))
DEFAULT_OFF_S = 5.0
TARGET_HELP = "shell address host[:port], or - for a USB-only board (with --serial/--volume)"


def _parents() -> list[argparse.ArgumentParser]:
    fmt = argparse.ArgumentParser(add_help=False)
    g = fmt.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    usb = argparse.ArgumentParser(add_help=False)
    usb.add_argument("--serial", action="append", metavar="URL", default=argparse.SUPPRESS,
                     help="add the board controller's USB serial link")
    usb.add_argument("--volume", action="append", metavar="PATH", default=argparse.SUPPRESS,
                     help="add the configuration SD volume (the mounted V2M-MPS3 drive)")
    return [fmt, usb]


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``power`` and its actions to the top-level subparsers. Returns the parser."""
    for layout, cols in POWER_TSV.items():
        TSV_COLUMNS.setdefault(layout, cols)
    fmt, usb = _parents()
    vp = subparsers.add_parser("power", help="the board's power meter, and a cold power cycle",
                               description="Board power through the meter in boards.toml: "
                                           "read it, or cycle the supply through the outlet.",
                               parents=[fmt])
    sub = vp.add_subparsers(dest="power_cmd", required=True, metavar="ACTION")

    def epilog(layout: str) -> str:
        return f"--tsv columns: {' '.join(POWER_TSV[layout])}"

    ap = sub.add_parser("show", help="W, V and A from the meter, and whether it can cycle",
                        parents=[fmt, usb], epilog=epilog("power show"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)

    ap = sub.add_parser("cycle", help="cut the board's supply, then switch it back on",
                        parents=[fmt, usb], epilog=epilog("power cycle"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    ap.add_argument("--off", type=float, default=DEFAULT_OFF_S, metavar="S", dest="off_s",
                    help=f"how long the power stays off (default {DEFAULT_OFF_S:g} s; 2 to 300)")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    vp.set_defaults(fn=cmd_power)
    return vp


def _power(ctx: Ctx, session: Any, capability: str) -> Any:
    """``session.power``, or ``UnavailableError`` with the reason (the pack's, or the daemon's)."""
    power = getattr(session, "power", None)
    if power is not None:
        return power
    from harness_manager.client.remote import RemoteEngine

    if isinstance(ctx.engine, RemoteEngine):    # harness-manager-daemon's sessions (CCR L4-3)
        raise UnavailableError(capability, "harness-manager-daemon does not pass the power meter "
                                           "to the command line yet (set HARNESS_MANAGER_NO_DAEMON=1 "
                                           "after closing the board in the UI, or use the UI)")
    return ctx.require(session, "power", capability)


def _reason(ctx: Ctx, session: Any, capability: str) -> str:
    """Why ``capability`` is unavailable on a board with no power adapter."""
    try:
        _power(ctx, session, capability)
    except UnavailableError as exc:
        return exc.reason
    return ""


def cmd_power(ctx: Ctx) -> int:
    return {"show": _show, "cycle": _cycle}[ctx.args.power_cmd](ctx)


def _show(ctx: Ctx) -> int:
    with ctx.board(note="power show") as (cand, session):
        try:
            power = _power(ctx, session, C.TELEMETRY_POWER)
        except UnavailableError as exc:
            readings = [Reading.unavailable(n, u, exc.reason, source="power-meter")
                        for n, u in POWER_ROWS]
            device, cycle_reason = None, _reason(ctx, session, C.POWER_CYCLE)
        else:
            readings = list(power.read())
            device = str(getattr(power, "label", "") or "") or None
            cycle_reason = str(power.cycle_reason or "")
    now = time.time()
    human = [reading_human(r) for r in readings]
    human.append(f"device           {device or '-'}")
    human.append("power cycle      " + (f"cannot: {cycle_reason}" if cycle_reason else
                                        f"yes (`harness-manager power cycle {ctx.args.target}`)"))
    ctx.emit(Result("power show", {
        "board_id": cand.board_id, "readings": [reading_json(r, now) for r in readings],
        "cycle_reason": cycle_reason, "device": device,
    }, rows=[reading_row(cand.board_id, r, now) for r in readings], human=human))
    return ExitCode.OK


def _cycle(ctx: Ctx) -> int:
    from harness_manager.power.base import check_off_s

    a = ctx.args
    off_s = check_off_s(a.off_s)                     # exit 2 before the board is opened
    with ctx.board(note="power cycle") as (cand, session):
        power = _power(ctx, session, C.POWER_CYCLE)
        if power.cycle_reason:
            raise UnavailableError(C.POWER_CYCLE, power.cycle_reason)
        device = str(getattr(power, "label", "") or "")
        ctx.confirm(f"cut the power of {cand.board_id} for {off_s:g} s through {device}? "
                    "Everything running on the board is lost")
        progress = StderrProgress("power cycle", ctx.err)
        evidence = dict(power.power_cycle(off_s, wait=True, progress=progress))
    ctx.emit(Result("power cycle", {**evidence, "board_id": cand.board_id, "device": device,
                                    "phases": progress.phases},
                    rows=[[cand.board_id, device, evidence.get("off_s", off_s),
                           evidence.get("confirmed_off"), evidence.get("confirmed_on"),
                           evidence.get("seconds")]],
                    human=[f"cycled     {cand.board_id}: off {off_s:g} s through {device}, "
                           f"back on after {evidence.get('seconds', '?')} s",
                           "the board now boots from its SD; `harness-manager info "
                           f"{a.target}` shows when the harness answers"]))
    return ExitCode.OK
