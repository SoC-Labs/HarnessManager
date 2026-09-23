"""Error taxonomy and the CLI exit-code table.

Every failure the engine can report maps to exactly one exit code. The codes
follow the HAPS helper contract (``HAPS-work/fpga/haps-sx/openocd/README.md``),
so scripts written against those helpers read the same numbers here. Codes are
append-only: never renumber one.

Rule: an error states what went wrong and what to do next. Never apologise,
and never report "unknown" where a specific reason is available.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    FAILED = 1            # unexpected internal failure (a bug); message says so
    USAGE = 2             # bad arguments / unknown target spelling
    ABSENT = 3            # the thing asked for does not exist (no board, no RM, no port)
    HELD = 4              # someone else holds it (lease, session lock, single-client port)
    PORT_BOUND = 5        # a local port we need is already in use
    ACTION_FAILED = 6     # the board refused or the action did not complete
    UNREACHABLE = 7       # transport failure: no route, timeout, connection refused
    ALREADY = 8           # already in the requested state (e.g. session already up)
    UNAVAILABLE = 12      # capability not available on this board/link set ("needs Debug USB")
    NOTHING_ON_TARGET = 13  # link fine, but nothing answered on the far side (e.g. no DAP)
    INCOMPATIBLE = 14     # identity mismatch (static_id, usercode, protocol version)
    REFUSED = 15          # refused by a safety policy (no SD backup, destructive MCC command)


class HarnessError(Exception):
    """Base class. ``code`` selects the exit code; ``hint`` is the next action."""

    code: ExitCode = ExitCode.FAILED

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} — {self.hint}" if self.hint else self.message


class UsageError(HarnessError):
    code = ExitCode.USAGE


class AbsentError(HarnessError):
    code = ExitCode.ABSENT


class HeldError(HarnessError):
    code = ExitCode.HELD

    def __init__(self, message: str, *, holder: str = "", hint: str = "") -> None:
        super().__init__(message, hint=hint)
        self.holder = holder


class PortBoundError(HarnessError):
    code = ExitCode.PORT_BOUND


class ActionFailedError(HarnessError):
    code = ExitCode.ACTION_FAILED


class UnreachableError(HarnessError):
    code = ExitCode.UNREACHABLE


class AlreadyError(HarnessError):
    code = ExitCode.ALREADY


class UnavailableError(HarnessError):
    """A capability the current links or firmware cannot provide.

    ``capability`` names it; ``reason`` is the exact missing prerequisite,
    e.g. "needs the Debug USB cable" or "needs harness firmware with 'stats'".
    """

    code = ExitCode.UNAVAILABLE

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__(f"{capability} is unavailable: {reason}")
        self.capability = capability
        self.reason = reason


class NothingOnTargetError(HarnessError):
    code = ExitCode.NOTHING_ON_TARGET


class IncompatibleError(HarnessError):
    code = ExitCode.INCOMPATIBLE


class RefusedError(HarnessError):
    code = ExitCode.REFUSED
