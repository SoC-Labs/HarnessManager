"""What a ``SlotStatus`` says about the boot, in words (lane LINUX-ANSWERS).

Board-agnostic: it reads ``core.pack.SlotStatus`` and the board's own reply beside it
(``SlotStatus.raw``). The Linux lead's answers to HM (``HM_ANSWERS_2026-09-26.md``,
feat/linux-harness) corrected three things HM assumed:

- **A fallback is visible.** stage0 never writes the card (STAGE0 §1), and harnessd writes
  boot-select only on ``commit``/``rollback``. So when the default slot does not come up
  healthy, stage0 boots the other one and the default STAYS on the bad slot:
  ``running != default`` with ``staged`` empty (between a commit and its reboot,
  ``staged == default``). The board then has no push target (``no free slot … rollback
  first``) until a ``rollback`` makes the running slot the default again; until then every
  power cycle or MCC REBOOT tries the bad default twice more (about 2 x 43 s of watchdog
  timeouts, STAGE0 §4). ``fell_back`` names the bad slot (S5).
- **``verified: boot`` is not "confirmed".** It means "this slot is running and its card
  table CRC equals stage0's ``image_hdr_crc``" (``slot_linux.c:449-451``). harnessd writes
  CONFIRMED later (at least 2 s after start, vital services healthy, a watchdog kick,
  ``boot-health`` healthy), and may never do so. Only the ``confirmed`` field (proposed,
  additive: true when stage0's ``att_confirm`` holds CONFIRMED) says a boot was confirmed.
  Until a harness reports it, a booted slot is "booted (not yet confirmed)" (``boot_words``).
- **The claim is in ``slot status``** (proposed, additive: ``claimed``). When present it is
  the lock's state; absent, the pack finds out another way (identify, or a lock probe).

Every field here is read when present and never required.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from harness_manager.core.pack import VERIFIED_BOOT, VERIFIED_READBACK, SlotStatus

#: ``slot status`` keys the Linux lead proposed (S2, S5): additive, read when present.
CONFIRMED_KEY = "confirmed"
CLAIMED_KEY = "claimed"

#: What a fallback costs until it is rolled back (the Linux lead's S5 answer).
RETRY_COST = ("until then every power cycle or MCC REBOOT tries slot {bad} twice more "
              "(about 2 x 43 s of watchdog timeouts) before {running} runs again")


def _flag(st: SlotStatus, key: str) -> bool | None:
    value = (st.raw or {}).get(key)
    return value if isinstance(value, bool) else None


def confirmed(st: SlotStatus) -> bool | None:
    """Did harnessd confirm THIS boot (stage0's ``att_confirm``)? None: the harness does not
    report it (every harness until the Linux lead's change 2 lands)."""
    return _flag(st, CONFIRMED_KEY)


def claimed(st: SlotStatus) -> bool | None:
    """Is the board's SSH claimed (the slot lock)? None: ``slot status`` does not say."""
    return _flag(st, CLAIMED_KEY)


def _ab(name: str) -> bool:
    return name in ("A", "B")


def fell_back(st: SlotStatus) -> str:
    """The default slot stage0 fell back FROM (it did not come up healthy), or "".

    ``running != default`` with nothing staged. A commit waiting for its reboot looks the
    same except that ``staged == default``."""
    if not (_ab(st.running) and _ab(st.default)) or st.running == st.default:
        return ""
    return "" if st.staged else st.default


def committed_unbooted(st: SlotStatus) -> str:
    """The slot committed as the default and not booted yet (rule 1), or "". Unlike
    ``SlotStatus.pending_commit`` this is never a fallback."""
    if not (_ab(st.running) and _ab(st.default)) or st.running == st.default:
        return ""
    return st.default if st.staged == st.default else ""


def fallback_line(st: SlotStatus, target: str = "TARGET") -> str:
    """"slot B failed to boot; A is running; roll back to make A the default", or ""."""
    bad = fell_back(st)
    if not bad:
        return ""
    return (f"slot {bad} failed to boot; {st.running} is running; roll back to make "
            f"{st.running} the default (`harness-manager slot rollback {target}`); "
            + RETRY_COST.format(bad=bad, running=st.running))


def fallback_hint(st: SlotStatus | None = None, target: str = "TARGET") -> str:
    """The fix for ``no free slot … rollback first``: a fallback's or rule 1's, or both
    when the status is not known."""
    if st is not None and fell_back(st):
        bad = fell_back(st)
        return (f"slot {bad} failed to boot and stage0 went back to {st.running}, which is "
                f"not the default yet: `harness-manager slot rollback {target}` makes "
                f"{st.running} the default and frees slot {bad} for a push")
    if st is not None and committed_unbooted(st):
        return (f"slot {committed_unbooted(st)} is committed and not booted yet, so no slot "
                f"is free (rule 1): `harness-manager slot rollback {target}` undoes the "
                "commit, or reboot into it")
    return ("no slot is free: either a commit waits for its reboot (rule 1) or the default "
            "slot failed to boot and stage0 went back to the other one; `harness-manager "
            f"slot status {target}` says which, and `harness-manager slot rollback {target}` "
            "fixes both")


def boot_words(st: SlotStatus, name: str) -> str:
    """How a slot's image is known good, in words. Never "confirmed" from ``verified:
    boot`` alone: only ``confirmed`` says harnessd confirmed the boot."""
    info = st.slots.get(name)
    if info is None or not info.valid:
        return ""
    if info.verified == VERIFIED_READBACK:
        return "read back this boot"
    if info.verified == VERIFIED_BOOT:
        if name == st.running and confirmed(st) is True:
            return "booted, confirmed healthy"
        return "booted (not yet confirmed)"
    return "not verified this boot"


def notes(st: SlotStatus, target: str = "TARGET") -> list[str]:
    """What a person should know from this status, beyond the table."""
    out: list[str] = []
    line = fallback_line(st, target)
    if line:
        out.append(line)
    run = st.slots.get(st.running)
    if run is not None and run.verified == VERIFIED_BOOT and confirmed(st) is None:
        out.append("this harness does not report whether harnessd confirmed the boot, so "
                   "`verified: boot` means only that stage0 booted this image")
    if claimed(st) is True:
        out.append("the board's SSH is claimed: slot push, commit and rollback are taken "
                   "only from the board itself (Harness Manager goes through its SSH)")
    return out


def view(st: SlotStatus, target: str = "TARGET") -> dict[str, Any]:
    """The additive keys the CLI and ``GET /slots`` add to ``slot_status_json``."""
    bad = fell_back(st)
    return {"fell_back": bad or None, "committed_unbooted": committed_unbooted(st) or None,
            "confirmed": confirmed(st), "claimed": claimed(st), "notes": notes(st, target),
            "boot": {n: boot_words(st, n) for n in sorted(st.slots)}}


def extend_json(doc: dict[str, Any], st: SlotStatus, target: str = "TARGET") -> dict[str, Any]:
    """``doc`` (a ``slot_status_json``) with ``view``'s keys, and each slot's ``boot`` words
    beside its ``verified``. Returns ``doc``."""
    extra = view(st, target)
    words = extra.pop("boot")
    doc.update(extra)
    for name, one in (doc.get("slots") or {}).items():
        if isinstance(one, dict):
            one["boot"] = words.get(name, "")
    return doc


#: How long the update waits for harnessd's confirm after the reboot, and how often it
#: asks (the board confirms at least 2 s after start, once it is healthy).
CONFIRM_WAIT_S = 30.0
CONFIRM_POLL_S = 1.0


def wait_confirmed(read: Callable[[], SlotStatus], *, timeout_s: float = CONFIRM_WAIT_S,
                   poll_s: float = CONFIRM_POLL_S,
                   sleep: Callable[[float], None] = time.sleep) -> SlotStatus:
    """Read the status until ``confirmed`` is not False (True, or not reported), at most
    ``timeout_s / poll_s`` more reads; the last status either way. Reads only: ``status``
    never starts a card job."""
    st = read()
    for _ in range(max(1, math.ceil(timeout_s / poll_s))):
        if confirmed(st) is not False:
            break
        sleep(poll_s)
        st = read()
    return st
