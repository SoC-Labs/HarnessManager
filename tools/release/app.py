"""An app release: preconditions, the wheel, a hashed lock, the pyverify ``dep``, the entry.

What an app release ``V`` puts in the tree (OTA §4.1), under the per-version release ``vV``:

- ``harness_manager-V-py3-none-any.whl``: built from ``git archive HEAD`` (committed files
  only) with ``SOURCE_DATE_EPOCH`` = the commit time;
- ``harness_manager-V.lock.txt``: every dependency pinned by hash. ``uv pip compile
  --universal --generate-hashes`` (every OS and Python >= 3.10, by markers); without uv,
  ``pip-compile --generate-hashes`` (pip-tools), which resolves for THIS machine only, so
  the release records ``universal: false`` and ``--publish`` refuses it unless told;
- ``mps3_pyverify-X-py3-none-any.whl``: the vendored codec as a ``dep`` artifact. The lock
  pins it by name and hash (``mps3-pyverify==X --hash=sha256:…``); the client's stage step
  installs it from the verified download (OTA §4.1), because no index serves it.

Every tool (build, uv, pip-compile, git) runs through an injectable runner, so the tests
use fakes and never reach the network.

**One version source** (CCR OTA-R): ``harness_manager.__version__``. ``pyproject.toml``
declares ``dynamic = ["version"]`` and setuptools reads that attribute; ``CHANGELOG.md``'s
newest heading is a checked mirror. The preconditions refuse a ``pyproject.toml`` that
carries its own version again, and the built wheel's METADATA must say the same version.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.services.update.version import is_version, parse_version

from .channel_doc import schema_allows_private_app
from .common import (
    EXIT_ACTION_FAILED,
    EXIT_USAGE,
    Layout,
    ReleaseError,
    Runner,
    git,
    git_dirty,
    git_export,
    git_tag_commit,
    iso,
    run,
    sha256_bytes,
    sha256_file,
    write_once,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

DIST = "harness_manager"
PYVERIFY = "mps3-pyverify"
#: Where the version lives; pyproject.toml's [tool.setuptools.dynamic] must point here.
VERSION_ATTR = "harness_manager.__version__"
LOCK_EXTRAS = ("serial", "ina260")          # the always-on small extras (OTA M6)
UV_ENV = "HARNESS_MANAGER_UV"


# --- facts from the source tree ----------------------------------------------------------


@dataclass(frozen=True)
class SourceFacts:
    init_version: str              # harness_manager.__version__: THE version
    pyproject_version: str         # a static [project] version: must be "" (CCR OTA-R)
    pyproject_dynamic: bool        # "version" in [project] dynamic
    pyproject_attr: str            # [tool.setuptools.dynamic] version.attr
    requires_python: str
    changelog_version: str
    changelog_heading: str
    notes: str


def version_source_problem(facts: SourceFacts) -> str:
    """Why pyproject.toml does not take its version from ``VERSION_ATTR`` ("" when it does)."""
    if facts.pyproject_version:
        return (f"pyproject.toml has its own version ({facts.pyproject_version}); the one "
                f"source is {VERSION_ATTR}")
    if not facts.pyproject_dynamic or facts.pyproject_attr != VERSION_ATTR:
        got = facts.pyproject_attr or "nothing"
        return (f"pyproject.toml does not read its version from {VERSION_ATTR} "
                f"(dynamic version: {'yes' if facts.pyproject_dynamic else 'no'}, attr: {got})")
    return ""


def read_facts(tree: Path) -> SourceFacts:
    pp = tomllib.loads((tree / "pyproject.toml").read_text(encoding="utf-8"))
    project = pp.get("project", {})
    dyn_version = pp.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version")
    init = (tree / "src" / "harness_manager" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"$', init, re.M)
    log = (tree / "CHANGELOG.md").read_text(encoding="utf-8") \
        if (tree / "CHANGELOG.md").is_file() else ""
    head = re.search(r"^## \[?(\d+\.\d+\.\d+[^\]\s]*)\]?(.*)$", log, re.M)
    notes = ""
    if head:
        rest = log[head.end():]
        nxt = re.search(r"^## ", rest, re.M)
        notes = (rest[:nxt.start()] if nxt else rest).strip()
    return SourceFacts(
        init_version=m.group(1) if m else "",
        pyproject_version=str(project.get("version", "")),
        pyproject_dynamic="version" in (project.get("dynamic") or ()),
        pyproject_attr=str(dyn_version.get("attr", "")) if isinstance(dyn_version, dict) else "",
        requires_python=str(project.get("requires-python", "")),
        changelog_version=head.group(1) if head else "",
        changelog_heading=head.group(0).strip() if head else "",
        notes=notes)


# --- preconditions -------------------------------------------------------------------------


@dataclass
class Preconditions:
    version: str
    commit: str
    commit_time: int
    tag: str
    tag_state: str                 # "free" | "at HEAD"
    facts: SourceFacts
    dirty: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_preconditions(repo: Path, version: str | None, *, allow_dirty: bool = False,
                        publish: bool = False, runner: Runner = run) -> Preconditions:
    """A clean tree, one version everywhere (bumped), and the tag free (or at HEAD)."""
    dirty = git_dirty(repo, runner=runner)
    if dirty and (publish or not allow_dirty):
        raise ReleaseError(
            f"the working tree is not clean ({len(dirty)} changed or untracked: "
            f"{', '.join(dirty[:3])}{' …' if len(dirty) > 3 else ''})",
            hint="commit or stash first: a release is built from a clean, committed tree"
                 + ("" if publish else " (--allow-dirty exists for a dry run only)"))
    commit = git(repo, "rev-parse", "HEAD", runner=runner)
    commit_time = int(git(repo, "log", "-1", "--format=%ct", "HEAD", runner=runner))
    facts = read_facts(repo)
    problem = version_source_problem(facts)
    if problem:
        raise ReleaseError(problem, hint='pyproject.toml: dynamic = ["version"] and '
                                         '[tool.setuptools.dynamic] version = '
                                         f'{{ attr = "{VERSION_ATTR}" }}, no version of its own')
    v = version or facts.init_version
    if not is_version(v):
        raise ReleaseError(f"{v!r} is not a version", code=EXIT_USAGE)
    where = {VERSION_ATTR: facts.init_version,
             "CHANGELOG.md (newest heading)": facts.changelog_version}
    wrong = {k: x for k, x in where.items() if not x or parse_version(x) != parse_version(v)}
    if wrong:
        raise ReleaseError(
            f"version {v} is not the version everywhere: "
            + "; ".join(f"{k} says {x or 'nothing'}" for k, x in wrong.items()),
            hint="bump __version__ in src/harness_manager/__init__.py (pyproject.toml reads "
                 f"it) and add a CHANGELOG.md section '## {v}', then commit")
    warnings = []
    if "unreleased" in facts.changelog_heading.lower():
        msg = f"CHANGELOG.md still says '{facts.changelog_heading}'"
        if publish:
            raise ReleaseError(msg, hint="date the section, commit, then publish")
        warnings.append(msg + " (fine for a dry run; publishing refuses it)")
    if not facts.notes:
        raise ReleaseError(f"CHANGELOG.md has no text under '## {v}': it becomes the signed notes")
    tag = f"v{v}"
    at = git_tag_commit(repo, tag, runner=runner)
    if at and at != commit:
        raise ReleaseError(f"tag {tag} already exists at {at[:12]}, not at HEAD {commit[:12]}",
                           hint="the version is taken: bump it")
    if not at:
        if publish:
            raise ReleaseError(f"tag {tag} does not exist", hint=f"git tag -a {tag} -m '{tag}' "
                               "on the release commit, then publish")
        warnings.append(f"tag {tag} is free (create it on HEAD before --publish)")
    if dirty:
        warnings.append(f"DIRTY TREE ({len(dirty)} paths): the wheel is built from HEAD "
                        f"{commit[:12]}, not from the working tree; NOT publishable")
    return Preconditions(version=v, commit=commit, commit_time=commit_time, tag=tag,
                         tag_state="at HEAD" if at else "free", facts=facts, dirty=dirty,
                         warnings=warnings)


# --- build steps ---------------------------------------------------------------------------


WheelBuilder = Callable[[Path, Path, int], Path]
"""(source tree, out dir, SOURCE_DATE_EPOCH) -> the built wheel."""


def build_wheel(runner: Runner = run, python: str = sys.executable) -> WheelBuilder:
    def _build(src: Path, out: Path, epoch: int) -> Path:
        out.mkdir(parents=True, exist_ok=True)
        res = runner([python, "-m", "build", "--wheel", "--outdir", str(out), str(src)],
                     env={"SOURCE_DATE_EPOCH": str(epoch)})
        if res.returncode != 0:
            tail = ((res.stderr or res.stdout or "").strip().splitlines() or [""])[-1]
            raise ReleaseError(f"building the wheel failed: {tail[:200]}",
                               hint="python -m build needs the package index for its build "
                                    "requirements", code=EXIT_ACTION_FAILED)
        wheels = sorted(out.glob(f"{DIST}-*.whl"))
        if len(wheels) != 1:
            raise ReleaseError(f"expected one wheel in {out}, found {len(wheels)}",
                               code=EXIT_ACTION_FAILED)
        return wheels[0]
    return _build


def wheel_version(wheel: Path) -> str:
    """The Version: in the wheel's METADATA (the file name alone is not proof)."""
    try:
        with zipfile.ZipFile(wheel) as zf:
            meta = next((n for n in zf.namelist() if n.endswith(".dist-info/METADATA")), None)
            if meta is None:
                raise ReleaseError(f"{wheel.name} has no METADATA")
            text = zf.read(meta).decode("utf-8", "replace")
    except zipfile.BadZipFile:
        raise ReleaseError(f"{wheel.name} is not a wheel (zip)") from None
    m = re.search(r"^Version: (\S+)$", text, re.M)
    n = re.search(r"^Name: (\S+)$", text, re.M)
    if not m or not n or n.group(1).replace("_", "-").lower() != "harness-manager":
        raise ReleaseError(f"{wheel.name} is not a harness-manager wheel")
    return m.group(1)


