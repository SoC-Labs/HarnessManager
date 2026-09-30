"""The Linux harness's refusals, as Harness Manager errors (lane LINUX-ANSWERS).

From the Linux lead's answers (``HM_ANSWERS_2026-09-26.md`` on feat/linux-harness), read
from harnessd's code:

**Two locks, not one** (their correction of HM's assumption 1).

- The **claim lock** (``slot locked: board claimed (use ssh)``): the board's SSH is claimed
  and the peer is not the board itself. It refuses slot push/``commit``/``rollback``. Fix:
  send it from the board (its SSH), which the pack does when your key claimed it.
- The **identity lock** (``identity lock: <reason>``, ``identity.c:108-126``): the fabric and
  the card's image disagree, claimed or not. It refuses ``swap`` and D13 ``commit``, and a
  flip to a slot known only by its boot (``slot A runs, but identity lock: …``). Two kinds:

  - **mismatch**: ``image 0x… != fabric 0x…`` or ``usr_access 0x… != image 0x…``;
  - **unknown**: ``no stage0 status block mapped``, ``no valid stage0 status block``,
    ``fabric static_id unknown``, ``image static_id not provisioned``.

  Neither means "incompatible, give up" (so neither is ``IncompatibleError``): a push checks
  the image's static_id against the fabric, and a flip to a read-back slot ignores the lock
  (``slot_linux.c:949-956``), so pushing the right image, committing it and rebooting fixes
  it over Ethernet. The exception is a fabric the harness cannot read (no stage0 block, no
  static_id): the board refuses every slot write then (``fabric_err``,
  ``slot_linux.c:500-509``), and the hint says so.

**Codes.** Today every refusal is ``{"ok":false,"err":…}``. The Linux lead proposed a stable
``code`` beside ``err`` and ``job.code`` for a failed job (S3/C2, additive). When a reply
carries one it decides the class; otherwise the ``err`` text does, as before.

**Records** (S1). ``verify`` accepts only the slot's on-card record (``S0SR``), written by a
harnessd push after its read-back and nowhere else. A slot written by ``stage0_mkcard.py`` +
``dd``, ``mps3-slot write`` or the factory has none, so its verify fails with the text at
``slot_linux.c:645``, ``no slot record: static_id unknown``, and a rollback to it after a
reboot is refused.
"""

from __future__ import annotations

from typing import Any

from harness_manager.core import errors as _errors
from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
)

CAPABILITY = "OS slot update"
#: The two locks' texts, verbatim (net-protocol "The lock"; identity.c "identity lock").
SLOT_LOCKED_ERR = "slot locked: board claimed (use ssh)"
IDENTITY_LOCK_PREFIX = "identity lock:"
#: ``slot_linux.c:645``, a verify of a slot with no on-card record.
NO_RECORD_ERR = "no slot record: static_id unknown"

#: The Linux lead's proposed codes (S3). Verb refusals, then card-job failures.
VERB_CODES = ("not_supported", "bad_act", "bad_slot", "locked", "no_card", "card_io",
              "no_stage0", "fabric_unknown", "busy", "nothing_staged", "slot_mismatch",
              "not_valid", "not_verified", "changed", "wrong_static", "identity_lock",
              "bootsel")
JOB_CODES = ("wrong_static", "no_free_slot", "slot_mismatch", "too_large", "slot_absent",
             "slot_bad", "bad_image", "torn", "aborted", "card_write", "readback", "record",
             "no_record", "restarted")

LOCK_MISMATCH = "mismatch"
LOCK_UNKNOWN = "unknown"

#: identity.c's "unknown" reasons, and which of them leave the FABRIC unknown (the board
#: then refuses slot writes too).
_UNKNOWN_FABRIC = ("no stage0 status block mapped", "no valid stage0 status block",
                   "no stage0 block", "fabric static_id unknown")
_UNKNOWN_IMAGE = ("image static_id not provisioned",)


#: The claim lock: the board is claimed and this peer is not the board itself (S12).
#: ``claim.ClaimLockedError`` is this class, and so is the core one the board-agnostic XVC
#: and debug services raise (CLAIMED-LOCK): one lock, one class.
ClaimLockedError = _errors.ClaimLockedError


