"""``POST /boards/{bid}/lab/{verb}``: the CLI's lab verbs, run by the CLI's own code.

The lab verbs (``link``, ``display``, ``macgen``, ``dutrx``) have no
board-agnostic service yet: they drive pyverify through the session's shell
handle (``cli/cmd_lab.py``). Rather than re-implement them, the daemon runs
``cmd_lab.cmd_lab`` itself, with

- an ``argparse.Namespace`` built from the request body (validated here the way
  argparse validates the CLI's flags), and
- an engine shim that hands the verb the daemon's ALREADY OPEN session instead
  of opening (and afterwards closing) the board,

and captures the ``Result`` the verb emits. So the API returns exactly the
object ``socharness --json lab TARGET <verb>`` prints (without ``ok``), and a
refusal is the same error with the same exit code.
"""

from __future__ import annotations

import argparse
import io
from typing import Any

from socharness.cli.output import Result, jsonable
from socharness.core.errors import UsageError
from socharness.core.pack import BoardSession

LAB_VERBS = ("link", "display", "macgen", "dutrx")
LINK_EVENTS = ("up", "down", "pulse")
DISPLAY_OWNERS = ("harness", "dut", "toggle", "query")


class _OpenSession:
    """The subset of the engine a CLI verb uses, answered from one open session."""

    def __init__(self, engine: Any, session: BoardSession) -> None:
        self._engine = engine
        self._session = session

    def candidate_for(self, target: str, pack: str = "mps3") -> Any:
        return self._session.candidate

    def open(self, candidate: Any, *, note: str = "") -> BoardSession:
        return self._session

    def close(self, board_id: str) -> None:
        """The daemon owns the session: a verb never closes it."""

    def info(self, board_id: str) -> Any:
        return self._engine.info(board_id)

    def packs(self) -> dict[str, Any]:
        return self._engine.packs()

    def lock_owner(self, board_id: str) -> Any:
        return self._engine.lock_owner(board_id)


def _choice(body: dict[str, Any], key: str, choices: tuple[str, ...],
            default: str | None = None) -> str:
    value = body.get(key, default)
    if value is None:
        raise UsageError(f"lab needs {key!r}", hint=f"{key}: one of {', '.join(choices)}")
    if value not in choices:
        raise UsageError(f"{key} {value!r} is not one of {', '.join(choices)}",
                         hint=f"{key}: one of {', '.join(choices)}")
    return value


def _bool(body: dict[str, Any], key: str, default: bool) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise UsageError(f"{key} must be true or false, not {value!r}")
    return value


def _number(body: dict[str, Any], key: str, default: float) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise UsageError(f"{key} must be a positive number, not {value!r}")
    return float(value)


def _int(body: dict[str, Any], key: str, default: int) -> int:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageError(f"{key} must be an integer, not {value!r}")
    return value


def lab_args(board_id: str, pack: str, verb: str, body: dict[str, Any]) -> argparse.Namespace:
    """The namespace the CLI's argparse would build for ``lab TARGET <verb> ...``."""
    from socharness.cli.cmd_lab import DISPLAY_TIMEOUT_S

    if verb not in LAB_VERBS:
        raise UsageError(f"no lab verb named {verb!r}", hint=f"lab verbs: {', '.join(LAB_VERBS)}")
    args = argparse.Namespace(cmd="lab", lab_cmd=verb, target=board_id, pack=pack,
                              json=True, tsv=False)
    if verb == "link":
        args.event = _choice(body, "event", LINK_EVENTS)
    elif verb == "display":
        args.owner = _choice(body, "owner", DISPLAY_OWNERS, "query")
        args.timeout = _number(body, "timeout", DISPLAY_TIMEOUT_S)
    elif verb == "macgen":
        args.gen = _bool(body, "gen", True)
        args.chk = _bool(body, "chk", True)
        inject = body.get("inject", "none")
        if not isinstance(inject, str) or not inject:
            raise UsageError(f"inject must be a fault name, not {inject!r}")
        args.inject = inject
    else:
        args.frames = _int(body, "frames", 1)
    return args


def run_lab(engine: Any, session: BoardSession, verb: str, body: dict[str, Any]) -> dict[str, Any]:
    """Run one lab verb on an open session; return the verb's JSON object (no ``ok``)."""
    from socharness.cli import cmd_lab
    from socharness.cli.context import Ctx

    board_id = session.candidate.board_id
    args = lab_args(board_id, session.candidate.pack, verb, body)
    captured: list[Result] = []
    ctx = Ctx(args, _OpenSession(engine, session), "json", err=io.StringIO())
    ctx.emit = captured.append          # type: ignore[method-assign]
    cmd_lab.cmd_lab(ctx)
    if not captured:                    # every lab verb emits exactly one result
        raise UsageError(f"lab {verb} produced no result")
    return jsonable(captured[-1].data)
