"""T10: golden files for known designs, and the generated XDC against the platform's own.

Goldens: ``tests/fakes/t10_golden/<kit>_<design>/``. After a deliberate change, rewrite
them with ``HM_UPDATE_GOLDEN=1 pytest tests/unit/test_t10_golden.py`` and review the diff.

The platform comparisons read the RMs' hand-written OOC XDCs and the fielded shell's pin
XDCs through ``git show`` (skipped without the platform repo): a generated kit must
constrain the same clocks at the same periods and false-path the same ports, and the
``harness_shell`` board export must place every pad the fielded shell places.
"""

from __future__ import annotations

import json
import os

import pytest

from harness_manager.services import xdc
from harness_manager_mps3 import pins
from tests.fakes.t10_platform import GOLDEN, git_show, xdc_semantics

CASES = [("rm-kit", "nanosoc"), ("rm-kit", "nanosoc_ila"), ("board", "blinky")]
MODEL = pins.load_model()
REF = MODEL["status"]["platform_commit"]


@pytest.mark.parametrize("kit_name, design", CASES)
def test_golden(kit_name, design):
    kit = xdc.export("mps3", kit_name, design)
    assert kit.ok
    gdir = GOLDEN / f"{kit_name}_{design}"
    files = dict(kit.files)
    files["checks.json"] = json.dumps([f.__dict__ for f in kit.findings], indent=1, sort_keys=True) + "\n"
    if os.environ.get("HM_UPDATE_GOLDEN"):
        gdir.mkdir(parents=True, exist_ok=True)
        for old in gdir.iterdir():
            old.unlink()
        for name, text in files.items():
            (gdir / name).write_text(text)
    assert gdir.is_dir(), f"no golden for {kit_name} {design}: run with HM_UPDATE_GOLDEN=1"
    assert sorted(p.name for p in gdir.iterdir()) == sorted(files)
    for name, text in files.items():
        assert (gdir / name).read_text() == text, f"{gdir.name}/{name} differs from its golden"


def test_the_golden_check_catches_a_changed_line():
    kit = xdc.export("mps3", "rm-kit", "nanosoc")
    golden = (GOLDEN / "rm-kit_nanosoc" / "nanosoc_ooc.xdc").read_text()
    assert golden == kit.files["nanosoc_ooc.xdc"]
    assert golden.replace("-period 20.000", "-period 10.000") != kit.files["nanosoc_ooc.xdc"]


# --- against the platform's own constraint files --------------------------------------------


@pytest.mark.parametrize("design, path", [
    ("nanosoc", "fpga/rp/nanosoc/nanosoc_ooc.xdc"),
    ("nanosoc_ila", "fpga/rp/nanosoc_ila/nanosoc_ila_ooc.xdc"),
])
def test_the_rm_kit_constrains_what_the_platforms_rm_constrains(design, path):
    theirs = xdc_semantics(git_show(REF, path))
    ours = xdc_semantics(xdc.export("mps3", "rm-kit", design).files[f"{design}_ooc.xdc"])
    assert ours == theirs


def test_the_minimal_kit_matches_the_platform_template_except_the_unused_rmii_clock():
    theirs = xdc_semantics(git_show(REF, "fpga/rp/_template/template_ooc.xdc"))
    ours = xdc_semantics(xdc.export("mps3", "rm-kit", "minimal").files["minimal_ooc.xdc"])
    # the template declares phy_rmii_ref_clk with "DELETE THIS LINE if your RM leaves the
    # Ethernet group tied off"; the minimal design ties it off, so it is not declared
    assert theirs["clocks"].pop("phy_rmii_ref_clk") == 20.0
    assert ours == theirs


def test_the_semantic_comparison_catches_a_dropped_false_path():
    text = xdc.export("mps3", "rm-kit", "nanosoc").files["nanosoc_ooc.xdc"]
    theirs = xdc_semantics(git_show(REF, "fpga/rp/nanosoc/nanosoc_ooc.xdc"))
    assert xdc_semantics(text.replace("set_false_path -to   [get_ports -quiet jtag_tdo]", "")) != theirs


def test_the_harness_shell_export_places_every_pad_the_fielded_shell_places():
    theirs: dict[str, tuple[str, str]] = {}
    for path in ("fpga/shell/constraints/mps3_harness.xdc",
                 "fpga/shell/constraints/optional/mps3_harness_touch.xdc"):
        ports, _ = xdc.hdl.parse_xdc_pins(git_show(REF, path))
        theirs |= {n: (p.pin, p.iostandard) for n, p in ports.items() if p.pin}
    kit = xdc.export("mps3", "board", "harness_shell")
    assert kit.ok
    pins_x, _ = xdc.hdl.parse_xdc_pins(kit.files["harness_shell_pins.xdc"] + kit.files["harness_shell_io.xdc"])
    ours = {n: (p.pin, p.iostandard) for n, p in pins_x.items() if p.pin}
    assert len(theirs) == 78
    assert ours == theirs
