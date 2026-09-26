"""The OS-slot seam for Linux harnesses (the ``ethernet`` door's OS image, user µSD).

The protocol and its value types now live in ``harness_manager.core.pack`` (CCR T7-2:
``BoardSession.os_slots``); this module re-exports them for the update service and
its importers. The MPS3 pack's implementation is ``harness_manager_mps3.os_slots``
(HARNESS-DIST H12), over ``pyverify.slot``.

The contract (net-protocol.md v0.14 "Slot images"; SLOT_VERB_DRAFT.md with HM's
answers in HARNESS_DISTRIBUTION.md §9)::

    status                               -> target "B", staged null
    push (6910 kind 2, static_id = the image's PROVISIONED static)
    poll status until job is ok          -> staged "B"
    commit                               -> default "B"
    reboot; identify/version             -> the new image answers

- the board never writes the running slot nor the default one: after a commit and
  before the reboot there is no target, so a second push needs a ``rollback`` first
  (rule 1; HM does that itself before pushing, and says so);
- a slot becomes the default only when it is verified (read back this boot, or it is
  the slot stage0 booted);
- the board CONFIRMS a healthy boot itself (harnessd writes stage0's marker), so an
  image that never comes up healthy is undone by stage0 (2 unconfirmed boots). stage0
  never writes the card, so after that FALLBACK the default stays on the bad slot
  (``running != default``, nothing staged) until a ``rollback``. ``verified: boot`` means
  only that stage0 booted the slot, never that harnessd confirmed it: that is the
  ``confirmed`` field, when a harness sends it (``services.slot_health``). An image
  that is healthy but wrong is undone by the host: ``verify`` the other slot, then
  ``rollback`` and ``reboot`` (``harness-manager slot rollback``);
- once the board's SSH is claimed, the mutations are accepted only from the board
  itself: the pack tunnels them over SSH to the board's 127.0.0.1.

Rescue: a Linux board whose card holds no bootable slot comes up in stage0 RESCUE
(``Health.control_channel == "rescue"``: pingable, TFTP 69, identify ``mode:"rescue"``,
no 6900). Provisioning a slot from there is HARNESS-DIST L3 (not frozen yet): the
adapter reports rescue in ``slots_reason()`` and refuses.
"""

from __future__ import annotations

from harness_manager.core.pack import (
    SLOT_ABSENT,
    SLOT_BAD,
    SLOT_EMPTY,
    SLOT_IO,
    SLOT_VALID,
    VERIFIED_BOOT,
    VERIFIED_NO,
    VERIFIED_READBACK,
    OsSlotAdapter,
    Progress,
    SlotInfo,
    SlotJob,
    SlotStatus,
)

__all__ = [
    "SLOT_ABSENT", "SLOT_BAD", "SLOT_EMPTY", "SLOT_IO", "SLOT_VALID",
    "VERIFIED_BOOT", "VERIFIED_NO", "VERIFIED_READBACK",
    "OsSlotAdapter", "Progress", "SlotInfo", "SlotJob", "SlotStatus",
]
