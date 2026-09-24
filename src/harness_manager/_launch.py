"""The ``harness-manager`` command of an install: it follows the app self-update pointer.

The installers (``scripts/install.sh``, ``scripts/install.ps1``) put this module's ``main``
on PATH as ``harness-manager``. It is the ``harness-manager-launch`` console script of the
installer's venv. It reads ``<install root>/current.json`` and runs the version the app's
self-update selected, with the same arguments and the same exit code. Otherwise it runs
the installer's own version. The install root::

    <install root>/          $HARNESS_MANAGER_HOME at install time (~/.local/share/harness-manager,
                             %LOCALAPPDATA%\\harness-manager)
        install.json         what the installer installed: venv, version, extras, uv
        venv/                the installer's venv (self-update never changes it)
        current.json         the pointer {"current": "0.2.0", "previous": "", "installer": {...}}
        versions/<v>/        one self-updated version's venv
        wheels/, reqs/       what the self-updater built those venvs from

- **POSIX:** ``os.execv``, so the same pid and the same terminal. **Windows** has no real
  exec, so the launcher runs a child process. The child handles Ctrl-C itself, and the
  launcher passes its exit code through.
- **The installer's version** runs, in this process, when ``current`` is ``""``, when there
  is no pointer, or when the version the pointer names has no venv (a note on stderr).
- **A developer install never follows the pointer.** That covers ``pip install -e``, code
  imported from outside this venv (``PYTHONPATH``), a venv that no installer made, and
  ``HARNESS_MANAGER_NO_SELF_UPDATE=1``. The self-updater applies the same test before it
  changes the pointer (``services/update/app.py``).
- **``HARNESS_MANAGER_USE_INSTALLED=1``** runs the installer's version whatever the pointer
  says. It is the way back when a self-updated version cannot start.

The target runs as ``<venv>/bin/python -c <BOOT>``, not through its console script. A venv
that was moved (the M4 migration below) keeps a working ``python``, but its scripts'
``#!`` lines point at the old place.

This module only changes when the installer runs again, so it stays small. It uses the
standard library only, and imports nothing from harness_manager until it runs the CLI.

``python -m harness_manager._launch --installer-hook …`` is the installer's side. It records
the install (``install.json``), registers the installer's venv in the pointer so rollback
can reach it (M2), lets a re-run of the installer win (M3), and moves an older
``<state dir>/update/app`` into the install root (M4).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# shutil, subprocess, time and uuid are imported where they are used: every command pays for
# this module's imports.

DIST = "harness-manager"
INSTALL_JSON = "install.json"
POINTER = "current.json"
USE_INSTALLED_ENV = "HARNESS_MANAGER_USE_INSTALLED"
NO_SELF_UPDATE_ENV = "HARNESS_MANAGER_NO_SELF_UPDATE"
# Run the CLI of another venv. `-c` puts '' (the current directory) first on sys.path, and a
# checkout in the current directory would shadow the venv's harness_manager: drop it.
BOOT = ("import sys; sys.path[:1] = [p for p in sys.path[:1] if p]; "
        "from harness_manager.cli.main import main; sys.exit(main())")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() not in ("", "0", "false", "no", "off")


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict) -> None:
    """Write-then-rename, so a reader sees the old file or the new one, never half."""
    import uuid

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def version_dir(version: str) -> str:
    """The directory name of a version under ``versions/`` (``state.safe_name``'s rule)."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", version) or "_"
    return "_" if name in (".", "..") else name


def python_in(venv: Path, *, windows: bool | None = None) -> Path:
    win = os.name == "nt" if windows is None else windows
    return Path(venv) / ("Scripts/python.exe" if win else "bin/python")


def _norm(path: Path | str) -> str:
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        resolved = Path(os.path.abspath(path))
    return os.path.normcase(str(resolved))


def same_path(a: Path | str, b: Path | str) -> bool:
    return bool(str(a)) and bool(str(b)) and _norm(a) == _norm(b)


def _inside(path: Path | str, root: Path | str) -> bool:
    return _norm(path).startswith(_norm(root).rstrip(os.sep) + os.sep)


def install_root(prefix: Path | str | None = None) -> Path | None:
    """The install root that the venv ``prefix`` belongs to, or None when no installer made it.

    Either ``<root>/venv``, which ``<root>/install.json`` names, or ``<root>/versions/<v>``, a
    self-updated version beside an ``install.json``.
    """
    p = Path(prefix or sys.prefix)
    info = read_json(p.parent / INSTALL_JSON)
    if info is not None and same_path(str(info.get("venv") or ""), p):
        return p.parent
    if p.parent.name == "versions" and read_json(p.parent.parent / INSTALL_JSON) is not None:
        return p.parent.parent
    return None


def _editable() -> bool:
    """``direct_url.json`` of the harness-manager distribution says ``editable``."""
    try:
        from importlib.metadata import distribution

        text = distribution(DIST).read_text("direct_url.json")
        return bool(text) and json.loads(text).get("dir_info", {}).get("editable") is True
    except Exception:  # noqa: BLE001 - no metadata or an odd file: not an editable install
        return False


def dev_install(prefix: Path | str | None = None, env: dict[str, str] | None = None, *,
                module_file: str | None = None, editable: bool | None = None) -> str:
    """Why this copy must never follow or change the self-update pointer. ``""``: it may."""
    env = os.environ if env is None else env
    prefix = Path(prefix or sys.prefix)
    if _truthy(env.get(NO_SELF_UPDATE_ENV)):
        return f"{NO_SELF_UPDATE_ENV} is set, so self-update is off"
    here = Path(module_file or __file__)
    if not _inside(here, prefix):
        return (f"this is a developer install: harness_manager runs from {here.parent.parent}, "
                f"not from its venv {prefix} (pip install -e, or PYTHONPATH). Update it with git")
    if _editable() if editable is None else editable:
        return "this is a developer install (pip install -e). Update it with git"
    if install_root(prefix) is None:
        return (f"{prefix} was not made by the Harness Manager installer, so self-update is off. "
                "Update it the way you installed it, or install with scripts/install.sh")
    return ""


def selected(prefix: Path | str | None = None, env: dict[str, str] | None = None, *,
             windows: bool | None = None, dev: str | None = None) -> tuple[Path | None, str]:
    """(the Python to run instead of this one, or None to run here; a note for stderr)."""
    env = os.environ if env is None else env
    prefix = Path(prefix or sys.prefix)
    if _truthy(env.get(USE_INSTALLED_ENV)):
        return None, ""
    if dev_install(prefix, env) if dev is None else dev:
        return None, ""
    root = install_root(prefix)
    if root is None:
        return None, ""
    current = (read_json(root / POINTER) or {}).get("current")
    if not isinstance(current, str) or not current:
        return None, ""
    venv = root / "versions" / version_dir(current)
    if same_path(venv, prefix):
        return None, ""
    py = python_in(venv, windows=windows)
    if not py.exists():
        return None, (f"the self-updated version {current} is missing ({venv}), so this runs "
                      "the installed version. Re-run the installer to tidy the pointer")
    return py, ""


def run_here(args: list[str]) -> int:
    from harness_manager.cli.main import main as cli

    return int(cli(args) or 0)


def _exit_code(rc: int, windows: bool) -> int:
    if windows and rc > 0x7FFFFFFF:        # an NTSTATUS such as 0xC000013A (Ctrl-C)
        return rc - (1 << 32)
    if rc < 0:                             # killed by a signal (POSIX)
        return 128 - rc
    return rc


def run_there(py: Path, args: list[str], *, windows: bool | None = None,
              execv=os.execv, call=None) -> int:
    """Run ``args`` with the CLI of the venv that ``py`` belongs to."""
    win = os.name == "nt" if windows is None else windows
    argv = [str(py), "-c", BOOT, *args]
    if not win:
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            execv(str(py), argv)           # does not return
        except OSError as exc:
            print(f"harness-manager: cannot run {py} ({exc}), so this runs the installed "
                  f"version. `{USE_INSTALLED_ENV}=1 harness-manager update rollback --app` "
                  "goes back for good", file=sys.stderr)
            return run_here(args)
    import signal
    import subprocess

    call = call or subprocess.call
    # The console sends Ctrl-C (and Ctrl-Break) to every process on it: the child answers
    # it, and the launcher waits for the child's exit code.
    saved = {}
    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            saved[sig] = signal.signal(sig, signal.SIG_IGN)
    try:
        return _exit_code(int(call(argv)), win)
    except OSError as exc:
        print(f"harness-manager: cannot run {py} ({exc}), so this runs the installed version",
              file=sys.stderr)
        return run_here(args)
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    """``harness-manager …`` as an installer put it on PATH."""
    args = list(sys.argv[1:] if argv is None else argv)
    py, note = selected()
    if note:
        print(f"harness-manager: {note}", file=sys.stderr)
    if py is None:
        return run_here(args)
    return run_there(py, args)


# --- the installer's side -------------------------------------------------------------------

_VERSION_RE = re.compile(
    r"^v?(?P<nums>\d+(?:\.\d+){0,3})"
    r"(?:[-.]?(?P<pre>(?:a|alpha|b|beta|rc|c|pre|preview|dev)[-.]?\d*))?"
    r"(?:\+[0-9A-Za-z.-]+)?$", re.IGNORECASE)
_PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "preview": 3, "c": 3,
             "rc": 3}


