"""Does the kit's Vivado start? (lane KIT-LIC; the guide's Tools step)

    >>> from harness_manager.services.kit import launch
    >>> launch.probe("/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado").detail
    'Vivado 2026.1 starts (licence tier: ENTERPRISE)'

Why: Vivado 2026.1 does not start at all without a licence file (exit 42), so a fresh
account fails at the very first command of the build. The guide says so before the build.

**``vivado -version`` cannot tell.** It exits 0 with no licence on 2026.1 too (measured
2026-09-28 on srv03335, HOME empty, no ``XILINXD_LICENSE_FILE``/``LM_LICENSE_FILE``). So
the probe launches Vivado for real, the way ``kit build`` does, with a two-line Tcl and no
design::

    vivado -mode batch -nolog -nojournal -notrace -source hm_launch.tcl
    # hm_launch.tcl:  puts HM_LAUNCH_OK ; exit 0

What it printed, measured (2026-09-28, srv03335)::

    2026.1, no licence   exit 42 in ~4 s: "ERROR: Vivado Design Suite cannot be launched
                         because a valid license was not found. ..."
    2026.1, lab server   exit 0 in ~18 s: "INFO: [Common 17-3922] A valid Vivado Design
                         Suite ENTERPRISE license has been detected. ..." then HM_LAUNCH_OK
    2024.1, no licence   exit 0 in ~10 s (it starts; only synthesis needs the licence)

It opens no design and names no part, so it never asks for the device licence: a start
does NOT prove the xcku115 licence, which synthesis still checks (``licence.py``).

How it runs: ``core.proc.run_to_file`` (output to a file, its own process group killed on
timeout, never a shell) in a scratch directory that is deleted afterwards, so nothing
lands in the caller's directory. Vivado writes its usual ``~/.Xilinx`` state, as any
launch does. The result is cached per (real path, mtime, size, the licence variables),
so setting ``XILINXD_LICENSE_FILE`` asks again; a start that failed is remembered for
``FAILURE_TTL_S`` only (a licence server can come back). ``tools.vivado = off`` (and
``HARNESS_MANAGER_VIVADO=off``, as the tests set it) runs nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_manager.core.pack import KitCheck
from harness_manager.core.proc import run_to_file

__all__ = ["LAUNCH_TIMEOUT_S", "NO_LICENCE_EXIT", "Launch", "argv", "probe", "clear_cache"]

LAUNCH_TIMEOUT_S = 60.0
#: Vivado 2026.1's exit code when no licence was found at start.
NO_LICENCE_EXIT = 42
LICENCE_ENV = ("XILINXD_LICENSE_FILE", "LM_LICENSE_FILE")
MARKER = "HM_LAUNCH_OK"
TCL_NAME = "hm_launch.tcl"
TCL = f"puts {MARKER}\nexit 0\n"
FAILURE_TTL_S = 30.0

_NO_LICENCE_RE = re.compile(r"cannot be launched because a valid licen[sc]e was not found",
                            re.IGNORECASE)
_TIER_RE = re.compile(r"A valid Vivado Design Suite\s+(\w+)\s+licen[sc]e has been detected",
                      re.IGNORECASE)
_BANNER_RE = re.compile(r"\bVivado\s+v(\d{4}\.\d+(?:\.\d+)?)", re.IGNORECASE)
_ROOT_REL = re.compile(r"^(\d{4}\.\d+(?:\.\d+)?)$")


@dataclass(frozen=True)
class Launch:
    """One launch of one Vivado. ``state``: ``ok`` (it started and ran the Tcl), ``failed``
    (it exited non-zero, or does not run), ``unchecked`` (not run, or it did not finish:
    never a pass)."""

    path: str
    state: str
    detail: str
    rc: int | None = None
    release: str = ""
    tier: str = ""                # "ENTERPRISE", from [Common 17-3922]; "" when not printed
    no_licence: bool = False      # exit 42 / "a valid license was not found"
    fix: str = ""
    action: str = ""              # a command to copy, when there is one ("export ...")
    seconds: float = 0.0
    ran: bool = True              # False: nothing was run (Vivado is off, or the file is gone)

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def check(self) -> KitCheck:
        return KitCheck("vivado_launch", {"ok": "ok", "failed": "mismatch"}.get(
            self.state, "unchecked"), self.detail)

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "state": self.state, "detail": self.detail, "rc": self.rc,
                "release": self.release, "tier": self.tier, "no_licence": self.no_licence,
                "fix": self.fix, "action": self.action, "seconds": round(self.seconds, 1),
                "ran": self.ran}


def argv(exe: str, tcl: str | os.PathLike[str]) -> list[str]:
    """The launch: batch mode, no log, no journal, the two-line Tcl."""
    return [exe, "-mode", "batch", "-nolog", "-nojournal", "-notrace", "-source", str(tcl)]


def _release_of(exe: str) -> str:
    for parent in Path(exe).parents:
        if _ROOT_REL.match(parent.name):
            return parent.name
    return ""


def _last_line(text: str) -> str:
    return next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")


def _licence_env(env: dict[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    e = os.environ if env is None else env
    return tuple((k, e.get(k, "")) for k in LICENCE_ENV)


def _no_licence_fix(lic: tuple[tuple[str, str], ...]) -> tuple[str, str, str]:
    """(what is wrong, the fix, a command) for exit 42, by what the licence variables say."""
    set_ = [(k, v) for k, v in lic if v.strip()]
    if not set_:
        return ("no licence file (set XILINXD_LICENSE_FILE or LM_LICENSE_FILE)",
                "export XILINXD_LICENSE_FILE=PORT@SERVER (the lab's licence server; "
                "LM_LICENSE_FILE works too), then run the guide again",
                "export XILINXD_LICENSE_FILE=PORT@SERVER")
    said = ", ".join(f"{k}={v}" for k, v in set_)
    return (f"no valid licence through {said}",
            "check that the server answers from this machine and serves Vivado "
            "(Core or higher for 2026.1), or point XILINXD_LICENSE_FILE at one that does", "")


def _ask(exe: str, release: str, timeout: float) -> Launch:
    lic = _licence_env()
    scratch = tempfile.mkdtemp(prefix="hm-vivado-launch-")
    t0 = time.monotonic()
    try:
        tcl = Path(scratch) / TCL_NAME
        tcl.write_text(TCL, encoding="utf-8")
        rc, out = run_to_file(argv(exe, tcl), timeout, cwd=scratch)
    except OSError as exc:
        return Launch(exe, "failed", f"Vivado {release or '?'} did not start: {exe} does not "
                                     f"run ({exc.strerror or exc})", release=release,
                      fix="check the path (tools.vivado / HARNESS_MANAGER_VIVADO)",
                      seconds=time.monotonic() - t0)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    took = time.monotonic() - t0
    m = _BANNER_RE.search(out)
    rel = release or (m.group(1) if m else "") or _release_of(exe)
    shown = f"Vivado {rel}" if rel else f"Vivado ({exe})"
    t = _TIER_RE.search(out)
    tier = t.group(1).upper() if t else ""
    if rc is None:
        return Launch(exe, "unchecked",
                      f"{shown} did not finish starting within {timeout:g} s: unchecked (a "
                      "licence server that does not answer can do this)",
                      release=rel, fix="check that the licence server answers from this "
                                       "machine, then run the guide again", seconds=took)
    if rc == NO_LICENCE_EXIT or _NO_LICENCE_RE.search(out):
        what, fix, cmd = _no_licence_fix(lic)
        return Launch(exe, "failed", f"{shown} did not start: {what}; it exited {rc}", rc=rc,
                      release=rel, no_licence=True, fix=fix, action=cmd, seconds=took)
    if rc != 0:
        last = _last_line(out)
        return Launch(exe, "failed", f"{shown} did not start: it exited {rc}"
                      + (f" ({last[:200]})" if last else " and printed nothing"), rc=rc,
                      release=rel, tier=tier,
                      fix=f"run `{exe} -mode tcl` to see why", seconds=took)
    if MARKER not in out:
        return Launch(exe, "unchecked", f"{shown} exited 0 but did not run the probe's Tcl "
                                        "(a wrapper?): unchecked", rc=rc, release=rel,
                      tier=tier, seconds=took)
    return Launch(exe, "ok", f"{shown} starts" + (f" (licence tier: {tier})" if tier else ""),
                  rc=rc, release=rel, tier=tier, seconds=took)


# --- the cache ----------------------------------------------------------------------------------

_mu = threading.Lock()
_cache: dict[tuple[Any, ...], tuple[Launch, float]] = {}


def _key(exe: str) -> tuple[Any, ...] | None:
    try:
        real = os.path.realpath(exe)
        st = os.stat(real)
    except OSError:
        return None
    return (os.path.normcase(real), st.st_mtime_ns, st.st_size, _licence_env())


def _off() -> str:
    """The setting's value when it turns Vivado off (nothing may run), else ``""``."""
    from harness_manager.settings import runtime

    from .vivado import OFF

    v = str(runtime.resolved("tools.vivado").value or "").strip()
    return v if v.lower() in OFF else ""


def probe(exe: str | os.PathLike[str], *, release: str = "",
          timeout: float | None = None) -> Launch:
    """Launch ``exe`` once (the module docstring). Never raises. ``release``: what the caller
    already knows (``vivado -version``), for the message. Cached (see above)."""
    exe = os.fspath(exe)
    off = _off()
    if off:
        return Launch(exe, "unchecked", f"not started: Vivado is off (tools.vivado={off})",
                      release=release, ran=False)
    timeout = LAUNCH_TIMEOUT_S if timeout is None else timeout
    key = _key(exe)
    if key is None:
        return Launch(exe, "unchecked", f"not started: {exe} is not there", release=release,
                      ran=False)
    with _mu:
        hit, when = _cache.get(key, (None, 0.0))
    if hit is not None and (hit.ok or time.monotonic() - when < FAILURE_TTL_S):
        return hit
    got = _ask(exe, release, timeout)
    with _mu:
        _cache[key] = (got, time.monotonic())
    return got


def clear_cache() -> None:
    """Forget every launch (tests)."""
    with _mu:
        _cache.clear()
