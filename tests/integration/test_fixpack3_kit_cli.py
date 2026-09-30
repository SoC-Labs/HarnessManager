"""FIX-PACK-3 items 4-6 through ``harness-manager kit`` (cli/main.py), as P8 typed them.

The fixture kit and a faked Vivado discovery, as ``test_kit_cli.py`` (its autouse fixture).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from harness_manager.services.kit import render
from tests.fakes import kit_fakes as kf
from tests.integration.test_kit_cli import design, fake_vivado, run  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parents[2]
REAL_RPT = ROOT / "docs/evidence/2026-09-30-kit-nanosoc/vivado/nanosoc_timing_head.rpt"


def test_kit_build_names_the_anchored_verdict(tmp_path, capsys):
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    bdir = tmp_path / "b"
    rc, _, err = run(capsys, "kit", "script", "--static-id", "0x72BB0A36", "--design",
                     str(design(tmp_path)), "--out", str(bdir))
    assert rc == 0, err
    rc, out, _ = run(capsys, "kit", "build", str(bdir))
    assert rc == 0 and render.VERDICT_GREP in out and "STARTS with HM_RM_BUILD_" in out
    assert "the last HM_RM_BUILD_* line" not in out                  # twin: the old words


def test_kit_fetch_suggests_a_build_dir_under_home(tmp_path, capsys):
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    rc, out, _ = run(capsys, "kit", "fetch", "--static-id", "0x72BB0A36", "--out",
                     str(tmp_path / "kit"))
    assert rc == 0 and "--out ~/builds/my_rm" in out
    assert "--out build/my_rm" not in out                            # twin: the relative one


def test_kit_check_says_there_is_no_timed_path_inside_the_partition(tmp_path, capsys):
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    receipt = kf.passed_build(tmp_path / "b", rm_wns="", rm_whs="")   # an old receipt
    shutil.copy(REAL_RPT, receipt.parent / "spike_rm_timing.rpt")
    rc, out, _ = run(capsys, "kit", "check", str(receipt))
    assert rc == 0, out
    assert ("no timed path inside the partition; whole-design WNS 0.207 ns, WHS 0.030 ns "
            "from spike_rm_timing.rpt") in out
    # twin: an RM with its own paths prints its own figures
    receipt = kf.passed_build(tmp_path / "c")
    rc, out, _ = run(capsys, "kit", "check", str(receipt))
    assert rc == 0 and "your RM's paths: setup WNS 18.057 ns, hold WHS 0.079 ns" in out
    assert "no timed path" not in out
