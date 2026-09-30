"""KIT-INTERACTIVE: the build run in the user's own Vivado (docs/evidence/2026-09-30-kit-interactive,
proven on Vivado 2026.1 with the RC2 kit, board-free).

- ``render.vivado_command(mode=...)``: ``batch`` (as before), ``gui`` and ``tcl`` (both stay
  open after the script, so ``STOP_AFTER=link`` leaves the linked design to floorplan);
- ``render.source_tcl``: the one line for a Vivado that is already open. It always sets
  ``argv``: a session keeps the last one, and the script reads it;
- ``build.stopped_words`` / ``finish_hint``: a ``stopped`` receipt in words, not a failure;
- the generated ``build_rm.tcl`` sourced into a session (Python's Tcl, Vivado stubbed): with
  no ``argv`` at all it builds with its own values (it stopped with "can't read argv"), and
  batch prints the same markers as before. (A design already open needs nothing: Vivado
  2026.1 opens the build's projects beside it, in -mode tcl and in the GUI; the evidence);
- ``hm_save_floorplan FILE``: the child pblocks of the partition's pblock, scoped for
  ``read_xdc -cell`` (what ``write_xdc -cell`` is not), and a round trip keeps the names.

Vivado is never run. Every check has a negative twin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.core.errors import UsageError
from harness_manager.services.kit import build, render, script
from harness_manager.services.kit.schema import load_receipt
from harness_manager.services.kit.service import HubSource, KitService
from harness_manager.services.store import ContentStore
from tests.fakes import kit_fakes as kf

# --- the commands --------------------------------------------------------------------------------


def test_the_gui_and_tcl_commands_are_the_batch_one_in_another_mode(tmp_path):
    kw = {"stop_after": "link", "jobs": 4, "vivado": "/v/vivado"}
    batch = render.vivado_command(tmp_path, **kw)
    assert batch[:3] == ["/v/vivado", "-mode", "batch"]                  # the default
    assert batch[-3:] == ["-tclargs", "STOP_AFTER=link", "JOBS=4"]
    for mode in ("gui", "tcl"):
        cmd = render.vivado_command(tmp_path, mode=mode, **kw)
        assert cmd[:3] == ["/v/vivado", "-mode", mode] and cmd[3:] == batch[3:]
    with pytest.raises(UsageError):                                       # twin
        render.vivado_command(tmp_path, mode="interactive", **kw)


def test_tclargs_are_only_what_was_asked_for():
    assert render.tclargs() == []
    assert render.tclargs(stop_after="synth", jobs=2) == ["STOP_AFTER=synth", "JOBS=2"]
    with pytest.raises(UsageError):                                       # twin
        render.tclargs(stop_after="route")


def test_the_source_line_always_sets_argv():
    d = Path("/home/u/builds/my rm")
    assert render.source_tcl(d, stop_after="link", jobs=4) == (
        "cd {/home/u/builds/my rm}; set argv {STOP_AFTER=link JOBS=4}; source build_rm.tcl")
    # twin: nothing asked for still sets argv, to nothing: a session keeps the last argv
    assert render.source_tcl(d) == "cd {/home/u/builds/my rm}; set argv {}; source build_rm.tcl"
    with pytest.raises(UsageError):                                       # a brace cannot ride
        render.source_tcl(Path("/home/u/{x}"))


def test_the_floorplan_words_name_the_static_s_partition():
    t = render.stays_open("u_rp_dut", "pblock_rp_dut")
    assert "hm_save_floorplan FILE" in t and "not write_xdc -cell" in t
    assert "set_property PARENT pblock_rp_dut" in t and "build.rm_xdc" in t
    assert "u_rp_other" in render.stays_open("u_rp_other", "pb_other")    # twin: not fixed text
    assert "u_rp_dut" not in render.stays_open("u_rp_other", "pb_other")


# --- a stopped receipt -----------------------------------------------------------------------------


def test_a_stopped_receipt_reads_as_stopped(tmp_path):
    p = kf.passed_build(tmp_path / "b", state="stopped", gates=[
        {"gate": "static_id", "verdict": "PASS", "detail": ""},
        {"gate": "vivado_build", "verdict": "NOTE", "detail": ""},
        {"gate": "rm_id_match", "verdict": "PASS", "detail": ""}])
    r = load_receipt(p)
    words = build.stopped_words(r)
    assert words.startswith(f"stopped after {r.stage} (STOP_AFTER={r.stage}), not a failure: "
                            "the 2 gates up to there passed")                # a NOTE is no pass
    assert build.finish_hint(r) == (f"harness-manager kit build {tmp_path / 'b'} "
                                    "--stop-after bitstream")
    # twin: kit pack's refusal of it is unchanged (a stopped build has no pair)
    assert {c.name: c.state for c in build.receipt_checks(r)} == {"build": "mismatch"}


def test_the_build_dir_of_a_receipt_outside_out(tmp_path):
    p = kf.passed_build(tmp_path / "b")
    loose = tmp_path / "loose" / p.name
    loose.parent.mkdir()
    loose.write_text(p.read_text())
    assert build.build_dir_of(load_receipt(p)) == tmp_path / "b"
    assert build.build_dir_of(load_receipt(loose)) == tmp_path / "loose"   # twin


# --- build_rm.tcl, sourced into a session (Python's Tcl, Vivado stubbed) ------------------------------

tkinter = pytest.importorskip("tkinter")

STUBS = """
proc version {args} {
    if {[lsearch -exact $args -short] >= 0} { return 2024.1 }
    return "Vivado v2024.1 (64-bit)\\nSW Build 5076996 on Wed May 22 2024"
}
proc get_parts {args} { return [lindex $args end] }
proc set_param {args} { }
"""
#: stdout lines into ::PUTS; a write to a file (the receipt) still goes to the file
CAPTURE = """
set ::PUTS {}
rename puts _puts
proc puts {args} {
    if {[llength $args] == 1 || ([llength $args] == 2 && [lindex $args 0] eq "stdout")} {
        lappend ::PUTS [lindex $args end]
        return
    }
    _puts {*}$args
}
"""


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    """A build dir whose script was WRITTEN to stop after preflight (kit script --stop-after
    preflight), so a session with no argv runs preflight only."""
    kits = KitService(ContentStore(tmp_path / "store"), tmp_path / "w", hub=HubSource(None))
    kits.import_(kf.FIXTURE)
    d = {"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
         "use": {"clkrst": {}, "status": {}, "gpio": {"timed": True}},
         "build": {"sources": [str(kf.SPIKE_RM)]}}
    dfile = tmp_path / "spike_rm.json"
    dfile.write_text(json.dumps(d))
    out = tmp_path / "builds" / "spike_rm"
    script.make_script(kits, pack="mps3", static_id="0x72BB0A36", design=str(dfile),
                       out_dir=out, stop_after="preflight")
    return out


def session(build_dir: Path, *, argv: list[str] | None) -> tuple[bool, str, list[str]]:
    """``cd DIR; [set argv {...}]; source build_rm.tcl`` in one Tcl interpreter, as a user
    types it into a Vivado that is already open: (ok, error, the lines it printed)."""
    tcl = tkinter.Tcl()
    tcl.eval(STUBS)
    tcl.eval(CAPTURE)
    if argv is None:
        tcl.eval("catch {unset ::argv}")
    else:
        tcl.call("set", "::argv", tuple(argv))
    tcl.eval(f"cd {{{build_dir.as_posix()}}}")
    try:
        tcl.eval("source build_rm.tcl")
        ok, err = True, ""
    except tkinter.TclError as exc:
        ok, err = False, str(exc)
    return ok, err, list(tcl.splitlist(tcl.eval("set ::PUTS")))


def test_a_session_that_never_set_argv_builds_with_the_script_s_own_values(build_dir):
    ok, err, lines = session(build_dir, argv=None)
    assert ok, err
    r = load_receipt(build_dir / "out" / "spike_rm_build.json")
    assert r.state == "stopped" and r.stage == "preflight"            # the written STOP_AFTER
    assert "HM_RM_BUILD_STOPPED after=preflight" in lines
    # twin: an argv that is set still wins over the written values
    ok, err, _ = session(build_dir, argv=["RM_ID=0x00000000"])
    assert not ok and "rm_id_nonzero" in err


def test_batch_prints_the_same_markers_as_before(build_dir):
    """What a batch run prints (argv from -tclargs) is the marker sequence the script printed
    before this lane: the argv guard and hm_save_floorplan add no line. It passes on 1a127de's
    template too (that is the point); Vivado's own old/new comparison is in the evidence."""
    _ok, _err, lines = session(build_dir, argv=["STOP_AFTER=preflight"])
    marks = [x.split()[0] + " " + x.split()[1] for x in lines if x.startswith("HM_")]
    assert marks == ["HM_STAGE preflight", "HM_GATE vivado_version", "HM_GATE part_installed",
                     "HM_GATE static_dcp_present", "HM_GATE static_id",
                     "HM_GATE source_present", "HM_GATE ooc_xdc_present",
                     f"HM_RECEIPT {build_dir.as_posix()}/out/spike_rm_build.json",
                     "HM_RM_BUILD_STOPPED after=preflight"]


