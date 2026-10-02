#!/usr/bin/env python3
"""Download the wheels Harness Manager needs on ANOTHER operating system (lane WINDOWS).

``scripts/make_wheelhouse.sh --platform win_amd64 --python-version 3.12 DIR`` runs this. It
fills DIR with wheels for a Windows (or macOS) laptop from a Linux machine with network, so
the laptop installs with no network (``install.ps1 -Offline DIR``).

Why not one ``pip download --platform ... -r``: pip evaluates environment markers against the
machine it RUNS on, not the ``--platform`` it was given, so a Windows-only dependency
(``pywin32-ctypes; sys_platform == "win32"``, pythonnet for pywebview) would be left out and
the offline install would fail on the laptop. Instead this walks the dependency tree itself:

1. start from Harness Manager's own wheel (py3-none-any, built locally) with the extras asked
   for, and ``uv`` (the app's self-update builds new versions with it);
2. each requirement whose marker holds on the TARGET (``sys_platform``, ``platform_system``,
   ``os_name``, ``platform_machine``, ``python_version``, the extras) is pinned from
   constraints.txt (the universal lock, ``scripts/lock_deps.sh``) and downloaded with
   ``pip download --no-deps --only-binary=:all: --platform P --python-version X.Y
   --implementation cp``;
3. each downloaded wheel's own ``Requires-Dist`` is read and step 2 repeats until nothing new
   is needed.

Only wheels (``--only-binary=:all:``): a package with no wheel for the target fails loudly,
never builds. Every pip call is one argv (``download_argv``), so the tests check the command
line and run the walk against a fake index (``--no-index --find-links DIR``), never the
network.

    python scripts/wheelhouse_cross.py --target win_amd64 --python-version 3.12 \\
        --hm-wheel dist/harness_manager-1.1.0-py3-none-any.whl --constraints constraints.txt \\
        --extras serial,app --find-links vendor --out DIR
"""

from __future__ import annotations

import argparse
import email.parser
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

try:                                    # the venv make_wheelhouse makes has pip, maybe not this
    from packaging.markers import Marker
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:                     # pragma: no cover - pip's own copy
    from pip._vendor.packaging.markers import Marker  # type: ignore[no-redef]
    from pip._vendor.packaging.requirements import Requirement  # type: ignore[no-redef]
    from pip._vendor.packaging.utils import canonicalize_name  # type: ignore[no-redef]


@dataclass(frozen=True)
class Target:
    """One laptop kind: pip's platform tags and the marker environment there."""

    name: str
    platforms: tuple[str, ...]
    sys_platform: str
    platform_system: str
    os_name: str
    platform_machine: str


#: ``--platform`` words make_wheelhouse.sh takes. pip expands a macosx tag to the older
#: macOS versions (and universal2) itself.
TARGETS: dict[str, Target] = {
    "win_amd64": Target("win_amd64", ("win_amd64",), "win32", "Windows", "nt", "AMD64"),
    "macos_arm64": Target("macos_arm64", ("macosx_11_0_arm64",), "darwin", "Darwin", "posix",
                          "arm64"),
    "macos_x86_64": Target("macos_x86_64", ("macosx_10_12_x86_64",), "darwin", "Darwin",
                           "posix", "x86_64"),
}
ALIASES = {"windows": "win_amd64", "win": "win_amd64", "macos-arm64": "macos_arm64",
           "macosx_arm64": "macos_arm64", "macos-x86_64": "macos_x86_64",
           "macosx_x86_64": "macos_x86_64"}


def target_of(word: str) -> Target:
    key = ALIASES.get(word.strip().lower(), word.strip().lower())
    if key not in TARGETS:
        raise SystemExit(f"wheelhouse_cross: unknown platform {word!r} "
                         f"(one of {', '.join(TARGETS)})")
    return TARGETS[key]


