"""Lane OTA-D test rig: a throwaway Harness Manager install with REAL side-by-side venvs.

``FakeInstall.build(base, {"0.1.0": "ok", "0.2.0": "ok", "0.3.0": "dies-at-start"})`` makes,
under ``base`` (always ``/tmp/otad-*``)::

    root/install.json            the installer's record (``_launch.register``)
    root/venv/                   the installer's venv: the FIRST version given
    root/versions/<v>/           one real venv (``python -m venv --without-pip``) per other version
    root/current.json            the pointer; every other version registered as ``staged``

Each venv is a real interpreter prefix whose site-packages holds a COPY of this checkout's
``harness_manager`` and ``harness_manager_mps3`` with ``__version__`` set to that version,
plus a ``harness_manager-<v>.dist-info``, and a ``.pth`` line to the test venv's
site-packages for the dependencies (fastapi, uvicorn, pyverify). So ``sys.prefix`` is the
install's own venv, ``_launch.dev_install()`` is ``""`` there, and every daemon, helper and
self-test runs exactly what that "version" contains. No uv, no wheel, no network.

Behaviours, patched into a version's copy:

- ``ok``: this checkout as it is;
- ``dies-at-start``: ``run_daemon`` exits at once (its ``--self-test`` still passes);
- ``dies-after:N``: the daemon answers ``/health``, then exits N seconds after it started;
- ``self-test-fails``: ``--self-test`` exits 1.

``env(...)`` is the isolated environment every process of a test gets: HOME, XDG, the state
dir, the PTY dir, no ``PYTHONPATH``, an update source that does not exist (no check ever
leaves the machine) and a first check an hour away.
"""

from __future__ import annotations

import json
import os
import shutil
import site
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from harness_manager import _launch

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
PACKAGES = ("harness_manager", "harness_manager_mps3")

_SERVER = "harness_manager/daemon/server.py"
#: how -> (anchor in server.py, text inserted, before (True) or after the anchor)
_PATCHES = {
    "dies-at-start": ("    state_dir = Path(state_dir)\n    if resume is not None:\n",
                      "    raise SystemExit('OTA-D test: this version cannot start')\n", True),
    "self-test-fails": ('    """``--self-test``: everything a start needs loads (app, engine, '
                        'packs, routes, server)."""\n', "    raise SystemExit(1)\n", False),
}


def _site_packages(venv: Path) -> Path:
    return venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def _patch(pkg_root: Path, how: str) -> None:
    if how == "ok":
        return
    path = pkg_root / _SERVER
    text = path.read_text()
    if how.startswith("dies-after:"):
        seconds = float(how.split(":", 1)[1])
        anchor = ("def _after_start(server: Any, d: Any, resume: dict[str, Any] | None) -> None:\n"
                  '    """Once the server answers: the resumed boards and PTYs, then the update '
                  'checker."""\n')
        insert, before = f"    threading.Timer({seconds}, os._exit, args=(3,)).start()\n", False
    else:
        anchor, insert, before = _PATCHES[how]
    assert anchor in text, f"server.py changed: update the {how!r} patch"
    path.write_text(text.replace(anchor, insert + anchor if before else anchor + insert, 1))


def make_venv(venv: Path, version: str, how: str) -> Path:
    """A real venv at ``venv`` running this checkout's code as ``version``."""
    base = getattr(sys, "_base_executable", "") or sys.executable
    subprocess.run([base, "-m", "venv", "--without-pip", str(venv)], check=True,
                   capture_output=True, timeout=120)
    sp = _site_packages(venv)
    sp.mkdir(parents=True, exist_ok=True)
    for pkg in PACKAGES:
        shutil.copytree(SRC / pkg, sp / pkg, ignore=shutil.ignore_patterns("__pycache__"))
    init = sp / "harness_manager" / "__init__.py"
    text = init.read_text()
    assert '__version__ = "' in text
    init.write_text(text.split('__version__ = "')[0] + f'__version__ = "{version}"\n')
    _patch(sp, how)
    info = sp / f"harness_manager-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: harness-manager\n"
                                   f"Version: {version}\n")
    (info / "INSTALLER").write_text("otad-test\n")
    # the board packs are found through this entry point (core/registry.py)
    (info / "entry_points.txt").write_text(
        "[harness_manager.boards]\nmps3 = harness_manager_mps3.pack:Mps3Pack\n")
    deps = [p for p in site.getsitepackages() if Path(p).is_dir()]
    (sp / "zz_otad_deps.pth").write_text("\n".join(deps) + "\n")
    return venv / "bin" / "python"


@dataclass
class FakeInstall:
    base: Path
    versions: dict[str, str]
    root: Path = field(init=False)
    installed: str = field(init=False)

    def __post_init__(self) -> None:
        self.root = self.base / "root"
        self.installed = next(iter(self.versions))

    @classmethod
    def build(cls, base: Path, versions: dict[str, str]) -> FakeInstall:
        assert str(base).startswith("/tmp/otad-"), base
        inst = cls(Path(base), dict(versions))
        inst.root.mkdir(parents=True, exist_ok=True)
        for v, how in inst.versions.items():
            venv = inst.root / "venv" if v == inst.installed else \
                inst.root / "versions" / _launch.version_dir(v)
            make_venv(venv, v, how)
        inst.reset()
        return inst

    def python(self, version: str) -> Path:
        if version == self.installed:
            return self.root / "venv" / "bin" / "python"
        return self.root / "versions" / _launch.version_dir(version) / "bin" / "python"

    def reset(self) -> None:
        """The pointer as installed (current = the installer's venv), every other version
        staged, no bad marks."""
        (self.root / "bad_versions.json").unlink(missing_ok=True)
        (self.root / "current.json").unlink(missing_ok=True)
        _launch.register(self.root, self.root / "venv", self.installed, extras=[], windows=False)
        ptr = json.loads((self.root / "current.json").read_text())
        ptr["versions"] = {v: {"state": "staged", "at": 0.0,
                               "wheel": f"harness_manager-{v}-py3-none-any.whl"}
                           for v in self.versions if v != self.installed}
        (self.root / "current.json").write_text(json.dumps(ptr, indent=1))

    def pointer(self) -> dict:
        return json.loads((self.root / "current.json").read_text())

    def bad(self) -> dict:
        path = self.root / "bad_versions.json"
        return json.loads(path.read_text()) if path.exists() else {}


def env(work: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The isolated environment of every process a test starts (``work`` is its own dir)."""
    home = work / "home"
    out = {k: v for k, v in os.environ.items()
           if not k.startswith("HARNESS_MANAGER_") and k not in ("PYTHONPATH", "PYTHONHOME")}
    out.update({
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
        "HARNESS_MANAGER_STATE_DIR": str(work / "state"),
        "HARNESS_MANAGER_PTY_DIR": str(work / "pty"),
        "HARNESS_MANAGER_UPDATE_SOURCE": str(work / "no-channel-here"),
        "HARNESS_MANAGER_UPDATE_FIRST_CHECK_S": "3600",
        **(extra or {}),
    })
    home.mkdir(parents=True, exist_ok=True)
    return out
