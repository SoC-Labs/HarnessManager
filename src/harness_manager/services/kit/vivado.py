"""Find the Vivado that will run ``build_rm.tcl``, and say whether it can open the kit.

Where it looks, in order:

1. the setting ``tools.vivado``: ``$HARNESS_MANAGER_VIVADO``, else the Settings menu /
   ``settings.toml`` (lane SET-WIRE): the ``vivado`` executable, its install directory in
   either layout (below), or a directory of releases (``/research/CAD/Xilinx/Vivado``: the
   release the kit wants, else the newest). ``off`` (or ``none``) turns discovery off:
   nothing is found and nothing is run (the tests set it; a machine with no Vivado may too);
2. ``vivado`` on ``PATH``;
3. ``$XILINX_VIVADO/bin/vivado``;
4. the install roots (``ROOTS_POSIX``, ``ROOTS_WINDOWS``): every release found is listed in
   ``others``, so the guide can say "2026.1 is installed but not on PATH".

An install directory has one of two layouts: ``<root>/<rel>/bin/vivado`` (2024.x and
earlier: ``/apps/Xilinx/Vivado/2024.1/bin/vivado``) or ``<root>/<rel>/Vivado/bin/vivado``
(2025.1 and later: ``/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado``,
``/tools/Xilinx/2025.1/Vivado/bin/vivado``).

**The release the kit asks for** (``discover(want=...)``, KIT-RC2): with no setting, a
``vivado`` on PATH of another release does not win over an installed one of the wanted
release (the PATH one is still reported, as ``on_path``). A setting is never overridden:
it is only flagged (``check_release``). ``on_path`` is always the ``vivado`` a BARE command
would run (``/etc/profile.d`` may put 2024.1 first); ``check_path`` says when it is not the
kit's release, so HM prints the full path of the chosen one (``command_vivado``).

The version comes from running ``vivado -version`` once per executable (a few seconds;
cached for the process, keyed on the file's mtime). It prints
``vivado v2024.1 (64-bit)`` and ``SW Build 5076996 on ...``. That is the only thing HM
ever runs, and never from ``make check``. An install that is not the chosen one is NOT
run: its release is its directory's name (``release_of_path``).

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from harness_manager.core.pack import KitCheck

from .schema import release_major_minor

ENV = "HARNESS_MANAGER_VIVADO"
OFF = ("off", "none", "0")
#: Directories whose children are releases (``<root>/<rel>/``, either layout). The first
#: three are the 2024.x installer's default; the next three the 2025.1+ unified installer's
#: (``/tools/Xilinx/2025.1/Vivado``); the last is this lab's (srv03335: 2025.2, 2026.1).
ROOTS_POSIX = ("/tools/Xilinx/Vivado", "/opt/Xilinx/Vivado", "/apps/Xilinx/Vivado",
               "/tools/Xilinx", "/opt/Xilinx", "/apps/Xilinx",
               "/research/CAD/Xilinx/Vivado")
ROOTS_WINDOWS = ("C:\\Xilinx\\Vivado", "C:\\AMDDesignTools\\Vivado", "C:\\Xilinx",
                 "C:\\AMDDesignTools")
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
    how: str = ""             # "env" | "setting" | "path" | "xilinx_vivado" | "install root"
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
    #: The ``vivado`` a BARE command runs (the first on PATH), or None. When it is not the
    #: chosen install its release is read from its path, never by running it.
    on_path: VivadoInstall | None = None
    #: The release(s) the caller asked for (the kit's), major.minor; () = any.
    want: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.install is not None

    def to_json(self) -> dict[str, object]:
        i = self.install
        return {"found": self.found, "path": i.path if i else None,
                "version": i.version if i else None, "build": i.build if i else None,
                "how": i.how if i else None, "error": i.error if i else None,
                "others": [o.to_json() for o in self.others], "reason": self.reason,
                "disabled": self.disabled,
                "on_path": self.on_path.to_json() if self.on_path else None,
                "want": list(self.want)}


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


def _names() -> tuple[str, ...]:
    return ("vivado", "vivado.bat")


def _exe_in(directory: Path) -> Path | None:
    """The ``vivado`` of an install directory, in either layout: ``<rel>/bin/vivado``
    (2024.x), ``<rel>/Vivado/bin/vivado`` (2025.1+), or the ``bin`` directory itself."""
    for name in _names():
        for cand in (directory / "bin" / name, directory / name,
                     directory / "Vivado" / "bin" / name):
            if cand.is_file():
                return cand
    return None


def release_of_path(path: str | Path) -> str:
    """The release in an executable's path (the nearest ``<rel>`` directory above it), or
    in the path a symlink points at: ``/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado``
    -> ``"2026.1"``; ``""`` when neither names one (a wrapper script)."""
    cands = [Path(path)]
    try:
        cands.append(Path(path).resolve())
    except OSError:
        pass
    for cand in cands:
        for parent in cand.parents:
            if _ROOT_REL.match(parent.name):
                return parent.name
    return ""


def _wants(want: str | Iterable[str]) -> tuple[str, ...]:
    items = [want] if isinstance(want, str) else list(want or ())
    return tuple(dict.fromkeys(m for m in (release_major_minor(w) for w in items) if m))


def _matches(version: str, wants: tuple[str, ...]) -> bool:
    return bool(wants) and release_major_minor(version) in wants


def _rel_key(rel: str) -> tuple[int, ...]:
    return tuple(int(x) for x in rel.split("."))


def _releases_under(root: Path) -> list[VivadoInstall]:
    """Every ``<root>/<rel>/`` holding a vivado (either layout), oldest first. Not run."""
    out: list[VivadoInstall] = []
    try:
        children = [c for c in root.iterdir() if _ROOT_REL.match(c.name)]
    except OSError:
        return out
    for rel in sorted(children, key=lambda c: _rel_key(c.name)):
        exe = _exe_in(rel)
        if exe is not None:
            out.append(VivadoInstall(str(exe), rel.name, 0, "install root"))
    return out


def _pick(candidates: list[VivadoInstall], wants: tuple[str, ...]) -> VivadoInstall | None:
    """The newest candidate of a wanted release, else None."""
    hits = [c for c in candidates if _matches(c.version, wants)]
    return max(hits, key=lambda c: _rel_key(c.version)) if hits else None


def _same_file(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def _near_miss(p: Path) -> str:
    """For a setting that names no vivado: the install it probably meant (the nearest
    existing directory above it that is one)."""
    for parent in p.parents:
        if parent.is_dir():
            exe = _exe_in(parent)
            return f"; did you mean {exe}?" if exe is not None else ""
    return ""


def _roots() -> tuple[str, ...]:
    return ROOTS_WINDOWS if os.name == "nt" else ROOTS_POSIX


def _scan(roots: tuple[str, ...] | None, seen: list[str], searched: list[str]
          ) -> list[VivadoInstall]:
    """Every release under the install roots, minus ``seen``. Not run: the release is the
    directory's name; only the chosen one is ever executed."""
    out: list[VivadoInstall] = []
    for root in roots if roots is not None else _roots():
        searched.append(root)
        rp = Path(root)
        if not rp.is_dir():
            continue
        for inst in _releases_under(rp):
            if not any(_same_file(inst.path, s) for s in seen):
                out.append(inst)
                seen.append(inst.path)
    return out


