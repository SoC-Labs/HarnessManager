"""A stand-in for a service that is not installed in this build.

The engine resolves the deploy, console and debug services lazily by module
path (``harness_manager.services.deploy`` and so on). When a module is missing, or
fails to load, the engine uses this stub instead. Every method call raises
``UnavailableError`` (exit code 12) with the reason, so a front-end reports
"deploy is unavailable: not installed in this build" instead of crashing.
"""

from __future__ import annotations

from typing import Any, NoReturn

from harness_manager.core.errors import UnavailableError


class UnavailableService:
    """Answers every public method with ``UnavailableError(service, reason)``."""

    def __init__(self, service: str, reason: str = "not installed in this build") -> None:
        self.service = service
        self.reason = reason

    def __getattr__(self, name: str) -> Any:
        # Private and dunder lookups (copy, pickle, repr helpers) must behave normally.
        if name.startswith("_"):
            raise AttributeError(name)

        def _unavailable(*args: Any, **kwargs: Any) -> NoReturn:
            raise UnavailableError(self.service, self.reason)

        _unavailable.__name__ = name
        return _unavailable

    def __repr__(self) -> str:
        return f"UnavailableService({self.service!r}, {self.reason!r})"


def is_unavailable(service: object) -> bool:
    """True when ``service`` is a stub, i.e. calling it can only raise."""
    return isinstance(service, UnavailableService)
