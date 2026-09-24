"""T10: the MPS3 board-pin model: shape, provenance, and drift against the platform repo.

The drift tests read the platform repo only through ``git show`` and skip (with the
reason) where it is not checked out next to this one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from harness_manager.services import xdc
from harness_manager.services.xdc.model import PinModel
from harness_manager_mps3 import pins
from tests.fakes.t10_platform import PLATFORM, REPO, git_show, md_boundary, platform_ref

FIELDED = "0x72BB0A36"


@pytest.fixture(scope="module")
def model() -> PinModel:
    return PinModel(pins.load_model(), pack="mps3")


def test_the_model_is_labelled_derived_until_lane_c_lands(model):
    st = model.status
    assert st["derived"] is True and st["lane_c"] is False
    assert "DERIVED" in st["label"] and "board_pins.yaml" in st["label"]
    assert st["platform_ref"] == "feat/rm-ila-mint"
    assert len(st["platform_commit"]) == 40


def test_the_fielded_shells_boundary_is_47_ports_148_bits_20_intfs(model):
    b = model.shell(FIELDED)["rp_boundary"]
    assert b["totals"] == {"ports": 47, "bits": 148, "decoupler_intfs": 20}
    sigs = model.boundary(FIELDED)
    assert len(sigs) == 47 and sum(s.width for s in sigs) == 148
    assert model.boundary_groups() == ["clkrst", "jtag", "dbgbscan", "eth", "uart", "status",
                                       "gpio", "qspi"]
    assert model.default_shell == FIELDED
    # a shell the model does not describe is an AbsentError, not a silent fallback
    with pytest.raises(Exception, match="no shell"):
        model.shell("0x3F1A560F")


def test_every_net_sits_on_a_package_io_pin_in_its_bank(model):
    for name, n in model.nets.items():
        pp = model.package_pins[n["pin"]]
        assert pp["bank"] == n["bank"], name
        assert n["verified"] in model.status["verified_levels"], name
    assert len(model.by_pin) == len(model.nets)          # no pin carries two nets


def test_every_fact_names_a_source_the_model_lists(model):
    refs: list[str] = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "src":
                    refs.extend(v if isinstance(v, list) else [v])
                else:
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk({k: v for k, v in model.doc.items() if k != "sources"})
    assert refs
    for ref in refs:
        assert ref.split(":")[0] in model.sources, ref
    for key, s in model.sources.items():
        assert s["sha256"] and s["path"], key


def test_the_shell_sources_are_the_fielded_static_build_inputs(model):
    fielded = {k: s["fielded_static_input"] for k, s in model.sources.items()
               if "fielded_static_input" in s}
    assert fielded and all(fielded.values()), fielded
    assert {"mps3_harness_xdc", "rp_dut_stub", "shell_top", "dfx_floorplan_xdc"} <= set(fielded)


def test_bank_voltages_come_from_the_boards_own_standards(model):
    assert model.banks["44"]["vcco"] == 1.8           # LEDs, switches, buttons
    assert model.banks["84"]["vcco"] == 3.3 and model.banks["94"]["vcco"] == 3.3   # shields
    assert model.banks["65"]["vcco"] == 3.3           # QSPI flash, MCC tie-offs
    assert model.banks["45"]["vcco"] is None and "unknown" in model.banks["45"]["vcco_reason"]


def test_clock_capability_comes_from_the_package_function(model):
    assert model.package_pins["AK16"]["clock_capable"] == "GC"     # OSCCLK[1]
    assert model.package_pins["BA29"]["clock_capable"] == ""       # USER_SW[0]
    assert model.nets["OSCCLK[1]"]["oscillator"]["mhz"] == 50.0


def test_every_boundary_signal_has_a_connectivity_fact(model):
    conn = {c["signal"] for c in model.shell()["connectivity"]}
    assert conn == {s.name for s in model.boundary()}


def test_the_catalogue_lists_the_builtin_designs():
    cat = xdc.catalogue("mps3")
    names = {d["name"]: d["kit"] for d in cat["designs"]}
    assert names == {"minimal": "rm-kit", "nanosoc": "rm-kit", "nanosoc_ila": "rm-kit",
                     "blinky": "board", "shield_gpio": "board", "harness_shell": "board"}
    assert cat["model"]["shells"][FIELDED]["totals"]["ports"] == 47


# --- drift against the platform repo -----------------------------------------------------------


def model_boundary(model: PinModel) -> list[tuple[str, str, str, str]]:
    out = []
    for g in model.shell()["rp_boundary"]["groups"]:
        for s in g["signals"]:
            out.append((g["id"], s["name"], s["shell_dir"], s["width_symbol"] or str(s["width"])))
    return out


def test_drift_the_boundary_matches_partition_pins_md_on_the_fielded_branch(model):
    ref = platform_ref(model.doc)
    md = git_show(ref, "docs/contracts/partition-pins.md")
    assert md_boundary(md) == model_boundary(model), (
        f"the boundary in {ref}'s partition-pins.md moved: run tools/gen_mps3_pins.py")


def test_drift_the_boundary_matches_at_the_recorded_commit(model):
    md = git_show(model.status["platform_commit"], "docs/contracts/partition-pins.md")
    assert md_boundary(md) == model_boundary(model)


def test_the_drift_check_catches_a_moved_signal(model):
    md = git_show(model.status["platform_commit"], "docs/contracts/partition-pins.md")
    tampered = md.replace("| `jtag_tdo` | I |", "| `jtag_tdo` | O |")
    assert tampered != md
    assert md_boundary(tampered) != model_boundary(model)
    widened = md.replace("| `rm_id` | I | 32 |", "| `rm_id` | I | 64 |")
    assert md_boundary(widened) != model_boundary(model)


def _gen(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "tools" / "gen_mps3_pins.py"), *args],
                          capture_output=True, text=True, timeout=120)


def _need_generator_inputs(model: PinModel) -> None:
    if not (PLATFORM / ".git").exists():
        pytest.skip(f"the platform repo is not at {PLATFORM}")
    from pathlib import Path
    if not Path(model.sources["xilinx_pkg"]["path"]).is_file():
        pytest.skip("the Xilinx IBIS package file is not on this host")


def test_the_committed_model_is_what_the_generator_writes(model):
    _need_generator_inputs(model)
    r = _gen("--check", "--platform", str(PLATFORM))
    assert r.returncode == 0, r.stderr


def test_the_generator_check_fails_on_a_hand_edited_model(model, tmp_path):
    _need_generator_inputs(model)
    edited = tmp_path / "edited.json"
    doc = json.loads(pins.MODEL_FILE.read_text())
    doc["nets"]["USER_nLED[0]"]["pin"] = "AU30"
    edited.write_text(json.dumps(doc, indent=1) + "\n")
    r = _gen("--check", "--platform", str(PLATFORM), "--out", str(edited))
    assert r.returncode == 1 and "stale" in r.stderr


def test_the_generator_refuses_a_ref_whose_shell_is_not_the_fielded_one(model, tmp_path):
    _need_generator_inputs(model)
    if shutil.which("git") is None:
        pytest.skip("no git")
    # master still carries the 35-port boundary of the previous static: the three-way
    # boundary check or the fielded-hash check must refuse it, never write a model.
    r = _gen("--platform", str(PLATFORM), "--ref", "master", "--out", str(tmp_path / "m.json"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (tmp_path / "m.json").exists()
