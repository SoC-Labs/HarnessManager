"""MPS3 DUT clock over the shell's ``set_clk`` verb (lead-owned; wired by pack.py).

What the fielded firmware can do (0x3F1A560F):

- It retunes the DUT MMCM to a *preset* only: 25, 50 or 100 MHz
  (``firmware/clkrst/clkrst.c:20-26``; names in pyverify ``DEFAULT_CLK_PRESETS``).
- It cannot read the clock back, so ``clocks()`` reports the value as
  unavailable rather than inventing one. After a successful ``set_clock``, it
  reports the value it set, labelled with its source.
- Any-frequency ``set_clk`` is harness firmware item A8. When a board reports
  that feature, this adapter will accept arbitrary MHz.
"""

from __future__ import annotations

from collections.abc import Sequence

from pyverify.client import DEFAULT_CLK_PRESETS

from socharness.core.errors import ActionFailedError, UsageError
from socharness.core.model import Reading

PRESET_MHZ = {25.0: "25mhz", 50.0: "50mhz", 100.0: "100mhz"}


class Mps3Clocks:
    def __init__(self, session) -> None:
        self._session = session
        self._last_set: Reading | None = None

    def clocks(self) -> Sequence[Reading]:
        if self._last_set is not None:
            return (self._last_set,)
        return (Reading.unavailable(
            "dut", "MHz", "the shell cannot read the DUT clock back; set it to know it",
            source="shell set_clk"),)

    def set_clock(self, name: str, mhz: float) -> Reading:
        if name != "dut":
            raise UsageError(f"clock {name!r} is not settable from the shell", hint="clocks: dut")
        preset = PRESET_MHZ.get(float(mhz))
        if preset is None:
            allowed = ", ".join(f"{int(m)}" for m in sorted(PRESET_MHZ))
            raise UsageError(
                f"{mhz} MHz is not a preset on this firmware",
                hint=f"presets: {allowed} MHz (any frequency needs harness firmware item A8)",
            )
        resp = self._session.shell.call(lambda c: c.set_clk(preset, presets=DEFAULT_CLK_PRESETS))
        if not resp.ok:
            raise ActionFailedError(f"shell refused set_clk {preset}")
        if not resp.locked:
            raise ActionFailedError(f"DUT MMCM did not lock at {preset}",
                                    hint="the DUT clock may be stopped; retry or reset the DUT")
        self._last_set = Reading("dut", float(mhz), "MHz", "shell set_clk (last set value)")
        return self._last_set


def make_clock_adapter(session) -> Mps3Clocks | None:
    return Mps3Clocks(session) if getattr(session, "shell", None) is not None else None
