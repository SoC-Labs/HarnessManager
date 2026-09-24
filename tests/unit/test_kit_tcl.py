"""The generated ``build_rm.tcl`` RUN, with no Vivado: its preflight stage in Python's own
Tcl interpreter (``tkinter.Tcl()``, Tcl 8.6, which has ``zlib crc32``), with the three
Vivado commands preflight uses stubbed (``version``, ``get_parts``, ``set_param``).

This proves what the rendered-text assertions cannot: the script parses, resolves its
paths against its own directory, computes the same CRC-32 as Python, refuses another
Vivado major.minor BEFORE any synthesis (david K4), only notes another build, and writes
a receipt that ``schema.load_receipt`` reads. Skipped where Python has no Tcl.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.services.kit import script
from harness_manager.services.kit.schema import load_receipt
from harness_manager.services.kit.service import HubSource, KitService
from harness_manager.services.store import ContentStore
from tests.fakes import kit_fakes as kf

tkinter = pytest.importorskip("tkinter")

STUBS = """
proc version {args} {
    if {[lsearch -exact $args -short] >= 0} { return $::STUB_VERSION }
    return "Vivado v$::STUB_VERSION (64-bit)\\nSW Build $::STUB_BUILD on Wed May 22 2024"
}
proc get_parts {args} { return [lindex $args end] }
proc set_param {args} { }
"""


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    kits = KitService(ContentStore(tmp_path / "store"), tmp_path / "w", hub=HubSource(None))
    kits.import_(kf.FIXTURE)
    d = {"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
         "use": {"clkrst": {}, "status": {}, "gpio": {"timed": True}},
         "build": {"sources": [str(kf.SPIKE_RM)]}}
    dfile = tmp_path / "spike_rm.json"
    dfile.write_text(json.dumps(d))
    out = tmp_path / "build dir with spaces" / "spike_rm"
    script.make_script(kits, pack="mps3", static_id="0x72BB0A36", design=str(dfile),
                       out_dir=out)
    return out


def run_preflight(build_dir: Path, release: str, build: int = 5076996,
                  *tclargs: str) -> tuple[bool, str]:
    """Source the script from ANOTHER directory (paths must resolve against the script's)."""
    tcl = tkinter.Tcl()
    tcl.eval(STUBS)
    tcl.eval(f"set STUB_VERSION {release}; set STUB_BUILD {build}")
    tcl.eval("set argv [list STOP_AFTER=preflight " + " ".join(tclargs) + "]")
    tcl.eval(f"cd {{{build_dir.parent.parent}}}")
    try:
        tcl.eval(f"source {{{(build_dir / 'build_rm.tcl').as_posix()}}}")
    except tkinter.TclError as exc:
        return False, str(exc)
    return True, ""


def gates(build_dir: Path) -> dict[str, str]:
    r = load_receipt(build_dir / "out" / "spike_rm_build.json")
    return {g.gate: g.verdict for g in r.gates}


def test_preflight_passes_on_the_kits_release_and_the_tcl_crc_is_the_static_id(build_dir):
    ok, err = run_preflight(build_dir, "2024.1")
    assert ok, err
    r = load_receipt(build_dir / "out" / "spike_rm_build.json")
    assert r.state == "stopped" and r.stage == "preflight"
    assert r.get("static_id") == "0x72BB0A36"            # Tcl's zlib crc32 == Python's
    g = gates(build_dir)
    assert g["vivado_version"] == "PASS" and g["static_id"] == "PASS"
    assert g["source_present"] == "PASS" and g["ooc_xdc_present"] == "PASS"
    assert "vivado_build" not in g


def test_another_major_minor_is_refused_before_synthesis(build_dir):
    ok, err = run_preflight(build_dir, "2021.1")
    assert not ok and "gate 'vivado_version' failed" in err
    r = load_receipt(build_dir / "out" / "spike_rm_build.json")
    assert r.state == "failed" and r.failed_gate.gate == "vivado_version"
    assert "a checkpoint opens only in the major.minor release that wrote it" in \
        r.failed_gate.detail
    assert "static_id" not in gates(build_dir)           # nothing after the refusal ran


def test_an_update_release_passes_and_another_build_is_only_noted(build_dir):
    ok, err = run_preflight(build_dir, "2024.1.2", 5100000)
    assert ok, err
    g = gates(build_dir)
    assert g["vivado_version"] == "PASS" and g["vivado_build"] == "NOTE"


def test_a_dcp_that_is_not_the_static_is_refused(build_dir):
    (build_dir / "kit" / "static" / "static_routed_locked.dcp").write_bytes(
        kf.fake_dcp("0x3F1A560F"))
    ok, err = run_preflight(build_dir, "2024.1")
    assert not ok and "gate 'static_id' failed" in err
    r = load_receipt(build_dir / "out" / "spike_rm_build.json")
    assert r.get("static_id") == "0x3F1A560F" and r.failed_gate.gate == "static_id"


def test_tclargs_override_the_rendered_values(build_dir):
    ok, err = run_preflight(build_dir, "2024.1", 5076996, "RM_ID=0x00000000")
    assert not ok and "rm_id_nonzero" in err
    ok, err = run_preflight(build_dir, "2024.1", 5076996, "NOPE=1")
    assert not ok and "unknown parameter 'NOPE'" in err
