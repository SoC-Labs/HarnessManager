"""Detect for the Tools section: ``config test tools [NAME]`` and ``POST /settings/test``
``{section: "tools", name?}`` (lane SET-UI; ``docs/design/SETTINGS.md`` §7 "Tools").

For each tool (``openocd``, ``vivado``, ``hw_server``, ``uv``, or the one named) it finds the
executable the way Harness Manager would (the setting's value, which already includes its
developer variable, else the tool's own search), then proves it runs:

- ``openocd --version`` and ``uv --version``;
- ``vivado -version`` (``services/kit/vivado.py``: the same parser and search);
- **hw_server is never run.** Started with an option it does not know, it may bind 3121 and
  serve; its release comes from its path (``/…/Vivado/2024.1/bin/hw_server``), as
  ``services/xvc.py`` reads it. It must be an executable file.

**It runs nothing but those version probes**, each with a timeout, never through a shell,
and changes nothing: no file is written and no setting is set (the menu's "Use this path"
is a separate ``PUT /settings``). A path that does not run is a failed step with the reason.

The report (``testers`` contract): ``steps: [{step: <tool>, ok, detail, hint}]``, one per
tool, and ``tools: {<tool>: {path, version, how, key}}`` for the menu. ``ok`` is every step's.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from harness_manager.core.errors import UsageError

#: tool -> (the setting's key, what the menu calls it)
TOOLS: dict[str, tuple[str, str]] = {
    "openocd": ("tools.openocd", "OpenOCD"),
    "vivado": ("tools.vivado", "Vivado"),
    "hw_server": ("tools.hw_server", "hw_server"),
    "uv": ("tools.uv", "uv"),
}
VERSION_TIMEOUT_S = 15.0
VIVADO_TIMEOUT_S = 60.0
_OPENOCD = re.compile(r"Open On-Chip Debugger\s+(v?\d+\.\d+(?:\.\d+)?[^\s(]*)", re.I)
_UV = re.compile(r"\buv\s+(\d+\.\d+(?:\.\d+)?)", re.I)
_RELEASE_IN_PATH = re.compile(r"(?:Vivado|Vivado_Lab)[/\\](\d{4}\.\d+)", re.I)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _which(name: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH") or os.defpath)


def _exe(value: str, env: Mapping[str, str]) -> str | None:
    """A configured value as an executable: a file, or a name on the service's PATH."""
    p = Path(value).expanduser()
    if p.is_file():
        return str(p)
    return _which(value, env) if os.sep not in value else None


def _probe(argv: list[str], pattern: re.Pattern[str], timeout_s: float,
           runner: Runner) -> tuple[str, str]:
    """``(version, error)`` from ``argv`` (a version flag only). Never a shell."""
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return "", f"`{' '.join(argv)}` did not finish in {timeout_s:g} s"
    except OSError as exc:
        return "", f"`{' '.join(argv)}` could not run: {exc.strerror or exc}"
    out = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    m = pattern.search(out)
    if m:
        return m.group(1), ""
    if proc.returncode != 0:
        last = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
        return "", f"`{' '.join(argv)}` exited {proc.returncode}" + (f": {last[:160]}" if last else "")
    return "", f"`{' '.join(argv)}` printed no version line"


def _step(tool: str, ok: bool, detail: str, hint: str = "") -> dict[str, Any]:
    return {"step": tool, "ok": ok, "detail": detail, "hint": hint}