# --- hm_save_floorplan: the floorplan loop's save -------------------------------------------------

FLOOR_STUBS = """
proc get_pblocks {args} {
    if {[set i [lsearch -exact $args -filter]] >= 0} {
        set f [lindex $args [expr {$i + 1}]]
        set out {}
        foreach pb [array names ::PB_PARENT] {
            if {"PARENT == $::PB_PARENT($pb)" eq $f} { lappend out $pb }
        }
        return $out
    }
    return [lindex $args end]
}
proc get_cells {args} {
    if {[set i [lsearch -exact $args -of_objects]] >= 0} {
        return $::PB_CELLS([lindex $args [expr {$i + 1}]])
    }
    return [lindex $args end]
}
proc get_property {k o} { return $::PB_PROP($k,$o) }
"""


def save_floorplan(tmp_path: Path, pblocks: dict[str, tuple[str, str, list[str]]],
                   props: dict[tuple[str, str], str] | None = None) -> tuple[bool, str, str]:
    """Run the template's hm_save_floorplan in Python's Tcl against stubbed pblocks
    ``{name: (parent, grid_ranges, cells)}``: (ok, error, the file it wrote)."""
    text = render.template_text()
    a = text.index("proc hm_save_floorplan")
    b = text.index("\n}\n", a) + 3
    tcl = tkinter.Tcl()
    tcl.eval(FLOOR_STUBS)
    tcl.eval(CAPTURE)
    tcl.eval("array set P {RP_INST u_rp_dut RP_PBLOCK pblock_rp_dut RM_NAME lfsr_floor "
             "STATIC_ID 0x44EE76D5}")
    for pb, (parent, grid, cells) in pblocks.items():
        tcl.call("set", f"::PB_PARENT({pb})", parent)
        tcl.call("set", f"::PB_CELLS({pb})", tuple(cells))
        tcl.call("set", f"::PB_PROP(GRID_RANGES,{pb})", grid)
        for prop in ("EXCLUDE_PLACEMENT", "CONTAIN_ROUTING"):
            tcl.call("set", f"::PB_PROP({prop},{pb})", (props or {}).get((prop, pb), "0"))
    tcl.eval(text[a:b])
    out = tmp_path / "floorplan.xdc"
    try:
        tcl.eval(f"hm_save_floorplan {{{out.as_posix()}}}")
    except tkinter.TclError as exc:
        return False, str(exc), ""
    return True, "", out.read_text()


