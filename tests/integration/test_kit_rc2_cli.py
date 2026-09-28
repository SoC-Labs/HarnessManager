"""KIT-RC2 through ``cli/main.py``: ``kit build`` prints the Vivado of the kit's release by
its full path (or fails with exit 12), ``kit script --design minimal`` builds the skeleton,
and ``kit check --static-id`` refuses a receipt of another static (exit 14).

A kit written by "2026.1" (the fixture DCP with a 2026.1 ``dcp.xml``) for 0x72BB0A36, a
static the committed pin model describes. Vivado is a POSIX-sh fake that only prints a
version (``kit_fakes.fake_vivado_script``); a fake 2024.1 on PATH plays
``/etc/profile.d/xilinx.sh``. Every success has a failing twin.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from harness_manager.cli import main as cli_main
from harness_manager.core.errors import ExitCode
from harness_manager.services.kit import vivado
from tests.fakes import kit_fakes as kf

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake vivado is POSIX sh")

SID = kf.STATIC_ID


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    # PATH's vivado is 2024.1 (the lab login profile's); nothing else named vivado on PATH
    old = kf.fake_vivado_script(tmp_path / "apps" / "Vivado" / "2024.1" / "bin", release="2024.1")
    monkeypatch.setenv("PATH", f"{old.parent}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return old


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli_main.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture
def bdir(tmp_path, capsys, monkeypatch):
    """The 2026.1 kit imported and ``kit script --design minimal`` written (the right Vivado)."""
    kit = kf.build_fixture(tmp_path / "kit2026", SID, release="2026.1")
    assert run(capsys, "kit", "import", str(kit))[0] == 0
    new = kf.fake_vivado_script(tmp_path / "research" / "2026.1" / "Vivado" / "bin",
                                release="2026.1")
    monkeypatch.setenv(vivado.ENV, str(tmp_path / "research" / "2026.1"))   # the install DIR
    d = tmp_path / "build" / "minimal"
    rc, out, err = run(capsys, "kit", "script", "--static-id", SID, "--design", "minimal",
                       "--out", str(d))
    assert rc == 0, err
    assert f"next: {new} -mode batch" in out
    # the notes say the skeleton is the RTL, and PATH's 2024.1 is not the one to run
    assert "RM_SOURCES is the XDC kit's skeleton xdc/minimal_wrapper_skeleton.sv" in out
    assert "`vivado` on PATH is Vivado 2024.1" in out
    return d, new


def test_kit_build_prints_the_full_path_of_the_kits_release(bdir, capsys):
    d, new = bdir
    rc, out, _ = run(capsys, "kit", "build", str(d))
    assert rc == 0 and out.startswith(f"{new} -mode batch -source ")
    assert "`vivado` on PATH is Vivado 2024.1" in out
    rc, out, _ = run(capsys, "kit", "build", str(d), "--json")
    doc = json.loads(out)
    assert doc["command"][0] == str(new) and doc["release"] == "2026.1"
    assert doc["vivado"]["on_path"]["version"] == "2024.1"
    tcl = (d / "build_rm.tcl").read_text()
    assert "RM_SOURCES        {xdc/minimal_wrapper_skeleton.sv}" in tcl
    sk = (d / "xdc" / "minimal_wrapper_skeleton.sv").read_text()
    assert "assign dut_lockup = 1'b0;" in sk and "assign irq_out = 1'b0;" in sk


def test_negative_twin_kit_build_fails_when_no_vivado_of_the_release(bdir, capsys, monkeypatch):
    d, _ = bdir
    # the setting names 2024.1 (as PATH does): no 2026.1 to print
    monkeypatch.setenv(vivado.ENV, os.environ["PATH"].split(os.pathsep)[0] + "/vivado")
    rc, _, err = run(capsys, "kit", "build", str(d))
    assert rc == ExitCode.UNAVAILABLE and "no Vivado 2026.1" in err and "Vivado 2024.1 found" in err
    # discovery off: the same refusal, never a bare `vivado`
    monkeypatch.setenv(vivado.ENV, "off")
    rc, out, err = run(capsys, "kit", "build", str(d))
    assert rc == ExitCode.UNAVAILABLE and "discovery is off" in err and "-mode batch" not in out


def test_kit_check_static_id_must_be_the_receipts(bdir, capsys):
    d, _ = bdir
    kf.passed_build(d, name="minimal", rm_id="0x0100F28A")        # a receipt for 0x72BB0A36
    rc, out, _ = run(capsys, "kit", "check", str(d), "--static-id", SID)
    assert rc == 0 and "expected_static" in out
    rc, _, err = run(capsys, "kit", "check", str(d), "--static-id", "0x44EE76D5")
    assert rc == ExitCode.INCOMPATIBLE and "expected_static" in err and "0x44EE76D5" in err