def detect_one(tool: str, value: str, env: Mapping[str, str], *,
               runner: Runner = subprocess.run) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(step, found)`` for one tool: ``value`` is its resolved setting ("" = search)."""
    key, label = TOOLS[tool]
    value = (value or "").strip()
    fix = f"set {key} to the {label} executable, or clear it to search again"
    if tool == "vivado":
        from harness_manager.services.kit import vivado as V

        if value.lower() in V.OFF:
            return (_step(tool, True, f"off: Harness Manager never looks for Vivado ({key})"),
                    {"path": "", "version": "", "how": "off", "key": key})
        found = V.discover(runner=lambda argv, **kw: runner(argv, **kw) if argv[1:] == ["-version"]
                           else _refuse(argv),
                           env={**env, V.ENV: value},
                           which=lambda n: _which(n, env))
        inst = found.install
        if inst is None:
            return (_step(tool, False, found.reason or "Vivado was not found",
                          fix if value else f"install Vivado, or set {key}"),
                    {"path": "", "version": "", "how": "", "key": key})
        if not inst.version:
            return (_step(tool, False, f"{inst.path} does not run: {inst.error}", fix),
                    {"path": inst.path, "version": "", "how": inst.how, "key": key})
        return (_step(tool, True, f"Vivado {inst.version} at {inst.path}"),
                {"path": inst.path, "version": inst.version, "how": inst.how, "key": key})

    if tool == "hw_server":
        path, how = "", ""
        if value:
            path, how = _exe(value, env) or "", "setting"
            if not path:
                return (_step(tool, False, f"{value} is not a file", fix),
                        {"path": "", "version": "", "how": "", "key": key})
        else:
            viv = _which("vivado", env)
            if viv and (Path(viv).parent / "hw_server").is_file():
                path, how = str(Path(viv).parent / "hw_server"), "beside vivado"
            elif env.get("XILINX_VIVADO") and \
                    (Path(env["XILINX_VIVADO"]) / "bin" / "hw_server").is_file():
                path, how = str(Path(env["XILINX_VIVADO"]) / "bin" / "hw_server"), "$XILINX_VIVADO"
            else:
                path, how = _which("hw_server", env) or "", "PATH"
        if not path:
            return (_step(tool, False, "hw_server was not found (not beside vivado, not in "
                                       "$XILINX_VIVADO, not on PATH)",
                          f"install Vivado (the Lab Edition is enough), or set {key}"),
                    {"path": "", "version": "", "how": "", "key": key})
        if not os.access(path, os.X_OK):
            return (_step(tool, False, f"{path} is not executable", fix),
                    {"path": path, "version": "", "how": how, "key": key})
        m = _RELEASE_IN_PATH.search(path)
        ver = m.group(1) if m else ""
        # Never run: its release is in its path (services/xvc.py vivado_version_of).
        return (_step(tool, True, f"hw_server{f' {ver}' if ver else ''} at {path} (not run: "
                                  "its release is read from its path)"),
                {"path": path, "version": ver, "how": how, "key": key})

    # openocd, uv: `--version`
    name = tool
    path = _exe(value, env) if value else _which(name, env)
    if not path:
        where = f"{value} is not a file or a command on the service's PATH" if value \
            else f"{name} is not on the service's PATH"
        return (_step(tool, False, where, fix if value else f"install {label}, or set {key}"),
                {"path": "", "version": "", "how": "", "key": key})
    pattern = _OPENOCD if tool == "openocd" else _UV
    ver, err = _probe([path, "--version"], pattern, VERSION_TIMEOUT_S, runner)
    how = "setting" if value else "PATH"
    if err:
        return (_step(tool, False, f"{path} does not run: {err}", fix),
                {"path": path, "version": "", "how": how, "key": key})
    return (_step(tool, True, f"{label} {ver} at {path}"),
            {"path": path, "version": ver, "how": how, "key": key})


def _refuse(argv: list[str]) -> Any:  # pragma: no cover - vivado.discover runs only -version
    raise OSError(f"refused to run {argv!r}: Detect runs only a version probe")


def detect_tools(req: Any, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    """The "tools" section tester (``testers.CONVENTION``). ``req.name``: one tool, or all."""
    names = list(TOOLS)
    if req.name:
        if req.name not in TOOLS:
            raise UsageError(f"no tool {req.name!r} to detect",
                             hint=f"tools: {', '.join(TOOLS)}")
        names = [req.name]
    r = req.resolver
    env: Mapping[str, str] = r.env if r is not None else os.environ
    steps, found = [], {}
    for i, tool in enumerate(names):
        req.progress(tool, i, len(names))
        key = TOOLS[tool][0]
        table = req.table or {}
        if tool in table:
            value = str(table[tool] or "")
        elif r is not None:
            value = str(r.resolve(key).value or "")
        else:
            value = ""
        step, info = detect_one(tool, value, env, runner=runner)
        steps.append(step)
        found[tool] = info
    return {"ok": all(s["ok"] for s in steps), "steps": steps, "tools": found}
