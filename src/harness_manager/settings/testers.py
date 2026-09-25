"""Section testers: ``harness-manager config test SECTION`` and ``POST /settings/test`` (SET-API).

A tester proves that a section's settings work, and changes nothing while it does: the
hub tester (lane SET-HUBS; ``docs/design/SETTINGS.md`` §6.2) walks config, reach, auth,
group, targets and target, and never takes, joins or releases a lease. Sections without
a tester answer "not testable yet" (``testable: false``), never an error.

**Finding a tester.** Two ways, so a lane plugs in without editing this file:

- ``register("hubs", fn, job=True)`` from the lane's own module; or
- by convention (``CONVENTION``): the module and function named there, imported when a test
  is asked for. SET-HUBS provides ``harness_manager.settings.hubs.test_connection``.

**The contract.** ``fn(req: TestRequest) -> Mapping`` with at least ``ok`` (bool) and
``steps``: ``[{step, ok, detail, hint}]`` in the order they ran, stopping at the first
failure. Any other keys (``targets``, ``transport``, ...) pass through to the reply. A
tester must never put a secret in its report: the reply, the job result and the log carry
it as it is. ``req.progress(step, done, total)`` reports progress (a job forwards it).

``job=True`` marks a tester that can take seconds (an ssh round trip): the service then
runs it as an engine-wide job (202 ``{job}``) instead of inline.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core.errors import UsageError

Progress = Callable[[str, int, int], None]


def _no_progress(_step: str, _done: int, _total: int) -> None:
    return None


@dataclass(frozen=True)
class TestRequest:
    """What a tester gets."""

    __test__ = False                                 # not a pytest class

    section: str                                     # the section's id: "hubs"
    name: str = ""                                   # the instance: a hub's name ("" = none)
    table: Mapping[str, Any] | None = None           # an unsaved table, to test before saving
    resolver: Any = None                             # settings.Resolver over the real files
    progress: Progress = field(default=_no_progress, repr=False, compare=False)


@dataclass(frozen=True)
class Tester:
    section: str
    run: Callable[[TestRequest], Mapping[str, Any]] = field(repr=False)
    job: bool = False                                # slow: run it as a job in the service
    needs_name: bool = False                         # the request must name an instance


#: A tester that another lane provides, found by module convention: section -> (module,
#: function, runs as a job, needs a name).
CONVENTION: dict[str, tuple[str, str, bool, bool]] = {
    "hubs": ("harness_manager.settings.hubs", "test_connection", True, True),
}

_REGISTRY: dict[str, Tester] = {}


def section_id(name: str) -> str:
    """A section's name as an id: ``"Harness + kits"`` -> ``"harness-kits"``."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def register(section: str, fn: Callable[[TestRequest], Mapping[str, Any]], *,
             job: bool = False, needs_name: bool = False) -> Tester:
    """Add (or replace) the tester for ``section`` (its name or id)."""
    t = Tester(section_id(section), fn, job=job, needs_name=needs_name)
    _REGISTRY[t.section] = t
    return t


def unregister(section: str) -> None:
    _REGISTRY.pop(section_id(section), None)


def tester_for(section: str) -> Tester | None:
    """The section's tester, or None: none registered, and the convention's module or
    function is not there (its lane has not landed)."""
    sid = section_id(section)
    if sid in _REGISTRY:
        return _REGISTRY[sid]
    conv = CONVENTION.get(sid)
    if conv is None:
        return None
    module, attr, job, needs_name = conv
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == module:
            return None
        raise
    fn = getattr(mod, attr, None)
    return Tester(sid, fn, job=job, needs_name=needs_name) if callable(fn) else None


def not_testable(section: str, name: str = "") -> dict[str, Any]:
    return {"section": section_id(section), "name": name, "testable": False, "passed": None,
            "steps": [], "why": f"the {section_id(section)} settings are not testable yet"}


def check(t: Tester, name: str, table: Mapping[str, Any] | None) -> None:
    """``UsageError`` when the request lacks the name the tester needs."""
    if t.needs_name and not name and table is None:
        raise UsageError(f"say which one to test: `harness-manager config test {t.section} NAME`")


def run(t: Tester, req: TestRequest) -> dict[str, Any]:
    """Run a tester and give the reply's shape: ``{section, name, testable, passed, steps,
    why, ...}``. ``UsageError`` when the request lacks a name the tester needs."""
    check(t, req.name, req.table)
    report = dict(t.run(req) or {})
    steps = [dict(s) for s in report.pop("steps", []) or []]
    passed = bool(report.pop("ok", all(s.get("ok") for s in steps) and bool(steps)))
    failed = next((s for s in steps if not s.get("ok")), None)
    why = report.pop("why", "") or ("" if passed else
                                     (f"{failed.get('step', '?')}: {failed.get('detail', '')}"
                                      if failed else "the test did not pass"))
    return {**report, "section": t.section, "name": req.name, "testable": True,
            "passed": passed, "steps": steps, "why": why}
