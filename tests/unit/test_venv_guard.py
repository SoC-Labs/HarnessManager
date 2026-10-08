"""The developer-venv guards (tools/venv_guard.py, the Makefile) and the runtime self-check.

Each guard has a test that it fires on the bad state and a twin that it stays quiet on the
good one. Throwaway venvs (no pip) and fake packages in tmp_path; the real .venv is never read
or written.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from harness_manager.core.errors import HarnessError
from harness_manager.core.registry import preflight
from harness_manager_mps3 import selfcheck
from tools import venv_guard as vg

ROOT = Path(__file__).resolve().parents[2]



def _make_venv(path: Path) -> Path:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)], check=True,
                   capture_output=True, timeout=60)
    return path


def _site(venv: Path) -> Path:
    return next((venv / "lib").glob("python*/site-packages"))


def _checkout(path: Path) -> Path:
    pkg = path / "src" / "harness_manager"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("__version__ = '0'\n")
    return path


def _link(venv: Path, checkout: Path, tag: str) -> None:
    """What `pip install -e` leaves: an __editable__ .pth pointing at the checkout's src."""
    (_site(venv) / f"__editable__.harness_manager-{tag}.pth").write_text(
        str(checkout / "src") + "\n")


def _wheel(path: Path, body: str = "VALUE = 1\n") -> Path:
    whl = path / "mps3_pyverify-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("pyverify/__init__.py", "")
        z.writestr("pyverify/slot.py", body)
    return whl


def _install_wheel(venv: Path, whl: Path) -> None:
    with zipfile.ZipFile(whl) as z:
        z.extractall(_site(venv))


@pytest.fixture
def venv(tmp_path) -> Path:
    return _make_venv(tmp_path / "v")


# --- (c) a VENV outside the checkout ------------------------------------------------------


def test_path_guard_refuses_a_venv_outside_the_checkout(tmp_path):
    co = _checkout(tmp_path / "wt")
    with pytest.raises(vg.Refused, match="outside this checkout"):
        vg.check_path(tmp_path / "main" / ".venv", co)


def test_path_guard_allows_the_checkouts_own_venv(tmp_path):
    co = _checkout(tmp_path / "wt")
    vg.check_path(".venv", co)
    vg.check_path(co / ".venv", co)
    vg.check_path(co / "deep" / "v", co)


def test_path_guard_refuses_dotdot_escape(tmp_path):
    co = _checkout(tmp_path / "wt")
    with pytest.raises(vg.Refused):
        vg.check_path("../main/.venv", co)


# --- (a) the venv's harness_manager is this checkout's ------------------------------------


def test_harness_guard_refuses_another_checkouts_source(tmp_path, venv):
    mine, other = _checkout(tmp_path / "mine"), _checkout(tmp_path / "other")
    _link(venv, other, "1.0")
    with pytest.raises(vg.Refused, match="not from this checkout"):
        vg.check_harness(venv, mine)


def test_harness_guard_passes_for_this_checkout(tmp_path, venv):
    mine = _checkout(tmp_path / "mine")
    _link(venv, mine, "1.0")
    vg.check_harness(venv, mine)


def test_harness_guard_refuses_two_editable_links(tmp_path, venv):
    mine, other = _checkout(tmp_path / "mine"), _checkout(tmp_path / "other")
    _link(venv, mine, "1.0")
    _link(venv, other, "1.1")
    with pytest.raises(vg.Refused, match="2 editable"):
        vg.check_harness(venv, mine)


def test_harness_guard_refuses_a_venv_with_no_harness_manager(tmp_path, venv):
    with pytest.raises(vg.Refused, match="nowhere"):
        vg.check_harness(venv, _checkout(tmp_path / "mine"))


# --- (b) the venv's pyverify is the vendored wheel ----------------------------------------


def test_pyverify_guard_passes_for_the_installed_wheel(tmp_path, venv):
    whl = _wheel(tmp_path)
    _install_wheel(venv, whl)
    vg.check_pyverify(venv, whl)


def test_pyverify_guard_refuses_an_editable_dev_checkout(tmp_path, venv):
    whl = _wheel(tmp_path)
    dev = tmp_path / "platform" / "host" / "pyverify"
    (dev / "pyverify").mkdir(parents=True)
    (dev / "pyverify" / "__init__.py").write_text("")
    (_site(venv) / "__editable__.mps3_pyverify-0.1.0.pth").write_text(str(dev) + "\n")
    with pytest.raises(vg.Refused, match="outside the venv"):
        vg.check_pyverify(venv, whl)


def test_pyverify_guard_refuses_changed_content(tmp_path, venv):
    whl = _wheel(tmp_path)
    _install_wheel(venv, whl)
    (_site(venv) / "pyverify" / "slot.py").write_text("VALUE = 2\n")
    with pytest.raises(vg.Refused, match="differs"):
        vg.check_pyverify(venv, whl)


