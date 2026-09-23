"""Output formats and error reporting for every verb.

Three formats, one per invocation:

- ``--json``: exactly ONE JSON object on stdout. Success objects carry
  ``"ok": true``; a failure prints ``{"ok": false, "error": {...}}`` so a GUI
  never has to scrape stderr.
- ``--tsv``: tab-separated rows whose columns are listed in ``TSV_COLUMNS``.
  The column lists are APPEND-ONLY: never reorder or remove a column. An empty
  field is ``-``; a field never contains a tab or a newline.
- human: short lines for a terminal.

Errors always go to stderr as ``socharness: <message> — <next action>``. The
exit code is the error's ``ExitCode``, never an ad-hoc number.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TextIO

from socharness.core.errors import ExitCode, HarnessError, HeldError, UnavailableError
from socharness.core.model import Reading

# --- TSV columns (append-only; the help text is generated from this table) --------------

READING_COLUMNS = ("BOARD_ID", "NAME", "VALUE", "UNIT", "SOURCE", "AGE_S", "REASON")
DEBUG_COLUMNS = ("BOARD_ID", "STATE", "GDB", "TELNET", "TCL", "PID", "CONFIG", "DETAIL")

TSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "version": ("VERSION", "ENGINE"),
    "packs": ("PACK", "TITLE"),
    "probe": ("BOARD_ID", "PACK", "LABEL", "EVIDENCE", "LINKS"),
    "info": ("BOARD_ID", "BOARD_TYPE", "SHELL_ID", "RM_ID", "RM_NAME", "HARNESS",
             "BUILD_CHECK", "CONTROL"),
    "attach": ("BOARD_ID", "STATE", "HOLDER", "LOCK"),
    "detach": ("BOARD_ID", "STATE", "HOLDER"),
    "overlays": ("BOARD_ID", "NAME", "STATE", "RM_ID", "STATIC_ID", "SIZE", "IP_CLASS", "REASON"),
    "program": ("BOARD_ID", "OVERLAY", "RM_ID", "VERIFIED", "SECONDS", "TRANSPORT"),
    "restore": ("BOARD_ID", "RM_ID", "VERIFIED", "SECONDS", "TRANSPORT"),
    "console": ("NAME", "TEXT"),
    "console --export": ("BOARD_ID", "NAME", "HOST", "PORT"),
    "debug up|down|status": DEBUG_COLUMNS,
    "debug detect": ("BOARD_ID", "IDCODE"),
    "reset": ("BOARD_ID", "TARGET", "RESULT"),
    "clock": READING_COLUMNS,
    "lab link": ("BOARD_ID", "EVENT", "RESULT"),
    "lab display": ("BOARD_ID", "REQUESTED", "OWNER", "LANDED"),
    "lab macgen": ("BOARD_ID", "TX", "RX", "ERR"),
    "lab dutrx": ("BOARD_ID", "LEN", "FRAMES_WAITING", "RX", "DROP_FULL", "DROP_GIANT", "OVF",
                  "DESYNC", "DATA"),
    "mcc temp|osc": READING_COLUMNS,
    "mcc reboot": ("BOARD_ID", "RESULT", "PHASES"),
    "mcc cmd": ("BOARD_ID", "COMMAND", "REPLY"),
    "sd backup": ("BOARD_ID", "PATH", "SHA256", "FILES", "VOLUME"),
    "sd install": ("BOARD_ID", "FILES", "BACKUP_SHA256"),
    "sd restore": ("BOARD_ID", "BACKUP", "SHA256"),
    "telemetry": READING_COLUMNS,
    "help": ("TAB", "LINE", "TEXT"),
}


# --- JSON ---------------------------------------------------------------------------------


def jsonable(obj: Any) -> Any:
    """Dataclasses, enums, sets and tuples -> plain JSON types. Bytes become hex."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).hex()
    if isinstance(obj, (frozenset, set)):
        return sorted(jsonable(v) for v in obj)
    if isinstance(obj, (tuple, list)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "__fspath__"):
        return str(obj)
    return obj


def reading_json(r: Reading, now: float | None = None) -> dict[str, Any]:
    d = jsonable(r)
    d["available"] = r.available
    d["age_s"] = round((now or time.time()) - r.observed_at, 3)
    return d


# --- TSV ----------------------------------------------------------------------------------