class IdentityLockError(RefusedError):
    """The identity lock: the fabric and the card's image disagree (``kind`` mismatch) or
    one of them cannot be read (``kind`` unknown). Fixable over Ethernet unless the fabric
    itself is unknown (``fabric_unknown``)."""

    def __init__(self, message: str, *, kind: str, reason: str, fabric_unknown: bool = False,
                 hint: str = "") -> None:
        super().__init__(message, hint=hint)
        self.kind = kind
        self.reason = reason
        self.fabric_unknown = fabric_unknown


def reply_code(reply: Any) -> str:
    """A reply's (or a job's) stable ``code``, "" when it has none."""
    code = reply.get("code") if isinstance(reply, dict) else None
    return code if isinstance(code, str) else ""


# --- the identity lock ----------------------------------------------------------------------------


def identity_lock_reason(err: str) -> str | None:
    """The reason of an identity-lock refusal (``identity lock: R`` or ``slot A runs, but
    identity lock: R``), else None."""
    text = (err or "").strip()
    at = text.lower().find(IDENTITY_LOCK_PREFIX)
    if at < 0:
        return None
    return text[at + len(IDENTITY_LOCK_PREFIX):].strip()


def identity_lock_kind(reason: str) -> str:
    """``mismatch`` (the two disagree) or ``unknown`` (one cannot be read)."""
    low = (reason or "").lower()
    if "!=" in low:
        return LOCK_MISMATCH
    return LOCK_UNKNOWN


def _fabric_unknown(reason: str) -> bool:
    low = (reason or "").lower()
    return any(low.startswith(r) for r in _UNKNOWN_FABRIC)


#: The fix, over Ethernet, for an identity lock the board can still take a push for.
PUSH_FIX = ("fix it over Ethernet: push the image built for this static (`harness-manager slot "
            "push TARGET --bundle linux_bundle.json`, or `harness-manager update`), then "
            "`harness-manager slot commit TARGET`, then reboot (`harness-manager mcc TARGET "
            "reboot`); a flip to a read-back slot ignores the lock")


def identity_lock_hint(reason: str) -> str:
    kind = identity_lock_kind(reason)
    if kind == LOCK_MISMATCH:
        return ("the card's OS image and the FPGA's static disagree (" + reason + "). This is "
                "the identity lock, not the SSH claim; " + PUSH_FIX)
    if _fabric_unknown(reason):
        return ("the harness cannot read the FPGA's static (" + reason + "): stage0's status "
                "block or its baked static_id is missing, and the board refuses slot writes "
                "until it has one. Boot through stage0 again (a power cycle or `harness-manager "
                "mcc TARGET reboot`); if it persists, the base (.bit) needs a stage0 with its "
                "static baked in, through the Debug USB or the hub")
    return ("the image on the card does not say which static it was provisioned for (" +
            reason + "). This is the identity lock, not the SSH claim; " + PUSH_FIX)


def identity_lock_error(err: str, what: str = "this operation") -> IdentityLockError | None:
    """``identity lock: <reason>`` as an ``IdentityLockError``; None if it is not one."""
    reason = identity_lock_reason(err)
    if reason is None:
        return None
    kind = identity_lock_kind(reason)
    words = ("the card's image and the FPGA's static disagree" if kind == LOCK_MISMATCH
             else "the harness cannot tell which static the fabric or the image is for")
    return IdentityLockError(
        f"the harness refused {what}: identity lock ({kind}): {words} ({reason}); nothing "
        "was changed", kind=kind, reason=reason, fabric_unknown=_fabric_unknown(reason),
        hint=identity_lock_hint(reason))


# --- the claim lock ------------------------------------------------------------------------------


CLAIM_LOCK_HINT = ("the board's SSH is claimed (the claim lock, not the identity lock): only a "
                   "connection from the board itself may change the slots. Harness Manager goes "
                   "through the board's SSH when your key claimed it: check `harness-manager "
                   "board claim-status TARGET`")


def is_claim_lock(err: str, code: str = "") -> bool:
    if code:
        return code == "locked"
    low = (err or "").strip().lower()
    return low.startswith("slot locked") or "board claimed" in low


# --- a slot act's refusal, and a failed card job ------------------------------------------------