@dataclass(frozen=True)
class LockTool:
    name: str                       # "uv" | "pip-tools" | "given"
    argv0: list[str]
    version: str = ""
    universal: bool = False


def find_lock_tool(choice: str = "auto", *, uv: str = "", pip_compile: str = "",
                   runner: Runner = run) -> LockTool:
    """uv first (universal); pip-tools as the fallback (this machine only)."""
    def _version(argv0: list[str]) -> str:
        res = runner([*argv0, "--version"])
        return (res.stdout or res.stderr or "").strip().splitlines()[0] if res.returncode == 0 \
            and (res.stdout or res.stderr) else ""

    if choice in ("auto", "uv"):
        cand = uv or os.environ.get(UV_ENV, "").strip() or shutil.which("uv") or ""
        if cand:
            return LockTool("uv", [cand], _version([cand]), universal=True)
        if choice == "uv":
            raise ReleaseError("--lock-tool uv: uv is not installed (or set HARNESS_MANAGER_UV)",
                               code=EXIT_USAGE)
    if choice in ("auto", "pip-tools"):
        cand = pip_compile or shutil.which("pip-compile") or ""
        if cand:
            return LockTool("pip-tools", [cand], _version([cand]), universal=False)
    raise ReleaseError("no lock tool: neither uv nor pip-compile (pip-tools) is installed",
                       hint="pip install uv (preferred: a universal lock) or pip-tools into a "
                            "tools venv and pass --uv/--pip-compile, or pass --lock FILE",
                       code=EXIT_USAGE)