def tsv_field(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ";".join(tsv_field(v) for v in value) or "-"
    text = str(value)
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ") or "-"


def tsv_line(layout: str, row: Sequence[Any]) -> str:
    cols = TSV_COLUMNS[layout]
    if len(row) != len(cols):   # a bug in the verb, never the user's fault
        raise AssertionError(f"tsv layout {layout!r} has {len(cols)} columns, row has {len(row)}")
    return "\t".join(tsv_field(v) for v in row)


def reading_row(board_id: str, r: Reading, now: float | None = None) -> list[Any]:
    age = round((now or time.time()) - r.observed_at, 1)
    return [board_id, r.name, r.value, r.unit, r.source, age, r.reason]


def reading_human(r: Reading) -> str:
    if r.available:
        extra = f"  ({r.reason})" if r.reason else ""
        return f"{r.name:<16} {r.value:g} {r.unit}  [{r.source or '?'}]{extra}"
    return f"{r.name:<16} unavailable: {r.reason or 'no reason given'}"


# --- results ------------------------------------------------------------------------------


@dataclass
class Result:
    layout: str                                   # key into TSV_COLUMNS
    data: dict[str, Any]                          # the JSON object (without "ok")
    rows: list[Sequence[Any]] = field(default_factory=list)
    human: list[str] = field(default_factory=list)


def emit(fmt: str, result: Result, out: TextIO | None = None) -> None:
    out = out or sys.stdout
    if fmt == "json":
        payload = {"ok": True, **jsonable(result.data)}
        out.write(json.dumps(payload, sort_keys=True) + "\n")
    elif fmt == "tsv":
        for row in result.rows:
            out.write(tsv_line(result.layout, row) + "\n")
    else:
        for line in result.human:
            out.write(line + "\n")
    out.flush()


# --- errors -------------------------------------------------------------------------------

DEFAULT_HINTS: dict[ExitCode, str] = {
    ExitCode.FAILED: "this is a bug in socharness; report it with the command line",
    ExitCode.USAGE: "run `socharness help` for the verbs and the TARGET forms",
    ExitCode.ABSENT: "check the spelling; `socharness probe` lists boards",
    ExitCode.HELD: "wait for the holder, or ask them to release it",
    ExitCode.PORT_BOUND: "pick another local port, or stop what is using it",
    ExitCode.ACTION_FAILED: "check `socharness info TARGET`, then retry",
    ExitCode.UNREACHABLE: "check the board is powered and the address is right",
    ExitCode.ALREADY: "nothing to do",
    ExitCode.NOTHING_ON_TARGET: "load a design that has the port, then retry",
    ExitCode.INCOMPATIBLE: "use an item built for this board's shell",
    ExitCode.REFUSED: "the safety rail is deliberate; read the message",
}


def with_data(exc: HarnessError, **data: Any) -> HarnessError:
    """Attach structured data to an error; ``--json`` prints it under ``error.data``."""
    existing = getattr(exc, "data", None) or {}
    exc.data = {**existing, **data}   # type: ignore[attr-defined]
    return exc


def error_line(exc: HarnessError) -> str:
    if isinstance(exc, UnavailableError):
        return f"socharness: {exc.capability} is unavailable — {exc.reason}"
    hint = exc.hint or DEFAULT_HINTS.get(exc.code, "")
    return f"socharness: {exc.message} — {hint}" if hint else f"socharness: {exc.message}"


def error_json(exc: HarnessError) -> dict[str, Any]:
    err: dict[str, Any] = {
        "code": int(exc.code),
        "name": ExitCode(exc.code).name,
        "message": exc.message,
        "hint": exc.hint or DEFAULT_HINTS.get(exc.code, ""),
    }
    if isinstance(exc, HeldError):
        err["holder"] = exc.holder
    if isinstance(exc, UnavailableError):
        err["capability"] = exc.capability
        err["reason"] = exc.reason
    data = getattr(exc, "data", None)
    if data:
        err["data"] = jsonable(data)
    return {"ok": False, "error": err}


def report_error(fmt: str, exc: HarnessError, *, out: TextIO | None = None,
                 err: TextIO | None = None) -> int:
    err = err or sys.stderr
    err.write(error_line(exc) + "\n")
    err.flush()
    if fmt == "json":
        out = out or sys.stdout
        out.write(json.dumps(error_json(exc), sort_keys=True) + "\n")
        out.flush()
    return int(exc.code)


# --- progress (always stderr: stdout carries only the result) ---------------------------


class StderrProgress:
    """A ``Progress`` callable: ``(phase, done, total)`` lines on stderr, throttled to
    phase changes and 10 % steps so a 12 MB write does not print 12 000 lines."""

    def __init__(self, label: str, err: TextIO | None = None) -> None:
        self.label = label
        self._err = err
        self._last: tuple[str, int] | None = None
        self.phases: list[str] = []

    def __call__(self, phase: str, done: int, total: int) -> None:
        if not self.phases or self.phases[-1] != phase:
            self.phases.append(phase)
        step = (100 * done // total // 10) if total else -1
        key = (phase, step)
        if key == self._last:
            return
        self._last = key
        if total:
            text = f"{self.label}: {phase} {done}/{total} ({100 * done // total}%)"
        elif done:
            text = f"{self.label}: {phase} {done}"
        else:
            text = f"{self.label}: {phase}"
        stream = self._err or sys.stderr
        stream.write(text + "\n")
        stream.flush()


def progress_line(err: TextIO | None, text: str) -> None:
    stream = err or sys.stderr
    stream.write(text + "\n")
    stream.flush()