NO_RECORD_HINT = ("this slot was written outside harnessd (stage0_mkcard.py + dd, `mps3-slot "
                  "write`, or the factory), so it has no record binding it to a static and "
                  "the board cannot verify it. Push it again from Harness Manager. (A harness "
                  "from platform 53f49b4 on stamps the record once the slot boots and is "
                  "confirmed: this slot has not, or the image predates that fix)")


def refusal(act: str, err: str, code: str = "", st: Any = None) -> HarnessError:
    """The board refused a ``slot`` act: the error class its ``code`` (else its text) means.
    ``st`` (a ``SlotStatus``, when known) picks the fix for ``no free slot``."""
    from harness_manager.services import slot_health

    low = (err or "").lower()
    c = code
    if c == "busy" or (not c and err.strip().upper() == "EBUSY"):
        return HeldError(f"slot {act}: the board's card is busy (EBUSY)",
                         hint="a push or a verify is running (a verify holds the card for "
                              "minutes); wait for it: `harness-manager slot status TARGET`")
    if is_claim_lock(err, c):
        return ClaimLockedError(f"slot {act}: {err or 'locked'}", hint=CLAIM_LOCK_HINT)
    if c == "identity_lock" or (not c and identity_lock_reason(err) is not None):
        lock = identity_lock_error(err, f"slot {act}")
        if lock is not None:
            return lock
        return IdentityLockError(f"slot {act}: {err}", kind=LOCK_UNKNOWN, reason=err,
                                 hint=identity_lock_hint(err))
    if c in ("not_supported", "bad_act") or (not c and any(
            k in low for k in ("unknown op", "slot not supported"))):
        return UnavailableError(CAPABILITY, f"this harness does not have the slot verb ({err})")
    if c in ("no_card", "card_io") or (not c and low in ("no card", "card io")):
        return RefusedError(f"slot {act}: {err}: nothing was changed",
                            hint="insert the board's user microSD card")
    if c in ("no_stage0", "fabric_unknown") or (not c and low in ("no stage0 block",
                                                                  "fabric static_id unknown")):
        return RefusedError(f"slot {act}: {err}: the harness cannot read the FPGA's static, "
                            "so the board refuses slot changes; nothing was changed",
                            hint=identity_lock_hint(err))
    if c == "wrong_static" or (not c and "!= fabric" in low):
        return IncompatibleError(f"slot {act}: {err}",
                                 hint="the image is provisioned for another static")
    if c == "nothing_staged" or (not c and low.startswith("nothing staged")):
        return RefusedError(f"slot {act}: {err}", hint="push an image first "
                            "(`harness-manager slot push TARGET IMAGE`)")
    if c in ("not_verified", "changed") or (not c and ("not verified" in low or
                                                       "changed since it was verified" in low)):
        return RefusedError(f"slot {act}: {err}", hint="verify it first "
                            "(`harness-manager slot verify TARGET --slot X`: it reads the slot "
                            "back and holds the card for minutes)")
    if not c and low.startswith("no free slot"):
        return RefusedError(f"slot {act}: {err}", hint=slot_health.fallback_hint(st))
    return ActionFailedError(f"slot {act} refused: {err or code or 'no reason given'}")


def job_failure(job: Any, code: str = "", st: Any = None, extra: str = "") -> HarnessError:
    """A failed card job (``SlotJob``; its ``code`` when the board sends one) as an error.
    ``extra`` is appended to the message (the card's state, when the caller has it)."""
    from harness_manager.services import slot_health

    where = f" {job.slot}" if job.slot else ""
    err = str(job.err or "")
    text = f"the board's {job.act}{where} failed: {err}" + (f" ({extra})" if extra else "")
    c = code
    if c == "no_record" or (not c and "no slot record" in err):
        return RefusedError(text, hint=NO_RECORD_HINT)
    if c == "wrong_static" or (not c and "!= fabric" in err):
        return IncompatibleError(text, hint="the image is provisioned for another static")
    if c == "no_free_slot" or (not c and err.startswith("no free slot")):
        return RefusedError(text, hint=slot_health.fallback_hint(st))
    if identity_lock_reason(err) is not None:
        return identity_lock_error(err, f"the {job.act}") or ActionFailedError(text)
    return ActionFailedError(text, hint="the card keeps its previous default; `harness-manager "
                                        "slot status TARGET` says what the card holds now")