def version_key(text: str) -> tuple | None:
    """The order of ``services/update/version.py`` (a pre-release before its release), or None."""
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return None
    nums = [int(x) for x in m.group("nums").split(".")]
    nums += [0] * (4 - len(nums))
    pre = m.group("pre")
    if not pre:
        return (tuple(nums), 9, 0)
    kind = re.match(r"[a-z]+", pre.lower()).group(0)
    num = re.search(r"\d+", pre)
    return (tuple(nums), _PRE_RANK[kind], int(num.group(0)) if num else 0)


def _rmdir_quiet(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def migrate(legacy: Path, root: Path) -> list[str]:
    """Move a pre-0.2 self-update layout (``<state dir>/update/app``) into the install root.

    The venvs move whole. Their ``python`` still runs (the launcher uses it); their scripts'
    ``#!`` lines do not, and nothing uses those. A version already in the root wins.
    """
    import shutil

    legacy, root = Path(legacy), Path(root)
    if not legacy.is_dir() or same_path(legacy, root):
        return []
    lines = []
    for name in ("versions", "wheels", "reqs"):
        src = legacy / name
        if not src.is_dir():
            continue
        for child in sorted(src.iterdir()):
            dst = root / name / child.name
            if dst.exists() or dst.is_symlink():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(child), str(dst))
            if name == "versions":
                lines.append(f"moved    self-updated version {child.name} to {dst}")
        _rmdir_quiet(src)
    old = legacy / POINTER
    if old.is_file():
        data = read_json(old)
        if data is not None and not (root / POINTER).exists():
            write_json(root / POINTER, data)
            lines.append(f"moved    the self-update pointer to {root / POINTER}")
        old.unlink()
    _rmdir_quiet(legacy)
    return lines


