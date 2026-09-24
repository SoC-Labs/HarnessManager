"""L5 packaging: what a user installs is complete.

Builds the sdist, then the wheel FROM the sdist (``python -m build``, as ``make dist``
does), installs the wheel with the vendored pyverify into a clean venv, and checks,
from that venv and away from this checkout:

- the web UI's static files are inside the package (``index.html``, ``vendor/``, all of them);
- the MPS3 board pack's entry point loads;
- ``python -m harness_manager.daemon --help`` and ``harness-manager version`` run;
- ``pip check`` finds nothing broken.

It builds from a copy of the checkout, so the checkout gets no ``build/`` or ``*.egg-info``.
It installs dependencies from the package index, so it skips, with the reason, when the
index is unreachable. About a minute.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

import pytest

import harness_manager

pytestmark = [pytest.mark.slow, pytest.mark.packaging, pytest.mark.timeout(900)]

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "harness_manager" / "web" / "static"
IGNORE = shutil.ignore_patterns(".git", ".venv", "build", "dist", "*.egg-info", "__pycache__",
                                ".pytest_cache", ".ruff_cache", "screenshots", "tests")


def index_unreachable() -> str | None:
    url = (os.environ.get("PIP_INDEX_URL") or "https://pypi.org/simple/").rstrip("/") + "/pip/"
    try:
        urllib.request.urlopen(url, timeout=15).close()
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot install from it"
        return f"the package index is unreachable ({url}: {exc}); this test installs from it"
    return None


def clean_env() -> dict[str, str]:
    """No PYTHONPATH or active venv leaking the checkout into the clean venv."""
    drop = {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_REQUIRE_VIRTUALENV"}
    return {k: v for k, v in os.environ.items() if k not in drop}


def run(argv, *, cwd: Path, timeout: float = 600) -> subprocess.CompletedProcess:
    res = subprocess.run([str(a) for a in argv], cwd=cwd, env=clean_env(), capture_output=True,
                         text=True, timeout=timeout)
    assert res.returncode == 0, (f"{' '.join(map(str, argv))} exited {res.returncode}\n"
                                 f"{res.stdout[-3000:]}\n{res.stderr[-3000:]}")
    return res


def static_files() -> list[str]:
    return sorted(p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts)


@pytest.fixture(scope="module")
def dist(tmp_path_factory) -> Path:
    pytest.importorskip("build", reason="the 'build' package is not installed (the dev extra)")
    reason = index_unreachable()
    if reason:
        pytest.skip(reason)
    work = tmp_path_factory.mktemp("l5-dist")
    src = work / "src"
    shutil.copytree(ROOT, src, ignore=IGNORE)
    out = work / "dist"
    run([sys.executable, "-m", "build", "--outdir", out, src], cwd=work)
    return out


@pytest.fixture(scope="module")
def venv_py(dist, tmp_path_factory) -> Path:
    home = tmp_path_factory.mktemp("l5-venv")
    venv = home / "venv"
    run([sys.executable, "-m", "venv", venv], cwd=home)
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheel = next(dist.glob("harness_manager-*.whl"))
    pip = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    # the vendored pyverify first, by path, as scripts/install.sh does: the index never
    # gets a say in which pyverify is installed
    run([*pip, *sorted((ROOT / "vendor").glob("mps3_pyverify-*.whl"))], cwd=home)
    run([*pip, "--find-links", ROOT / "vendor", wheel], cwd=home)
    return py


def test_the_sdist_carries_the_sources(dist):
    sdist = next(dist.glob("harness_manager-*.tar.gz"))
    top = f"harness_manager-{harness_manager.__version__}"
    with tarfile.open(sdist) as t:
        names = set(t.getnames())
    for rel in ("pyproject.toml", "README.md", "src/harness_manager_mps3/pack.py",
                "src/harness_manager/daemon/__main__.py"):
        assert f"{top}/{rel}" in names, rel
    missing = [f for f in static_files()
               if f"{top}/src/harness_manager/web/static/{f}" not in names]
    assert not missing, f"static files missing from the sdist: {missing}"


def test_the_wheel_carries_the_web_ui_and_the_entry_points(dist):
    wheel = next(dist.glob("harness_manager-*.whl"))
    assert wheel.name == f"harness_manager-{harness_manager.__version__}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        info = f"harness_manager-{harness_manager.__version__}.dist-info"
        entry_points = z.read(f"{info}/entry_points.txt").decode()
        metadata = z.read(f"{info}/METADATA").decode()
    missing = [f for f in static_files() if f"harness_manager/web/static/{f}" not in names]
    assert not missing, f"static files missing from the wheel: {missing}"
    assert "harness-manager = harness_manager.cli.main:main" in entry_points
    # what the installers put on PATH (lane OTA-L): it follows the self-update pointer
    assert "harness-manager-launch = harness_manager._launch:main" in entry_points
    assert "[harness_manager.boards]" in entry_points
    assert "mps3 = harness_manager_mps3.pack:Mps3Pack" in entry_points
    assert "Requires-Dist: mps3-pyverify" in metadata
    assert not any(n.startswith(("tests/", "vendor/")) for n in names)


def test_a_clean_install_has_the_static_files(venv_py, tmp_path):
    code = ("import sys, harness_manager.web as w; p = w.static_dir(); "
            "print(p); print((p / 'index.html').is_file()); print((p / 'vendor').is_dir()); "
            "print(sum(1 for f in p.rglob('*') if f.is_file() and '__pycache__' not in f.parts))")
    out = run([venv_py, "-c", code], cwd=tmp_path).stdout.split()
    where = Path(out[0])
    assert ROOT not in where.parents, f"the static files came from the checkout: {where}"
    assert out[1:3] == ["True", "True"], out
    assert int(out[3]) == len(static_files())


def test_a_clean_install_loads_the_mps3_board_pack(venv_py, tmp_path):
    code = ("from importlib.metadata import entry_points; "
            "eps = {e.name: e for e in entry_points(group='harness_manager.boards')}; "
            "cls = eps['mps3'].load(); print(cls.__module__, cls.__name__)")
    out = run([venv_py, "-c", code], cwd=tmp_path).stdout.strip()
    assert out == "harness_manager_mps3.pack Mps3Pack"


def test_a_clean_install_runs_the_daemon_module_and_the_command(venv_py, tmp_path):
    res = run([venv_py, "-m", "harness_manager.daemon", "--help"], cwd=tmp_path)
    assert "usage" in res.stdout.lower()
    exe = venv_py.parent / ("harness-manager.exe" if os.name == "nt" else "harness-manager")
    assert run([exe, "version"], cwd=tmp_path).stdout.strip() == harness_manager.__version__


def test_a_clean_install_passes_pip_check(venv_py, tmp_path):
    run([venv_py, "-m", "pip", "check", "--disable-pip-version-check"], cwd=tmp_path)
