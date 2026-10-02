"""Lane WINDOWS: ``scripts/make_wheelhouse.sh --platform win_amd64`` end to end, with the
real pip, against a fake index (``--no-index --find-links``): the network is never used."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.fakes import win_wheels

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "make_wheelhouse.sh"


def _run(*args: str, timeout: float = 240) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), "--python", sys.executable, *args],
                          capture_output=True, text=True, timeout=timeout,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
                               "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})


def test_a_windows_wheelhouse_from_linux_with_the_real_pip(tmp_path):
    idx, hm, _ = win_wheels.index(tmp_path)
    out = tmp_path / "wh-win"
    # The fake lock stands in for constraints.txt: the script reads the checkout's, so the
    # fake's packages are unpinned there and pip takes the index's newest, except fakedep:
    # HM asks >=1 and the index has 1.0 and 2.0.
    res = _run("--platform", "win_amd64", "--python-version", "3.12", "--with-serial",
               "--hm-wheel", str(hm), "--no-index", "--find-links", str(idx), str(out))
    assert res.returncode == 0, res.stderr + res.stdout
    names = sorted(p.name for p in out.iterdir())
    assert "fakewinonly-1.0-cp312-cp312-win_amd64.whl" in names
    assert "uv-0.5.0-py3-none-win_amd64.whl" in names and "fakeserial-1.0-py3-none-any.whl" in names
    assert not any(n.startswith(("fakemaconly", "fakedev", "fakeold")) for n in names)
    assert {"harness_manager-9.0.0-py3-none-any.whl", "constraints.txt", "install.ps1",
            "install.sh", "SHA256SUMS"} <= set(names)
    assert any(n.startswith("mps3_pyverify-") for n in names)
    sums = (out / "SHA256SUMS").read_text()
    assert "install.ps1" in sums and "fakewinonly-1.0-cp312-cp312-win_amd64.whl" in sums
    assert "win_amd64, Python 3.12" in res.stdout and "install.ps1 -Offline" in res.stdout


def test_twin_no_wheel_for_that_python_fails_and_says_so(tmp_path):
    idx, hm, _ = win_wheels.index(tmp_path)
    res = _run("--platform", "win_amd64", "--python-version", "3.11", "--hm-wheel", str(hm),
               "--no-index", "--find-links", str(idx), str(tmp_path / "wh"))
    assert res.returncode != 0
    assert "could not download a win_amd64 wheel for Python 3.11" in res.stderr + res.stdout


def test_twin_platform_without_its_python_version_is_a_usage_error(tmp_path):
    res = _run("--platform", "win_amd64", str(tmp_path / "wh"))
    assert res.returncode == 2 and "needs --python-version" in res.stderr
    res = _run("--no-index", str(tmp_path / "wh"))
    assert res.returncode == 2 and "go with --platform" in res.stderr
