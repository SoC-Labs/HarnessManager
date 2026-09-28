"""How the CLI gets an ``Engine``.

Order of precedence:

1. a factory installed with ``set_engine_factory`` (the test hook);
2. ``$HARNESS_MANAGER_CLI_ENGINE=package.module:callable``: a factory that takes the
   parsed args (or ``None``) and returns an engine;
3. a running ``harness-manager-daemon`` for this state dir (``harness_manager.client.RemoteEngine``),
   so the CLI shares the daemon's engine and board sessions with the web UI;
4. ``harness_manager.engine.Engine`` (Team T1), the in-process one.

Step 3 is skipped, and the verb runs on the in-process engine, when:

- ``$HARNESS_MANAGER_NO_DAEMON`` is set (to anything but ``0``);
- the verb is one of ``IN_PROCESS_VERBS``: ``attach``/``detach`` are about THIS
  process holding the board's lock; ``daemon``/``ui`` manage the daemon itself. Their
  read-only sub-verbs in ``SERVICE_READS`` (``slot status``, ``card status``) still go
  through the service: it holds the board, so an in-process read is refused by its lock;
- the verb was given ``--overlay-dir``: that sets the overlay search path in
  this process's environment, which the daemon cannot see.

A daemon that is recorded but does not answer ``/api/v1/health`` within a
second is not used. The CLI only ever uses the frozen
``harness_manager.core.services.Engine`` protocol, so every verb behaves the same
over any of them.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import UsageError
from harness_manager.core.services import EngineConfig

log = logging.getLogger(__name__)

ENV_ENGINE = "HARNESS_MANAGER_CLI_ENGINE"
ENV_NO_DAEMON = "HARNESS_MANAGER_NO_DAEMON"

#: Verbs that always run on the in-process engine, and why.
IN_PROCESS_VERBS: dict[str, str] = {
    "attach": "holds the board's lock in this process",
    "detach": "signals the process that holds the lock",
    "daemon": "manages harness-manager-daemon itself",
    "update": "writes the SD and reboots in this process; a daemon holding the board refuses it by name",
    "ui": "manages harness-manager-daemon itself",
    "slot": "writes the board's OS slots through the pack's own adapter in this process",
    "card": "writes the board's user microSD through the pack's own adapter in this process",
    "app": "manages harness-manager-daemon itself",
}

#: Read-only sub-verbs of an ``IN_PROCESS_VERBS`` verb that go through a running service like
#: every other read (SERIAL-6900 4a): verb -> (the sub-verb's dest, the sub-verbs).
SERVICE_READS: dict[str, tuple[str, frozenset[str]]] = {
    "slot": ("slot_cmd", frozenset({"status"})),
    "card": ("card_cmd", frozenset({"status"})),
}

EngineFactory = Callable[[argparse.Namespace | None], Any]

_factory: EngineFactory | None = None


def set_engine_factory(factory: EngineFactory | None) -> EngineFactory | None:
    """Install (or clear, with ``None``) the engine factory. Returns the previous one."""
    global _factory
    previous, _factory = _factory, factory
    return previous


def _load_factory(spec: str) -> EngineFactory:
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise UsageError(f"{ENV_ENGINE}={spec!r} is not 'package.module:callable'",
                         hint=f"unset {ENV_ENGINE} to use the installed engine")
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as exc:
        raise UsageError(f"{ENV_ENGINE}={spec!r} cannot be loaded: {exc}",
                         hint=f"unset {ENV_ENGINE} to use the installed engine") from exc


def daemon_disabled() -> bool:
    return os.environ.get(ENV_NO_DAEMON, "").strip() not in ("", "0")


def wants_daemon(args: argparse.Namespace | None) -> bool:
    """Whether this invocation may use a running daemon (see the module docstring)."""
    if daemon_disabled():
        return False
    if args is not None:
        cmd = getattr(args, "cmd", None)
        if cmd in IN_PROCESS_VERBS and not _service_read(cmd, args):
            return False
        if getattr(args, "overlay_dir", None):
            return False
    return True


def _service_read(cmd: str, args: argparse.Namespace) -> bool:
    dest, subs = SERVICE_READS.get(cmd, ("", frozenset()))
    return bool(dest) and getattr(args, dest, None) in subs


def daemon_engine(args: argparse.Namespace | None = None) -> Any | None:
    """A ``RemoteEngine`` for the running daemon, or ``None``."""
    if not wants_daemon(args):
        return None
    try:
        from harness_manager.client import RemoteEngine

        return RemoteEngine.discover()
    except Exception:  # noqa: BLE001 - no daemon, or a broken one: use the in-process engine
        log.debug("harness-manager-daemon discovery failed", exc_info=True)
        return None


def get_engine(args: argparse.Namespace | None = None) -> Any:
    if _factory is not None:
        return _factory(args)
    spec = os.environ.get(ENV_ENGINE, "").strip()
    if spec:
        return _load_factory(spec)(args)
    remote = daemon_engine(args)
    if remote is not None:
        return remote
    from harness_manager.engine import Engine

    return Engine(EngineConfig())


def describe_engine() -> str:
    """Which engine ``get_engine`` would build, without building it (no network)."""
    if _factory is not None:
        return "test factory"
    spec = os.environ.get(ENV_ENGINE, "").strip()
    if spec:
        return f"{spec} (from ${ENV_ENGINE})"
    if not daemon_disabled():
        try:
            from harness_manager.daemon.state import discover

            info = discover()
        except Exception:  # noqa: BLE001
            info = None
        if info is not None:
            return f"harness-manager-daemon at {info.base_url} (pid {info.pid})"
    return "harness_manager.engine.Engine"
