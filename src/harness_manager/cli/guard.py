"""The reset guard's CLI side (lane SLOT-TIMING; ``services.reset_guard``).

``mcc reboot``, ``mcc cmd REBOOT`` and ``power cycle`` refuse while the board's card job is
writing or verifying (exit 4, the job named). ``--force`` resets anyway, for the one case it
exists for: a card job that never ends, whose recovery IS a power cycle. It needs the typed
phrase ``RESET <board_id>`` (``--consent PHRASE``, or typed at the prompt); ``--yes`` never
implies it.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from harness_manager.core.errors import RefusedError
from harness_manager.services import reset_guard

from .context import Ctx

is_reboot_line = reset_guard.is_reboot_line


def add_force_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true",
                        help="reset even while the board's card job writes or reads back "
                             "(only to recover a job that never ends; it can wedge the card). "
                             "Needs the typed phrase RESET <board_id>; --yes never implies it")
    parser.add_argument("--consent", default="", metavar="PHRASE",
                        help="with --force: the phrase, typed here instead of at the prompt")


def guard_consent(ctx: Ctx, session: Any, action: str) -> tuple[bool, str]:
    """``(force, consent)`` for ``reset_guard.guarded``. Without ``--force``: ``(False, "")``.
    With it: the typed phrase, asked for loudly, or ``RefusedError`` (exit 15)."""
    a = ctx.args
    if not getattr(a, "force", False):
        return False, ""
    bid = session.candidate.board_id
    phrase = reset_guard.force_phrase(bid)
    given = str(getattr(a, "consent", "") or "")
    if not given:
        stream = ctx.err or sys.stderr
        stream.write(f"--force: {action} of {bid} EVEN WHILE ITS CARD IS BEING WRITTEN OR READ "
                     "BACK. A reset mid-write can WEDGE THE CARD (recovery: an MCC power "
                     f"cycle).\nTo go ahead, type exactly: {phrase}\n> ")
        stream.flush()
        try:
            given = sys.stdin.readline()
        except (OSError, ValueError):
            given = ""
    if given.strip() != phrase:
        raise RefusedError(f"{action} --force was not confirmed",
                           hint=f"type exactly: {phrase} (--yes never implies it)")
    return True, given.strip()
