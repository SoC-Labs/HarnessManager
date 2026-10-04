"""Lane WINDOWS: scripts/install.ps1's offline wheelhouse, its pins and its Start menu entry,
read statically (no PowerShell here: tests/integration/test_q3_installer.py runs the script
under pwsh where pwsh is installed, and the Windows laptop checklist runs it for real)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PS1 = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")


def _line(pattern: str) -> list[str]:
    return [ln for ln in PS1.splitlines() if re.search(pattern, ln)]


def test_offline_latest_and_start_menu_are_parameters_with_help():
    params = PS1.split("param(", 1)[1].split(")", 1)[0]
    for p in ('[string]$Offline = ""', "[switch]$Latest", "[switch]$NoStartMenu"):
        assert p in params
    for doc in (".PARAMETER Offline", ".PARAMETER Latest", ".PARAMETER NoStartMenu"):
        assert doc in PS1
    assert "-Offline D:\\wheelhouse" in PS1                         # the example


def test_offline_never_contacts_the_index_and_takes_the_wheelhouses_wheel_and_pins():
    assert "$indexArgs = @('--no-index')" in PS1 and "$uvFlags = @('--offline')" in PS1
    (hm_pip,) = _line(r"Invoke-Native \$VenvPy \(\$pipBase \+ @\('--upgrade'\)")
    (hm_uv,) = _line(r"Invoke-Native \$uv \(\$pipBase \+ \$indexArgs \+ @\('--upgrade-package'")
    for line in (hm_pip, hm_uv):
        assert "$indexArgs" in line and "$findLinks" in line and "$constraintArgs" in line
    assert "Filter 'harness_manager-*.whl'" in PS1                  # -Offline with no -From
    assert "Join-Path $Offline 'constraints.txt'" in PS1             # the wheelhouse's pins first
    # pip is not upgraded offline (it would ask the index)
    assert re.search(r"if \(-not \$Offline\) \{\s+\$r = Invoke-Native \$VenvPy \(\$pipBase \+ "
                     r"@\('--upgrade', 'pip'\)\)", PS1)


def test_twin_offline_refuses_a_git_url_and_a_missing_directory():
    assert 'if ($Offline) { Fail "-Offline cannot clone $From; give a checkout or a wheel" }' in PS1
    assert "-Offline $Offline is not a directory" in PS1


def test_pins_hold_unless_latest_and_latest_rebuilds_the_venv():
    assert re.search(r"if \(-not \$Latest\) \{\s+\$candidates = @\(\)", PS1)
    assert "rebuilding $Venv with the newest dependency versions" in PS1


def test_the_start_menu_entry_is_this_users_and_uninstall_removes_it():
    assert "Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs'" in PS1
    assert "'Harness Manager.lnk'" in PS1 and "$lnk.Arguments = 'app'" in PS1
    assert "if ($NoStartMenu) { Remove-StartMenuEntry } else { Set-StartMenuEntry $Command }" in PS1
    uninstall = PS1.split("if ($Uninstall) {", 1)[1].split("exit 0", 1)[0]
    assert "Remove-StartMenuEntry" in uninstall


def test_twin_nothing_asks_for_administrator():
    for bad in ("RunAs", "-Verb", "Start-Process", "HKLM:", "ProgramData", "CommonPrograms"):
        assert bad not in PS1, bad
    assert "Nothing needs Administrator" in PS1


def test_a_failed_install_says_what_to_do_offline_and_online():
    assert "lacks a wheel this needs" in PS1 and "--platform win_amd64 --python-version X.Y" in PS1
    assert "with no network: make a wheelhouse elsewhere, then -Offline DIR" in PS1
