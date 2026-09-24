"""T10, opt-in: Vivado reads a generated OOC XDC against the kit's own wrapper skeleton.

Skipped unless ``HM_T10_VIVADO=1`` and ``vivado`` is on PATH. It synthesises the kit's
skeleton (plus one flop on dut_clk) out of context, then ``read_xdc``s the kit's OOC XDC and fails on any XDC
CRITICAL WARNING or ERROR. A few minutes of Vivado: never part of ``make check``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness_manager.services import xdc

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HM_T10_VIVADO") == "1" and shutil.which("vivado")),
    reason="opt-in: HM_T10_VIVADO=1 and vivado on PATH")

TCL = """
read_verilog -sv {sv}
synth_design -top rm_{name} -part xcku115-flvb1760-1-c -mode out_of_context
read_xdc {xdc}
puts "T10_CLOCKS [llength [get_clocks]]"
"""


@pytest.mark.timeout(900)
@pytest.mark.parametrize("design", ["nanosoc", "nanosoc_ila", "minimal"])
def test_vivado_reads_the_generated_ooc_xdc(design: str, tmp_path: Path):
    kit = xdc.export("mps3", "rm-kit", design)
    xdc.write_kit(kit, tmp_path)
    # One flop on dut_clk, so the port has a load: HD.CLK_SRC on an unloaded port is a
    # CRITICAL WARNING about the empty skeleton, not about the XDC.
    sv = tmp_path / f"{design}_wrapper_skeleton.sv"
    text = sv.read_text()
    assert "  // assign irq_out = ...;" in text
    sv.write_text(text.replace("  // assign irq_out = ...;",
                               "  always_ff @(posedge dut_clk) irq_out <= dut_resetn;"))
    tcl = tmp_path / "read.tcl"
    tcl.write_text(TCL.format(sv=tmp_path / f"{design}_wrapper_skeleton.sv", name=design,
                              xdc=tmp_path / f"{design}_ooc.xdc"))
    r = subprocess.run(["vivado", "-mode", "batch", "-nojournal", "-nolog", "-source", str(tcl)],
                       cwd=tmp_path, capture_output=True, text=True, timeout=880)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-4000:]
    bad = [line for line in out.splitlines()
           if ("CRITICAL WARNING" in line or line.startswith("ERROR")) and ".xdc" in line]
    assert bad == [], "\n".join(bad)
    assert "T10_CLOCKS" in out
