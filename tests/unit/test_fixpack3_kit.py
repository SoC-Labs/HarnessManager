"""FIX-PACK-3 items 4-6: kit guidance the P8 clean run (HM 506a5ef) found wrong.

- 4: ``build_rm.log`` echoes the script, so the literal ``HM_RM_BUILD_FAILED gate=`` stands
  in it AFTER a real ``HM_RM_BUILD_COMPLETE``: "the verdict is the last HM_RM_BUILD_* line"
  points at the echo. The verdict is the receipt's state, or the last line that STARTS with
  ``HM_RM_BUILD_``; HM's own parser was already anchored (proved again on the real log).
- 5: the guide's ``next:`` suggested ``--out build/<name>`` (relative): now ``~/builds/<name>``
  as USER_GUIDE.md section 7.2 (the absolute path on Windows).
- 6: build times (about 30 min, up to an hour loaded, nanosoc about 50), and a receipt with
  no timed path inside the partition says so, with the whole-design WNS/WHS, instead of a
  blank ``rm_wns``.

Every check has a negative twin.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from harness_manager.services.kit import build as kbuild
from harness_manager.services.kit import guide as kguide
from harness_manager.services.kit import render, script
from harness_manager.services.kit import vivado as kvivado
from harness_manager.services.kit.service import HubSource, KitService
from harness_manager.services.store import ContentStore
from tests.fakes import kit_fakes as kf

ROOT = Path(__file__).resolve().parents[2]
REAL_LOG = ROOT / "docs/evidence/2026-09-30-kit-nanosoc/vivado/build_rm.log"
REAL_RPT = ROOT / "docs/evidence/2026-09-30-kit-nanosoc/vivado/nanosoc_timing_head.rpt"
TEMPLATE = (ROOT / "src/harness_manager/services/kit/templates/build_rm.tcl.template")
OLD_VERDICT = "the verdict is the last HM_RM_BUILD_* line"


@pytest.fixture(autouse=True)
def _no_real_vivado(monkeypatch):
    monkeypatch.setenv(kvivado.ENV, "off")          # discovery and the launch probe run nothing


@pytest.fixture
def kits(tmp_path: Path) -> KitService:
    k = KitService(ContentStore(tmp_path / "store"), tmp_path / "w", hub=HubSource(None))
    k.import_(kf.FIXTURE)
    return k


def design_file(tmp_path: Path) -> Path:
    d = {"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
         "use": {"clkrst": {}, "status": {}}, "build": {"sources": [str(kf.SPIKE_RM)]}}
    p = tmp_path / "design" / "spike_rm.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))
    return p


def found() -> kvivado.VivadoFound:
    return kvivado.VivadoFound(kvivado.VivadoInstall("/tools/Xilinx/Vivado/2024.1/bin/vivado",
                                                     "2024.1", 5076996, "path"), ())


def readme(kits, tmp_path: Path) -> str:
    return script.make_script(kits, pack="mps3", static_id=kf.STATIC_ID,
                              design=str(design_file(tmp_path))).files["README.txt"]


# --- item 4: the verdict line -------------------------------------------------------------------


def test_the_real_log_the_anchored_verdict_is_complete():
    text = REAL_LOG.read_text()
    marks = [m for m, _ in render.parse_markers(text) if m.startswith("HM_RM_BUILD_")]
    assert marks == ["HM_RM_BUILD_COMPLETE"]
    got = subprocess.run(["bash", "-c", render.VERDICT_GREP], cwd=REAL_LOG.parent,
                         capture_output=True, text=True, check=True).stdout
    assert got.startswith("HM_RM_BUILD_COMPLETE rm=nanosoc ")


def test_negative_twin_the_last_line_holding_the_text_is_the_echo():
    """What the old guidance found in the same log: the script's own puts, echoed."""
    lines = REAL_LOG.read_text().splitlines()
    holding = [ln for ln in lines if "HM_RM_BUILD_" in ln]
    assert "HM_RM_BUILD_FAILED gate=tcl_error" in holding[-1] and holding[-1].startswith("#")
    complete = next(i for i, ln in enumerate(lines) if ln.startswith("HM_RM_BUILD_COMPLETE"))
    assert any("HM_RM_BUILD_FAILED gate=" in ln for ln in lines[complete + 1:])


