"""UI2-API-BUILD G8, board-free: a running build's stage and start time from ``build_rm.log``
(``build.running_build``), ``<name>_util.rpt`` against the pblock and the pblock's facts from
the pin model (``floorplan``), "Run it your way" (``render.run_commands``), and My RTL
(``rtl_scan``). Every reader has a negative twin that feeds it what it must not accept."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from harness_manager.core.errors import AbsentError, UsageError
from harness_manager.services.kit import build, floorplan, render, rtl_scan
from tests.fakes import ui2_build_fakes as bf
from tests.fakes.ui2_build_fakes import rtl

# --- (a) the running stage -----------------------------------------------------------------------


def test_a_running_build_says_its_stage_and_when_vivado_started(tmp_path):
    bf.running_log(tmp_path, "impl")
    rb = build.running_build(tmp_path)
    assert rb is not None and rb.stage == "impl" and rb.stage_index == 4 and rb.fresh
    assert rb.started_at == bf.session_epoch() and rb.stage_started_at is None
    doc = rb.to_json(now=rb.started_at + 600)
    assert doc["stages"] == list(render.STAGES) and doc["elapsed_s"] == 600.0
    assert doc["stage_elapsed_s"] is None and doc["log"].endswith("build_rm.log")


def test_the_markers_clock_seconds_are_the_stages_start(tmp_path):
    at = time.time() - 120
    bf.running_log(tmp_path, "link", stage_at=at)
    rb = build.running_build(tmp_path)
    assert rb.stage == "link" and rb.stage_started_at == float(int(at))
    assert 119 <= rb.to_json()["stage_elapsed_s"] <= 125


def test_twins_a_verdict_no_log_a_dead_run_and_words_that_are_not_seconds(tmp_path):
    bf.running_log(tmp_path / "done", "bitstream", verdict="HM_RM_BUILD_COMPLETE rm=x")
    assert build.running_build(tmp_path / "done") is None           # the run ended
    assert build.running_build(tmp_path / "none") is None           # no log
    old = time.time() - 2 * 3600
    bf.running_log(tmp_path / "dead", "synth", mtime=old)
    assert build.running_build(tmp_path / "dead").fresh is False
    log = bf.running_log(tmp_path / "odd", "synth")
    log.write_text(log.read_text().replace("HM_STAGE synth", "HM_STAGE synth soon"))
    assert build.running_build(tmp_path / "odd").stage_started_at is None
    log.write_text(log.read_text().replace("# Start of session at:", "# Started:"))
    assert build.running_build(tmp_path / "odd").started_at is None
    assert build.running_build(tmp_path / "odd").stage_index == 2


# --- (b) utilisation against the pblock ----------------------------------------------------------


def test_the_real_report_against_the_pblock():
    u = floorplan.utilisation(bf.UTIL_FIXTURE)
    assert u["against"] == "pblock" and u["pblock"] == "pblock_rp_dut"
    assert u["design_state"] == "Routed" and u["device"] == "xcku115-flvb1760-1-c"
    rows = {r["key"]: r for r in u["rows"]}
    assert (rows["LUT"]["used"], rows["LUT"]["available"], rows["LUT"]["util_pct"]) == (
        11051, 42824, 25.81)
    assert (rows["FF"]["used"], rows["BRAM"]["used"], rows["DSP"]["available"]) == (
        6210, 20.5, 432)
    assert all(r["capacity_from"] == "report" and r["level"] == "ok" for r in u["rows"])
    assert u["worst"] == {"key": "CLB", "util_pct": 37.16, "level": "ok"}


def test_a_full_pblock_is_high_and_the_worst(tmp_path):
    p = bf.util_report(tmp_path, "big", lut_used=40000)
    u = floorplan.utilisation(p)
    lut = next(r for r in u["rows"] if r["key"] == "LUT")
    assert lut["level"] == "high" and lut["util_pct"] == 93.41
    assert u["worst"]["key"] == "LUT" and u["worst"]["level"] == "high"
    assert next(r for r in floorplan.utilisation(bf.util_report(
        tmp_path, "mid", lut_used=32000))["rows"] if r["key"] == "LUT")["level"] == "warn"


def test_a_report_against_the_device_takes_the_pin_models_capacity(tmp_path):
    text = bf.UTIL_FIXTURE.read_text().replace("-pblocks [get_pblocks -quiet pblock_rp_dut]",
                                               "-cells [get_cells u_rp_dut]")
    p = tmp_path / "x_util.rpt"
    p.write_text(text)
    facts = floorplan.pblock_facts("mps3", "0x72BB0A36")
    u = floorplan.utilisation(p, facts)
    rows = {r["key"]: r for r in u["rows"]}
    assert u["against"] == "cells"
    assert rows["LUT"]["capacity_from"] == "pin model" and rows["LUT"]["available"] == 42824
    assert rows["DSP"]["capacity_from"] == "report (device)"         # the model has no DSP


def test_twins_what_is_not_a_utilisation_report(tmp_path):
    assert floorplan.utilisation(tmp_path / "missing.rpt") is None
    junk = tmp_path / "junk.rpt"
    junk.write_text("| Design Timing Summary\n| WNS(ns) | TNS(ns) |\n| 1.0 | 0 |\n")
    assert floorplan.utilisation(junk) is None
    assert floorplan.parse_util_report("") is None


def test_the_report_beside_a_receipt_by_its_name_only(tmp_path):
    from harness_manager.services.kit.schema import load_receipt
    from tests.fakes import kit_fakes as kf

    r = load_receipt(kf.passed_build(tmp_path))
    assert floorplan.build_utilisation(r) is None                    # none written yet
    bf.util_report(r.path.parent, "spike_rm")
    assert floorplan.build_utilisation(r)["rows"][0]["key"] == "LUT"


# --- (c) the pblock's facts ------------------------------------------------------------------------


def test_the_pblock_facts_come_from_the_pin_model_with_their_sources():
    f = floorplan.pblock_facts("mps3", "0x72bb0a36")
    assert f["name"] == "pblock_rp_dut" and f["rp_instance"] == "u_rp_dut" and f["slr"] == "SLR0"
    assert f["clock_regions"] == ["X2Y0", "X3Y0", "X2Y1", "X3Y1"]
    assert f["slice_range"] == "SLICE_X48Y0:SLICE_X95Y119" and f["io_sites"] == 0
    assert f["capacity"] == {"LUT": 42824, "FF": 85648, "CLB": 5353, "BRAM": 144, "DSP": None}
    assert f["fixed"] is True and "mint" in f["fixed_why"]
    assert f["sources"]["name"].startswith("fpga/dfx/dfx_floorplan.xdc")


def test_twins_no_static_an_unknown_static_or_pack():
    assert floorplan.pblock_facts("mps3", "") is None
    assert floorplan.pblock_facts("mps3", "0x12345678") is None
    assert floorplan.pblock_facts("nosuchpack", "0x72BB0A36") is None


# --- (d) run it your way ---------------------------------------------------------------------------


def test_run_commands_batch_gui_and_an_open_session(tmp_path):
    d = tmp_path / "my build"
    out = render.run_commands(d, vivado="/opt/Xilinx/2024.1/bin/vivado", stop_after="link")
    assert out["stop_after"] == "link"
    assert out["batch"]["argv"][:3] == ["/opt/Xilinx/2024.1/bin/vivado", "-mode", "batch"]
    assert out["gui"]["argv"][1:3] == ["-mode", "gui"]
    assert out["gui"]["argv"][-2:] == ["-tclargs", "STOP_AFTER=link"]
    assert f"'{d}/build_rm.tcl'" in out["gui"]["text"]             # quoted for the shell
    # KIT-INTERACTIVE's line for an open Vivado (render.source_tcl, proven on 2026.1): one
    # source with `kit build` (tests/integration/test_kit_interactive_cli.py checks the CLI)
    assert out["session"]["text"] == render.source_tcl(d, stop_after="link")
    assert out["session"]["lines"] == [f"cd {{{d}}}", "set argv {STOP_AFTER=link}",
                                       "source build_rm.tcl"]
    assert "hm_save_floorplan FILE" in out["stays_open"]
    assert "receipt only" in out["session"]["watch"] and out["log"] == f"{d}/build_rm.log"
    plain = render.run_commands(tmp_path)
    assert plain["stop_after"] == "bitstream" and "-tclargs" not in plain["batch"]["argv"]
    assert plain["session"]["lines"][1] == "set argv {}" and plain["stays_open"] is None


def test_twins_run_commands_refuse_a_bad_stage_or_a_brace():
    with pytest.raises(UsageError):
        render.run_commands(Path("/b"), stop_after="route")
    with pytest.raises(UsageError):
        render.run_commands(Path("/b/{x}"))


# --- (e) My RTL -------------------------------------------------------------------------------------


def test_a_folder_becomes_a_design_in_compile_order(tmp_path):
    src = rtl(tmp_path)
    s = rtl_scan.scan(src, static_id="0x72BB0A36")
    names = [p.name for p in s.sources]
    assert names[0] == "z_pkg.sv"                                   # the package first
    assert names.index("c_leaf.v") < names.index("b_core.sv") < names.index("a_top.sv")
    assert s.top == "rm_demo" and s.name == "demo" and s.tops == ["rm_demo"]
    assert s.include_dirs == [src / "inc"] and s.packages == ["demo_pkg"]
    assert s.generics == {"IMG": {"path": str((src / "fw.hex").resolve())}}
    assert s.use == {"clkrst": {}} and [p["group"] for p in s.ports] == ["clkrst", "clkrst"]
    assert sorted(p.name for p in s.left_out) == ["demo_tb.sv", "tb_demo.sv"]
    d = s.design()
    assert d["kind"] == "rm" and d["build"]["top"] == "rm_demo" and d["use"] == {"clkrst": {}}
    assert d["build"]["sources"][0].endswith("z_pkg.sv") and d["build"]["include_dirs"]


def test_a_file_list_keeps_its_order_and_reads_incdir_define_and_nesting(tmp_path, monkeypatch):
    src = rtl(tmp_path)
    monkeypatch.setenv("UI2_RTL", str(src))
    (src / "more.f").write_text("$UI2_RTL/c_leaf.v\n")
    fl = src / "files.f"
    fl.write_text("// my list\n+incdir+inc\n+define+SIM=0\n-y libdir\n"
                  "z_pkg.sv\nb_core.sv\n-f more.f\na_top.sv\nmissing.sv\n")
    s = rtl_scan.scan(fl, static_id="0x72BB0A36")
    assert s.kind == "filelist"
    assert [p.name for p in s.sources] == ["z_pkg.sv", "b_core.sv", "c_leaf.v", "a_top.sv"]
    assert s.include_dirs == [src / "inc"] and s.defines == ["SIM=0"]
    assert any("-y" in w for w in s.warnings) and any("do not exist" in w for w in s.warnings)


def test_the_top_the_name_and_the_rm_id_can_be_given_and_are_proposed_otherwise(tmp_path):
    from harness_manager_mps3.kit import make_kit_adapter

    src = rtl(tmp_path)
    s = rtl_scan.scan(src, top="demo_core", name="core_only", rm_id="0x0100_8123".replace("_", ""))
    assert s.top == "demo_core" and s.name == "core_only" and s.rm_id == "0x01008123"
    assert not s.rm_id_proposed
    p = rtl_scan.scan(src, adapter=make_kit_adapter(), static_id="0x72BB0A36")
    assert p.rm_id_proposed and 0x8000 <= int(p.rm_id, 16) & 0xFFFF <= 0xFFFF


def test_a_top_that_is_not_a_wrapper_says_so_and_uses_nothing(tmp_path):
    src = rtl(tmp_path)
    s = rtl_scan.scan(src, top="demo_core", static_id="0x72BB0A36")
    assert s.use == {} and any("not a wrapper of the partition" in w for w in s.warnings)


def test_writing_the_design_and_its_twin(tmp_path):
    s = rtl_scan.scan(rtl(tmp_path))
    out = rtl_scan.write_design(s, tmp_path / "designs")
    assert out == tmp_path / "designs" / "demo.json"
    assert json.loads(out.read_text())["name"] == "demo" and s.written == out
    assert rtl_scan.write_design(s, out) == out                      # the same design again
    (tmp_path / "other.json").write_text(json.dumps({"name": "someone_else"}))
    with pytest.raises(UsageError):
        rtl_scan.write_design(s, tmp_path / "other.json")


def test_twins_what_the_scan_refuses(tmp_path):
    with pytest.raises(AbsentError):
        rtl_scan.scan(tmp_path / "nope")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("no rtl here")
    with pytest.raises(AbsentError):
        rtl_scan.scan(tmp_path / "docs")
    with pytest.raises(UsageError):
        rtl_scan.scan(tmp_path / "docs" / "README.md")               # not a .f list
    src = rtl(tmp_path / "r")
    with pytest.raises(UsageError):
        rtl_scan.scan(src, top="no_such_module")
    with pytest.raises(UsageError):
        rtl_scan.scan(src, name="has space")
    with pytest.raises(UsageError):
        rtl_scan.scan(src, rm_id="banana")
