"""The OS-slot seam for Linux harnesses (the Ethernet-updatable target, user µSD).

The board's user µSD holds two OS slots (A/B) that stage0 boots from. An OS
update writes the INACTIVE slot, arms it try-once, reboots, and then the new
slot must be confirmed healthy; if it is not (a panic, a hang, a failed
health check), stage0 boots the old slot again. Nothing here writes the card
itself: the board writes its own card.

The mechanism that carries the write is NOT frozen yet (a 6910 push kind plus
a ``slot`` verb, or SSH + ``mps3-update``; ``docs/planning/linux_lanes/
FLOW_CONTRACT.md`` in the platform repo, not landed). So the executor talks
to this protocol only, and the tests use a fake. A board pack provides an
implementation as ``session.os_slots`` (see the contract change request).

Rescue: a Linux board whose card holds no bootable slot comes up in stage0
RESCUE (``Health.control_channel == "rescue"``: pingable, TFTP 69, identify
``mode:"rescue"``, no 6900). Re-provisioning a slot from there (a TFTP push of
the slot image) belongs behind this same interface, in the pack's implementation.

The confirm step: on the Linux image, harnessd writes stage0's CONFIRMED marker
itself once the boot is healthy (IMAGE_CONTRACT §8). ``confirm`` is therefore
"make sure it is confirmed": an implementation may confirm from the host or
wait for the board's own confirm; either way it raises if it cannot see it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

Progress = Callable[[str, int, int], None]

SLOT_EMPTY = "empty"
SLOT_CONFIRMED = "confirmed"
SLOT_TRY_ONCE = "try-once"
SLOT_BAD = "bad"


@dataclass(frozen=True)
class SlotInfo:
    name: str                 # "A" | "B"
    image_sha256: str = ""    # "" when empty or unknown
    version: str = ""
    state: str = SLOT_EMPTY   # empty | confirmed | try-once | bad


@dataclass(frozen=True)
class SlotStatus:
    active: str                                   # the slot the board booted
    slots: dict[str, SlotInfo] = field(default_factory=dict)

    @property
    def inactive(self) -> str:
        others = [n for n in sorted(self.slots) if n != self.active]
        if not others:
            raise ValueError("the board reports no inactive slot")
        return others[0]

    @property
    def active_info(self) -> SlotInfo:
        return self.slots.get(self.active, SlotInfo(self.active))


@runtime_checkable
class OsSlotAdapter(Protocol):
    def status(self) -> SlotStatus:
        """Which slot is running, and what each slot holds."""
        ...

    def write_inactive(self, image: Path, *, sha256: str, version: str,
                       progress: Progress | None = None) -> str:
        """Write ``image`` into the inactive slot; return the slot name. Never the active one."""
        ...

    def arm_try_once(self, slot: str) -> None:
        """Boot ``slot`` once at the next reboot; stage0 falls back unless it is confirmed."""
        ...

    def reboot(self, progress: Progress | None = None, wait_s: float = 180.0) -> dict | None:
        """Reboot the board and witness it go down and come back (the `reboot` verb)."""
        ...

    def confirm(self, slot: str) -> None:
        """Make sure ``slot`` is confirmed (host confirm, or wait for the board's own)."""
        ...
