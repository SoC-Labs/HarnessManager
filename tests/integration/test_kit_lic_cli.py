"""KIT-LIC through ``cli/main.py``: ``kit guide`` says which device licence the kit's
release needs, launches the kit's Vivado once, and fails the Tools step when 2026.1 does
not start with no licence file (exit 42).

Vivado is a POSIX-sh fake (``kit_fakes.fake_vivado_script``) that answers ``-version`` and
a launch the way the real one did on 2026-09-28. PATH holds no vivado. Every success has a
failing twin.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from harness_manager.cli import main as cli_main
from harness_manager.services.kit import launch, vivado
from tests.fakes import kit_fakes as kf

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake vivado is POSIX sh")

SID = kf.STATIC_ID


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}/bin")
    launch.clear_cache()
    yield
    launch.clear_cache()


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli_main.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def kit26(tmp_path, capsys, monkeypatch, how: str, **kw):
    kit = kf.build_fixture(tmp_path / "kit26", SID, release="2026.1")
    assert run(capsys, "kit", "import", str(kit))[0] == 0
    kf.fake_vivado_script(tmp_path / "research" / "2026.1" / "Vivado" / "bin", "2026.1",
                          launch=how, **kw)
    monkeypatch.setenv(vivado.ENV, str(tmp_path / "research" / "2026.1"))    # the install dir


def tools_line(out: str) -> str:
    return next(ln for ln in out.splitlines() if " 2 Tools " in ln)


def test_kit_guide_2026_1_starts_and_names_the_device_licence(tmp_path, capsys, monkeypatch):
    kit26(tmp_path, capsys, monkeypatch, "ok")
    rc, out, err = run(capsys, "kit", "guide", "--static-id", SID)
    assert rc == 0, err
    line = tools_line(out)
    assert line.startswith("  done   2 Tools"), out
    assert "Vivado 2026.1 starts (licence tier: ENTERPRISE)" in line
    assert "synthesis still checks it" in line
    assert ("licence: unchecked (a device licence for xcku115 is needed from synthesis on: "
            "Vivado 2026.1 Core or higher (2024.1: Enterprise); the static's IP needs none; "
            "unchecked is not a pass") in line
    rc, out, _ = run(capsys, "kit", "guide", "--static-id", SID, "--json")
    doc = json.loads(out)
    tools = doc["steps"][1]
    assert {c["name"]: c["state"] for c in tools["checks"]}["vivado_launch"] == "ok"
    assert doc["vivado"]["launch"]["state"] == "ok" and doc["vivado"]["launch"]["rc"] == 0


def test_negative_twin_kit_guide_2026_1_with_no_licence_file(tmp_path, capsys, monkeypatch):
    kit26(tmp_path, capsys, monkeypatch, "no-licence")
    for k in launch.LICENCE_ENV:
        monkeypatch.delenv(k, raising=False)
    rc, out, err = run(capsys, "kit", "guide", "--static-id", SID)
    assert rc == 0, err                                     # the guide reports; it never fails
    line = tools_line(out)
    assert line.startswith("  FAILED 2 Tools"), out
    assert ("Vivado 2026.1 did not start: no licence file (set XILINXD_LICENSE_FILE or "
            "LM_LICENSE_FILE); it exited 42") in line
    assert "fix: export XILINXD_LICENSE_FILE=PORT@SERVER" in out
    rc, out, _ = run(capsys, "kit", "guide", "--static-id", SID, "--json")
    doc = json.loads(out)
    assert doc["steps"][1]["state"] == "failed"
    assert doc["steps"][1]["actions"][0]["text"] == "export XILINXD_LICENSE_FILE=PORT@SERVER"
    assert doc["vivado"]["launch"]["no_licence"] is True and doc["vivado"]["launch"]["rc"] == 42


def test_kit_guide_2024_1_names_enterprise(tmp_path, capsys, monkeypatch):
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    exe = kf.fake_vivado_script(tmp_path / "apps" / "Vivado" / "2024.1" / "bin", "2024.1")
    monkeypatch.setenv(vivado.ENV, str(exe))
    rc, out, err = run(capsys, "kit", "guide", "--static-id", SID)
    assert rc == 0, err
    line = tools_line(out)
    assert line.startswith("  done   2 Tools") and "Vivado 2024.1 starts" in line
    assert ("needed from synthesis on: Vivado 2024.1 Enterprise (2026.1: Core or higher); the "
            "static's IP needs none") in line
    assert "Core or higher (2024.1" not in line


def test_negative_twin_kit_guide_any_other_failed_start_shows_its_exit_code(tmp_path, capsys,
                                                                            monkeypatch):
    kit26(tmp_path, capsys, monkeypatch, "error")
    rc, out, _ = run(capsys, "kit", "guide", "--static-id", SID)
    assert rc == 0
    line = tools_line(out)
    assert line.startswith("  FAILED 2 Tools") and "Vivado 2026.1 did not start: it exited 1" in line
    assert "no licence" not in line