def test_hm_save_floorplan_writes_the_children_scoped_for_read_xdc_cell(tmp_path):
    from harness_manager.services.xdc import check_xdc

    ok, err, text = save_floorplan(tmp_path, {
        "pblock_rp_dut": ("ROOT", "SLICE_X48Y0:SLICE_X95Y119", ["u_rp_dut"]),
        "pblock_lfsr": ("pblock_rp_dut", "SLICE_X80Y90:SLICE_X87Y104",
                        ["u_rp_dut/lfsr_q_reg[0]", "u_rp_dut/count_q_reg[5]"]),
        "pblock_shell": ("ROOT", "SLICE_X0Y0:SLICE_X9Y9", ["u_shell/x"])})
    assert ok, err
    body = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert body == [
        "create_pblock pblock_lfsr",
        "resize_pblock [get_pblocks pblock_lfsr] -add {SLICE_X80Y90:SLICE_X87Y104}",
        "add_cells_to_pblock [get_pblocks pblock_lfsr] [get_cells [list \\",
        "    {lfsr_q_reg[0]} \\",
        "    {count_q_reg[5]}]]"]
    assert "pblock_rp_dut" not in "\n".join(body)          # never the partition's own pblock
    assert check_xdc(text) == []                           # HM's XDC checker takes it
    # twin: no child of the partition's pblock is an error that says how to make one
    ok, err, _ = save_floorplan(tmp_path, {
        "pblock_rp_dut": ("ROOT", "SLICE_X48Y0:SLICE_X95Y119", ["u_rp_dut"]),
        "pblock_lfsr": ("ROOT", "SLICE_X80Y90:SLICE_X87Y104", ["u_rp_dut/a"])})
    assert not ok and "set_property PARENT pblock_rp_dut" in err


def test_hm_save_floorplan_round_trips_the_scoped_name(tmp_path):
    # a floorplan read back by the build (read_xdc -cell) is named u_rp_dut_pblock_lfsr;
    # saving it again must give pblock_lfsr, or every loop would add a prefix
    ok, err, text = save_floorplan(tmp_path, {
        "u_rp_dut_pblock_lfsr": ("pblock_rp_dut", "SLICE_X80Y90:SLICE_X87Y104", ["u_rp_dut/a"])},
        props={("EXCLUDE_PLACEMENT", "u_rp_dut_pblock_lfsr"): "1"})
    assert ok, err
    assert "create_pblock pblock_lfsr" in text and "u_rp_dut_pblock" not in text
    assert "set_property EXCLUDE_PLACEMENT 1 [get_pblocks pblock_lfsr]" in text
    assert "CONTAIN_ROUTING" not in text                   # twin: an unset property is not written


def test_rm_xdc_is_read_with_the_cell_asked_for_again():
    """Vivado 2026.1 segfaulted (2 of 2, the evidence's crash/) at read_xdc -cell $rp_cell after
    read_checkpoint -cell: the cell object from before the link is stale. The template asks
    for the cell again; the old spelling must not come back."""
    text = render.template_text()
    link = text[text.index("stage link"):text.index("stage impl")]
    assert "read_xdc -cell [get_cells $rp] $P(RM_XDC)" in link
    assert "read_xdc -cell $rp_cell" not in text                     # twin: the crashing form
    assert link.index("read_checkpoint -cell $rp_cell") < link.index("read_xdc -cell [get_cells $rp]")
