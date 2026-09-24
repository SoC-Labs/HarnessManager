"""Find the Vivado that will run ``build_rm.tcl``, and say whether it can open the kit.

Where it looks, in order:

1. ``$HARNESS_MANAGER_VIVADO``: the ``vivado`` executable (or its install directory).
   ``off`` (or ``none``) turns discovery off: nothing is found and nothing is run
   (the tests set it; a machine with no Vivado may too);
2. ``vivado`` on ``PATH``;
3. ``$XILINX_VIVADO/bin/vivado``;
4. the standard install roots (``/tools/Xilinx/Vivado/*``, ``/opt/Xilinx/Vivado/*``,
   ``/apps/Xilinx/Vivado/*``, ``C:\\Xilinx\\Vivado\\*``): every release found is listed
   in ``others``, so the guide can say "2024.1 is installed but not on PATH".

The version comes from running ``vivado -version`` once per executable (a few seconds;
cached for the process, keyed on the file's mtime). It prints
``vivado v2024.1 (64-bit)`` and ``SW Build 5076996 on ...``. That is the only thing HM
ever runs, and never from ``make check``.

The rule (david K4): the kit's ``vivado.release`` and the Vivado found must agree on
major.minor, or the DCP will not open; HM WARNS (``check_release``), and the generated
``build_rm.tcl`` REFUSES. A different build number is a warning in both.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harness_manager.core.pack import KitCheck

from .schema import release_major_minor

ENV = "HARNESS_MANAGER_VIVADO"
OFF = ("off", "none", "0")
ROOTS_POSIX = ("/tools/Xilinx/Vivado", "/opt/Xilinx/Vivado", "/apps/Xilinx/Vivado")
ROOTS_WINDOWS = ("C:\\Xilinx\\Vivado", "C:\\AMDDesignTools\\Vivado")
VERSION_TIMEOUT_S = 120.0

_VER = re.compile(r"\bvivado\s+v(\d{4}\.\d+(?:\.\d+)?)", re.IGNORECASE)
_BUILD = re.compile(r"SW Build\s+(\d+)")
_ROOT_REL = re.compile(r"^(\d{4}\.\d+(?:\.\d+)?)$")

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class VivadoInstall:
    path: str                 # the vivado executable
    version: str = ""         # "2024.1" from `vivado -version`; "" when it could not be read
    build: int = 0
    how: str = ""             # "env" | "path" | "xilinx_vivado" | "install root"
    error: str = ""           # why the version could not be read

    def to_json(self) -> dict[str, object]:
        return {"path": self.path, "version": self.version, "build": self.build,
                "how": self.how, "error": self.error}


@dataclass(frozen=True)
class VivadoFound:
    """The discovery result: the Vivado HM would run, and the other releases installed."""

    install: VivadoInstall | None
    others: tuple[VivadoInstall, ...] = ()
    searched: tuple[str, ...] = ()
    disabled: bool = False
    reason: str = ""          # why nothing was found

    @property
    def found(self) -> bool:
        return self.install is not None

    def to_json(self) -> dict[str, object]:
        i = self.install
        return {"found": self.found, "path": i.path if i else None,
                "version": i.version if i else None, "build": i.build if i else None,
                "how": i.how if i else None, "error": i.error if i else None,
                "others": [o.to_json() for o in self.others], "reason": self.reason,
                "disabled": self.disabled}


_cache: dict[tuple[str, float], tuple[str, int, str]] = {}
_cache_lock = threading.Lock()


def read_version(exe: str, runner: Runner = subprocess.run,
                 timeout_s: float = VERSION_TIMEOUT_S) -> tuple[str, int, str]:
    """(release, build, error) from ``exe -version``. Cached per (path, mtime)."""
    try:
        mtime = os.stat(exe).st_mtime
    except OSError as exc:
        return "", 0, f"cannot stat {exe}: {exc.strerror}"
    key = (exe, mtime)
    with _cache_lock:
        if key in _cache and runner is subprocess.run:
            return _cache[key]
    try:
        proc = runner([exe, "-version"], capture_output=True, text=True, timeout=timeout_s)
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return "", 0, f"`{exe} -version` did not finish in {timeout_s:g} s"
    except OSError as exc:
        return "", 0, f"`{exe} -version` could not run: {exc}"
    m = _VER.search(out)
    b = _BUILD.search(out)
    result = (m.group(1) if m else "", int(b.group(1)) if b else 0,
              "" if m else f"`{exe} -version` printed no version line")
    if runner is subprocess.run:
        with _cache_lock:
            _cache[key] = result
    return result


def _exe_in(directory: Path) -> Path | None:
    for name in ("vivado", "vivado.bat"):
        for cand in (directory / "bin" / name, directory / name):
            if cand.is_file():
                return cand
    return None


def _roots() -> tuple[str, ...]:
    return ROOTS_WINDOWS if os.name == "nt" else ROOTS_POSIX


def discover(*, runner: Runner = subprocess.run, env: dict[str, str] | None = None,
             which: Callable[[str], str | None] = shutil.which,
             roots: tuple[str, ...] | None = None) -> VivadoFound:
    """Find Vivado (the order in the module docstring) and read its version."""
    env = dict(os.environ) if env is None else env
    searched: list[str] = []
    forced = env.get(ENV, "").strip()
    if forced.lower() in OFF:
        return VivadoFound(None, disabled=True, searched=(f"${ENV}={forced}",),
                           reason=f"Vivado discovery is off (${ENV}={forced})")

    def make(path: str, how: str) -> VivadoInstall:
        ver, build, err = read_version(path, runner)
        return VivadoInstall(path, ver, build, how, err)

    if forced:
        searched.append(f"${ENV}")
        p = Path(forced)
        exe = p if p.is_file() else _exe_in(p) if p.is_dir() else None
        if exe is None:
            return VivadoFound(None, searched=tuple(searched),
                               reason=f"${ENV}={forced} is not a vivado executable or install")
        return VivadoFound(make(str(exe), "env"), searched=tuple(searched))

    primary: VivadoInstall | None = None
    searched.append("PATH")
    on_path = which("vivado")
    if on_path:
        primary = make(on_path, "path")
    xv = env.get("XILINX_VIVADO", "").strip()
    if primary is None and xv:
        searched.append("$XILINX_VIVADO")
        exe = _exe_in(Path(xv))
        if exe is not None:
            primary = make(str(exe), "xilinx_vivado")
    others: list[VivadoInstall] = []
    for root in roots if roots is not None else _roots():
        searched.append(root)
        r = Path(root)
        if not r.is_dir():
            continue
        for rel in sorted(r.iterdir()):
            if not _ROOT_REL.match(rel.name):
                continue
            exe = _exe_in(rel)
            if exe is None or (primary and Path(primary.path).resolve() == exe.resolve()):
                continue
            # Not run: the release is the directory's name. Only the primary is executed.
            others.append(VivadoInstall(str(exe), rel.name, 0, "install root"))
    if primary is None and others:
        pick = others.pop()                       # the newest release under the roots
        primary = make(pick.path, "install root")
    reason = "" if primary else ("vivado is not on PATH, $XILINX_VIVADO is unset and no "
                                 "standard install root holds one")
    return VivadoFound(primary, tuple(others), tuple(searched), reason=reason)


def check_release(found: VivadoFound, need: str, need_build: int = 0) -> KitCheck:
    """The kit's release against the Vivado found. Never a ``mismatch`` (david K4: HM
    warns; the script refuses a different major.minor)."""
    if not need:
        return KitCheck("vivado", "unchecked", "the kit's Vivado release is not known yet "
                                               "(no kit for this static)")
    if found.install is None:
        return KitCheck("vivado", "warning", f"no Vivado found ({found.reason}); this kit "
                                             f"needs Vivado {need}")
    have = found.install.version
    if not have:
        return KitCheck("vivado", "warning", f"{found.install.path}: {found.install.error}; "
                                             f"this kit needs Vivado {need}")
    also = [o.version for o in found.others if release_major_minor(o.version)
            == release_major_minor(need)]
    if release_major_minor(have) != release_major_minor(need):
        hint = (f"; Vivado {also[0]} is installed at "
                f"{next(o.path for o in found.others if o.version == also[0])}: put it first "
                f"on PATH or set ${ENV}") if also else ""
        return KitCheck("vivado", "warning",
                        f"Vivado {have} found at {found.install.path}, but this kit's static was "
                        f"written by {need}: a checkpoint opens only in its own release, and "
                        f"build_rm.tcl will refuse to start{hint}")
    if need_build and found.install.build and found.install.build != need_build:
        return KitCheck("vivado", "warning",
                        f"Vivado {have} build {found.install.build}; the static was written by "
                        f"build {need_build} (the same release: the script notes it and goes on)")
    return KitCheck("vivado", "ok", f"Vivado {have} at {found.install.path}")

