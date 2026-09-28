"""REVIEW-W5 items 13-16 (Windows; CI is billing-blocked, so these run here), board-free.
Every check has a negative twin.

13. the hub SD upload's remote mkdir is a POSIX path;
14. ``core.proc.no_window`` for the Detect runner, ``openocd_probe`` and the hub runners;
15. ``openocd_probe``'s fix hint encodes in cp1252 (a Windows console);
16. the Windows skips are in place, and the settings tests write TOML literal strings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

# --- 14. no console window ---------------------------------------------------------------------


def test_no_window_is_the_windows_flag_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness_manager.core import proc

    monkeypatch.setattr(proc.sys, "platform", "linux")
    assert proc.no_window() == {}
    monkeypatch.setattr(proc.sys, "platform", "win32")
    assert set(proc.no_window()) == {"creationflags"}


def test_the_hub_runner_passes_no_window_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hub call (``hub_mcc``'s MCC reads and REBOOTs among them) is a ``subprocess.run``
    of ssh: on Windows each flashed a console window. Twin: elsewhere pyverify's own runner
    class and call are used, unchanged."""
    from pyverify.lease import SshHubRunner

    from harness_manager_mps3 import hub

    seen: list[dict[str, Any]] = []

    def fake_run(cmd: Any, **kw: Any) -> Any:
        seen.append(kw)
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    plain = hub.default_runner_factory("hub.invalid", "fpga")
    # FIX-PACK-1 5a (updated deliberately): pyverify's SshHubRunner under the per-hub
    # one-shot cap, a subclass whose call is pyverify's own; still not the windowless one.
    assert isinstance(plain, SshHubRunner) and "Windowless" not in type(plain).__name__
    plain(["fpgahub", "whoami"], timeout=5)
    assert "creationflags" not in seen[-1]
    monkeypatch.setattr(hub, "no_window", lambda: {"creationflags": 0})
    for runner in (hub.default_runner_factory("hub.invalid", "fpga"),
                   hub.default_runner_factory("hub.invalid", "fpga", jump="bastion.invalid"),
                   hub.default_runner_factory("local", "fpga")):
        assert isinstance(runner, SshHubRunner) or type(runner).__name__.startswith("Windowless")
        out = runner(["fpgahub", "whoami"], timeout=5)
        assert out.returncode == 0 and out.stdout == "ok\n"
        assert seen[-1].get("creationflags") == 0 and seen[-1]["timeout"] == 5


# --- 13. the hub SD upload's remote mkdir -------------------------------------------------------


def test_the_remote_mkdir_is_a_posix_path_even_where_path_is_windows(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from harness_manager_mps3 import hub_sd

    monkeypatch.setattr(hub_sd, "Path", PureWindowsPath)      # what Path is on Windows
    argv = hub_sd.SshUploader("hub.invalid").argv("/srv/hm-stage/abc.bit")
    assert argv[-1].startswith("mkdir -p /srv/hm-stage && cat > /srv/hm-stage/abc.bit.part")
    assert "\\" not in argv[-1]
    # twin: a bare name makes its directory "."
    assert hub_sd.SshUploader("hub.invalid").argv("abc.bit")[-1].startswith("mkdir -p . &&")


# --- 15. cp1252 --------------------------------------------------------------------------------


def test_the_openocd_fix_hint_prints_on_a_cp1252_console() -> None:
    from harness_manager.services import openocd_probe

    for hint in (openocd_probe.fix_hint(), openocd_probe.fix_hint(env_var="X")):
        hint.encode("cp1252")                 # UnicodeEncodeError on "→" before the fix
        assert "Settings -> Tools" in hint


# --- 16. the Windows skips ---------------------------------------------------------------------


def _skip_reasons(fn: Any) -> list[str]:
    return [m.kwargs.get("reason", "") for m in getattr(fn, "pytestmark", [])
            if m.name == "skipif"]


def test_the_sh_dependent_tests_skip_off_posix() -> None:
    from tests.unit import test_linux_claim as LC
    from tests.unit import test_mcc_fix_hub as MF

    for fn in (LC.test_the_hub_helper_refuses_clearly_when_the_hub_has_only_python_3_6,
               LC.test_negative_twin_python3_11_is_tried_first_and_runs_the_helper,
               LC.test_negative_twin_a_bare_python3_that_is_new_enough_is_used,
               LC.test_the_hub_call_turns_no_python_into_a_hint,
               MF.test_the_python_probe_takes_310_and_never_the_bare_python3):
        assert any("/bin/sh" in r for r in _skip_reasons(fn)), fn.__name__


def test_no_settings_wire_test_writes_a_path_into_a_toml_basic_string() -> None:
    """``openocd = "{tmp_path / …}"`` is ``C:\\Users\\…`` on Windows: TOML takes ``\\U`` as an
    escape. The tests write TOML literal strings (``lit``)."""
    import re

    src = (Path(__file__).parent / "test_settings_wire.py").read_text(encoding="utf-8")
    assert not re.findall(r"""(?:= |\[|, )"\{[^}]*\}\"""", src)
    import tomllib

    from tests.unit.test_settings_wire import lit

    win = "C:\\Users\\me\\AppData\\Local\\Temp\\openocd"
    assert tomllib.loads(f"k = {lit(win)}\n")["k"] == win


def test_the_hub_sd_upload_and_the_hub_test_open_no_console_on_windows(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness_manager.settings import hubtest
    from harness_manager_mps3 import hub_sd

    seen: list[dict[str, Any]] = []

    def fake_run(cmd: Any, **kw: Any) -> Any:
        seen.append(kw)
        return subprocess.CompletedProcess(cmd, 0, "", b"" if "text" not in kw else "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    bit = tmp_path / "x.bit"
    bit.write_bytes(b"\0")
    hub_sd.SshUploader("hub.invalid")(bit, "/srv/stage/x.bit")
    hubtest._default_run(5.0)(["ssh", "hub.invalid", "true"])
    assert all("creationflags" not in kw for kw in seen), "twin: not on Linux"
    seen.clear()
    monkeypatch.setattr(hub_sd, "no_window", lambda: {"creationflags": 0})
    monkeypatch.setattr(hubtest, "no_window", lambda: {"creationflags": 0})
    hub_sd.SshUploader("hub.invalid")(bit, "/srv/stage/x.bit")
    hubtest._default_run(5.0)(["ssh", "hub.invalid", "true"])
    assert len(seen) == 2 and all(kw.get("creationflags") == 0 for kw in seen)
