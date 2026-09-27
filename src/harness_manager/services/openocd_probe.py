"""Which debug adapters an OpenOCD binary was built with (lane DEBUG-OCD).

    >>> from harness_manager.services import openocd_probe
    >>> openocd_probe.probe_adapters("/usr/bin/openocd").has("remote_bitbang")

Why: OpenOCD builds differ in their adapters. The SoC Labs build on srv03335
(0.12.0-g9ea7f3d) has only jlink, buspirate and hostio4, so ``debug up`` failed deep inside
OpenOCD with "The specified debug interface was not found (remote_bitbang)". The debug
service asks first (``services.debug.find_openocd``) and says which binary, what it has and
what to install instead.

How it asks: ``<bin> -c "adapter list" -c shutdown``, and ``-c interface_list -c shutdown``
when that lists nothing (builds before 0.11). No config file is loaded: a ``-c`` on the
command line stops OpenOCD from reading ``openocd.cfg`` (helper/configuration.c,
``parse_config_file``), and ``shutdown`` runs before ``init``, so no adapter is opened and no
hardware is touched. A 10 s timeout, never a shell, the output in a file (not a pipe, so a
child the binary left behind cannot hold the probe open).

What OpenOCD prints, on stderr (measured 2026-09-27)::

    The following debug adapters are available:     0.12.0 release
    1: jlink
    2: buspirate

    remote_bitbang { jtag swd }                     xPack 0.12.0+dev (0.12.0-7)

    The following debug interfaces are available:   interface_list (0.10, 0.11)
    1: ftdi

A list is cached by the binary's (real path, mtime, size), so a replaced binary is asked
again; a probe that failed (a timeout, no list) is remembered for ``FAILURE_TTL_S`` only.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["PROBE_TIMEOUT_S", "REMOTE_BITBANG", "AdapterList", "probe_adapters",
           "parse_adapters", "candidates", "fix_hint", "clear_cache"]

PROBE_TIMEOUT_S = 10.0
REMOTE_BITBANG = "remote_bitbang"
ADAPTER_LIST = ("-c", "adapter list", "-c", "shutdown")
INTERFACE_LIST = ("-c", "interface_list", "-c", "shutdown")

# "1: jlink" (0.12 release, interface_list) or "remote_bitbang { jtag swd }" (0.12+dev).
_NUMBERED_RE = re.compile(r"^\s*\d+:\s*([A-Za-z0-9_.+-]+)\s*$")
_TRANSPORTS_RE = re.compile(r"^\s*([A-Za-z0-9_.+-]+)\s+\{[^{}]*\}\s*$")
_BANNER_RE = re.compile(r"Open On-Chip Debugger\s+(\S+)")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class AdapterList:
    """What one binary said. ``error``: why there is no list ("" when there is one)."""

    binary: str
    adapters: tuple[str, ...] = ()
    command: str = ""               # the one that listed them: "adapter list" / "interface_list"
    version: str = ""               # from the banner, e.g. "0.12.0+dev-02228-ge5888bda3-dirty"
    error: str = ""

    @property
    def listed(self) -> bool:
        return not self.error

    def has(self, adapter: str) -> bool:
        return adapter in self.adapters

    def shown(self) -> str:
        """The adapters for a message: "jlink, buspirate, hostio4"."""
        return ", ".join(self.adapters) if self.adapters else "none"

    def verdict(self, need: str = REMOTE_BITBANG) -> str:
        """One line: "/x/openocd: has remote_bitbang (27 adapters)", or what is wrong."""
        if self.error:
            return f"{self.binary}: could not list its adapters ({self.error})"
        if self.has(need):
            return f"{self.binary}: has {need} ({len(self.adapters)} adapters)"
        return f"{self.binary}: no {need} (it has: {self.shown()})"

    def as_dict(self, need: str = REMOTE_BITBANG) -> dict[str, Any]:
        return {"path": self.binary, "adapters": list(self.adapters), "command": self.command,
                "version": self.version, "error": self.error, "need": need,
                "ok": self.has(need), "verdict": self.verdict(need)}


def fix_hint(need: str = REMOTE_BITBANG, *, env_var: str = "") -> str:
    """The next action when no usable binary was found. ``env_var``: the variable that chose
    the binary, which overrides the setting, so it must change (or go) too."""
    where = ("then set tools.openocd (Settings → Tools, or "
             "`harness-manager config set tools.openocd PATH`)")
    if env_var:
        where = (f"then point ${env_var} at it (it overrides tools.openocd), or unset it and "
                 "set tools.openocd (Settings → Tools, or "
                 "`harness-manager config set tools.openocd PATH`)")
    return (f"use an OpenOCD with {need}, e.g. xPack OpenOCD 0.12 (the build the lab hub "
            f"uses), {where}")


# --- parsing ------------------------------------------------------------------------------------


def parse_adapters(text: str) -> tuple[str, ...]:
    """The adapter names in an ``adapter list`` / ``interface_list`` output, in order."""
    out: list[str] = []
    for line in text.splitlines():
        m = _NUMBERED_RE.match(line) or _TRANSPORTS_RE.match(line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return tuple(out)


def _version(text: str) -> str:
    m = _BANNER_RE.search(text)
    return m.group(1) if m else ""


def _last_line(text: str) -> str:
    return next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")


# --- running ------------------------------------------------------------------------------------


def _run(argv: Sequence[str], timeout: float) -> tuple[int | None, str]:
    """``(exit code, stdout + stderr)``; ``None`` for the code when it timed out (killed)."""
    flags: dict[str, Any] = {}
    if os.name == "posix":
        flags["start_new_session"] = True            # a wrapper's children die with it
    elif sys.platform == "win32":
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, **flags)
        try:
            rc: int | None = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            rc = None
        out.seek(0)
        return rc, out.read().decode(errors="replace")


def _kill(proc: subprocess.Popen) -> None:
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)       # start_new_session: the group is ours
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(5.0)


def _run_with(runner: Runner, argv: Sequence[str], timeout: float) -> tuple[int | None, str]:
    """A caller's ``subprocess.run``-like runner (a test seam; the Settings menu's Detect)."""
    try:
        res = runner(list(argv), capture_output=True, text=True, timeout=timeout,
                     stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, ""
    return res.returncode, f"{res.stdout or ''}\n{res.stderr or ''}"


def _ask(binary: str, timeout: float, runner: Runner | None) -> AdapterList:
    name = Path(binary).name
    tried: list[str] = []
    version = ""
    for args in (ADAPTER_LIST, INTERFACE_LIST):
        shown = f'{name} -c "{args[1]}" -c shutdown'
        try:
            rc, text = (_run_with(runner, [binary, *args], timeout) if runner is not None
                        else _run(argv=[binary, *args], timeout=timeout))
        except OSError as exc:
            return AdapterList(binary, error=f"it does not run: {exc.strerror or exc}")
        version = version or _version(text)
        if rc is None:
            return AdapterList(binary, version=version,
                               error=f"`{shown}` did not finish within {timeout:g} s")
        found = parse_adapters(text)
        if found:
            return AdapterList(binary, found, command=args[1], version=version)
        last = _last_line(text)
        tried.append(f"`{shown}` exited {rc}" + (f": {last[:160]}" if last else
                                                 " and printed nothing"))
    return AdapterList(binary, version=version,
                       error="it listed no adapters (" + "; ".join(tried) + ")")


# --- the cache ----------------------------------------------------------------------------------

#: How long a failed probe (a timeout, no list) is remembered: long enough that a polled
#: ``debug status`` does not wait out the timeout on every call, short enough that a
#: transient failure under load is retried soon. A list is kept until the binary changes.
FAILURE_TTL_S = 30.0

_mu = threading.Lock()
_cache: dict[tuple[str, int, int], tuple[AdapterList, float]] = {}   # key -> (list, when)


def _key(binary: str) -> tuple[str, int, int] | None:
    try:
        real = os.path.realpath(binary)
        st = os.stat(real)
    except OSError:
        return None
    return (os.path.normcase(real), st.st_mtime_ns, st.st_size)


def probe_adapters(binary: str | os.PathLike[str], *, timeout: float | None = None,
                   runner: Runner | None = None) -> AdapterList:
    """The adapters ``binary`` was built with. Never raises for a binary that does not run
    or answer: ``error`` says why. ``timeout``: each command's (default ``PROBE_TIMEOUT_S``).
    Cached per (real path, mtime, size) unless ``runner`` is given (a caller's own
    ``subprocess.run``-like seam, e.g. the Settings menu's Detect)."""
    binary = os.fspath(binary)
    timeout = PROBE_TIMEOUT_S if timeout is None else timeout
    key = _key(binary) if runner is None else None
    if key is not None:
        with _mu:
            hit, when = _cache.get(key, (None, 0.0))
        if hit is not None and (hit.listed or time.monotonic() - when < FAILURE_TTL_S):
            return AdapterList(binary, hit.adapters, hit.command, hit.version, hit.error)
    got = _ask(binary, timeout, runner)
    if key is not None:
        with _mu:
            _cache[key] = (got, time.monotonic())
    return got


def clear_cache() -> None:
    """Forget every list (tests)."""
    with _mu:
        _cache.clear()


# --- the search ---------------------------------------------------------------------------------


def _exts(name: str) -> tuple[str, ...]:
    if sys.platform != "win32":
        return ("",)
    pathext = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    if os.path.splitext(name)[1].lower() in {e.lower() for e in pathext}:
        return ("",)                                 # "openocd.exe" asked for by name
    return tuple(pathext)


def candidates(name: str = "openocd", path: str | None = None) -> list[str]:
    """Every ``name`` on ``path`` (default ``$PATH``), in PATH order, one per real file.

    ``shutil.which`` returns only the first; this returns them all, so a build without the
    adapter early on PATH does not hide a good one later. Windows: ``PATHEXT`` (the first
    extension that matches in each directory, as ``which`` picks)."""
    path = os.environ.get("PATH", os.defpath) if path is None else path
    seen: set[str] = set()
    out: list[str] = []
    for d in _dirs(path):
        for ext in _exts(name):
            p = os.path.join(d, name + ext)
            if not (os.path.isfile(p) and os.access(p, os.X_OK)):
                continue
            real = os.path.normcase(os.path.realpath(p))
            if real not in seen:
                seen.add(real)
                out.append(p)
            break
    return out


def _dirs(path: str) -> Iterable[str]:
    for d in path.split(os.pathsep):
        d = d.strip().strip('"') if sys.platform == "win32" else d
        if d:
            yield d
