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


def test_openocd_on_the_path_is_proven_by_its_version_and_nothing_else(tmp_path):
    ocd = exe(tmp_path / "bin" / "openocd", 'echo "Open On-Chip Debugger 0.12.0" >&2\n')
    run = Recorder()
    step, found = tooltest.detect_one("openocd", "", {"PATH": str(tmp_path / "bin")}, runner=run)
    assert step == {"step": "openocd", "ok": True, "detail": f"OpenOCD 0.12.0 at {ocd}", "hint": ""}
    assert found == {"path": str(ocd), "version": "0.12.0", "how": "PATH", "key": "tools.openocd"}
    assert run.argv == [[str(ocd), "--version"]]


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
