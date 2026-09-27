"""Never reset a board while its card is being written or read back (lane SLOT-TIMING).

Silicon (B2 on the real board, 2026-09-26): an OS slot of 29 MB takes ~12 min to write to the
user microSD (SPI, ~70 KB/s) and up to ~35 min to read back (14-135 KB/s). A reset in the
middle of the write wedged the card, and only an MCC power cycle brought it back. So while
``slot status`` shows the card job ``writing`` or ``verifying``, every action that resets the
board or its harness is refused, and the refusal names the job::

    slot B is being written (12.3/29 MB); a reset now can wedge the card. Wait ~6 min.

What is guarded, and where:

- **MCC REBOOT** (``mcc reboot``, ``mcc cmd REBOOT``, ``POST /controller/reboot|command``)
  and an **outlet power cycle** (``power cycle``, ``POST /power/cycle``): at the call site.
  These two alone take ``force``: the way out of a job that never ends IS a power cycle. It
  needs the typed phrase ``force_phrase(board_id)`` (``--yes`` never implies it);
- **the harness's own reboot** (the ``reboot`` verb: ``slot rollback``, the update's OS-only
  reboot): in the pack's adapter (``Mps3OsSlots.reboot``);
- **deploy and restore** (``DeployService``): refused. A swap on its own does not reset the
  card's SPI controller (it lives in the static region), but the swap runs in the same
  harnessd as the card job, its bitstream goes over the same 6910 port the slot image is
  streaming on (one transfer at a time), "Keep on the card" writes the same card, and a swap
  that fails ends in a harness recovery. Nobody has run a swap during a card write on
  silicon: when unsure, refuse;
- **the update executor's reboot step** waits for the job instead (``wait_idle``), then
  reboots inside ``guarded``.

How the job is read: ``session.os_slots`` (CCR T7-2): ``busy_job()`` when the adapter has it
(the MPS3 one does: its rate and ETA estimates, and no ``slot`` request to a bare-metal
harness), else ``slots_reason()`` then ``status()``. A board with no OS slots (bare metal,
no card, stage0 rescue) or a harness that does not answer at all (refused, no route) has no
job to protect: allowed (a harness that does not answer is what a reboot recovers). A
harness whose control port is HELD cannot say, so the reset is refused as busy
(``HeldError``); ``force`` is the way past it.

**The hook for the pack's own reset paths (lane MCC-FIX).** Call
``check(session, ACTION_MCC_REBOOT)`` at the top of the MCC REBOOT path (``mcc.py``
``reboot()``, the hub door's reboot). It raises ``CardBusyError`` (exit 4, HTTP 409 HELD)
with the text above, and returns when the reset is safe. A caller that already checked (the
CLI or the daemon, with or without ``--force``) runs the reset inside
``guarded(session, action, ...)``; a ``check`` for the same board on the same thread inside
that block passes without asking the board again, so the two layers never disagree and
``--force`` reaches through. ``scope_of(board_id)`` is what the remote client forwards.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from harness_manager.core.errors import (
    HarnessError,
    HeldError,
    RefusedError,
    UsageError,
)
from harness_manager.core.pack import Progress, SlotJob, SlotStatus, report_progress

log = logging.getLogger(__name__)

#: The resets, by the name a refusal gives them.
ACTION_MCC_REBOOT = "MCC REBOOT"
ACTION_POWER_CYCLE = "power cycle"
ACTION_HARNESS_REBOOT = "harness reboot"
ACTION_DEPLOY = "deploy"
ACTION_RESTORE = "restore"
#: Only these take ``force``: the recovery of a card job that never ends is a power cycle.
FORCIBLE = (ACTION_MCC_REBOOT, ACTION_POWER_CYCLE)
#: The executor's wait for a job it did not start: at least this, else 1.5 x its ETA.
WAIT_MIN_S = 300.0
WAIT_UNKNOWN_S = 3600.0
WAIT_POLL_S = 5.0


class CardBusyError(HeldError):
    """A reset refused because the board's card job is writing or verifying (exit 4)."""

    def __init__(self, message: str, *, job: SlotJob | None = None, hint: str = "") -> None:
        super().__init__(message, holder="the card job", hint=hint)
        self.job = job


def is_reboot_line(line: str) -> bool:
    """An MCC command line that is a REBOOT (``mcc cmd REBOOT``: the adapter runs it)."""
    words = (line or "").split()
    return bool(words) and words[0].upper() == "REBOOT"


def force_phrase(board_id: str) -> str:
    """What a person types to reset anyway (``--force``): it names the board."""
    return f"RESET {board_id}"


def busy_words(job: SlotJob) -> str:
    """"slot B is being written (12.3/29 MB)"."""
    from harness_manager.services.slots import mb

    where = f"slot {job.slot}" if job.slot else "the card"
    if job.state == "writing" or 0 < job.got < job.length:
        size = f"{mb(job.got)}/{mb(job.length)} MB"
    else:
        size = f"{mb(job.length)} MB" if job.length else "size unknown"
    verb = "written" if job.state == "writing" else "read back"
    return f"{where} is being {verb} ({size})"


