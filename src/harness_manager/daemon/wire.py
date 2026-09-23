"""What crosses the wire: the success/error shapes, HTTP statuses and event frames.

Everything here follows docs/API.md "Conventions":

- success is ``{"ok": true, ...}`` with objects serialised by
  ``harness_manager.cli.output.jsonable`` (so the CLI's ``--json`` and the API emit
  the same JSON for the same object);
- failure is the CLI's ``--json`` error object, ``cli.output.error_json``;
- the HTTP status follows the error's exit code (``HTTP_STATUS``);
- an event frame is ``{"topic", "board_id", "data", "at"}``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from harness_manager.cli.output import error_json, jsonable
from harness_manager.core.errors import ExitCode, HarnessError, RefusedError
from harness_manager.core.events import Event
from harness_manager.core.session import LockOwner

HTTP_STATUS: dict[ExitCode, int] = {
    ExitCode.USAGE: 400,
    ExitCode.ABSENT: 404,
    ExitCode.HELD: 409,
    ExitCode.ALREADY: 409,
    ExitCode.UNREACHABLE: 502,
    ExitCode.UNAVAILABLE: 422,
    ExitCode.NOTHING_ON_TARGET: 422,
    ExitCode.INCOMPATIBLE: 409,
    ExitCode.REFUSED: 409,
}

#: A missing or wrong token. docs/API.md fixes the status (401); the error object
#: carries REFUSED (15), the code for "refused by a policy".
UNAUTHORISED = 401


def http_status(exc: HarnessError) -> int:
    try:
        return HTTP_STATUS.get(ExitCode(exc.code), 500)
    except ValueError:
        return 500


def ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, **jsonable(data)}


def error_body(exc: HarnessError) -> dict[str, Any]:
    return error_json(exc)


def error_object(exc: HarnessError) -> dict[str, Any]:
    """The inside of the envelope: what a job's ``error`` and ``job.failed`` carry."""
    return error_json(exc)["error"]


def auth_error() -> HarnessError:
    return RefusedError("missing or wrong token",
                        hint="send `Authorization: Bearer <token>` (WebSockets: ?token=); "
                             "`harness-manager ui` opens the UI with the current token")


def owner_json(owner: LockOwner | None) -> dict[str, Any] | None:
    """A lock holder, as the CLI's ``attach``/``detach`` print it (plus ``text``)."""
    if owner is None:
        return None
    return {"user": owner.user, "host": owner.host, "pid": owner.pid, "since": owner.since,
            "note": owner.note, "text": owner.describe()}


# --- events -----------------------------------------------------------------------------------


def encode_event(ev: Event) -> str:
    return json.dumps({"topic": ev.topic, "board_id": ev.board_id,
                       "data": jsonable(ev.data), "at": ev.at},
                      sort_keys=True, default=str)


def topic_matches(pattern: str, topic: str) -> bool:
    """The EventBus rule: ``*`` is everything, ``prefix.*`` a prefix, else an exact topic."""
    if pattern == "*" or pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])
    return False


def parse_topics(spec: str | None) -> tuple[str, ...]:
    """``"board.*,deploy.*"`` -> ``("board.*", "deploy.*")``; empty means everything."""
    topics = tuple(t.strip() for t in (spec or "").split(",") if t.strip())
    return topics or ("*",)


def wants(patterns: Iterable[str], topic: str) -> bool:
    return any(topic_matches(p, topic) for p in patterns)
