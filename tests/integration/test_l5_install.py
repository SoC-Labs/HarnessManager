"""L5 install: ``scripts/install.sh`` end to end, in a throwaway HOME.

``scripts/smoke_install.sh`` installs this checkout, runs ``harness-manager version`` from
PATH, checks the launcher follows the app self-update pointer, starts
``harness-manager ui --demo --no-browser`` and fetches the page (it must carry the CSP
header) and ``/api/v1/health``, re-runs the installer (an upgrade in place, which stops
the demo service), and uninstalls (the state dir stays).

Once with venv + pip, and once with uv when uv is on PATH. It installs dependencies
from the package index, so it skips, with the reason, when the index is unreachable.
Linux and macOS only: CI runs ``scripts/install.ps1`` on Windows.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.packaging,
    pytest.mark.timeout(900),
    pytest.mark.skipif(os.name == "nt", reason="install.sh is for Linux and macOS"),
]

ROOT = Path(__file__).resolve().parents[2]


def index_unreachable() -> str | None:
    url = (os.environ.get("PIP_INDEX_URL") or "https://pypi.org/simple/").rstrip("/") + "/pip/"
    try:
        urllib.request.urlopen(url, timeout=15).close()
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot install from it"
        return f"the package index is unreachable ({url}: {exc}); this test installs from it"
    return None


@pytest.mark.parametrize("tool", ["pip", "uv"])
def test_install_sh_end_to_end(tool, tmp_path):
    if tool == "uv" and not shutil.which("uv"):
        pytest.skip("uv is not on PATH")
    if not shutil.which("curl"):
        pytest.skip("curl is not installed")
    reason = index_unreachable()
    if reason:
        pytest.skip(reason)
    args = ["--no-uv"] if tool == "pip" else []
    res = subprocess.run(["bash", str(ROOT / "scripts" / "smoke_install.sh"), *args],
                         cwd=tmp_path, capture_output=True, text=True, timeout=850)
    assert res.returncode == 0, f"{res.stdout[-4000:]}\n{res.stderr[-4000:]}"
    assert "SMOKE PASS" in res.stdout
    assert "content-security-policy:" in res.stdout.lower()