def discover(*, runner: Runner = subprocess.run, env: dict[str, str] | None = None,
             which: Callable[[str], str | None] = shutil.which,
             roots: tuple[str, ...] | None = None,
             want: str | Iterable[str] = ()) -> VivadoFound:
    """Find Vivado (the order in the module docstring) and read its version. ``want``:
    the release(s) the kit needs (major.minor is compared): with no setting, an installed
    Vivado of that release is chosen over another release on PATH."""
    from harness_manager.settings import runtime

    wants = _wants(want)
    # tools.vivado: ENV first (the caller's ``env`` when it gives one), then the settings
    r = runtime.resolved("tools.vivado", env=env)
    env = dict(os.environ) if env is None else env
    searched: list[str] = []
    forced = str(r.value or "").strip()
    said = f"${ENV}" if r.source == "env" else f"tools.vivado ({r.where})"
    if forced.lower() in OFF:
        return VivadoFound(None, disabled=True, searched=(f"{said}={forced}",),
                           reason=f"Vivado discovery is off ({said}={forced})", want=wants)

    def make(path: str, how: str) -> VivadoInstall:
        ver, build, err = read_version(path, runner)
        return VivadoInstall(path, ver, build, how, err)

    bare = which("vivado")
    # KIT-NIGHT: AMD's settings64.sh puts ``<root>//<rel>/Vivado/bin`` on PATH; the printed
    # command, the README and the guide showed that ``//``. Same file, clean spelling.
    bare = os.path.normpath(bare) if bare else bare
    ran: list[VivadoInstall] = []            # the PATH one, when it was run

    def path_view(chosen: VivadoInstall | None) -> VivadoInstall | None:
        if not bare:
            return None
        for known in ([chosen] if chosen else []) + ran:
            if _same_file(known.path, bare):
                return known
        rel = release_of_path(bare)
        return VivadoInstall(bare, rel, 0, "path",
                             "" if rel else f"its release is not in its path ({bare}); "
                                            "not run")

    if forced:
        searched.append(said)
        p = Path(forced).expanduser()
        exe = p if p.is_file() else _exe_in(p) if p.is_dir() else None
        if exe is None and p.is_dir():            # a directory of releases
            under = _releases_under(p)
            pick = _pick(under, wants) or (under[-1] if under else None)
            exe = Path(pick.path) if pick else None
        if exe is None:
            what = ("a directory with no vivado in bin/, Vivado/bin/ or a release under it"
                    if p.is_dir() else "no such file or directory")
            return VivadoFound(None, searched=tuple(searched), want=wants, on_path=path_view(None),
                               reason=f"{said}={forced} is not a vivado executable or install "
                                      f"({what}){_near_miss(p)}")
        inst = make(str(exe), "env" if r.source == "env" else "setting")
        # never overridden; the roots are listed (not run) so a mismatch can name the fix
        return VivadoFound(inst, tuple(_scan(roots, [inst.path], searched)), tuple(searched),
                           want=wants, on_path=path_view(inst))

    primary: VivadoInstall | None = None
    searched.append("PATH")
    if bare:
        primary = make(bare, "path")
        ran.append(primary)
    xv = env.get("XILINX_VIVADO", "").strip()
    xv_inst: VivadoInstall | None = None
    if xv:
        exe = _exe_in(Path(xv))
        if exe is not None:
            xv_inst = VivadoInstall(str(exe), release_of_path(exe), 0, "xilinx_vivado")
    if primary is None and xv_inst is not None:
        searched.append("$XILINX_VIVADO")
        primary = make(xv_inst.path, "xilinx_vivado")
        xv_inst = None
    others: list[VivadoInstall] = []
    seen = [primary.path] if primary else []
    if xv_inst is not None and not any(_same_file(xv_inst.path, s) for s in seen):
        searched.append("$XILINX_VIVADO")
        others.append(xv_inst)
        seen.append(xv_inst.path)
    others += _scan(roots, seen, searched)
    if wants and not (primary and _matches(primary.version, wants)):
        pick = _pick(others, wants)
        if pick is not None:                       # the kit's release, off PATH: prefer it
            others.remove(pick)
            if primary is not None:
                others.append(primary)
            primary = make(pick.path, pick.how)
    if primary is None and others:
        newest = max(others, key=lambda o: _rel_key(o.version) if o.version else (0,))
        others.remove(newest)
        primary = make(newest.path, newest.how)
    reason = "" if primary else ("vivado is not on PATH, $XILINX_VIVADO is unset and no "
                                 "standard install root holds one")
    return VivadoFound(primary, tuple(others), tuple(searched), reason=reason, want=wants,
                       on_path=path_view(primary))


