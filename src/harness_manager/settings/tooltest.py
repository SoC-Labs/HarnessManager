"""Detect for the Tools section: ``config test tools [NAME]`` and ``POST /settings/test``
``{section: "tools", name?}`` (lane SET-UI; ``docs/design/SETTINGS.md`` §7 "Tools").

For each tool (``openocd``, ``vivado``, ``hw_server``, ``uv``, or the one named) it finds the
executable the way Harness Manager would (the setting's value, which already includes its
developer variable, else the tool's own search), then proves it runs:

- ``openocd --version`` and ``uv --version``;
- OpenOCD's adapters: ``openocd -c "adapter list" -c shutdown`` (``services/openocd_probe.py``,
  lane DEBUG-OCD: no config is loaded, no adapter opened). The step passes only when it has
  ``remote_bitbang``, the adapter the board's JTAG server speaks; the SoC Labs build (jlink,
  buspirate, hostio4) fails with the fix. With no setting, every ``openocd`` on the service's
  PATH is tried in order and the first with remote_bitbang is taken, as ``debug up`` does;
- ``vivado -version`` (``services/kit/vivado.py``: the same parser and search);
- **hw_server is never run.** Started with an option it does not know, it may bind 3121 and
  serve; its release comes from its path (``/…/Vivado/2024.1/bin/hw_server``), as
  ``services/xvc.py`` reads it. It must be an executable file.

**It runs nothing but those probes**, each with a timeout, never through a shell,
and changes nothing: no file is written and no setting is set (the menu's "Use this path"
is a separate ``PUT /settings``). A path that does not run is a failed step with the reason.
Each probe runs the ``openocd_probe`` way (``core.proc.probe_run``, REVIEW-W5 12): its output
to a file, its process group killed on timeout, no console window on Windows. The demo
refuses a request ``table`` that names a path (``daemon/settings_api.py``).

The report (``testers`` contract): ``steps: [{step: <tool>, ok, detail, hint}]``, one per
tool, and ``tools: {<tool>: {path, version, how, key}}`` for the menu (``openocd`` adds
``adapters``: the list its binary printed). ``ok`` is every step's.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from harness_manager.core.errors import UsageError
from harness_manager.core.proc import probe_run
from harness_manager.services import openocd_probe

#: tool -> (the setting's key, what the menu calls it)
OPENOCD_ENV = "HARNESS_MANAGER_OPENOCD"        # services/debug.py: it overrides tools.openocd
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
    """A configured value as an executable: a file, or a name on the service's PATH. On
    Windows a path without an extension is tried with each ``PATHEXT`` one
    (``C:\\Xilinx\\…\\hw_server`` is ``hw_server.bat``), then through ``_which``."""
    p = Path(value).expanduser()
    if p.is_file():
        return str(p)
    if sys.platform == "win32" and not p.suffix:
        for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").lower().split(";"):
            if ext and p.with_name(p.name + ext).is_file():
                return str(p.with_name(p.name + ext))
        return _which(str(p), env)
    return _which(value, env) if os.sep not in value else None


def _hw_server_names() -> tuple[str, ...]:
    """Vivado's ``bin`` holds ``hw_server`` on Linux, ``hw_server.bat`` on Windows."""
    return ("hw_server.bat", "hw_server.exe", "hw_server") if sys.platform == "win32" \
        else ("hw_server",)


