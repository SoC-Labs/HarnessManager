"""How the CLI gets an ``Engine``.

Order of precedence:

1. a factory installed with ``set_engine_factory`` (the test hook);
2. ``$SOCHARNESS_CLI_ENGINE=package.module:callable``: a factory that takes the
   parsed args (or ``None``) and returns an engine;
3. ``socharness.engine.Engine`` (Team T1), the real one.

The CLI only ever uses the frozen ``socharness.core.services.Engine`` protocol,
so every verb behaves the same over any of them.
"""

from __future__ import annotations

import argparse
import importlib
import os
from collections.abc import Callable
from typing import Any

from socharness.core.errors import UsageError
from socharness.core.services import EngineConfig

ENV_ENGINE = "SOCHARNESS_CLI_ENGINE"

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


def get_engine(args: argparse.Namespace | None = None) -> Any:
    if _factory is not None:
        return _factory(args)
    spec = os.environ.get(ENV_ENGINE, "").strip()
    if spec:
        return _load_factory(spec)(args)
    from socharness.engine import Engine

    return Engine(EngineConfig())


def describe_engine() -> str:
    """Which engine ``get_engine`` would build, without building it."""
    if _factory is not None:
        return "test factory"
    spec = os.environ.get(ENV_ENGINE, "").strip()
    if spec:
        return f"{spec} (from ${ENV_ENGINE})"
    return "socharness.engine.Engine"
