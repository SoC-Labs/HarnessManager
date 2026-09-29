"""The service process's own lifetime (lane LEASE-FRESH, SSH-MUX).

Some resources are worth keeping only in the long-lived Harness Manager service, and must be
let go when it stops: a board pack's reused SSH connections to a hub (``harness_manager_mps3
.hub``: one ssh master per hub instead of a new connection, and a new key exchange, for every
``fpgahub`` call). A CLI verb is short-lived and has no stop to clean up after, so it keeps
one connection per call, as before.

- ``mark_service()``: ``run_daemon`` says this process IS the service (never a CLI verb, a
  test's in-process app, or the demo's self-test);
- ``is_service()``: whether it is;
- ``on_service_stop(name, fn)``: ``fn()`` runs when the service stops (once per ``name``);
- ``run_stop_hooks()``: ``run_daemon`` calls it after the boards are closed. Every hook runs,
  whatever the others do, and each runs once.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

__all__ = ["is_service", "mark_service", "on_service_stop", "run_stop_hooks"]

log = logging.getLogger(__name__)

_mu = threading.Lock()
_service = False
_hooks: dict[str, Callable[[], None]] = {}


def mark_service(on: bool = True) -> None:
    """This process is (``on``) or is no longer the Harness Manager service."""
    global _service
    with _mu:
        _service = bool(on)


def is_service() -> bool:
    with _mu:
        return _service


def on_service_stop(name: str, fn: Callable[[], None]) -> None:
    """Run ``fn()`` when the service stops. A second registration under ``name`` replaces
    the first (a module registers its hook once, whenever it first needs it)."""
    with _mu:
        _hooks[name] = fn


def run_stop_hooks() -> list[str]:
    """Run every registered hook once (a failing hook is logged; the rest still run).
    Returns the names that ran."""
    with _mu:
        hooks = list(_hooks.items())
        _hooks.clear()
    ran: list[str] = []
    for name, fn in hooks:
        try:
            fn()
        except Exception:  # noqa: BLE001 - stopping must finish
            log.exception("service stop hook %s failed", name)
        ran.append(name)
    return ran