def _beside(directory: Path) -> str:
    return next((str(directory / n) for n in _hw_server_names() if (directory / n).is_file()), "")


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
               runner: Runner = probe_run,
               source: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """``(step, found)`` for one tool: ``value`` is its resolved setting ("" = search);
    ``source``: where it came from (the resolver's: "env" means its variable set it)."""
    key, label = TOOLS[tool]
    value = (value or "").strip()
    fix = f"set {key} to the {label} executable, or clear it to search again"
    if tool == "openocd":
        return _detect_openocd(value, env, runner, source=source)
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
            beside = _beside(Path(viv).parent) if viv else ""
            xil = _beside(Path(env["XILINX_VIVADO"]) / "bin") if env.get("XILINX_VIVADO") else ""
            if beside:
                path, how = beside, "beside vivado"
            elif xil:
                path, how = xil, "$XILINX_VIVADO"
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

    # uv: `--version`
    name = tool
    path = _exe(value, env) if value else _which(name, env)
    if not path:
        where = f"{value} is not a file or a command on the service's PATH" if value \
            else f"{name} is not on the service's PATH"
        return (_step(tool, False, where, fix if value else f"install {label}, or set {key}"),
                {"path": "", "version": "", "how": "", "key": key})
    ver, err = _probe([path, "--version"], _UV, VERSION_TIMEOUT_S, runner)
    how = "setting" if value else "PATH"
    if err:
        return (_step(tool, False, f"{path} does not run: {err}", fix),
                {"path": path, "version": "", "how": how, "key": key})
    return (_step(tool, True, f"{label} {ver} at {path}"),
            {"path": path, "version": ver, "how": how, "key": key})


def _detect_openocd(value: str, env: Mapping[str, str], runner: Runner, *,
                    source: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """OpenOCD: ``--version``, then its adapters (DEBUG-OCD). The step passes only for a binary
    with remote_bitbang: the one ``services.debug.find_openocd`` would run."""
    tool, need = "openocd", openocd_probe.REMOTE_BITBANG
    key, label = TOOLS[tool]
    none = {"path": "", "version": "", "how": "", "key": key, "adapters": []}
    fix = f"set {key} to the {label} executable, or clear it to search again"
    if value:
        path = _exe(value, env)
        if not path:
            return (_step(tool, False, f"{value} is not a file or a command on the service's "
                                       "PATH", fix), none)
        paths, how = [path], "setting"
    else:
        paths, how = openocd_probe.candidates(tool, env.get("PATH") or os.defpath), "PATH"
        if not paths:
            return (_step(tool, False, f"{tool} is not on the service's PATH",
                          f"install {label} (xPack OpenOCD 0.12 has {need}), or set {key}"),
                    none)
    tried: list[tuple[str, str, str, openocd_probe.AdapterList | None]] = []
    for path in paths:
        ver, err = _probe([path, "--version"], _OPENOCD, VERSION_TIMEOUT_S, runner)
        if err:
            tried.append((path, "", err, None))
            continue
        got = openocd_probe.probe_adapters(path, runner=runner)
        if got.has(need):
            return (_step(tool, True, f"{label} {ver} at {got.verdict(need)}"),
                    {"path": path, "version": ver, "how": how, "key": key,
                     "adapters": list(got.adapters)})
        tried.append((path, ver, "", got))
    hint = openocd_probe.fix_hint(need, env_var=OPENOCD_ENV if source == "env" else "")
    path, ver, err, got = tried[0]
    found = {"path": path, "version": ver, "how": how, "key": key,
             "adapters": list(got.adapters) if got else []}
    if len(tried) == 1:
        if got is None:
            return _step(tool, False, f"{path} does not run: {err}", fix), found
        return _step(tool, False, f"{label} {ver} at {got.verdict(need)}", hint), found
    each = "; ".join(f"{p} does not run" if g is None else g.verdict(need)
                     for p, _, _, g in tried)
    return _step(tool, False, f"no {tool} on the service's PATH has {need}: {each}", hint), found


def _refuse(argv: list[str]) -> Any:  # pragma: no cover - vivado.discover runs only -version
    raise OSError(f"refused to run {argv!r}: Detect runs only a version probe")


def detect_tools(req: Any, *, runner: Runner = probe_run) -> dict[str, Any]:
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
        source = ""
        if tool in table:
            value = str(table[tool] or "")
        elif r is not None:
            got = r.resolve(key)
            value, source = str(got.value or ""), str(got.source or "")
        else:
            value = ""
        step, info = detect_one(tool, value, env, runner=runner, source=source)
        steps.append(step)
        found[tool] = info
    return {"ok": all(s["ok"] for s in steps), "steps": steps, "tools": found}
