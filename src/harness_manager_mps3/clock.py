"""MPS3 DUT clock over the shell's ``set_clk`` verb (lead-owned; wired by pack.py).

What the fielded firmware can do (0x3F1A560F):

- It retunes the DUT MMCM to a *preset* only: 25, 50 or 100 MHz
  (``firmware/clkrst/clkrst.c:20-26``; names in pyverify ``DEFAULT_CLK_PRESETS``).
- It cannot read the clock back, so ``clocks()`` reports the value as
  unavailable rather than inventing one. After a successful ``set_clock``, it
  reports the value it set, labelled with its source.
- Any-frequency ``set_clk`` is harness firmware item A8. When a board reports
  that feature, this adapter will accept arbitrary MHz.

UI2-POLISH (david, first real-board look, 10-01): on the Linux harness the DUT clock is FIXED by
the shell (its ``dut_clk`` boundary clock; harnessd has no clock verb). ``clocks()`` reports that
rate from the pin model (``pins/mps3_board_pins.json``, the shell's ``boundary_clocks``) with the
reason "fixed by the shell", and unavailable with the reason when the model has no such shell;
``set_clock`` is refused there.
"""

from __future__ import annotations

from collections.abc import Sequence

from pyverify.client import DEFAULT_CLK_PRESETS

from harness_manager.core.errors import ActionFailedError, UnavailableError, UsageError
from harness_manager.core.model import Reading

PRESET_MHZ = {25.0: "25mhz", 50.0: "50mhz", 100.0: "100mhz"}
FIXED = "fixed by the shell"


def fixed_dut_mhz(static_id: str) -> float | None:
    """The DUT clock shell ``static_id`` drives (its ``dut_clk`` boundary clock in the pin
    model), in MHz; None when the model has no such shell or clock."""
    from .pins import load_model

    try:
        want = int(str(static_id), 16)
    except (TypeError, ValueError):
        return None
    for key, shell in load_model().get("shells", {}).items():
        try:
            if int(key, 16) != want:
                continue
        except ValueError:
            continue
        for bc in shell.get("boundary_clocks", []):
            if bc.get("signal") == "dut_clk" and float(bc.get("period_ns") or 0) > 0:
                return round(1000.0 / float(bc["period_ns"]), 3)
    return None


def _linux_identity(session) -> object | None:
    ident = getattr(getattr(session, "candidate", None), "identity", None)
    return ident if getattr(ident, "harness_impl", "") == "linux" else None


class Mps3Clocks:
    def __init__(self, session) -> None:
        self._session = session
        self._last_set: Reading | None = None

    def clocks(self) -> Sequence[Reading]:
        linux = _linux_identity(self._session)
        if linux is not None:
            sid = str(getattr(linux, "shell_id", "") or "")
            mhz = fixed_dut_mhz(sid)
            if mhz is None:
                return (Reading.unavailable(
                    "dut", "MHz", f"{FIXED}; the pin model has no rate for shell {sid or '(not reported)'}",
                    source="the pin model"),)
            return (Reading("dut", mhz, "MHz", f"the pin model, shell {sid}", reason=FIXED),)
        if self._last_set is not None:
            return (self._last_set,)
        return (Reading.unavailable(
            "dut", "MHz", "the shell cannot read the DUT clock back; set it to know it",
            source="shell set_clk"),)

    def set_clock(self, name: str, mhz: float) -> Reading:
        if name != "dut":
            raise UsageError(f"clock {name!r} is not settable from the shell", hint="clocks: dut")
        if _linux_identity(self._session) is not None:
            raise UnavailableError("set_clock", f"the Linux harness's DUT clock is {FIXED}: it cannot be set",
                                   hint="the rate is the shell's dut_clk (GET /boards/{bid}/clocks)")
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
