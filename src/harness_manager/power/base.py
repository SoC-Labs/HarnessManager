"""What every power meter driver shares: reading names, honest-data helpers, and the
device-timed power cycle for switched outlets.

Every driver answers ``read()`` with exactly three readings, in this order:
``board_power`` (W), ``supply_voltage`` (V), ``supply_current`` (A). A value the
meter did not give is ``Reading.unavailable`` with the reason. It is never 0.
``read()`` does not raise for a device fault (timeout, bad password): the fault
becomes the reason.

The power cycle asks the DEVICE to switch back on after ``off_s`` (Shelly
``toggle_after``, Tasmota ``Backlog ...; Delay``, NETIO action 2 "short off").
The board therefore comes back even if this process dies mid-cycle.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import ActionFailedError, UnavailableError, UsageError
from harness_manager.core.model import Reading
from harness_manager.core.pack import Progress

from .config import PowerConfig
from .http import HttpFailure, JsonHttp

POWER = ("board_power", "W")
VOLTAGE = ("supply_voltage", "V")
CURRENT = ("supply_current", "A")
READINGS = (POWER, VOLTAGE, CURRENT)

AC_CAVEAT = "AC at the wall outlet: includes the board power supply's own losses"
OFF_CAVEAT = "the outlet is switched OFF"

DEFAULT_OFF_S = 5.0
MIN_OFF_S = 2.0          # long enough for the board's supplies to drop out
MAX_OFF_S = 300.0        # Tasmota's Delay tops out at 360 s
ON_GRACE_S = 15.0        # after off_s, how long the outlet may take to report ON again
POLL_S = 0.5


def unavailable_readings(reason: str, source: str) -> list[Reading]:
    return [Reading.unavailable(name, unit, reason, source=source) for name, unit in READINGS]


def number(obj: Any, key: str) -> float | None:
    """``obj[key]`` if it is a finite real number (not a bool); else None."""
    if not isinstance(obj, dict):
        return None
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def reading(name: str, unit: str, value: float | None, source: str, *, what: str,
            caveat: str = "") -> Reading:
    if value is None:
        return Reading.unavailable(name, unit, f"the meter did not report {what}", source=source)
    return Reading(name, value, unit, source, reason=caveat)


def check_off_s(off_s: float) -> float:
    if isinstance(off_s, bool) or not isinstance(off_s, (int, float)) or not math.isfinite(off_s) \
            or not MIN_OFF_S <= off_s <= MAX_OFF_S:
        raise UsageError(f"power-cycle off time {off_s!r} s is out of range",
                         hint=f"use {MIN_OFF_S:g} to {MAX_OFF_S:g} seconds")
    return float(off_s)


class SwitchedPlug:
    """A networked metered outlet: read W/V/A, and cycle power with a device-side timer."""

    kind = ""
    scheme = "none"               # HTTP auth scheme: "basic" | "digest" | "none"

    def __init__(self, cfg: PowerConfig, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.label = cfg.label
        self.http = JsonHttp(cfg.url, timeout_s=cfg.timeout_s, auth=cfg.auth,
                             scheme=self.scheme, label=f"{cfg.kind} {cfg.address}")
        self._clock = clock
        self._sleep = sleep

    # -- driver-specific ------------------------------------------------------------------

    def _measure(self) -> list[Reading]:
        """The three readings, or ``HttpFailure``."""
        raise NotImplementedError

    def _is_on(self) -> bool:
        raise NotImplementedError

    def _schedule_cycle(self, off_s: float) -> None:
        """Switch OFF now and have the device switch back ON after ``off_s``."""
        raise NotImplementedError

    # -- the driver interface -----------------------------------------------------------

    def read(self) -> list[Reading]:
        try:
            return self._measure()
        except HttpFailure as f:
            return unavailable_readings(f.reason, self.label)

    @property
    def cycle_reason(self) -> str:
        """Why this outlet cannot be power-cycled, or "" when it can."""
        if not self.cfg.cycle:
            return f"{self.label}: power cycling is disabled in boards.toml (power.cycle = false)"
        return ""

    def power_cycle(self, off_s: float = DEFAULT_OFF_S, *, wait: bool = True,
                    progress: Progress | None = None) -> dict:
        """Cut the board's power for ``off_s`` seconds; return the evidence.

        Raises ``UnavailableError`` (cycling disabled), ``UsageError`` (bad off time),
        ``UnreachableError`` (no reply) or ``ActionFailedError`` (refused, or the outlet
        never reported OFF, or never came back ON).
        """
        if self.cycle_reason:
            raise UnavailableError("power_cycle", self.cycle_reason)
        off_s = check_off_s(off_s)
        t0 = self._clock()
        try:
            was_on = self._is_on()
            self._schedule_cycle(off_s)
            on_now = self._is_on()
        except HttpFailure as f:
            raise f.as_error() from f
        confirmed_off: bool | None = True
        if on_now:
            if self._clock() - t0 < off_s:
                raise ActionFailedError(
                    f"{self.label} accepted the power cycle but still reports ON",
                    hint="check power.outlet in boards.toml")
            confirmed_off = None      # the device-side timer already ran: not observed
        if progress:
            progress("off", 1, 2)
        evidence: dict[str, Any] = {"meter": self.label, "off_s": off_s, "was_on": was_on,
                                    "confirmed_off": confirmed_off, "confirmed_on": None}
        if not wait:
            return evidence
        deadline = t0 + off_s + ON_GRACE_S
        last = ""
        while True:
            self._sleep(POLL_S)
            try:
                if self._is_on():
                    break
            except HttpFailure as f:
                last = f.reason
            if self._clock() >= deadline:
                raise ActionFailedError(
                    f"{self.label} did not report ON again within {off_s + ON_GRACE_S:g} s"
                    + (f" (last error: {last})" if last else ""),
                    hint="switch the outlet on by hand; the board is unpowered")
        evidence["confirmed_on"] = True
        evidence["seconds"] = round(self._clock() - t0, 3)
        if progress:
            progress("on", 2, 2)
        return evidence
