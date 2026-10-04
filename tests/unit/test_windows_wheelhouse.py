"""Lane WINDOWS: the cross-platform wheelhouse (scripts/wheelhouse_cross.py). The pip
command line is checked, and the dependency walk runs against a fake index with a fake
``pip download`` (it copies from the fake index): no pip, no network."""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest

from tests.fakes import win_wheels

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("wheelhouse_cross",
                                               ROOT / "scripts" / "wheelhouse_cross.py")
WC = importlib.util.module_from_spec(_spec)
sys.modules["wheelhouse_cross"] = WC
_spec.loader.exec_module(WC)


def test_the_pip_command_line_is_wheels_only_for_the_laptops_tags():
    argv = WC.download_argv("py", WC.target_of("win_amd64"), "3.12", Path("/out"),
                            ["fakedep==1.0"], find_links=["vendor"])
    assert argv == ["py", "-m", "pip", "download", "--disable-pip-version-check", "--quiet",
                    "--no-deps", "--only-binary=:all:", "--platform", "win_amd64",
                    "--python-version", "3.12", "--implementation", "cp", "--dest", "/out",
                    "--find-links", "vendor", "fakedep==1.0"]
    mac = WC.download_argv("py", WC.target_of("macos-arm64"), "3.11", Path("/o"), ["x"],
                           no_index=True, index_url="https://mirror.example/simple")
    assert mac[mac.index("--platform") + 1] == "macosx_11_0_arm64"
    assert "--no-index" in mac and mac[mac.index("--index-url") + 1] == \
        "https://mirror.example/simple"


def test_twin_an_unknown_platform_or_an_old_python_is_refused():
    with pytest.raises(SystemExit, match="unknown platform 'win32_arm'"):
        WC.target_of("win32_arm")
    with pytest.raises(SystemExit, match="not 3.10 or newer"):
        WC.python_version_of("3.9")
    with pytest.raises(SystemExit, match="not 3.10 or newer"):
        WC.python_version_of("3")


def test_markers_are_evaluated_as_on_the_laptop_not_here():
    from packaging.requirements import Requirement

    win, mac = WC.target_of("win_amd64"), WC.target_of("macos_x86_64")
    r = Requirement('pywin32-ctypes>=0.2; sys_platform == "win32"')
    assert WC.applies(r, win, "3.12", ()) and not WC.applies(r, mac, "3.12", ())
    old = Requirement('tomli>=2; python_version < "3.11"')
    assert WC.applies(old, win, "3.10", ()) and not WC.applies(old, win, "3.12", ())
    ser = Requirement('pyserial>=3.5; extra == "serial"')
    assert WC.applies(ser, win, "3.12", ("serial",)) and not WC.applies(ser, win, "3.12", ())


def _fake_pip(idx: Path, calls: list[list[str]]):
    """``pip download --no-deps``: copy the named version's wheel for the target's tag."""

    def run(argv: list[str]) -> int:
        calls.append(argv)
        out = Path(argv[argv.index("--dest") + 1])
        tags = [argv[i + 1] for i, a in enumerate(argv) if a == "--platform"]
        py = argv[argv.index("--python-version") + 1].replace(".", "")
        for spec in argv[argv.index("--dest") + 2:]:
            if spec.startswith("--") or spec in out.as_posix() or Path(spec).is_dir():
                continue
            name = re.split(r"[<>=!~]", spec, maxsplit=1)[0]
            version = spec.partition("==")[2]
            dist = name.replace("-", "_")
            hits = []
            for w in idx.glob(f"{dist}-{version or '*'}-*.whl"):
                tag = w.stem.split("-", 2)[2]
                ok = tag.endswith("none-any") or any(tag.endswith(t) for t in tags)
                if ok and ("cp3" not in tag or f"cp{py}" in tag):
                    hits.append(w)
            if not hits:
                return 1
            shutil.copy(sorted(hits)[-1], out)
        return 0
    return run


def test_the_walk_fetches_what_the_laptop_needs_pinned(tmp_path):
    idx, hm, cons = win_wheels.index(tmp_path)
    out, calls = tmp_path / "wh", []
    got = WC.walk(hm, cons, WC.target_of("win_amd64"), "3.12", out, extras=["serial"],
                  find_links=[str(idx)], no_index=True, python="py",
                  run=_fake_pip(idx, calls))
    names = sorted(p.name for p in out.glob("*.whl"))
    assert names == ["fakechild-1.0-py3-none-any.whl", "fakedep-1.0-py3-none-any.whl",
                     "fakegrand-1.0-py3-none-any.whl", "fakeserial-1.0-py3-none-any.whl",
                     "fakewinonly-1.0-cp312-cp312-win_amd64.whl",
                     "uv-0.5.0-py3-none-win_amd64.whl"]
    assert "fakedep==1.0" in got and "uv>=0.4" in got           # pinned; uv by its range
    assert all(c[c.index("--platform") + 1] == "win_amd64" for c in calls)
    assert all("--no-deps" in c and "--only-binary=:all:" in c for c in calls)


def test_twin_macos_gets_its_own_and_never_the_windows_only_or_the_dev_extra(tmp_path):
    idx, hm, cons = win_wheels.index(tmp_path)
    out, calls = tmp_path / "wh", []
    WC.walk(hm, cons, WC.target_of("macos_arm64"), "3.10", out, find_links=[str(idx)],
            no_index=True, python="py", run=_fake_pip(idx, calls))
    names = sorted(p.name.split("-")[0] for p in out.glob("*.whl"))
    assert names == ["fakechild", "fakedep", "fakegrand", "fakemaconly", "fakeold", "uv"]
    assert not any("fakedev" in " ".join(c) or "mps3" in " ".join(c) for c in calls)


def test_twin_no_wheel_for_the_laptop_fails_loudly(tmp_path):
    idx, hm, cons = win_wheels.index(tmp_path)
    with pytest.raises(SystemExit, match=r"no\nwheel for win_amd64|could not download a "
                                         r"win_amd64 wheel for Python 3.11"):
        WC.walk(hm, cons, WC.target_of("win_amd64"), "3.11", tmp_path / "wh",
                find_links=[str(idx)], no_index=True, python="py",
                run=_fake_pip(idx, []))
