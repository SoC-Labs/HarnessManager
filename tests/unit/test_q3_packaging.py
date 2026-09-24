"""Q3 install: the packaging files agree with each other and with the code.

- constraints.txt (the lock ``scripts/install.sh`` installs by default) pins every runtime
  dependency and every user extra, satisfies pyproject.toml, and leaves pyverify to vendor/;
- the Linux menu entry (packaging/linux) is a valid .desktop template whose window class
  is the one the app window sets;
- the new scripts are executable and pass shellcheck;
- docs/INSTALL.md gives each distribution exactly the prerequisites the CI install matrix
  proves (.github/workflows/install-matrix.yml).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from harness_manager.web import window

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
CONSTRAINTS = ROOT / "constraints.txt"
DESKTOP = ROOT / "packaging" / "linux" / "harness-manager.desktop"
ICON = ROOT / "packaging" / "linux" / "harness-manager.svg"
MATRIX = ROOT / ".github" / "workflows" / "install-matrix.yml"
Q3_SCRIPTS = ("install.sh", "smoke_install.sh", "make_wheelhouse.sh", "lock_deps.sh")
USER_EXTRAS = ("serial", "app", "ina260")


def _name(req: str) -> str:
    return re.split(r"[\s<>=!~;\[]", req.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _pins() -> dict[str, list[str]]:
    pins: dict[str, list[str]] = {}
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"not a pin: {line}"
        pins.setdefault(_name(line), []).append(line)
    return pins


def test_constraints_pin_every_runtime_dependency_and_user_extra():
    pins = _pins()
    wanted = list(PYPROJECT["project"]["dependencies"])
    for extra in USER_EXTRAS:
        wanted += PYPROJECT["project"]["optional-dependencies"][extra]
    missing = [r for r in wanted if _name(r) not in pins and _name(r) != "mps3-pyverify"]
    assert not missing, f"constraints.txt lacks {missing}: run `make lock`"


def test_constraints_leave_pyverify_and_ourselves_out():
    pins = _pins()
    assert "mps3-pyverify" not in pins, "pyverify comes from vendor/ by path, never a pin"
    assert "harness-manager" not in pins


def test_tomli_is_pinned_for_python_310_only():
    (line,) = _pins()["tomli"]
    assert "python_full_version < '3.11'" in line or "python_version < '3.11'" in line


def test_each_pin_satisfies_pyproject():
    specifiers = pytest.importorskip("packaging.specifiers")
    requirements = pytest.importorskip("packaging.requirements")
    pins = _pins()
    wanted = list(PYPROJECT["project"]["dependencies"])
    for extra in USER_EXTRAS:
        wanted += PYPROJECT["project"]["optional-dependencies"][extra]
    for text in wanted:
        req = requirements.Requirement(text)
        name = _name(text)
        if name == "mps3-pyverify":
            continue
        for line in pins[name]:
            version = re.search(r"==\s*([^\s;]+)", line).group(1)
            assert specifiers.SpecifierSet(str(req.specifier)).contains(version), (
                f"{line} does not satisfy {text} in pyproject.toml")


def _desktop_keys(text: str) -> dict[str, str]:
    keys: dict[str, str] = {}
    group = ""
    for line in text.splitlines():
        if line.startswith("["):
            group = line.strip("[]")
        elif "=" in line and group == "Desktop Entry":
            k, v = line.split("=", 1)
            keys[k] = v
    return keys


def test_desktop_template():
    text = DESKTOP.read_text(encoding="utf-8")
    keys = _desktop_keys(text)
    assert keys["Type"] == "Application" and keys["Name"] == "Harness Manager"
    assert keys["Exec"] == '"@LAUNCHER@" app'        # quoted: a home path may have spaces
    assert keys["TryExec"] == "@LAUNCHER@" and keys["Icon"] == "@ICON@"
    assert keys["Terminal"] == "false"
    assert keys["X-HarnessManager-Installer"] == "scripts/install.sh"   # the uninstall marker
    assert '"@LAUNCHER@" app --demo' in text
    # The launcher icon groups with the app window: Chrome's --class sets WM_CLASS.
    args = window.app_mode_args("chrome", "http://127.0.0.1:1/", Path("/p"))
    assert f"--class={keys['StartupWMClass']}" in args
    assert "socharness" not in text.lower()


def test_desktop_file_validates(tmp_path: Path):
    tool = shutil.which("desktop-file-validate")
    if not tool:
        pytest.skip("desktop-file-validate is not installed")
    out = tmp_path / "harness-manager.desktop"
    out.write_text(DESKTOP.read_text(encoding="utf-8")
                   .replace("@LAUNCHER@", "/home/u/.local/bin/harness-manager")
                   .replace("@ICON@", "/home/u/.local/share/icons/hicolor/scalable/apps/"
                                      "harness-manager.svg"), encoding="utf-8")
    res = subprocess.run([tool, str(out)], capture_output=True, text=True)
    assert res.returncode == 0 and "error" not in res.stdout.lower(), res.stdout + res.stderr


def test_icon_is_the_app_favicon():
    ns = "{http://www.w3.org/2000/svg}"
    icon = ET.parse(ICON).getroot()
    fav = ET.parse(ROOT / "src" / "harness_manager" / "web" / "static" / "favicon.svg").getroot()
    assert icon.tag == f"{ns}svg" and icon.get("viewBox") == fav.get("viewBox")
    assert [e.tag for e in icon.iter()] == [e.tag for e in fav.iter()]


@pytest.mark.parametrize("script", Q3_SCRIPTS)
def test_script_is_executable_and_clean(script):
    path = ROOT / "scripts" / script
    assert os.access(path, os.X_OK) or os.name == "nt", f"{script} is not executable"
    assert "socharness" not in path.read_text(encoding="utf-8").lower()
    tool = shutil.which("shellcheck")
    if tool:
        res = subprocess.run([tool, str(path)], capture_output=True, text=True)
        assert res.returncode == 0, res.stdout


def _matrix_prereqs() -> list[str]:
    return re.findall(r"^\s+prereq: (.+)$", MATRIX.read_text(encoding="utf-8"), re.MULTILINE)


def test_install_matrix_covers_the_distributions():
    text = MATRIX.read_text(encoding="utf-8")
    for image in ("rockylinux/rockylinux:8", "rockylinux/rockylinux:9", "ubuntu:22.04",
                  "ubuntu:24.04", "debian:12", "fedora:latest"):
        assert f"image: {image}" in text, image
    assert 'branches: [main, "ci/**"]' in text
    assert "scripts/smoke_install.sh" in text and "--network none" in text


def test_install_doc_gives_what_ci_proves():
    doc = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    prereqs = _matrix_prereqs()
    assert len(prereqs) >= 6
    for line in prereqs:
        # The doc says it with sudo; the container runs as root.
        cmd = line.split("&& ", 1)[-1] if line.startswith("apt-get update") else line
        assert f"sudo {cmd}" in doc, f"docs/INSTALL.md lacks `sudo {cmd}`"
