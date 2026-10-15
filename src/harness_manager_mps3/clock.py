"""MPS3 DUT clock over the shell's ``set_clk`` verb (lead-owned; wired by pack.py).

What the fielded firmware can do (0x3F1A560F):

- It retunes the DUT MMCM to a *preset* only: 25, 50 or 100 MHz
  (``firmware/clkrst/clkrst.c:20-26``; names in pyverify ``DEFAULT_CLK_PRESETS``).
- Bare metal cannot read the clock back, so ``clocks()`` reports the value as
  unavailable rather than inventing one. After a successful ``set_clock``, it
  reports the value it set, labelled with its source.
- Any-frequency ``set_clk`` is release item A8. When a board reports
  that feature, this adapter will accept arbitrary MHz.

LINUX-CLOCK (HM 1.2): the Linux harness DOES have a clock verb (since v2.1.0): it answers the
6900 ``set_clk`` verb with the same presets (25, 50, 100 MHz) and ``stats.dut_mhz`` is read back
from the clk_wiz_dut register file (``clk_linux.c``). So on a Linux board ``set_clock`` takes the
bare-metal preset path (claim-routed), ``clocks()`` reports ``stats.dut_mhz`` (source "board
stats (clk_wiz_dut read-back)"), and only when the stats cannot be read does it fall back to the
pin model's 50 MHz start-up rate and say so. The Linux reply may lack ``locked`` (the client then
reads False), so it is not trusted there: the read-back decides. The clock goes back to 50 MHz
when the harness restarts after a fabric reset.

FIX-PACK-9 (CLI): ``harness-manager clock <ip>`` opens a candidate the CLI made from the
address, with no identity, so a Linux board read as bare metal. The adapter asks the session for
the identity (the shell's ``version``) when the candidate carries none, once per adapter.
"""

from __future__ import annotations

from collections.abc import Sequence

from pyverify.client import DEFAULT_CLK_PRESETS

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    UsageError,
)
from harness_manager.core.model import Reading

from . import claim_route

PRESET_MHZ = {25.0: "25mhz", 50.0: "50mhz", 100.0: "100mhz"}
#: The reason on a Linux board's reading (the CLI's line: "dut  100 MHz  [board stats (…)]  (…)").
LINUX_WHY = "set with presets 25, 50 or 100 MHz; goes back to 50 MHz when the harness restarts"
#: The reading's source when the board answered (``stats.dut_mhz``).
STATS_SOURCE = "board stats (clk_wiz_dut read-back)"
PIN_MODEL = "pin-model"


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
        self._asked: tuple[object | None] | None = None   # FIX-PACK-9: the session's answer

    def _linux(self) -> object | None:
        """The board's identity when it runs the Linux harness, else None: the candidate's
        when it has one (the daemon probed it), else the session's, asked once (the CLI's
        candidate is only an address)."""
        cand = getattr(getattr(self._session, "candidate", None), "identity", None)
        if getattr(cand, "harness_impl", ""):
            return _linux_identity(self._session)
        if self._asked is None:
            ask = getattr(self._session, "identity", None)
            ident = None
            if callable(ask):
                try:
                    ident = ask()
                except HarnessError:
                    ident = None
            self._asked = (ident,)
        ident = self._asked[0]
        return ident if getattr(ident, "harness_impl", "") == "linux" else None

    def _stats_mhz(self) -> float | None:
        """``stats.dut_mhz`` of the board, or None when the stats cannot be read."""
        shell = getattr(self._session, "shell", None)
        try:
            st = shell.call(lambda c: c.stats())
        except (HarnessError, AttributeError):
            return None
        mhz = getattr(st, "dut_mhz", 0)
        return float(mhz) if getattr(st, "ok", False) and mhz else None

    def clocks(self) -> Sequence[Reading]:
        linux = self._linux()
        if linux is not None:
            mhz = self._stats_mhz()
            if mhz is not None:
                return (Reading("dut", mhz, "MHz", STATS_SOURCE, reason=LINUX_WHY),)
            sid = str(getattr(linux, "shell_id", "") or "")
            pin = fixed_dut_mhz(sid)
            if pin is None:
                return (Reading.unavailable(
                    "dut", "MHz", f"the board's stats could not be read, and the pin model has no "
                    f"rate for shell {sid or '(not reported)'}; {LINUX_WHY}", source=PIN_MODEL),)
            return (Reading("dut", pin, "MHz", PIN_MODEL,
                            reason=f"the board's stats could not be read, so this is the shell's "
                                   f"start-up rate, not a reading; {LINUX_WHY}"),)
        if self._last_set is not None:
            return (self._last_set,)
        return (Reading.unavailable(
            "dut", "MHz", "the shell cannot read the DUT clock back; set it to know it",
            source="shell set_clk"),)

    def set_clock(self, name: str, mhz: float) -> Reading:
        if name != "dut":
            raise UsageError(f"clock {name!r} is not settable from the shell", hint="clocks: dut")
        linux = self._linux() is not None
        preset = PRESET_MHZ.get(float(mhz))
        if preset is None:
            allowed = ", ".join(f"{int(m)}" for m in sorted(PRESET_MHZ))
            raise UsageError(
                f"{mhz} MHz is not a preset on this firmware",
                hint=f"presets: {allowed} MHz (any frequency needs release item A8)",
            )
        resp = claim_route.routed_call(self._session, f"setting the DUT clock to {preset}",
                                       self._session.shell,
                                       lambda c: c.set_clk(preset, presets=DEFAULT_CLK_PRESETS))
        if not resp.ok:
            raise ActionFailedError(f"shell refused set_clk {preset}")
        if linux:
            # The Linux reply may lack ``locked``: read the clock back instead of guessing.
            seen = self._stats_mhz()
            if seen is None:
                self._last_set = Reading("dut", float(mhz), "MHz", "shell set_clk (last set value)",
                                         reason=LINUX_WHY)
                return self._last_set
            if seen != float(mhz):
                raise ActionFailedError(f"the board reads {seen:g} MHz after set_clk {preset}",
                                        hint="the DUT clock may be stopped; retry or reset the DUT")
            self._last_set = Reading("dut", seen, "MHz", STATS_SOURCE, reason=LINUX_WHY)
            return self._last_set
        if not resp.locked:
            raise ActionFailedError(f"DUT MMCM did not lock at {preset}",
                                    hint="the DUT clock may be stopped; retry or reset the DUT")
        self._last_set = Reading("dut", float(mhz), "MHz", "shell set_clk (last set value)")
        return self._last_set


def make_clock_adapter(session) -> Mps3Clocks | None:
    return Mps3Clocks(session) if getattr(session, "shell", None) is not None else None
