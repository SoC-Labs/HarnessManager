"""Guards for the developer venv (Makefile: ``venv``, ``release``).

The venv must run THIS checkout's harness-manager and the pyverify the app ships with (the
vendored wheel). Three silent failures happened in October 2026: pyverify linked editable from
a sibling platform checkout on another branch (twice), and a worktree's ``make check
VENV=<main venv>`` re-pointed the main venv's editable install at the worktree. Each guard
below fails loudly with a one-line fix instead. Standard library only (it runs before the venv
exists, and inside it).

    python tools/venv_guard.py path     --venv DIR --checkout DIR
    python tools/venv_guard.py harness  --venv DIR --checkout DIR
    python tools/venv_guard.py pyverify --venv DIR --wheel FILE [--allow-dev]

Exit 0: fine. Exit 1: refused, with the reason and the fix on stderr.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

#: Run inside the venv's interpreter (-I: no PYTHONPATH, no user site). Prints one JSON line.
_PROBE = r"""
import json, site, sys, importlib.util
out = {"site": site.getsitepackages(), "harness": None, "pyverify": None}
for name in ("harness_manager", "pyverify"):
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        spec = None
    if spec is not None and spec.origin:
        out["harness" if name == "harness_manager" else "pyverify"] = spec.origin
print(json.dumps(out))
"""


class Refused(Exception):
    """A guard fired: ``str(exc)`` is the message, with its fix."""


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def check_path(venv: str | Path, checkout: str | Path) -> None:
    """(c) A venv that this checkout will ``pip install -e`` into must be inside it."""
    venv_p = Path(venv)
    if not venv_p.is_absolute():
        venv_p = Path(checkout) / venv_p
    if not _inside(venv_p, Path(checkout)):
        raise Refused(
            f"VENV={venv} is outside this checkout ({Path(checkout).resolve()}). Installing "
            "here would re-point that venv's editable harness-manager at this checkout's "
            f"source. Fix: use this checkout's own venv (plain `make venv`), or run make from "
            "the checkout that owns that venv.")


def probe(venv: str | Path) -> dict:
    py = Path(venv) / "bin" / "python"
    if not py.exists():
        py = Path(venv) / "Scripts" / "python.exe"
    if not py.exists():
        raise Refused(f"{venv} has no python. Fix: make venv")
    proc = subprocess.run([str(py), "-I", "-c", _PROBE], capture_output=True, text=True,
                          timeout=60, check=False)
    if proc.returncode != 0:
        raise Refused(f"{py} did not start: {proc.stderr.strip()[-200:]}. Fix: make clean venv")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def editable_pths(info: dict) -> list[str]:
    found: set[str] = set()                     # lib and lib64 may be one directory
    for d in info["site"]:
        found.update(str(Path(p).resolve())
                     for p in glob.glob(os.path.join(d, "__editable__.harness_manager-*.pth")))
    return sorted(found)


def check_harness(venv: str | Path, checkout: str | Path, info: dict | None = None) -> None:
    """(a) The venv's harness_manager comes from this checkout's src, through one editable link."""
    info = info or probe(venv)
    src = Path(checkout).resolve() / "src" / "harness_manager"
    where = info.get("harness")
    pths = editable_pths(info)
    fix = f"make clean venv   (in {Path(checkout).resolve()})"
    if where is None or not _inside(Path(where), src):
        raise Refused(
            f"the venv {venv} loads harness_manager from {where or 'nowhere'}, not from this "
            f"checkout ({src}). Another checkout or a worktree re-pointed it. Fix: {fix}")
    if len(pths) > 1:
        raise Refused(
            f"the venv {venv} has {len(pths)} editable harness-manager links "
            f"({', '.join(os.path.basename(p) for p in pths)}): two checkouts installed into it. "
            f"Fix: {fix}")


def _wheel_files(wheel: str | Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as z:
        return {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()
                if n.startswith("pyverify/") and n.endswith(".py")}


def check_pyverify(venv: str | Path, wheel: str | Path, info: dict | None = None) -> None:
    """(b) The venv's pyverify is the vendored wheel's content, installed (not editable)."""
    info = info or probe(venv)
    where = info.get("pyverify")
    fix = f"make venv   (installs {Path(wheel).name}); a dev pyverify needs ALLOW_DEV_PYVERIFY=1"
    if where is None:
        raise Refused(f"the venv {venv} has no pyverify. Fix: {fix}")
    pkg = Path(where).resolve().parent
    if not any(_inside(pkg, Path(s)) for s in info["site"]):
        raise Refused(
            f"the venv {venv} loads pyverify from {pkg}, outside the venv: an editable link to "
            f"a development checkout, not the vendored wheel. The release would be tested "
            f"against code it does not ship. Fix: {fix}")
    bad = []
    for name, digest in sorted(_wheel_files(wheel).items()):
        f = pkg.parent / name
        if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != digest:
            bad.append(name)
    if bad:
        raise Refused(
            f"the venv {venv}'s pyverify ({pkg}) differs from {Path(wheel).name}: "
            f"{', '.join(bad[:3])}{' …' if len(bad) > 3 else ''}. Fix: {fix}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("what", choices=["path", "harness", "pyverify"])
    p.add_argument("--venv", required=True)
    p.add_argument("--checkout", default=".")
    p.add_argument("--wheel", default="")
    p.add_argument("--allow-dev", action="store_true",
                   help="pyverify: warn instead of refusing (ALLOW_DEV_PYVERIFY=1)")
    a = p.parse_args(argv)
    try:
        if a.what == "path":
            check_path(a.venv, a.checkout)
        elif a.what == "harness":
            check_harness(a.venv, a.checkout)
        else:
            try:
                check_pyverify(a.venv, a.wheel)
            except Refused as exc:
                if not a.allow_dev:
                    raise
                print(f"venv-guard: WARNING (allowed): {exc}", file=sys.stderr)
    except Refused as exc:
        print(f"venv-guard: REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
