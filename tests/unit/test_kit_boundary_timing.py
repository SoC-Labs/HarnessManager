"""N1 kept as a guard after N2 (FIX-PACK-8, david: "rebase and keep as a guard"): a build whose
static<->RM boundary was not timed says "boundary not timed (known issue 11)", a WARNING that
never refuses.

Linux v2.0.0 known issue 11: a build_rm.tcl that read the OOC XDC before it wrote the RM
checkpoint carried ``create_clock -name dut_clk`` to the link, where it overwrote the static's
clock on OSCCLK1; check_timing then counted 27,984 unclocked register/latch pins and 86,692
unconstrained endpoints while the summary said "All user specified timing constraints are met".
N2 (``build_rm.tcl.template``: the checkpoint first) took both to 0, leaving the 423 endpoints on
a constant clock that every build of the static has. The guard reads check_timing from the
build's ``<name>_timing.rpt`` and warns above 0 unclocked pins or 1,000 unconstrained endpoints
(not counting the constant-clock ones).

Fixtures, both real Vivado 2026.1 reports of ``minimal`` on the RC2 static:
- ``minimal_timing_noclock.rpt``: the guide lead's clean-account run before N2 (its first 148
  lines);
- ``minimal_timing_n2.rpt``: KIT-INTERACTIVE's full build with N2 (evidence 2026-09-30 §11).
Every check has its twin.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from harness_manager.core.pack import kit_refusal
from harness_manager.services import xdc
from harness_manager.services.kit import build
from harness_manager.services.kit.schema import load_receipt
from tests.fakes import kit_fakes as kf
from tests.integration.test_kit_cli import design, fake_vivado, run  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parents[2]
NOCLOCK = ROOT / "tests/fixtures/kit/minimal_timing_noclock.rpt"
N2 = ROOT / "tests/fixtures/kit/minimal_timing_n2.rpt"
TEMPLATE = Path(build.__file__).parent / "templates" / "build_rm.tcl.template"
SHELL_TCK = "u_shell/shell_bd_i/debug_bridge_0/inst/axi_jtag/inst/u_jtag_proc/tck_i_reg/Q"
WARNING = ("boundary not timed (known issue 11): Vivado's check_timing in spike_rm_timing.rpt "
           "found 27,984 register/latch pins with no clock (27,446 on OSCCLK1; 538 on "
           f"{SHELL_TCK}) and 86,692 unconstrained endpoints (Harness Manager warns above 0 and "
           "1,000), so the static<->RM partition boundary was not timing-analysed, even where the "
           "summary says every constraint is met. Your RM's own paths were still timed.")
TIMED = ("the static<->RM boundary was timed: Vivado's check_timing in spike_rm_timing.rpt found "
         "0 register/latch pins with no clock and 0 unconstrained endpoints (423 more on a "
         "constant clock: not timing paths)")


def receipt_with(tmp_path: Path, report: Path | str | None) -> Path:
    r = kf.passed_build(tmp_path, rm_wns="", rm_whs="", timing_rpt="spike_rm_timing.rpt")
    if isinstance(report, Path):
        shutil.copy(report, r.parent / "spike_rm_timing.rpt")
    elif report is not None:
        (r.parent / "spike_rm_timing.rpt").write_text(report)
    return r


# --- the reports --------------------------------------------------------------------------------------


def test_the_issue_11_report_is_not_timed():
    b = build.boundary_from_report(NOCLOCK)
    assert (b.no_clock, b.unconstrained, b.constant_clock) == (27984, 86692, 423)
    assert b.no_clock_roots == ((27446, "OSCCLK1"), (538, SHELL_TCK))
    assert not b.timed
    assert "All user specified timing constraints are met." in NOCLOCK.read_text()   # the trap


def test_twin_the_n2_report_is_timed_and_its_423_constant_clock_endpoints_do_not_count():
    b = build.boundary_from_report(N2)
    assert (b.no_clock, b.unconstrained, b.constant_clock, b.no_clock_roots) == (0, 0, 423, ())
    assert b.timed


def test_the_limits_are_the_ones_documented():
    assert (build.BOUNDARY_MAX_NO_CLOCK, build.BOUNDARY_MAX_UNCONSTRAINED) == (0, 1000)
    one = build.BoundaryTiming("r", no_clock=1)
    assert not one.timed                                          # any unclocked pin warns
    assert build.BoundaryTiming("r", 0, (), 1000, 423).timed      # at the limit: timed
    assert not build.BoundaryTiming("r", 0, (), 1001, 0).timed    # above it: not


def test_twin_no_report_or_no_check_timing_is_none(tmp_path):
    assert build.boundary_from_report(tmp_path / "missing.rpt") is None
    p = tmp_path / "t.rpt"
    p.write_text("| Design Timing Summary\n")                    # -no_check_timing
    assert build.boundary_from_report(p) is None


def test_a_report_without_the_split_lines_counts_the_total_less_the_constant_clock(tmp_path):
    p = tmp_path / "t.rpt"
    p.write_text("1. checking no_clock (0)\n----\n There are 0 register/latch pins with no clock.\n"
                 "4. checking unconstrained_internal_endpoints (1500)\n----\n"
                 " There are 600 pins that are not constrained for maximum delay due to constant "
                 "clock. (MEDIUM)\n5. checking no_input_delay (0)\n")
    b = build.boundary_from_report(p)
    assert (b.unconstrained, b.constant_clock, b.timed) == (900, 600, True)


# --- receipt_checks: a warning, never a refusal ----------------------------------------------------


def test_a_build_with_an_untimed_boundary_gets_a_warning_not_a_refusal(tmp_path):
    r = load_receipt(receipt_with(tmp_path, NOCLOCK))
    checks = {c.name: c for c in build.receipt_checks(r)}
    w = checks["boundary_timing"]
    assert (w.state, w.detail) == ("warning", WARNING)
    assert kit_refusal(list(checks.values()), "x") is None           # never a failure
    j = build.boundary_of(r).to_json()
    assert j["timed"] is False and j["fix"] == build.BOUNDARY_FIX
    assert j["no_clock_roots"][0] == {"pins": 27446, "root": "OSCCLK1"}


def test_twin_an_n2_build_is_ok(tmp_path):
    r = load_receipt(receipt_with(tmp_path, N2))
    checks = {c.name: c for c in build.receipt_checks(r)}
    assert (checks["boundary_timing"].state, checks["boundary_timing"].detail) == ("ok", TIMED)
    assert build.boundary_of(r).to_json()["fix"] == ""


def test_twin_no_report_beside_the_receipt_adds_nothing(tmp_path):
    r = load_receipt(receipt_with(tmp_path, None))
    assert "boundary_timing" not in {c.name for c in build.receipt_checks(r)}
    assert build.boundary_of(r) is None


def test_twin_a_failed_build_is_not_read(tmp_path):
    path = receipt_with(tmp_path, NOCLOCK)
    doc = json.loads(path.read_text())
    doc["state"] = "failed"
    path.write_text(json.dumps(doc))
    assert build.boundary_of(load_receipt(path)) is None


# --- kit check, kit build --------------------------------------------------------------------------


def test_kit_check_prints_the_warning_and_its_fix_and_passes(tmp_path, capsys):
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    r = receipt_with(tmp_path / "b", NOCLOCK)
    rc, out, _ = run(capsys, "kit", "check", str(r))
    assert rc == 0, out
    lines = out.splitlines()
    assert lines[0] == "the build spike_rm: passed (unchecked is not a pass: see the list)"
    assert lines[1] == f"WARNING: {WARNING}"
    assert lines[2] == f"  fix: {build.BOUNDARY_FIX}"
    assert any(ln.split()[:2] == ["warning", "boundary_timing"] for ln in lines)
    rc, out, _ = run(capsys, "kit", "check", str(r), "--json")
    d = json.loads(out)
    assert rc == 0 and d["passed"] is True
    assert {c["name"]: c["state"] for c in d["checks"]}["boundary_timing"] == "warning"
    b = d["facts"]["boundary"]
    assert (b["timed"], b["no_clock"], b["unconstrained"], b["constant_clock"]) == \
        (False, 27984, 86692, 423)
    assert b["limits"] == {"no_clock": 0, "unconstrained": 1000}
    # twin: the same build with N2's report prints no warning
    shutil.rmtree(tmp_path / "b")
    r = receipt_with(tmp_path / "b", N2)
    rc, out, _ = run(capsys, "kit", "check", str(r))
    assert rc == 0 and "WARNING" not in out and "boundary not timed" not in out
    assert any(ln.split()[:2] == ["ok", "boundary_timing"] for ln in out.splitlines())


A, B = "write_checkpoint -force $synth_dcp", "read_xdc $P(RM_OOC_XDC)"


def test_the_template_writes_the_checkpoint_first_and_an_old_script_does_not():
    text = TEMPLATE.read_text()
    assert build.script_reads_ooc_xdc_first(text) is False         # N2's order
    old = text.replace(A, "@@A@@").replace(B, A).replace("@@A@@", B)
    assert build.script_reads_ooc_xdc_first(old) is True            # before N2
    assert build.script_reads_ooc_xdc_first("puts hi") is None      # not one HM wrote


def test_kit_build_warns_about_an_old_script_and_an_untimed_last_build(
        fake_vivado, tmp_path, capsys):  # noqa: F811 - the fixture
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    bdir = tmp_path / "build" / "spike_rm"
    rc, out, _ = run(capsys, "kit", "script", "--static-id", "0x72BB0A36", "--design",
                     str(design(tmp_path)), "--out", str(bdir))
    assert rc == 0, out
    # twin first: this Harness Manager's script, no build yet: nothing to say
    rc, out, _ = run(capsys, "kit", "build", str(bdir))
    assert rc == 0 and "WARNING" not in out
    rc, out, _ = run(capsys, "kit", "build", str(bdir), "--json")
    assert json.loads(out)["boundary"] == {"script_reads_ooc_xdc_first": False,
                                           "last_build": None, "last_receipt": None}
    # an old script (the OOC XDC read before the checkpoint), and a last build with issue 11
    script = bdir / "build_rm.tcl"
    text = script.read_text()
    script.write_text(text.replace(A, "@@A@@").replace(B, A).replace("@@A@@", B))
    receipt_with(bdir, NOCLOCK)
    rc, out, _ = run(capsys, "kit", "build", str(bdir))
    assert rc == 0
    assert f"WARNING: {build.SCRIPT_OOC_FIRST}" in out
    assert f"WARNING (the last build here, spike_rm_build.json): {WARNING}" in out
    assert f"  fix: {build.BOUNDARY_FIX}" in out
    rc, out, _ = run(capsys, "kit", "build", str(bdir), "--json")
    b = json.loads(out)["boundary"]
    assert b["script_reads_ooc_xdc_first"] is True and b["last_build"]["timed"] is False
    # the guide's Check step (the Build tab's): passed, with the warning and its fix
    g = guide(capsys, tmp_path, bdir)
    check = next(x for x in g["steps"] if x["id"] == "check")
    assert check["state"] == "next" and check["reason"] == \
        f"warning: {WARNING} Fix: {build.BOUNDARY_FIX}"
    assert {c["name"]: c["state"] for c in check["checks"]}["boundary_timing"] == "warning"
    assert g["boundary"]["timed"] is False and g["boundary"]["fix"] == build.BOUNDARY_FIX
    # twin: the script written again and a build with N2's report: nothing to say
    script.write_text(text)
    shutil.copy(N2, bdir / "out" / "spike_rm_timing.rpt")
    rc, out, _ = run(capsys, "kit", "build", str(bdir))
    assert rc == 0 and "WARNING" not in out
    g = guide(capsys, tmp_path, bdir)
    check = next(x for x in g["steps"] if x["id"] == "check")
    assert check["reason"] == "" and g["boundary"]["timed"] is True


def guide(capsys, tmp_path: Path, bdir: Path) -> dict:
    rc, out, _ = run(capsys, "kit", "guide", "--static-id", "0x72BB0A36", "--design",
                     str(design(tmp_path)), "--build-dir", str(bdir), "--json")
    assert rc == 0, out
    return json.loads(out)


def test_the_ooc_xdc_no_longer_claims_the_static_clocks_supersede_it():
    kit = xdc.export("mps3", "rm-kit", "minimal")
    ooc = kit.files["minimal_ooc.xdc"]
    assert "propagated clocks supersede every create_clock here" not in ooc
    assert "only\n# after the RM checkpoint is written" in ooc and "known issue 11" in ooc
    # twin: the constraints themselves are unchanged (only the comment moved)
    assert "create_clock -name dut_clk -period 20.000" in ooc
