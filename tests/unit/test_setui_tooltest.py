"""SET-UI: Tools Detect (``settings/tooltest.py``): what it runs, and what it refuses to.

Board-free and hermetic: fake executables in ``tmp_path``, a PATH of their own, and a runner
that records every argv. Each check has a negative twin.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from harness_manager.core.errors import UsageError
from harness_manager.settings import testers, tooltest
from harness_manager.settings.testers import TestRequest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="/bin/sh scripts stand in for the tools")


def exe(path: Path, body: str = "exit 0\n", *, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(mode)
    return path


class Recorder:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.argv.append(list(argv))
        return subprocess.run(argv, **kw)                     # noqa: S603 - a fake in tmp_path


# An OpenOCD 0.12 that prints its version, and its adapter list for `-c "adapter list"`.
OCD_012 = ('echo "Open On-Chip Debugger 0.12.0" >&2\n'
           'case "$2" in "adapter list") printf "The following debug adapters are available:\\n'
           '1: ftdi\\n2: remote_bitbang\\n\\n" >&2;; esac\n')
OCD_SOCLABS = OCD_012.replace("1: ftdi\\n2: remote_bitbang", "1: jlink\\n2: buspirate\\n3: hostio4")


def test_openocd_on_the_path_is_proven_by_its_version_and_its_adapters_and_nothing_else(tmp_path):
    ocd = exe(tmp_path / "bin" / "openocd", OCD_012)
    run = Recorder()
    step, found = tooltest.detect_one("openocd", "", {"PATH": str(tmp_path / "bin")}, runner=run)
    assert step == {"step": "openocd", "ok": True, "hint": "",
                    "detail": f"OpenOCD 0.12.0 at {ocd}: has remote_bitbang (2 adapters)"}
    assert found == {"path": str(ocd), "version": "0.12.0", "how": "PATH", "key": "tools.openocd",
                     "adapters": ["ftdi", "remote_bitbang"]}
    assert run.argv == [[str(ocd), "--version"], [str(ocd), "-c", "adapter list", "-c", "shutdown"]]


def test_negative_twin_an_openocd_without_remote_bitbang_fails_with_the_fix(tmp_path):
    from harness_manager.services import openocd_probe

    ocd = exe(tmp_path / "soclabs" / "openocd", OCD_SOCLABS)
    step, found = tooltest.detect_one("openocd", str(ocd), {"PATH": ""}, runner=Recorder())
    assert step["ok"] is False
    assert step["detail"] == f"OpenOCD 0.12.0 at {ocd}: no remote_bitbang (it has: jlink, buspirate, hostio4)"
    assert step["hint"] == openocd_probe.fix_hint()
    assert found["adapters"] == ["jlink", "buspirate", "hostio4"] and found["how"] == "setting"
    # set by the variable: the hint says the variable, which overrides the setting
    step, _ = tooltest.detect_one("openocd", str(ocd), {"PATH": ""}, runner=Recorder(),
                                  source="env")
    assert "$HARNESS_MANAGER_OPENOCD" in step["hint"] and "unset it" in step["hint"]


def test_the_path_search_takes_the_first_openocd_with_remote_bitbang(tmp_path):
    bad = exe(tmp_path / "a" / "openocd", OCD_SOCLABS)
    good = exe(tmp_path / "b" / "openocd", OCD_012)
    path = f"{bad.parent}{os.pathsep}{good.parent}"
    step, found = tooltest.detect_one("openocd", "", {"PATH": path}, runner=Recorder())
    assert step["ok"] is True and found["path"] == str(good)          # as debug up picks
    # twin: none has it: each is named, with what it has
    step, found = tooltest.detect_one("openocd", "", {"PATH": str(bad.parent)}, runner=Recorder())
    assert step["ok"] is False and "jlink, buspirate, hostio4" in step["detail"]
    other = exe(tmp_path / "c" / "openocd", OCD_SOCLABS.replace("hostio4", "cmsis-dap"))
    step, found = tooltest.detect_one("openocd", "", {"PATH": f"{bad.parent}{os.pathsep}{other.parent}"},
                                      runner=Recorder())
    assert step["ok"] is False and found["path"] == str(bad)
    assert step["detail"].startswith("no openocd on the service's PATH has remote_bitbang: ")
    assert str(bad) in step["detail"] and str(other) in step["detail"] and "cmsis-dap" in step["detail"]
    assert "xPack OpenOCD 0.12" in step["hint"]


def test_negative_twin_openocd_not_on_the_path_runs_nothing_and_says_how_to_fix_it(tmp_path):
    run = Recorder()
    step, _ = tooltest.detect_one("openocd", "", {"PATH": str(tmp_path / "empty")}, runner=run)
    assert step["ok"] is False and "not on the service's PATH" in step["detail"]
    assert "set tools.openocd" in step["hint"] and run.argv == []


def test_a_version_probe_that_prints_nothing_useful_fails(tmp_path):
    uv = exe(tmp_path / "uv", 'echo "hello"\n')
    step, _ = tooltest.detect_one("uv", str(uv), {"PATH": ""})
    assert step["ok"] is False and "printed no version line" in step["detail"]


def test_hw_server_is_never_run_and_its_release_comes_from_its_path(tmp_path):
    hw = exe(tmp_path / "Xilinx" / "Vivado" / "2023.2" / "bin" / "hw_server", 'touch "$0.ran"\n')
    run = Recorder()
    step, found = tooltest.detect_one("hw_server", str(hw), {"PATH": ""}, runner=run)
    assert step["ok"] is True and found["version"] == "2023.2" and run.argv == []
    assert not Path(f"{hw}.ran").exists()


def test_negative_twin_a_hw_server_that_is_not_executable_is_refused(tmp_path):
    hw = exe(tmp_path / "hw_server", mode=0o644)
    step, _ = tooltest.detect_one("hw_server", str(hw), {"PATH": ""})
    assert step["ok"] is False and "not executable" in step["detail"]


def test_vivado_off_is_a_pass_that_runs_nothing(tmp_path):
    run = Recorder()
    step, found = tooltest.detect_one("vivado", "off", {"PATH": ""}, runner=run)
    assert step["ok"] is True and found["how"] == "off" and run.argv == []


def test_negative_twin_a_vivado_path_that_is_not_one_fails_without_running(tmp_path):
    run = Recorder()
    step, _ = tooltest.detect_one("vivado", str(tmp_path / "nope"), {"PATH": ""}, runner=run)
    assert step["ok"] is False and run.argv == []


def test_the_tools_tester_is_found_by_convention_and_is_a_job(tmp_path):
    t = testers.tester_for("tools")
    assert t is not None and t.job is True and t.needs_name is False
    with pytest.raises(UsageError, match="no tool 'make'"):
        tooltest.detect_tools(TestRequest("tools", "make", None, None))
    got = tooltest.detect_tools(TestRequest("tools", "vivado", {"vivado": "off"}, None))
    assert got["ok"] is True and [s["step"] for s in got["steps"]] == ["vivado"]