def test_hm_reads_the_real_log_as_finished_and_a_cut_one_as_running(tmp_path):
    lines = REAL_LOG.read_text().splitlines(keepends=True)
    complete = next(i for i, ln in enumerate(lines) if ln.startswith("HM_RM_BUILD_COMPLETE"))
    (tmp_path / "build_rm.log").write_text("".join(lines))
    assert kbuild.running_build(tmp_path) is None             # a verdict: the run ended
    # twin: cut before the verdict, the echoed FAILED text (line 85) is no verdict
    (tmp_path / "build_rm.log").write_text("".join(lines[:complete]))
    going = kbuild.running_build(tmp_path)
    assert going is not None and going.stage


def test_every_verdict_text_says_starts_with(kits, tmp_path, monkeypatch, capsys):
    rd = readme(kits, tmp_path)
    assert "STARTS with HM_RM_BUILD_" in rd and render.VERDICT_GREP in rd
    assert "out/spike_rm_build.json's state" in rd
    # the guide while a build runs
    bdir = tmp_path / "b"
    script.make_script(kits, pack="mps3", static_id=kf.STATIC_ID,
                       design=str(design_file(tmp_path)), out_dir=bdir)
    (bdir / "build_rm.log").write_text("HM_STAGE preflight\nHM_STAGE synth\n")
    g = kguide.guide(kits, static_id=kf.STATIC_ID, design=str(design_file(tmp_path)),
                     build_dir=bdir, vivado=found())
    assert "STARTS with HM_RM_BUILD_" in g.steps[4].reason
    assert render.VERDICT_GREP in g.steps[4].reason
    assert "STARTS with HM_RM_BUILD_FAILED" in kguide.GATE_HELP["tcl_error"]
    assert "STARTS with HM_RM_BUILD_" in render.VERDICT_HOW       # `kit build` prints it
    tcl = TEMPLATE.read_text()
    assert "# A marker is a line that STARTS with it" in tcl
    for text in (rd, g.steps[4].reason, render.VERDICT_HOW, kguide.GATE_HELP["tcl_error"]):
        assert OLD_VERDICT not in text                              # twin: the old words


# --- item 5: the build directory the guide suggests ---------------------------------------------


def test_the_guide_suggests_builds_under_home_as_the_user_guide(kits):
    g = kguide.guide(kits, static_id=kf.STATIC_ID, design="minimal", vivado=found())
    (action,) = g.steps[4].actions
    assert "--out ~/builds/minimal" in action["text"]
    assert "--out ~/builds/minimal" in (ROOT / "docs/USER_GUIDE.md").read_text()
    assert " --out build/" not in action["text"]                   # twin: the old, relative one


def test_negative_twin_windows_gets_the_absolute_path():
    got = kguide.builds_dir("minimal", windows=True)
    assert Path(got).is_absolute() and got == str(Path.home() / "builds" / "minimal")
    assert "~" not in got
    assert kguide.builds_dir("minimal", windows=False) == "~/builds/minimal"


# --- item 6: build times, and a receipt with no timed path inside the partition -----------------


def test_the_build_time_is_the_measured_one_everywhere(kits, tmp_path):
    rd = readme(kits, tmp_path)
    assert script.BUILD_TIME in " ".join(rd.split())
    assert "about 30 min" in script.BUILD_TIME and "nanosoc about 50 min" in script.BUILD_TIME
    guide_md = (ROOT / "docs/USER_GUIDE.md").read_text()
    js = (ROOT / "src/harness_manager/web/static/js/sections/build.js").read_text()
    assert "about 30 minutes for\na small RM on a quiet machine" in guide_md
    assert "about 30 minutes on a quiet machine" in js
    for text in (rd, guide_md, js):                                 # twin: the old figure
        assert not re.search(r"about 20\s+min", text)


def test_the_readme_says_where_the_timing_is(kits, tmp_path):
    rd = readme(kits, tmp_path)
    assert "rm_timing_note" in rd and "out/spike_rm_timing.rpt" in rd


def _receipt(tmp_path: Path, **fields: str) -> kbuild.BuildReceipt:
    return kbuild.load(kf.passed_build(tmp_path, **fields))


def _timing(r) -> str:
    (c,) = [c for c in kbuild.receipt_checks(r) if c.name == "timing"]
    assert c.state == "ok"                              # never a refusal: the gate is the verdict
    return c.detail


