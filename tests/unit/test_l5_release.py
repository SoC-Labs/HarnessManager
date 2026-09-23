"""L5 release: the facts a release depends on agree with each other.

- one version, in pyproject.toml, ``harness_manager.__version__`` and CHANGELOG.md;
- the vendored pyverify wheel is the one vendor/README.md describes (sha256), and it
  satisfies the dependency in pyproject.toml;
- the install scripts are executable and pass shellcheck (when shellcheck is installed);
- no release file brings back the old product name.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import harness_manager

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VENDOR = ROOT / "vendor"
SCRIPTS = ("install.sh", "smoke_install.sh", "vendor_pyverify.sh")
RELEASE_FILES = ("README.md", "CHANGELOG.md", "docs/INSTALL.md", "docs/USER_GUIDE.md",
                 "vendor/README.md", "Makefile", "pyproject.toml", ".github/workflows/ci.yml",
                 *(f"scripts/{s}" for s in SCRIPTS), "scripts/install.ps1")


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def _pyverify_wheels() -> list[Path]:
    return sorted(VENDOR.glob("mps3_pyverify-*.whl"))


def test_one_version_everywhere():
    version = PYPROJECT["project"]["version"]
    assert harness_manager.__version__ == version
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    first = re.search(r"^## \[?(\d+\.\d+\.\d+[^\]\s]*)", changelog, re.MULTILINE)
    assert first, "CHANGELOG.md has no '## <version>' heading"
    assert first.group(1) == version, "the newest CHANGELOG entry is not the pyproject version"


def test_exactly_one_vendored_pyverify_wheel():
    assert len(_pyverify_wheels()) == 1, [p.name for p in _pyverify_wheels()]


def test_vendored_wheel_matches_its_readme():
    wheel = _pyverify_wheels()[0]
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    assert f"`{wheel.name}`" in readme
    recorded = re.search(r"\| sha256 \| `([0-9a-f]{64})` \|", readme)
    assert recorded, "vendor/README.md records no sha256"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == recorded.group(1), (
        "the wheel changed without scripts/vendor_pyverify.sh")
    assert re.search(r"\| Platform commit \| `[0-9a-f]{40}` ", readme), "no platform commit"


def test_vendored_wheel_satisfies_the_dependency_and_carries_what_we_use():
    wheel = _pyverify_wheels()[0]
    dep = next(d for d in PYPROJECT["project"]["dependencies"] if d.startswith("mps3-pyverify"))
    floor = re.search(r">=\s*([\d.]+)", dep)
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        meta = z.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
    assert "Name: mps3-pyverify" in meta
    have = re.search(r"^Version: (\S+)", meta, re.MULTILINE).group(1)
    if floor:
        assert _version_tuple(have) >= _version_tuple(floor.group(1))
    # the codec, the lease client (L1) and the executable spec the tests run against
    for module in ("client.py", "console.py", "swap.py", "pusher.py", "overlay.py", "rm_id.py",
                   "lease.py", "testing/fakeshell.py"):
        assert f"pyverify/{module}" in names, module


def test_vendored_openocd_configs_match_the_readme():
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    rows = dict(re.findall(r"\| `openocd/([^`]+)` \| `([0-9a-f]{64})` \|", readme))
    files = {p.name: p for p in (VENDOR / "openocd").iterdir() if p.is_file()}
    assert set(rows) == set(files), "vendor/openocd and vendor/README.md list different files"
    for name, digest in rows.items():
        # git may give a text file CRLF endings on Windows: hash it with LF endings
        data = files[name].read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == digest, f"openocd/{name} changed"
    # the configs the MPS3 pack maps a design with a debug port to
    from harness_manager_mps3.constants import DAP_DESIGN_CONFIGS
    for configs in DAP_DESIGN_CONFIGS.values():
        assert set(configs) <= set(files), configs


@pytest.mark.skipif(os.name == "nt", reason="executable bits are POSIX")
@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_executable(name):
    path = ROOT / "scripts" / name
    assert os.access(path, os.X_OK), f"chmod +x scripts/{name}"
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck is not installed")
def test_scripts_pass_shellcheck():
    res = subprocess.run(["shellcheck", *(str(ROOT / "scripts" / s) for s in SCRIPTS)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr


def test_release_files_use_the_product_name():
    old = "soc" + "harness"          # spelled apart so this file does not match itself
    for rel in RELEASE_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8").lower()
        assert old not in text, f"{rel} still says {old}"