def register(root: Path, venv: Path, version: str, *, extras: list[str], uv: str = "",
             installer: str = "", windows: bool | None = None) -> list[str]:
    """Record the install, and make the installer's venv a version the pointer knows."""
    import time

    root, venv = Path(root), Path(venv)
    before = read_json(root / INSTALL_JSON) or {}
    kept = [e for e in before.get("extras") or [] if isinstance(e, str)]
    write_json(root / INSTALL_JSON, {
        "schema": 1, "venv": str(venv), "version": version,
        "extras": sorted(set(kept) | set(extras)), "uv": uv, "installer": installer,
        "installed_at": time.time(),
    })
    ptr = read_json(root / POINTER) or {}
    ptr.setdefault("current", "")
    ptr.setdefault("previous", "")
    ptr.setdefault("versions", {})
    # M2: "" in current/previous is this venv, and rollback may return to it.
    ptr["installer"] = {"version": version, "venv": str(venv)}
    lines = []
    current = ptr["current"] if isinstance(ptr["current"], str) else ""
    if current:
        there = python_in(root / "versions" / version_dir(current), windows=windows).exists()
        new, cur = version_key(version), version_key(current)
        if not there:
            ptr["current"] = ""
            lines.append(f"pointer  the self-updated {current} is gone: the command runs "
                         f"{version}")
        elif new is None or cur is None or new >= cur:
            # M3: a re-run of the installer wins when it installs a version at least as new.
            ptr["previous"], ptr["current"] = current, ""
            lines.append(f"pointer  the command runs {version} now. The self-updated "
                         f"{current} stays for `harness-manager update rollback --app`")
        else:
            ptr["previous"] = ""
            lines.append(f"pointer  the self-updated {current} is newer than {version}, so "
                         f"the command keeps running it. `harness-manager update rollback "
                         f"--app` switches to {version}")
    write_json(root / POINTER, ptr)
    return lines


def installer_hook(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m harness_manager._launch --installer-hook")
    ap.add_argument("--root", required=True, help="the install root")
    ap.add_argument("--venv", required=True, help="the installer's venv")
    ap.add_argument("--version", required=True, help="the version just installed")
    ap.add_argument("--extras", default="", help="the extras installed, space- or comma-separated")
    ap.add_argument("--uv", default="", help="the uv the installer put in the venv, if any")
    ap.add_argument("--legacy", default="", help="<state dir>/update/app of an older install")
    ap.add_argument("--installer", default="", help="which installer ran")
    a = ap.parse_args(argv)
    lines = migrate(Path(a.legacy), Path(a.root)) if a.legacy else []
    lines += register(Path(a.root), Path(a.venv), a.version, uv=a.uv, installer=a.installer,
                      extras=[e for e in re.split(r"[\s,]+", a.extras) if e])
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--installer-hook"]:
        sys.exit(installer_hook(sys.argv[2:]))
    sys.exit(main())