def test_no_timed_path_says_so_with_the_whole_design_figures(tmp_path):
    note = ("no timed path inside the partition; whole-design WNS 0.207 ns, WHS 0.030 ns "
            "from spike_rm_timing.rpt")
    assert _timing(_receipt(tmp_path, rm_wns="", rm_whs="", rm_timing_note=note)) == note
    # a receipt from before FIX-PACK-3 (no note): read from the report beside it
    r = _receipt(tmp_path / "old", rm_wns="", rm_whs="")
    shutil.copy(REAL_RPT, r.path.parent / "spike_rm_timing.rpt")
    assert _timing(r) == note
    (r.path.parent / "spike_rm_timing.rpt").unlink()
    assert "not beside the receipt" in _timing(r)
    assert kbuild.design_slack_from_report(REAL_RPT) == ("0.207", "0.030")


def test_negative_twin_an_rm_with_its_own_paths_gives_its_own_figures(tmp_path):
    words = _timing(_receipt(tmp_path))                     # kit_fakes: rm_wns 18.057, 0.079
    assert words == "your RM's paths: setup WNS 18.057 ns, hold WHS 0.079 ns"
    assert "no timed path" not in words
    assert kbuild.design_slack_from_report(tmp_path / "none.rpt") is None


def test_a_failed_build_gets_no_timing_line(tmp_path):
    r = _receipt(tmp_path, rm_wns="", rm_whs="")
    failed = kbuild.load(kf.passed_build(tmp_path / "f", state="failed"))
    assert any(c.name == "timing" for c in kbuild.receipt_checks(r))
    assert not any(c.name == "timing" for c in kbuild.receipt_checks(failed))


# --- item 6: the Tcl that writes the note (the procs, in Python's Tcl) ---------------------------

tkinter = pytest.importorskip("tkinter")


def _procs(*names: str) -> str:
    text = TEMPLATE.read_text()
    out = []
    for n in names:
        m = re.search(rf"^proc {n} .*?^\}}$", text, re.M | re.S)
        assert m, n
        out.append(m.group(0))
    return "\n".join(out)


def test_the_tcl_note_is_the_python_one(tmp_path):
    tcl = tkinter.Tcl()
    tcl.eval(_procs("timing_note"))
    got = tcl.eval('timing_note "" "" 0.207 0.030 spike_rm_timing.rpt')
    r = _receipt(tmp_path, rm_wns="", rm_whs="", design_wns="0.207", design_whs="0.030",
                 timing_rpt="spike_rm_timing.rpt")
    assert got == _timing(r)
    assert tcl.eval('timing_note "" "" "" "" x.rpt').endswith("WNS none, WHS none from x.rpt")
    # twin: an RM with a figure of its own gets no note
    assert tcl.eval('timing_note 18.057 "" 0.207 0.030 x.rpt') == ""
    assert tcl.eval('timing_note "" 0.079 0.207 0.030 x.rpt') == ""


def test_design_slack_reads_the_worst_paths_and_never_fails():
    tcl = tkinter.Tcl()
    tcl.eval(_procs("design_slack"))
    tcl.eval('proc get_timing_paths {args} { if {"-hold" in $args} { return hp } else { return sp } }')
    tcl.eval('proc get_property {k p} { if {$p eq "sp"} { return 0.207 }; return 0.030 }')
    assert tcl.eval("design_slack") == "0.207 0.030"
    tcl.eval("proc get_timing_paths {args} { return {} }")          # no timed path at all
    assert tcl.eval("design_slack") == "{} {}"
    tcl.eval('proc get_timing_paths {args} { error "not in a design" }')   # twin: an error
    assert tcl.eval("design_slack") == "{} {}"


def test_the_impl_stage_writes_the_note_into_the_receipt():
    """The template's rm_timing block itself (test_kit_rc2's stubbed run)."""
    from tests.unit.test_kit_rc2 import _rm_timing

    got: dict = {}
    v, d = _rm_timing("", "", 0, receipt=got)
    assert v == "PASS"                                            # the gate is unchanged
    assert got["rm_wns"] == "" and got["rm_whs"] == ""
    assert (got["design_wns"], got["design_whs"], got["timing_rpt"]) == ("0.207", "0.030",
                                                                         "minimal_timing.rpt")
    assert got["rm_timing_note"] == ("no timed path inside the partition; whole-design WNS "
                                     "0.207 ns, WHS 0.030 ns from minimal_timing.rpt")
    # twin: an RM with its own paths gets no note, and its gate detail is as before
    got = {}
    v, d = _rm_timing("0.412", "0.031", 12, receipt=got)
    assert "rm_timing_note" not in got and got["rm_wns"] == "0.412"
    assert d == "your RM's paths (12 registers): setup WNS 0.412 ns, hold WHS 0.031 ns"
