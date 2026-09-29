"""PYVERIFY-VENDOR: Harness Manager never leans on a pyverify default for a slot or commit budget.

pyverify's defaults move with the platform. Platform 6e6a2a9 (vendored at 3f7cea2) raised
``slot push``'s ``--push-timeout`` 30 -> 600 s and ``--timeout`` 180 -> 3600 s, and the library
keeps its own (``push_slot_image`` 30 s, ``slot_request`` 5 s, ``wait_job`` 180 s,
``BitstreamPusher`` 2 s, ``ShellClient`` 5 s). HM's budgets are its own: the ``mps3.slot.*``
rows (``os_slots.slot_timeouts``), ``card.commit_budget``, the deploy's ``push_timeout_s`` and
the shell's control timeout. So every call HM's source makes to a pyverify callable of the
slot / commit path that takes a timeout passes EVERY timeout parameter itself: a re-vendor
that moves a default moves nothing in HM.

The scan reads HM's source (AST). A call is pyverify's when its callee is a name imported
from a guarded module (``from pyverify.pusher import tftp_put``), an attribute of a guarded
module's alias (``pv_slot.push_slot_image``), a class HM derives from a guarded class
(``_ReportingPusher(BitstreamPusher)``), or an attribute with one of the guarded function names
(a module handed in as a seam). Its negative twin: the same scan fails a call that leaves a
timeout out, or hides its arguments in ``*args``/``**kwargs``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

#: The slot / commit path's pyverify callables that take a timeout.
GUARDED: dict[str, tuple[str, ...]] = {
    "pyverify.slot": ("push_slot_image", "slot_request", "slot_status", "wait_job", "SshTunnel"),
    "pyverify.pusher": ("BitstreamPusher", "tcp_send", "tcp_send_windowed", "tftp_put"),
    # the control connection a commit's reply (and every slot request) is read on
    "pyverify.client": ("ShellClient", "SocketTransport"),
}
#: Names distinctive enough to count as pyverify's on any object (HM has its own SshTunnel).
BY_ATTR = {"push_slot_image": "pyverify.slot", "slot_request": "pyverify.slot",
           "tcp_send": "pyverify.pusher", "tcp_send_windowed": "pyverify.pusher",
           "tftp_put": "pyverify.pusher"}


def _params(module: str, name: str) -> list[str]:
    obj = getattr(importlib.import_module(module), name)
    return list(inspect.signature(obj).parameters)


def _timeouts(module: str, name: str) -> tuple[str, ...]:
    """The callable's timeout parameters that have a default (the ones HM could lean on)."""
    obj = getattr(importlib.import_module(module), name)
    return tuple(p.name for p in inspect.signature(obj).parameters.values()
                 if "timeout" in p.name and p.default is not inspect.Parameter.empty)


def _resolver(tree: ast.AST):
    """``callee node -> (module, name) | None`` for one file's imports and classes."""
    names: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                full = f"{node.module}.{a.name}"
                if full in GUARDED:
                    modules[a.asname or a.name] = full
                elif a.name in GUARDED.get(node.module, ()):
                    names[a.asname or a.name] = (node.module, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in GUARDED:
                    modules[a.asname or a.name] = a.name

    def resolve(func: ast.AST) -> tuple[str, str] | None:
        if isinstance(func, ast.Name):
            return names.get(func.id)
        if isinstance(func, ast.Attribute):
            owner = ast.unparse(func.value)
            module = modules.get(owner) or (owner if owner in GUARDED else None)
            if module and func.attr in GUARDED[module]:
                return module, func.attr
            if func.attr in BY_ATTR:
                return BY_ATTR[func.attr], func.attr
        return None

    for node in ast.walk(tree):                       # a class HM derives from a guarded one
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                target = resolve(base)
                if target:
                    names[node.name] = target
    return resolve


def scan(source: str, where: str = "<src>") -> tuple[list[tuple[str, str]], list[str]]:
    """``(calls found as (module, name), findings)`` for one file's source."""
    tree = ast.parse(source)
    resolve = _resolver(tree)
    found: list[tuple[str, str]] = []
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = resolve(node.func)
        if target is None:
            continue
        found.append(target)
        at = f"{where}:{node.lineno} {ast.unparse(node.func)}(...)"
        if any(isinstance(a, ast.Starred) for a in node.args) or any(
                k.arg is None for k in node.keywords):
            findings.append(f"{at}: *args/**kwargs hide whether the timeout is passed")
            continue
        given = {k.arg for k in node.keywords} | set(_params(*target)[:len(node.args)])
        missing = [p for p in _timeouts(*target) if p not in given]
        if missing:
            findings.append(f"{at}: leans on pyverify's default for {', '.join(missing)} "
                            f"({target[0]}.{target[1]})")
    return found, findings


def test_the_guarded_callables_still_take_their_timeouts():
    # a re-vendor that renames one (or drops its timeout) must update GUARDED, not pass quietly
    for module, names in GUARDED.items():
        for name in names:
            assert _timeouts(module, name), f"{module}.{name} has no timeout with a default"


def test_hm_passes_its_own_budget_to_every_slot_and_commit_call():
    found: Counter[tuple[str, str]] = Counter()
    findings: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        calls, bad = scan(path.read_text(encoding="utf-8"), str(path.relative_to(SRC)))
        found.update(calls)
        findings += bad
    assert findings == []
    # the scan sees the calls HM makes today (never passes vacuously)
    assert found[("pyverify.slot", "push_slot_image")] >= 2
    assert found[("pyverify.slot", "slot_request")] >= 1
    assert found[("pyverify.pusher", "BitstreamPusher")] >= 4        # _ReportingPusher(...)
    assert found[("pyverify.pusher", "tftp_put")] >= 1
    assert found[("pyverify.client", "ShellClient")] >= 2
    assert found[("pyverify.client", "SocketTransport")] >= 1


def test_twin_a_call_that_leaves_a_timeout_to_pyverify_is_caught():
    bad = '''
from pyverify import slot as pv_slot
from pyverify.pusher import BitstreamPusher
import pyverify.client as pvc


class Pusher(BitstreamPusher):
    pass


def push(data, host, seam, kw):
    pv_slot.push_slot_image(data, host, static_id=1, slot="B")
    Pusher(host=host, transport="tcp")
    seam.tftp_put(data, host, 69, filename="k")
    pvc.SocketTransport(host, 6900)
    pv_slot.slot_request(host, "commit", None, **kw)
    pv_slot.slot_request(host, "commit", None, port=6900, timeout=5.0)    # fine
    pvc.SocketTransport(host, 6900, 5.0)                                  # fine: positional
'''
    found, findings = scan(bad)
    assert len(found) == 7
    assert [f.split(" ", 1)[1].split("(")[0] for f in findings] == [
        "pv_slot.push_slot_image", "Pusher", "seam.tftp_put", "pvc.SocketTransport",
        "pv_slot.slot_request"]
    assert "timeout_s" in findings[0] and "**kwargs" in findings[4]
