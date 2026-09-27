"""The inverse of ``harness_manager.cli.output.jsonable``: JSON back into core objects and errors.

The daemon serialises with the CLI's rules (enums become values, frozensets
sorted lists, tuples lists). ``from_json(cls, data)`` rebuilds the dataclass
from its field types, so a ``BoardInfo`` that crossed the API compares equal
to the one the engine returned. Unknown keys are ignored (the API may add
fields; ``reading_json`` adds ``available``/``age_s``), and a missing key takes
the field's default.

``error_from_json`` rebuilds the ``HarnessError`` an error envelope describes,
with the same exit code, message, hint, holder, capability/reason and data, so
the CLI prints the same error whether the engine ran in-process or in the
daemon.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any, TypeVar, Union

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    AlreadyError,
    ExitCode,
    HarnessError,
    HeldError,
    IncompatibleError,
    NothingOnTargetError,
    PortBoundError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)

T = TypeVar("T")

_NONE = type(None)


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def from_json(tp: Any, value: Any) -> Any:
    """Rebuild a value of type ``tp`` from JSON. ``UsageError`` if it cannot be done."""
    try:
        return _decode(tp, value)
    except UsageError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        name = getattr(tp, "__name__", str(tp))
        raise UsageError(f"not a valid {name}: {exc}",
                         hint="send the object the API returned, unchanged") from exc


def _decode(tp: Any, value: Any) -> Any:
    if tp is Any or tp is object:
        return value
    origin = typing.get_origin(tp)
    if origin in (Union, types.UnionType):
        args = typing.get_args(tp)
        if value is None and _NONE in args:
            return None
        errors: list[Exception] = []
        for arg in args:
            if arg is _NONE:
                continue
            try:
                return _decode(arg, value)
            except (TypeError, ValueError, KeyError, AttributeError, UsageError) as exc:
                errors.append(exc)
        raise ValueError(f"{value!r} matches none of {args}: {errors}")
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        if isinstance(value, tp):
            return value
        if not isinstance(value, dict):
            raise TypeError(f"{tp.__name__} needs an object, got {type(value).__name__}")
        hints = _hints(tp)
        kwargs = {f.name: _decode(hints[f.name], value[f.name])
                  for f in dataclasses.fields(tp) if f.init and f.name in value}
        return tp(**kwargs)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if origin is tuple:
        args = typing.get_args(tp)
        _require_list(value, tp)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], v) for v in value)
        if not args:
            return tuple(value)
        if len(args) != len(value):
            raise ValueError(f"expected {len(args)} items, got {len(value)}")
        return tuple(_decode(a, v) for a, v in zip(args, value, strict=True))
    if origin in (frozenset, set, list):
        (arg,) = typing.get_args(tp) or (Any,)
        _require_list(value, tp)
        items = [_decode(arg, v) for v in value]
        return origin(items)
    if origin is dict:
        key_t, val_t = typing.get_args(tp) or (Any, Any)
        if not isinstance(value, dict):
            raise TypeError(f"expected an object, got {type(value).__name__}")
        return {_decode(key_t, k): _decode(val_t, v) for k, v in value.items()}
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"expected a number, got {value!r}")
        return float(value)
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"expected an integer, got {value!r}")
        return value
    if tp is bool:
        if not isinstance(value, bool):
            raise TypeError(f"expected true or false, got {value!r}")
        return value
    if tp is str:
        if not isinstance(value, str):
            raise TypeError(f"expected a string, got {value!r}")
        return value
    if tp is Path:
        return Path(value)
    if tp in (dict, list, tuple, frozenset, set):
        return tp(value)
    return value


def _require_list(value: Any, tp: Any) -> None:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"expected a list for {tp}, got {type(value).__name__}")


# --- errors ------------------------------------------------------------------------------------

_BY_CODE: dict[int, type[HarnessError]] = {
    ExitCode.USAGE: UsageError,
    ExitCode.ABSENT: AbsentError,
    ExitCode.HELD: HeldError,
    ExitCode.PORT_BOUND: PortBoundError,
    ExitCode.ACTION_FAILED: ActionFailedError,
    ExitCode.UNREACHABLE: UnreachableError,
    ExitCode.ALREADY: AlreadyError,
    ExitCode.UNAVAILABLE: UnavailableError,
    ExitCode.NOTHING_ON_TARGET: NothingOnTargetError,
    ExitCode.INCOMPATIBLE: IncompatibleError,
    ExitCode.REFUSED: RefusedError,
}


def error_from_json(err: dict[str, Any]) -> HarnessError:
    """The ``HarnessError`` an API error object describes (the inside of ``{"ok": false, "error"}``)."""
    try:
        code = ExitCode(int(err.get("code", ExitCode.FAILED)))
    except (TypeError, ValueError):
        code = ExitCode.FAILED
    message = str(err.get("message") or "the daemon reported an error without a message")
    hint = str(err.get("hint") or "")
    cls = _BY_CODE.get(code, HarnessError)
    exc: HarnessError
    if cls is HeldError:
        exc = HeldError(message, holder=str(err.get("holder") or ""), hint=hint)
    elif cls is UnavailableError:
        exc = UnavailableError(str(err.get("capability") or "?"),
                               str(err.get("reason") or message), hint=hint)
    else:
        exc = cls(message, hint=hint)
        if cls is HarnessError:
            exc.code = code
    data = err.get("data")
    if data:
        exc.data = data          # type: ignore[attr-defined]  (cli.output.error_json prints it)
    return exc