def matching(found: VivadoFound, need: str) -> VivadoInstall | None:
    """The chosen install when its release is ``need``'s major.minor, else None."""
    i = found.install
    if i is None or not need or not i.version:
        return None
    return i if release_major_minor(i.version) == release_major_minor(need) else None


def command_vivado(found: VivadoFound, need: str) -> str:
    """What a printed command should start with: the FULL path of the chosen Vivado when it
    is the kit's release (a bare ``vivado`` runs whatever PATH has first), else ``vivado``."""
    m = matching(found, need)
    return m.path if m is not None else "vivado"


def check_path(found: VivadoFound, need: str) -> KitCheck | None:
    """The ``vivado`` on PATH against the kit's release: what a BARE ``vivado`` would run.
    None when there is nothing to say (no release asked for, or no vivado on PATH)."""
    b = found.on_path
    if not need or b is None:
        return None
    if not b.version:
        return KitCheck("vivado_path", "unchecked",
                        f"`vivado` on PATH is {b.path}: {b.error or 'its release is unknown'}")
    if release_major_minor(b.version) == release_major_minor(need):
        return KitCheck("vivado_path", "ok", f"`vivado` on PATH is Vivado {b.version} ({b.path})")
    m = matching(found, need)
    bindir = str(Path(m.path).parent) if m else ""
    fix = (f"Run the full path Harness Manager prints ({m.path}), or put its bin first on "
           f"PATH: export PATH={bindir}:$PATH" if m else
           f"Install Vivado {need} and put it first on PATH, or set ${ENV}")
    return KitCheck("vivado_path", "warning",
                    f"`vivado` on PATH is Vivado {b.version} ({b.path}), not this kit's {need}: "
                    f"a bare `vivado` runs {b.version}, and build_rm.tcl refuses it. {fix}")


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
    also = [o for o in found.others if release_major_minor(o.version)
            == release_major_minor(need)]
    if release_major_minor(have) != release_major_minor(need):
        forced = found.install.how in ("env", "setting")
        hint = (f"; Vivado {also[-1].version} is installed at {also[-1].path}: "
                + (f"point ${ENV} (or tools.vivado) at it" if forced else
                   f"set ${ENV} (or tools.vivado) to it, or put its bin first on PATH")
                if also else "")
        return KitCheck("vivado", "warning",
                        f"Vivado {have} found at {found.install.path}, but this kit's static was "
                        f"written by {need}: a checkpoint opens only in its own release, and "
                        f"build_rm.tcl will refuse to start{hint}")
    if need_build and found.install.build and found.install.build != need_build:
        return KitCheck("vivado", "warning",
                        f"Vivado {have} build {found.install.build}; the static was written by "
                        f"build {need_build} (the same release: the script notes it and goes on)")
    return KitCheck("vivado", "ok", f"Vivado {have} at {found.install.path}")

