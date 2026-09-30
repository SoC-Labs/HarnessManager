"""KIT-NANOSOC: what building the platform's real DUT (nanosoc) through ``kit script`` needed.

- ``build.generics``: nanosoc's IMEM image is a top-level parameter (``IMEM_MEM_FPGA_IMG``,
  a ``$readmemh`` path). The design had no way to set one; a synth hook setting the
  fileset's ``generic`` property was the only route. Now ``{NAME: value}`` renders one
  ``-generic`` each, and ``{"path": FILE}`` is written absolute and checked at preflight
  (a missing ``$readmemh`` file is only a Vivado warning, and a blank ROM);
- ``build.sources`` holds HDL only: a .hex/.xci/.xdc/.tcl/.dcp there is refused with the
  key that takes it (the template would read it as SystemVerilog).

- ``kit pack --import`` says when Program lists another overlay of the same name, rm_id and
  static instead (the catalogue keeps the first);
- the guide sees a build that is running (a ``build_rm.log`` with no verdict) and offers no
  second Vivado, and says when a log stopped long ago (the run died).

The generated script is RUN in Python's Tcl (``tkinter.Tcl()``) with Vivado's commands
stubbed, as ``test_kit_tcl.py`` does. Every test has its negative twin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.core.errors import UsageError
from harness_manager.services.kit import render, script
from harness_manager.services.kit.schema import load_receipt
from harness_manager.services.kit.service import HubSource, KitService
from harness_manager.services.store import ContentStore
from tests.fakes import kit_fakes as kf


@pytest.fixture(autouse=True)
def _no_real_vivado(monkeypatch):
    from harness_manager.services.kit import vivado

    monkeypatch.setenv(vivado.ENV, "off")          # discovery and the launch probe run nothing


@pytest.fixture
def kits(tmp_path: Path) -> KitService:
    k = KitService(ContentStore(tmp_path / "store"), tmp_path / "w", hub=HubSource(None))
    k.import_(kf.FIXTURE)
    return k


def design_file(tmp_path: Path, **build) -> Path:
    d = {"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
         "use": {"clkrst": {}, "status": {}}, "build": {"sources": [str(kf.SPIKE_RM)], **build}}
    p = tmp_path / "design" / "spike_rm.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))
    return p


def make(kits, design, out=None):
    return script.make_script(kits, pack="mps3", static_id=kf.STATIC_ID, design=design,
                              out_dir=out)


# --- build.generics -----------------------------------------------------------------------------


def test_generics_render_one_generic_each_and_a_path_is_absolute(kits, tmp_path):
    p = design_file(tmp_path, generics={"IMG": {"path": "fw/hello.hex"}, "DEPTH": 1024,
                                        "MODE": "fast", "ON": True})
    s = make(kits, str(p))
    img = (p.parent / "fw" / "hello.hex").as_posix()
    assert s.params["RM_GENERICS"] == f"IMG={img} DEPTH=1024 MODE=fast ON=1"
    assert s.params["RM_GENERIC_FILES"] == img
    params = render.script_params(s.files["build_rm.tcl"])
    assert params["RM_GENERICS"] == s.params["RM_GENERICS"]
    assert params["RM_GENERIC_FILES"] == img


def test_negative_twin_no_generics_renders_empty_lists(kits, tmp_path):
    s = make(kits, str(design_file(tmp_path)))
    assert s.params["RM_GENERICS"] == "" and s.params["RM_GENERIC_FILES"] == ""
    assert "RM_GENERICS       {}" in s.files["build_rm.tcl"]


@pytest.mark.parametrize("generics, words", [
    (["IMG=a.hex"], "must be an object"),
    ({"1BAD": 1}, "not a parameter name"),
    ({"IMG": {"file": "a.hex"}}, '{"path": FILE}'),
    ({"IMG": {"path": ""}}, '{"path": FILE}'),
    ({"IMG": [1, 2]}, "is not a string, a number"),
])
def test_a_malformed_generic_is_refused_with_the_reason(kits, tmp_path, generics, words):
    with pytest.raises(UsageError) as e:
        make(kits, str(design_file(tmp_path, generics=generics)))
    assert words in e.value.message


def test_a_relative_generic_path_in_an_inline_design_is_refused(kits):
    d = {"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
         "use": {"clkrst": {}, "status": {}},
         "build": {"sources": [str(kf.SPIKE_RM)], "generics": {"IMG": {"path": "a.hex"}}}}
    with pytest.raises(UsageError) as e:
        make(kits, d)
    assert "relative" in e.value.message
    # twin: an absolute one is taken as it is
    d["build"]["generics"] = {"IMG": {"path": "/abs/a.hex"}}
    assert make(kits, d).params["RM_GENERIC_FILES"] == "/abs/a.hex"


# --- build.sources: HDL only --------------------------------------------------------------------


@pytest.mark.parametrize("name, key", [
    ("hello_image.hex", "build.generics"), ("fw.mem", "build.generics"),
    ("clk_wiz.xci", "build.synth_hook"), ("rm.xdc", "build.rm_xdc"),
    ("filelist.tcl", "build.synth_hook"), ("rm_synth.dcp", "build.synth_dcp"),
])
def test_a_non_hdl_source_is_refused_with_the_key_that_takes_it(kits, tmp_path, name, key):
    p = design_file(tmp_path)
    doc = json.loads(p.read_text())
    doc["build"]["sources"].append(name)
    p.write_text(json.dumps(doc))
    with pytest.raises(UsageError) as e:
        make(kits, str(p))
    assert name in e.value.message and "not HDL" in e.value.message
    assert key in e.value.hint


def test_negative_twin_hdl_sources_of_every_language_are_taken(kits, tmp_path):
    p = design_file(tmp_path)
    doc = json.loads(p.read_text())
    doc["build"]["sources"] += ["pkg.sv", "old.v", "defs.vh", "ent.vhd", "arch.VHDL", "t.SV"]
    p.write_text(json.dumps(doc))
    s = make(kits, str(p))
    assert len(s.params["RM_SOURCES"].split()) == 7


# --- the script, run in Tcl with Vivado stubbed ---------------------------------------------------

tkinter = pytest.importorskip("tkinter")

STUBS = """
proc version {args} {
    if {[lsearch -exact $args -short] >= 0} { return $::STUB_VERSION }
    return "Vivado v$::STUB_VERSION (64-bit)\\nSW Build 5076996 on Wed May 22 2024"
}
proc get_parts {args} { return [lindex $args end] }
proc set_param {args} { }
proc create_project {args} { }
proc read_verilog {args} { }
proc read_vhdl {args} { }
proc synth_design {args} { set ::SARGS $args }
proc read_xdc {args} { }
proc report_utilization {args} { }
proc report_timing_summary {args} { }
proc write_checkpoint {args} { }
proc get_cells {args} { return {} }
proc get_ports {args} { return {} }
proc get_clocks {args} { return {} }
proc close_project {args} { }
set ::SARGS {}
"""


def run(build_dir: Path, stop_after: str) -> tuple[bool, str, list[str]]:
    params = render.script_params((build_dir / "build_rm.tcl").read_text())
    tcl = tkinter.Tcl()
    tcl.eval(STUBS)
    tcl.eval(f"set STUB_VERSION {params['VIVADO_VERSION']}")
    tcl.eval(f"set argv [list STOP_AFTER={stop_after}]")
    try:
        tcl.eval(f"source {{{(build_dir / 'build_rm.tcl').as_posix()}}}")
        ok, err = True, ""
    except tkinter.TclError as exc:
        ok, err = False, str(exc)
    return ok, err, list(tcl.splitlist(tcl.eval("set ::SARGS")))


def gates(build_dir: Path) -> dict[str, str]:
    r = load_receipt(build_dir / "out" / "spike_rm_build.json")
    return {g.gate: g.verdict for g in r.gates}


def test_synth_design_gets_one_generic_each(kits, tmp_path):
    p = design_file(tmp_path, generics={"IMG": {"path": "fw/hello.hex"}, "DEPTH": 1024})
    (p.parent / "fw").mkdir()
    (p.parent / "fw" / "hello.hex").write_text("00\n")
    out = tmp_path / "b"
    make(kits, str(p), out)
    _ok, _err, sargs = run(out, "synth")   # the stubbed netlist fails boundary_bits: fine
    img = (p.parent / "fw" / "hello.hex").as_posix()
    assert sargs[sargs.index("-top") + 1] == "rm_spike_rm"
    pairs = [sargs[i + 1] for i, a in enumerate(sargs) if a == "-generic"]
    assert pairs == [f"IMG={img}", "DEPTH=1024"]
    assert gates(out)["generic_file_present"] == "PASS"


def test_negative_twin_no_generics_no_generic_argument(kits, tmp_path):
    out = tmp_path / "b"
    make(kits, str(design_file(tmp_path)), out)
    _ok, _err, sargs = run(out, "synth")
    assert "-top" in sargs and "-generic" not in sargs
    assert "generic_file_present" not in gates(out)


def test_a_missing_generic_file_is_refused_before_synthesis(kits, tmp_path):
    p = design_file(tmp_path, generics={"IMG": {"path": "fw/nope.hex"}})
    out = tmp_path / "b"
    make(kits, str(p), out)
    ok, err, sargs = run(out, "synth")
    assert not ok and "gate 'generic_file_present' failed" in err
    assert sargs == []                                  # synth_design never ran
    r = load_receipt(out / "out" / "spike_rm_build.json")
    assert r.failed_gate.gate == "generic_file_present" and "nope.hex" in r.failed_gate.detail


# --- an import that Program does not list (the catalogue keeps the first of a key) ----------------

from harness_manager.services.kit.schema import load_receipt as _load  # noqa: E402
from harness_manager_mps3 import kit as mkit  # noqa: E402
from harness_manager_mps3.overlays import OVERLAY_DIRS_ENV  # noqa: E402


def triple(tmp_path: Path, tag: str, partial: bytes | None = None) -> Path:
    """An overlay dir ``<tmp>/<tag>/ov/spike_rm`` packed from a passed receipt."""
    r = _load(kf.passed_build(tmp_path / tag / "b", partial=partial))
    return mkit.make_kit_adapter().pack_receipt(r, tmp_path / tag / "ov")


def test_an_import_an_overlay_dir_shadows_names_it(tmp_path, monkeypatch):
    store = ContentStore(tmp_path / "store")
    fielded = triple(tmp_path, "fielded", partial=kf.stream() + b"\x00" * 4)   # other bits
    monkeypatch.setenv(OVERLAY_DIRS_ENV, str(fielded.parent))
    got = mkit.make_kit_adapter().import_overlay(store, triple(tmp_path, "mine"))
    assert got["shadowed_by"] == str(fielded / "manifest.json")
    assert got["shadow_same_bits"] is False


def test_negative_twin_no_other_overlay_the_import_is_listed(tmp_path, monkeypatch):
    monkeypatch.delenv(OVERLAY_DIRS_ENV, raising=False)
    store = ContentStore(tmp_path / "store")
    got = mkit.make_kit_adapter().import_overlay(store, triple(tmp_path, "mine"))
    assert got["shadowed_by"] == "" and got["shadow_same_bits"] is False
    # another name in the dir does not shadow it either
    other = _load(kf.passed_build(tmp_path / "o" / "b", name="other_rm", rm_id="0x010080F1"))
    od = mkit.make_kit_adapter().pack_receipt(other, tmp_path / "o" / "ov")
    monkeypatch.setenv(OVERLAY_DIRS_ENV, str(od.parent))
    got = mkit.make_kit_adapter().import_overlay(store, triple(tmp_path, "mine2"))
    assert got["shadowed_by"] == ""


def test_a_shadow_with_the_same_bits_says_so(tmp_path, monkeypatch):
    # KIT-NANOSOC's own case: the HM-built nanosoc is byte-identical to the fielded one
    store = ContentStore(tmp_path / "store")
    fielded = triple(tmp_path, "fielded")
    monkeypatch.setenv(OVERLAY_DIRS_ENV, str(fielded.parent))
    got = mkit.make_kit_adapter().import_overlay(store, triple(tmp_path, "mine"))
    assert got["shadowed_by"].endswith("manifest.json") and got["shadow_same_bits"] is True


def test_cli_pack_import_says_program_lists_the_other_one(tmp_path, monkeypatch, capsys):
    from harness_manager.cli import main as cli_main

    fielded = triple(tmp_path, "fielded", partial=kf.stream() + b"\x00" * 4)
    monkeypatch.setenv(OVERLAY_DIRS_ENV, str(fielded.parent))
    receipt = kf.passed_build(tmp_path / "mine")
    assert cli_main.main(["kit", "pack", str(receipt), "--import"]) == 0
    out = capsys.readouterr().out
    assert "but Program lists" in out and str(fielded / "manifest.json") in out
    assert "--overlay-dir" in out and "shows in Program" not in out
    # twin: nothing shadows it
    monkeypatch.delenv(OVERLAY_DIRS_ENV)
    receipt = kf.passed_build(tmp_path / "mine2", name="lonely_rm", rm_id="0x010080F2")
    assert cli_main.main(["kit", "pack", str(receipt), "--import"]) == 0
    out = capsys.readouterr().out
    assert "it shows in Program" in out and "but Program lists" not in out


# --- the guide sees a build that is running (G7) ------------------------------------------------

import os  # noqa: E402

from harness_manager.services.kit import build as kbuild  # noqa: E402
from harness_manager.services.kit import guide as kguide  # noqa: E402
from harness_manager.services.kit import vivado as kvivado  # noqa: E402

RUNNING_LOG = """# echoed script lines are not markers:
#     puts "HM_RM_BUILD_FAILED gate=$name"
HM_STAGE preflight
HM_GATE static_id PASS CRC-32 is 0x72BB0A36
HM_STAGE synth
HM_GATE boundary_bits PASS 148
HM_STAGE link
Phase 1 Placer Initialization
"""


def _found() -> kvivado.VivadoFound:
    return kvivado.VivadoFound(kvivado.VivadoInstall("/tools/Xilinx/Vivado/2024.1/bin/vivado",
                                                     "2024.1", 5076996, "path"), ())


def _bdir(kits, tmp_path: Path) -> tuple[Path, str]:
    p = design_file(tmp_path)
    out = tmp_path / "b"
    make(kits, str(p), out)
    return out, str(p)


def _states(g) -> dict[str, str]:
    return {s.id: s.state for s in g.steps}


def test_running_build_reads_the_last_stage_with_no_verdict(tmp_path):
    assert kbuild.running_build(tmp_path) is None                       # no log
    log = tmp_path / "build_rm.log"
    log.write_text(RUNNING_LOG)
    r = kbuild.running_build(tmp_path)
    assert r is not None and r.stage == "link" and r.fresh
    old = kbuild.running_build(tmp_path, now=log.stat().st_mtime + 2 * 3600)
    assert old is not None and not old.fresh                            # it died
    # twins: a verdict after the last stage is a finished run
    for verdict in ("HM_RM_BUILD_COMPLETE rm=x", "HM_RM_BUILD_FAILED gate=rm_timing",
                    "HM_RM_BUILD_STOPPED after=synth"):
        log.write_text(RUNNING_LOG + verdict + "\n")
        assert kbuild.running_build(tmp_path) is None, verdict


def test_the_guide_says_a_build_is_running_and_offers_no_second_vivado(kits, tmp_path):
    bdir, dfile = _bdir(kits, tmp_path)
    (bdir / "build_rm.log").write_text(RUNNING_LOG)
    g = kguide.guide(kits, static_id="0x72BB0A36", design=dfile, build_dir=bdir, vivado=_found())
    s = g.steps[4]
    assert s.state == "next" and "a build is running here: stage link" in s.detail
    assert s.actions == [] and "do not start another Vivado" in s.reason
    assert _states(g)["check"] == "blocked"
    # twin: the same dir with no log offers the Vivado command, as before
    (bdir / "build_rm.log").unlink()
    g = kguide.guide(kits, static_id="0x72BB0A36", design=dfile, build_dir=bdir, vivado=_found())
    assert "no receipt in" in g.steps[4].detail
    assert g.steps[4].actions[0]["text"].startswith("/tools/Xilinx/Vivado/2024.1/bin/vivado")


def test_a_running_rebuild_hides_the_last_runs_receipt(kits, tmp_path):
    bdir, dfile = _bdir(kits, tmp_path)
    kf.passed_build(bdir)                                     # the LAST run's receipt
    g = kguide.guide(kits, static_id="0x72BB0A36", design=dfile, build_dir=bdir, vivado=_found())
    assert _states(g)["build"] == "done"
    (bdir / "build_rm.log").write_text(RUNNING_LOG)           # a new run, in link
    g = kguide.guide(kits, static_id="0x72BB0A36", design=dfile, build_dir=bdir, vivado=_found())
    assert _states(g)["build"] == "next" and "running" in g.steps[4].detail
    assert g.to_json()["receipt"] is None and _states(g)["check"] == "blocked"


def test_a_log_that_stopped_long_ago_is_a_run_that_died(kits, tmp_path):
    bdir, dfile = _bdir(kits, tmp_path)
    log = bdir / "build_rm.log"
    log.write_text(RUNNING_LOG)
    t = log.stat().st_mtime - 3 * 3600
    os.utime(log, (t, t))
    g = kguide.guide(kits, static_id="0x72BB0A36", design=dfile, build_dir=bdir, vivado=_found())
    s = g.steps[4]
    assert "stops at stage link" in s.detail and "that run died" in s.detail
    assert s.actions and s.actions[0]["text"].startswith("/tools/Xilinx/Vivado/2024.1/bin/vivado")
    # with an older passed receipt: still done, and the reason says a later run died
    kf.passed_build(bdir)
    r = kbuild.find_receipts(bdir)[0]
    os.utime(r, (t - 60, t - 60))
    g = kguide.guide(kits, static_id="0x72BB0A36", design=dfile, build_dir=bdir, vivado=_found())
    assert _states(g)["build"] == "done" and "that later run died" in g.steps[4].reason