def python_version_of(text: str) -> str:
    m = re.fullmatch(r"3\.(\d{1,2})", text.strip())
    if not m or int(m.group(1)) < 10:
        raise SystemExit(f"wheelhouse_cross: --python-version {text!r} is not 3.10 or newer "
                         f"(as 3.12)")
    return text.strip()


def marker_env(target: Target, py: str, extra: str = "") -> dict[str, str]:
    """The environment markers are evaluated in, as they are ON THE TARGET."""
    return {"sys_platform": target.sys_platform, "platform_system": target.platform_system,
            "os_name": target.os_name, "platform_machine": target.platform_machine,
            "python_version": py, "python_full_version": f"{py}.0",
            "implementation_name": "cpython", "platform_python_implementation": "CPython",
            "implementation_version": f"{py}.0", "platform_release": "", "platform_version": "",
            "extra": extra}


def applies(req: Requirement, target: Target, py: str, extras: Iterable[str]) -> bool:
    """Does ``req`` hold on the target, for one of the ``extras`` asked of its parent?"""
    if req.marker is None:
        return True
    marker: Marker = req.marker
    return any(marker.evaluate(marker_env(target, py, e)) for e in ("", *extras))


def read_pins(path: Path) -> dict[str, tuple[str, Marker | None]]:
    """constraints.txt: ``name==version [; marker]`` -> {canonical name: (version, marker)}.
    A name pinned twice (different versions per marker) keeps every line: the first whose
    marker holds on the target wins (``pin_for``)."""
    pins: dict[str, list[tuple[str, Marker | None]]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        req = Requirement(line)
        spec = str(req.specifier)
        m = re.fullmatch(r"==([^,]+)", spec)
        if not m:
            continue
        pins.setdefault(canonicalize_name(req.name), []).append((m.group(1), req.marker))
    return pins  # type: ignore[return-value]


def pin_for(pins: dict, name: str, target: Target, py: str) -> str:
    """The pinned version of ``name`` on the target, or "" when constraints.txt has none."""
    for version, marker in pins.get(canonicalize_name(name), []):
        if marker is None or marker.evaluate(marker_env(target, py)):
            return version
    return ""


def requires_of(wheel: Path) -> list[Requirement]:
    """A wheel's ``Requires-Dist`` (its ``*.dist-info/METADATA``)."""
    with zipfile.ZipFile(wheel) as z:
        meta = next((n for n in z.namelist()
                     if n.count("/") == 1 and n.endswith(".dist-info/METADATA")), None)
        if meta is None:
            raise SystemExit(f"wheelhouse_cross: {wheel.name} has no METADATA")
        text = z.read(meta).decode("utf-8", errors="replace")
    msg = email.parser.Parser().parsestr(text, headersonly=True)
    return [Requirement(r) for r in msg.get_all("Requires-Dist") or []]


def wheel_name(path: Path) -> str:
    """The canonical distribution name of a wheel file."""
    return canonicalize_name(path.name.split("-", 1)[0])


def download_argv(python: str, target: Target, py: str, out: Path, specs: Sequence[str], *,
                  find_links: Sequence[str] = (), no_index: bool = False,
                  index_url: str = "") -> list[str]:
    """THE pip command line: wheels only, for the target's tags, never the dependencies
    (this walk resolves them, with the target's markers)."""
    argv = [python, "-m", "pip", "download", "--disable-pip-version-check", "--quiet",
            "--no-deps", "--only-binary=:all:"]
    for tag in target.platforms:
        argv += ["--platform", tag]
    argv += ["--python-version", py, "--implementation", "cp", "--dest", str(out)]
    if no_index:
        argv.append("--no-index")
    if index_url:
        argv += ["--index-url", index_url]
    for d in find_links:
        argv += ["--find-links", d]
    return argv + list(specs)


Runner = Callable[[list[str]], int]


def _run(argv: list[str]) -> int:
    return subprocess.call(argv)


def walk(hm_wheel: Path, constraints: Path, target: Target, py: str, out: Path, *,
         extras: Iterable[str] = (), more: Iterable[str] = ("uv>=0.4",),
         find_links: Sequence[str] = (), no_index: bool = False, index_url: str = "",
         python: str = sys.executable, run: Runner = _run,
         skip: Iterable[str] = ("mps3-pyverify",)) -> list[str]:
    """Download every wheel ``hm_wheel[extras]`` and ``more`` need on the target into ``out``.
    Returns the ``name==version`` specs it fetched. ``skip``: names copied in by the caller
    (the vendored pyverify)."""
    out.mkdir(parents=True, exist_ok=True)
    pins = read_pins(constraints)
    skipped = {canonicalize_name(s) for s in skip}
    done: dict[str, str] = {canonicalize_name(wheel_name(hm_wheel)): "local"}
    fetched: list[str] = []
    # (requirement, the extras its parent was asked with)
    todo: list[tuple[Requirement, frozenset[str]]] = [
        (r, frozenset(extras)) for r in requires_of(hm_wheel)]
    todo += [(Requirement(m), frozenset()) for m in more]
    want_extras: dict[str, set[str]] = {}
    while todo:
        batch: dict[str, str] = {}
        for req, parent_extras in todo:
            if not applies(req, target, py, parent_extras):
                continue
            name = canonicalize_name(req.name)
            if name in skipped:
                continue
            new_extras = set(req.extras) - want_extras.get(name, set())
            want_extras.setdefault(name, set()).update(req.extras)
            if name in done and not new_extras:
                continue
            if name in done:                  # asked again with a new extra: walk it again
                done.pop(name)
            version = pin_for(pins, name, target, py)
            spec = f"{name}=={version}" if version else f"{name}{req.specifier}"
            batch.setdefault(name, spec)
        todo = []
        if not batch:
            break
        argv = download_argv(python, target, py, out, sorted(batch.values()),
                             find_links=find_links, no_index=no_index, index_url=index_url)
        rc = run(argv)
        if rc != 0:
            raise SystemExit(
                f"wheelhouse_cross: pip could not download a {target.name} wheel for Python "
                f"{py} of: {', '.join(sorted(batch.values()))} (exit {rc}). A package with no "
                f"wheel for {target.name} cannot go in a wheelhouse; try another "
                f"--python-version")
        fetched += sorted(batch.values())
        wheels = {wheel_name(p): p for p in out.glob("*.whl")}
        for name, spec in batch.items():
            done[name] = spec
            wheel = wheels.get(name)
            if wheel is None:
                raise SystemExit(f"wheelhouse_cross: pip said it downloaded {spec}, but no "
                                 f"wheel for it is in {out}")
            todo += [(r, frozenset(want_extras.get(name, set())))
                     for r in requires_of(wheel)]
    return fetched


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--target", required=True, help=", ".join(TARGETS))
    ap.add_argument("--python-version", required=True, help="the laptop's Python, as 3.12")
    ap.add_argument("--hm-wheel", required=True, type=Path)
    ap.add_argument("--constraints", required=True, type=Path)
    ap.add_argument("--extras", default="", help="comma-separated: serial,app")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--find-links", action="append", default=[])
    ap.add_argument("--no-index", action="store_true")
    ap.add_argument("--index-url", default="")
    ap.add_argument("--no-uv", action="store_true", help="leave uv out")
    a = ap.parse_args(argv)
    target = target_of(a.target)
    py = python_version_of(a.python_version)
    extras = [e for e in a.extras.split(",") if e]
    got = walk(a.hm_wheel, a.constraints, target, py, a.out, extras=extras,
               more=() if a.no_uv else ("uv>=0.4",), find_links=a.find_links,
               no_index=a.no_index, index_url=a.index_url)
    print(f"wheelhouse_cross: {len(got)} wheels for {target.name}, Python {py}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
