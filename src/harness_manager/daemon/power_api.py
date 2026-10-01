"""Board power over the daemon API: read the meter, and a cold power cycle as a job (lane L4).

docs/API.md "Week-plan additions -> Power and update" (frozen):

- ``GET /boards/{bid}/power`` -> ``{readings: [Reading], cycle_reason, device}``;
- ``POST /boards/{bid}/power/cycle`` ``{off_s?}`` -> 202 job ``power_cycle``; the
  result is the device's evidence.

The meter is the session's ``PowerAdapter`` (T9, ``harness_manager.power``), configured
per board in ``boards.toml``. What the routes add:

- **Honest readings.** A board with no meter still answers: three
  ``Reading.unavailable`` rows whose reason is the pack's own capability reason
  (``telemetry_power``), ``device: null``, and the ``power_cycle`` reason. Never a 0.
- **Refusals before any job.** A device that cannot cycle (a meter-only INA260,
  ``power.cycle = false``, no meter at all) is refused at once with 422 UNAVAILABLE and
  the adapter's ``cycle_reason``; a bad ``off_s`` is 400 USAGE. No job is created.
- **The board gate.** Both routes go through ``BoardGates`` like every other board
  request: 409 HELD naming the job while one runs on the board, and one power cycle
  per board at a time.
- **Events.** The job publishes ``power.cycle`` ``{phase, off_s, device}`` (docs/CONTRACTS.md)
  for ``off`` (the outlet confirmed OFF) and ``on`` (it reports ON again), next to
  ``job.progress`` with the same phases. ``up`` is NOT published here: the daemon
  has no board-agnostic witness that the board came back after a cold cycle (the
  outlet only proves the supply is back), so it never claims one.

The job always waits for the outlet to report ON again (``wait=True``): the result
then carries ``confirmed_on`` and ``seconds``, and a device that never comes back
fails the job with ``ActionFailedError`` and the hand-recovery hint.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from harness_manager.cli.output import reading_json
from harness_manager.core import capabilities as C
from harness_manager.core.errors import UnavailableError
from harness_manager.core.events import Event
from harness_manager.core.model import Reading
from harness_manager.power.base import DEFAULT_OFF_S, check_off_s
from harness_manager.services import reset_guard

from .app import _JSON, JsonBody, RouteContext, _number, _obj, ok, reset_force

#: Every meter answers these three rows, in this order (``harness_manager.power.READINGS``).
POWER_ROWS = (("board_power", "W"), ("supply_voltage", "V"), ("supply_current", "A"))
#: The ``source`` of the rows for a board with no meter (the MPS3 telemetry adapter's label).
NO_METER_SOURCE = "power-meter"
#: The outlet phases the job reports as ``power.cycle`` events (docs/CONTRACTS.md).
EVENT_PHASES = ("off", "on")


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api

    def reason_for(session: Any, capability: str) -> str:
        """The pack's reason ``capability`` is unavailable on a board with no power adapter."""
        try:
            ctx.require(session, "power", capability)
        except UnavailableError as exc:
            return exc.reason
        return ""                  # an adapter appeared since the caller looked: no reason

    @api.get("/boards/{bid:path}/power")
    def power_read(bid: str) -> Any:
        s = ctx.board(bid)
        with d.gates.op(bid):
            power = getattr(s, "power", None)
            if power is None:
                why = reason_for(s, C.TELEMETRY_POWER) or "this board has no power meter"
                readings = [Reading.unavailable(n, u, why, source=NO_METER_SOURCE)
                            for n, u in POWER_ROWS]
                cycle_reason = reason_for(s, C.POWER_CYCLE) or "no power adapter"
                device = None
            else:
                readings = list(power.read())           # never raises; a fault is a reason
                cycle_reason = str(power.cycle_reason or "")
                device = str(getattr(power, "label", "") or "") or None
        now = time.time()
        return _JSON(ok(board_id=bid, readings=[reading_json(r, now) for r in readings],
                        cycle_reason=cycle_reason, device=device))

    @api.post("/boards/{bid:path}/power/cycle")
    def power_cycle(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        off_s = check_off_s(_number(b, "off_s", DEFAULT_OFF_S))     # 400 before any job
        from .drive_gate import require_holder  # ui2 api-hub (G7)

        require_holder(d, bid, s, "power_cycle", b)                 # 409 HELD before the plug
        with d.gates.op(bid):
            power = ctx.require(s, "power", C.POWER_CYCLE)       # 422 with the pack's reason
            why = str(power.cycle_reason or "")
            if why:                                                 # a meter-only device
                raise UnavailableError(C.POWER_CYCLE, why)
            device = str(getattr(power, "label", "") or "")
        # SLOT-TIMING: never while the board's card job writes or reads back: the job fails
        # HELD, naming it (checked in the job, like the reboot's)
        force, consent = reset_force(b)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            def step(phase: str, done: int, total: int) -> None:
                progress(phase, done, total)
                if phase in EVENT_PHASES:
                    d.bus.publish(Event("power.cycle", bid, {"phase": phase, "off_s": off_s,
                                                             "device": device}))

            with reset_guard.guarded(s, reset_guard.ACTION_POWER_CYCLE, force=force,
                                     consent=consent):
                evidence = power.power_cycle(off_s, wait=True, progress=step)
            # FIX-PACK-6 item 3: the reported design against the DAP, once (never fails it)
            from harness_manager.services import design_check

            return design_check.attach(evidence, design_check.after_cold_boot(
                d.engine, s, after=design_check.AFTER_POWER_CYCLE, bus=d.bus))

        return ctx.accepted(d.jobs.submit("power_cycle", bid, run))
