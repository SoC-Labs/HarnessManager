"""UI2-API-BUILD G8 fakes: a build directory as ``build_rm.tcl`` leaves it while Vivado runs
(``build_rm.log`` with its ``HM_STAGE`` markers) and after (``out/<name>_util.rpt``), for the
guide's ``running`` and ``utilisation`` (docs/API.md "Import a design, and the build's
progress, floorplan and utilisation"). The Build section's browser tests can use them too.

- ``running_log(build_dir, stage, ...)``: Vivado's log header (``# Start of session at:``),
  the stages up to ``stage`` (``HM_STAGE <n>``, or ``HM_STAGE <n> <clock seconds>`` with
  ``stage_at``), the script's echo of its own verdict lines (FIX-PACK-3: a real log holds
  them), and a verdict line when ``verdict`` is given;
- ``rtl(root)``: a small RTL folder for My RTL (``POST /kits/design/scan``): a package, a
  core and a leaf (Verilog), the ``rm_demo`` wrapper on two boundary ports (``dut_clk``,
  ``dut_resetn``: group ``clkrst``) with a ``$readmemh`` image parameter, an include folder,
  and two test benches the scan leaves out;
- ``util_report(out_dir, name, lut_used=...)``: the real post-route report of the kit-nanosoc
  build (``tests/fixtures/ui2/nanosoc_util.rpt``: ``report_utilization -pblocks
  pblock_rp_dut``), with the LUT row's used count and percentage changed when asked.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ui2"
UTIL_FIXTURE = FIXTURES / "nanosoc_util.rpt"
STAGES = ("preflight", "synth", "link", "impl", "verify", "bitstream")
SESSION = "Wed Sep 30 00:16:42 2026"


def running_log(build_dir: Path, stage: str = "impl", *, stage_at: float | None = None,
                started: str = SESSION, verdict: str = "", mtime: float | None = None) -> Path:
    """``<build_dir>/build_rm.log``; ``mtime`` sets its modification time (a dead run)."""
    d = Path(build_dir)
    d.mkdir(parents=True, exist_ok=True)
    lines = ["#-----------------------------------------------------------",
             "# Vivado v2026.1 (64-bit)",
             f"# Start of session at: {started}",
             "# Process ID         : 4242",
             "#-----------------------------------------------------------",
             "source build_rm.tcl",
             '#     puts "HM_RM_BUILD_FAILED gate=$name"',        # the echo, indented: not a verdict
             '#         puts "HM_RM_BUILD_COMPLETE rm=$name"']
    for s in STAGES[:STAGES.index(stage) + 1]:
        mark = f"HM_STAGE {s}"
        if s == stage and stage_at is not None:
            mark += f" {int(stage_at)}"
        lines += [mark, f"HM_GATE {s}_gate PASS fine", "INFO: [Common 17-83] Releasing license"]
    if verdict:
        lines.append(verdict)
    log = d / "build_rm.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(log, (mtime, mtime))
    return log


def util_report(out_dir: Path, name: str = "spike_rm", *, lut_used: int | None = None) -> Path:
    """``<out_dir>/<name>_util.rpt`` from the real report; ``lut_used`` rewrites the LUT row
    (used, and Util% against the pblock's 42824)."""
    text = UTIL_FIXTURE.read_text(encoding="utf-8")
    if lut_used is not None:
        old = "| CLB LUTs                   |  11051 |     0 |            0 | 11051 |     0 |" \
              "        376 |     42824 | 25.81 |"
        assert old in text, "the fixture's LUT row changed"
        pct = f"{100.0 * lut_used / 42824:.2f}"
        new = (f"| CLB LUTs                   | {lut_used:>6} |     0 |            0 | "
               f"{lut_used:>5} |     0 |        376 |     42824 | {pct:>5} |")
        text = text.replace(old, new)
    p = Path(out_dir) / f"{name}_util.rpt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def session_epoch(started: str = SESSION) -> float:
    return time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y"))


def rtl(root: Path) -> Path:
    (root / "rtl" / "inc").mkdir(parents=True)
    (root / "rtl" / "tb").mkdir()
    (root / "rtl" / "inc" / "defs.svh").write_text("`define WIDTH 8\n")
    (root / "rtl" / "a_top.sv").write_text(
        "// the RM wrapper\n"
        "module rm_demo #(parameter string IMG = \"fw.hex\", parameter int W = 8) (\n"
        "  input  logic dut_clk,\n  input  logic dut_resetn\n);\n"
        "  import demo_pkg::*;\n  demo_core #(.W(W)) u_core (.clk(dut_clk));\nendmodule\n")
    (root / "rtl" / "b_core.sv").write_text(
        "`include \"defs.svh\"\nmodule demo_core #(parameter W = 4) (input logic clk);\n"
        "  demo_leaf u_leaf ();\nendmodule\n")
    (root / "rtl" / "c_leaf.v").write_text("module demo_leaf; endmodule\n")
    (root / "rtl" / "z_pkg.sv").write_text("package demo_pkg;\n  localparam int N = 2;\n"
                                           "endpackage\n")
    (root / "rtl" / "fw.hex").write_text("00\n")
    (root / "rtl" / "tb" / "tb_demo.sv").write_text("module tb_demo; rm_demo dut (); endmodule\n")
    (root / "rtl" / "demo_tb.sv").write_text("module demo_tb; endmodule\n")
    return root / "rtl"