def compile_lock(tool: LockTool, src: Path, out: Path, *, extras: tuple[str, ...] = LOCK_EXTRAS,
                 runner: Runner = run) -> Path:
    """The raw hashed lock of ``src/pyproject.toml`` (pyverify still as the resolver wrote it)."""
    raw = out.with_name(out.name + ".raw")
    ex = [a for e in extras for a in ("--extra", e)]
    if tool.name == "uv":
        argv = [*tool.argv0, "pip", "compile", "--quiet", "--universal", "--generate-hashes",
                "--python-version", "3.10", "--find-links", "vendor", "-c", "constraints.txt",
                *ex, "--no-header", "--no-annotate", "-o", str(raw), "pyproject.toml"]
    else:
        argv = [*tool.argv0, "--quiet", "--generate-hashes", "--find-links", "vendor",
                "-c", "constraints.txt", *ex, "--strip-extras", "--no-header", "--no-annotate",
                "-o", str(raw), "pyproject.toml"]
    res = runner(argv, cwd=src)
    if res.returncode != 0 or not raw.is_file():
        tail = ((res.stderr or res.stdout or "").strip().splitlines() or [""])[-1]
        raise ReleaseError(f"{tool.name} could not compile the lock: {tail[:240]}",
                           hint="it needs the package index", code=EXIT_ACTION_FAILED)
    return raw