def refusal(action: str, job: SlotJob | None, *, why: str = "") -> CardBusyError:
    """The refusal of ``action`` while ``job`` runs (``job`` None: it could not be read)."""
    from harness_manager.services.slots import eta_text

    forcible = action in FORCIBLE
    way_out = (" Only if the job never ends: --force (the recovery is a power cycle)."
               if forcible else "")
    if job is None:
        return CardBusyError(
            f"{action} refused: the board's card job cannot be read ({why}); a reset now can "
            "wedge the card if it is being written.",
            hint="`harness-manager slot status TARGET` shows the job once the control port is "
                 "free (a card commit parks it)." + way_out)
    left = eta_text(job.eta_s)
    wait = f" Wait {left.removesuffix(' left')}." if left else " Wait for it to finish."
    return CardBusyError(
        f"{action} refused: {busy_words(job)}; a reset now can wedge the card.{wait}",
        job=job, hint="`harness-manager slot status TARGET` shows the job; reset once it is "
                      "ok or failed." + way_out)


# --- reading the job --------------------------------------------------------------------------------


def busy_job(session: Any) -> SlotStatus | None:
    """The board's status when its card job is writing or verifying; None when there is
    none to protect. ``HeldError`` when the job cannot be read (module docstring)."""
    slots = getattr(session, "os_slots", None)
    if slots is None:
        return None
    probe = getattr(slots, "busy_job", None)
    if callable(probe):
        return probe()
    try:
        if slots.slots_reason():
            return None
        st = slots.status()
    except HeldError:
        raise
    except HarnessError as exc:
        log.info("reset guard: no card job to read on %s: %s", _bid(session), exc)
        return None
    return st if st.job.busy else None


def _bid(session: Any) -> str:
    cand = getattr(session, "candidate", None)
    return str(getattr(cand, "board_id", "") or "")


# --- the scope: a caller that checked, so a nested check passes ----------------------------------


@dataclass(frozen=True)
class Scope:
    action: str
    force: bool = False
    consent: str = ""


_local = threading.local()


def _scopes() -> dict[str, Scope]:
    scopes = getattr(_local, "scopes", None)
    if scopes is None:
        scopes = _local.scopes = {}
    return scopes


def scope_of(board_id: str) -> Scope | None:
    """The reset this thread is inside for ``board_id`` (the remote client forwards its
    ``force``/``consent`` to the service)."""
    return _scopes().get(board_id)


def check(session: Any, action: str, *, force: bool = False, consent: str = "") -> SlotJob | None:
    """Refuse ``action`` while the card job runs (``CardBusyError``). Returns None when the
    reset is safe, or the job a FORCED reset goes ahead over (``force`` with the typed
    ``consent``: ``force_phrase``). Inside ``guarded`` for this board: passes."""
    bid = _bid(session)
    if bid and scope_of(bid) is not None:
        return None
    if force and action not in FORCIBLE:
        raise UsageError(f"--force is not offered for {action}: only an MCC REBOOT or a power "
                         "cycle recovers a card job that never ends")
    try:
        st = busy_job(session)
    except HeldError as exc:
        job, err = None, refusal(action, None, why=exc.message)
    else:
        if st is None:
            return None
        job, err = st.job, refusal(action, st.job)
    if not force:
        raise err
    want = force_phrase(bid)
    if consent.strip() != want:
        raise RefusedError(f"{action} --force needs the typed phrase: {err.message}",
                           hint=f"type exactly: {want}")
    log.warning("reset guard: %s of %s FORCED during the card job (%s)", action, bid,
                err.message)
    return job or SlotJob(state="unknown")


@contextlib.contextmanager
def guarded(session: Any, action: str, *, force: bool = False,
            consent: str = "") -> Iterator[SlotJob | None]:
    """``check``, then run the reset: a nested ``check`` for this board passes."""
    job = check(session, action, force=force, consent=consent)
    bid = _bid(session)
    scopes = _scopes()
    outer = scopes.get(bid)
    scopes[bid] = Scope(action, force=force, consent=consent)
    try:
        yield job
    finally:
        if outer is None:
            scopes.pop(bid, None)
        else:
            scopes[bid] = outer


# --- waiting for the job (the update executor) ---------------------------------------------------


def wait_idle(session: Any, *, timeout_s: float | None = None, poll_s: float = WAIT_POLL_S,
              progress: Progress | None = None, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> SlotJob | None:
    """Wait until the card job is not writing or verifying. Returns the job it waited for
    (None: there was none). ``timeout_s`` None: 1.5 x the job's ETA, at least ``WAIT_MIN_S``
    (``WAIT_UNKNOWN_S`` without an ETA). A control port held by another client is read
    again until the same budget (``WAIT_MIN_S`` when there is no job yet). Raises
    ``CardBusyError`` when the job is still running then, or still cannot be read."""
    from harness_manager.services.slots import job_detail

    first: SlotJob | None = None
    deadline: float | None = None
    held_until: float | None = None
    while True:
        try:
            st = busy_job(session)
        except HeldError as exc:
            now = clock()
            if held_until is None:
                held_until = deadline if deadline is not None else now + (
                    timeout_s if timeout_s is not None else WAIT_MIN_S)
            if now >= held_until:
                raise refusal("the reboot", first, why=exc.message) from exc
            sleep(poll_s)
            continue
        held_until = None
        if st is None:
            return first
        job = st.job
        if first is None:
            first = job
            budget = timeout_s if timeout_s is not None else (
                max(WAIT_MIN_S, 1.5 * job.eta_s) if job.eta_s else WAIT_UNKNOWN_S)
            deadline = clock() + budget
            log.info("reset guard: %s waits for %s (up to %.0f s)", _bid(session),
                     busy_words(job), budget)
        detail = job_detail(job)
        detail["text"] = f"waiting for the card job before the reboot: {detail['text']}"
        report_progress(progress, f"waiting-card:{job.state}", job.got, job.length, detail)
        assert deadline is not None
        if clock() >= deadline:
            raise refusal("the reboot", job)
        sleep(poll_s)