def test_pyverify_guard_cli_override_warns_instead_of_refusing(tmp_path, venv, capsys):
    whl = _wheel(tmp_path)
    args = ["pyverify", "--venv", str(venv), "--wheel", str(whl)]
    assert vg.main(args) == 1                       # no pyverify at all: refused
    assert vg.main([*args, "--allow-dev"]) == 0     # the explicit override: a warning
    err = capsys.readouterr().err
    assert "REFUSED" in err and "WARNING (allowed)" in err
    _install_wheel(venv, whl)
    assert vg.main(args) == 0                       # and quiet when it is fine
    assert capsys.readouterr().err == ""


# --- the Makefile -------------------------------------------------------------------------


def _make_n(tmp_path, *extra) -> str:
    platform = tmp_path / "platform"
    (platform / "host" / "pyverify").mkdir(parents=True, exist_ok=True)
    (platform / "host" / "pyverify" / "pyproject.toml").write_text("")
    out = subprocess.run(["make", "-n", "-B", f"VENV={tmp_path / 'v'}", f"PLATFORM={platform}",
                          "venv", *extra], cwd=ROOT, capture_output=True, text=True,
                         timeout=60, check=True).stdout
    return out


def test_make_venv_installs_the_vendored_wheel_even_with_a_platform_checkout(tmp_path):
    out = _make_n(tmp_path)
    assert "mps3_pyverify-" in out and "pip install -q -e " + str(tmp_path / "platform") not in out
    assert "venv_guard.py path" in out and "venv_guard.py harness" in out


def test_make_venv_links_the_platform_checkout_only_when_asked(tmp_path):
    assert f"-e {tmp_path / 'platform'}/host/pyverify" in _make_n(tmp_path, "DEV_PYVERIFY=1")
    assert "-e ../x/pv" in _make_n(tmp_path, "PYVERIFY=../x/pv")


def test_make_release_runs_the_pyverify_guard_and_the_override_is_explicit():
    base = ["make", "-n", "release"]
    out = subprocess.run(base, cwd=ROOT, capture_output=True, text=True, timeout=60).stdout
    assert "venv_guard.py pyverify" in out and "--allow-dev" not in out
    out = subprocess.run([*base, "ALLOW_DEV_PYVERIFY=1"], cwd=ROOT, capture_output=True,
                         text=True, timeout=60).stdout
    assert "--allow-dev" in out


# --- the runtime self-check ---------------------------------------------------------------


def _pack(tmp_path, imports: str) -> Path:
    d = tmp_path / "pack"
    d.mkdir()
    (d / "mod.py").write_text(imports + "\n")
    return d


def _fake_pyverify(tmp_path, monkeypatch, *, with_slot: bool) -> Path:
    root = tmp_path / "fakepv"
    pkg = root / "pyverify"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "client.py").write_text("CONTROL_PORT = 6900\n")
    if with_slot:
        (pkg / "slot.py").write_text("")
    for name in [n for n in sys.modules if n == "pyverify" or n.startswith("pyverify.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    return pkg


IMPORTS = "from pyverify import slot\nfrom pyverify.client import CONTROL_PORT\n"


def test_selfcheck_names_the_pyverify_and_the_fix_when_slot_is_missing(tmp_path, monkeypatch):
    pkg = _fake_pyverify(tmp_path, monkeypatch, with_slot=False)
    with pytest.raises(HarnessError) as ei:
        selfcheck.check(_pack(tmp_path, IMPORTS))
    assert "pyverify.slot" in ei.value.message and str(pkg) in ei.value.message
    assert "make clean venv" in ei.value.hint
    assert "internal error" not in str(ei.value)


def test_selfcheck_is_quiet_for_a_complete_pyverify(tmp_path, monkeypatch):
    _fake_pyverify(tmp_path, monkeypatch, with_slot=True)
    selfcheck.check(_pack(tmp_path, IMPORTS))


def test_selfcheck_catches_a_missing_name_not_only_a_missing_module(tmp_path, monkeypatch):
    _fake_pyverify(tmp_path, monkeypatch, with_slot=True)
    gone = selfcheck.missing(_pack(tmp_path, "from pyverify.client import NO_SUCH_THING\n"))
    assert gone == ["pyverify.client.NO_SUCH_THING"]


def test_selfcheck_reads_what_the_real_pack_imports():
    needs = selfcheck.needed()
    assert "slot" in needs["pyverify"] and "CONTROL_PORT" in needs["pyverify.client"]
    selfcheck.check()                      # the pyverify the tests run against is complete


def test_service_preflight_raises_the_clear_error_and_stays_quiet_when_fine(monkeypatch):
    monkeypatch.setattr(selfcheck, "missing", lambda *a, **k: ["pyverify.slot"])
    with pytest.raises(HarnessError, match="pyverify.slot"):
        preflight()
    monkeypatch.setattr(selfcheck, "missing", lambda *a, **k: [])
    preflight()


def test_make_available_for_these_tests():
    assert shutil.which("make"), "the Makefile tests need make"