def pin_dep_in_lock(raw: str, dep_name: str, dep_version: str, dep_sha256: str) -> str:
    """Replace the resolver's ``mps3-pyverify`` entry (and its hash lines) with one line that
    pins the vendored wheel by version and sha256. Refuses a lock that does not name it, or
    names another version."""
    out, skipping, found = [], False, ""
    for ln in raw.splitlines():
        head = re.match(r"^([A-Za-z0-9_.-]+)\s*(?:==\s*([^\s;\\]+))?", ln)
        if head and not ln.startswith((" ", "#")) and \
                head.group(1).replace("_", "-").lower() == dep_name:
            found = head.group(2) or "?"
            skipping = True
            out.append(f"{dep_name}=={dep_version} --hash=sha256:{dep_sha256}")
            continue
        if skipping and ln.startswith((" ", "\t")):
            continue
        skipping = False
        out.append(ln)
    if not found:
        raise ReleaseError(f"the lock does not name {dep_name}",
                           hint="pyproject.toml must depend on it")
    if parse_version(found) != parse_version(dep_version):
        raise ReleaseError(f"the lock resolved {dep_name}=={found}, but vendor/ holds "
                           f"{dep_version}", hint="run make vendor-pyverify, or fix the pin")
    lines = [ln for ln in out if ln.strip()]
    return "\n".join(lines) + "\n"


def lock_packages(text: str) -> dict[str, str]:
    """name -> version for every ``name==version`` line of a requirements lock."""
    pkgs = {}
    for ln in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", ln)
        if m:
            pkgs[m.group(1).replace("_", "-").lower()] = m.group(2)
    return pkgs


# --- the release ---------------------------------------------------------------------------


@dataclass
class AppBuild:
    version: str
    tag: str
    wheel: Path
    lock: Path
    dep: Path
    lock_tool: LockTool
    entry: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    pending: list[tuple[Path, bytes]] = field(default_factory=list, repr=False)

    def write(self) -> None:
        """Put the assets in the tree (write-once), after the channel accepted the entry."""
        for path, data in self.pending:
            write_once(path, data)


def build_app_release(repo: Path, layout: Layout, pre: Preconditions, *, access: str,
                      wheel_builder: WheelBuilder | None = None, prebuilt_wheel: Path | None = None,
                      lock_tool: LockTool | None = None, prebuilt_lock: Path | None = None,
                      prebuilt_lock_universal: bool = False, runner: Runner = run,
                      workdir: Path | None = None) -> AppBuild:
    """Build (or take) the wheel and lock, pin pyverify, write the assets, return the entry."""
    v, tag = pre.version, pre.tag
    warnings: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="otar-app-", dir=workdir))
    try:
        src = tmp / "src"
        git_export(repo, pre.commit, src, runner=runner)
        # the wheel
        if prebuilt_wheel is not None:
            wheel = Path(prebuilt_wheel)
            warnings.append(f"wheel taken as given ({wheel.name}), not built from {pre.commit[:12]}")
        else:
            wheel = (wheel_builder or build_wheel(runner))(src, tmp / "wheel", pre.commit_time)
        got = wheel_version(wheel)
        if parse_version(got) != parse_version(v):
            raise ReleaseError(f"the wheel says version {got}, the release is {v}",
                               code=EXIT_ACTION_FAILED)
        wheel_name = f"{DIST}-{v}-py3-none-any.whl"
        if wheel.name != wheel_name:
            raise ReleaseError(f"the wheel is named {wheel.name}, expected {wheel_name}")
        # the dep: the vendored pyverify wheel of THAT commit
        deps = sorted((src / "vendor").glob("mps3_pyverify-*.whl"))
        if len(deps) != 1:
            raise ReleaseError(f"vendor/ at {pre.commit[:12]} holds {len(deps)} pyverify wheels, "
                               "expected one")
        dep = deps[0]
        dep_version = dep.name.split("-")[1]
        # the lock
        if prebuilt_lock is not None:
            raw_text = Path(prebuilt_lock).read_text(encoding="utf-8")
            tool = LockTool("given", [], f"from {Path(prebuilt_lock).name}",
                            universal=prebuilt_lock_universal)
            warnings.append(f"lock taken as given ({Path(prebuilt_lock).name})")
        else:
            tool = lock_tool or find_lock_tool(runner=runner)
            raw = compile_lock(tool, src, tmp / "lock.txt", runner=runner)
            raw_text = raw.read_text(encoding="utf-8")
        lock_text = pin_dep_in_lock(raw_text, PYVERIFY, dep_version, sha256_file(dep))
        if not tool.universal:
            warnings.append(f"the lock is NOT universal ({tool.name}: resolved for this machine "
                            "only); --publish refuses it without --allow-non-universal-lock")
        pkgs = lock_packages(lock_text)
        if "harness-manager" in pkgs:
            raise ReleaseError("the lock pins harness-manager itself; it must pin only its "
                               "dependencies")
        missing_hash = [ln for ln in lock_text.splitlines()
                        if re.match(r"^[A-Za-z0-9_.-]+==", ln) and "--hash=" not in ln
                        and not ln.rstrip().endswith("\\")]
        if missing_hash:
            raise ReleaseError(f"lock lines without a hash: {missing_hash[:2]}",
                               hint="compile with --generate-hashes")
        # the assets: written by AppBuild.write() once the channel accepts the entry
        lock_name = f"{DIST}-{v}.lock.txt"
        dst_wheel = layout.asset_path(tag, wheel_name)
        dst_lock = layout.asset_path(tag, lock_name)
        dst_dep = layout.asset_path(tag, dep.name)
        pending = [(dst_wheel, wheel.read_bytes()), (dst_lock, lock_text.encode("utf-8")),
                   (dst_dep, dep.read_bytes())]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    data_of = dict(pending)

    if access != "public" and not schema_allows_private_app():
        warnings.append("app assets are emitted as access: public: this tree's schema refuses "
                        "a private app wheel until OTA-C allows github-token (U1)")
        access = "public"

    def asset(path: Path, **extra: Any) -> dict[str, Any]:
        data = data_of[path]
        a: dict[str, Any] = {"name": path.name, "url": layout.rel_url(tag, path.name),
                             "sha256": sha256_bytes(data), "size": len(data), **extra}
        if access != "public":
            a["access"], a["repo"] = access, layout.repo
        return a

    entry: dict[str, Any] = {
        "version": v, "status": "current",
        "requires_python": pre.facts.requires_python,
        "released_at": iso(pre.commit_time),
        "notes": pre.facts.notes,
        "artifacts": [asset(dst_wheel, kind="wheel"), asset(dst_dep, kind="dep")],
        "lock": asset(dst_lock),
        "source": {"commit": pre.commit, "tag": tag, "dirty": bool(pre.dirty)},
        "lock_info": {"tool": tool.name, "tool_version": tool.version,
                      "universal": tool.universal, "extras": list(LOCK_EXTRAS),
                      "packages": len(pkgs),
                      "resolved_for": "python>=3.10, every OS (markers)" if tool.universal
                      else f"this machine only ({sys.platform}, python "
                           f"{sys.version_info.major}.{sys.version_info.minor} tool env)"},
    }
    return AppBuild(version=v, tag=tag, wheel=dst_wheel, lock=dst_lock, dep=dst_dep,
                    lock_tool=tool, entry=entry, warnings=[*pre.warnings, *warnings],
                    pending=pending)
