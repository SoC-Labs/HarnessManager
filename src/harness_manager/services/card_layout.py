"""The harness card's partition layout, read from its MBR the way stage0 reads it.

stage0 finds the OS slots by MBR entries 1 and 2 (A and B; type 0x7F) and the design store by
the entry of type 0xDA; /persist is entry 3. The disk order of the partitions differs between
layouts, so nothing here assumes a fixed sector: the card's own table is the truth.

``layout(store_mib)`` mirrors ``layout()`` in the platform's ``stage0_mkcard.py`` (512-byte
sectors). Layout v2 (store 96 MiB, the default since mint 4) and v1 (store 32 MiB) are the two
known ones; a table that matches neither is ``unknown`` (still usable: the entries decide).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SECTOR = 512
MIB_SECTORS = (1 << 20) // SECTOR
TYPE_SLOT = 0x7F
TYPE_PERSIST = 0x83
TYPE_STORE = 0xDA
STORE_MIB_V1 = 32
STORE_MIB_V2 = 96
STORE_MIB_DEFAULT = STORE_MIB_V2
LBA_STORE = 2048                  # the store's first sector in both layouts
SLOT_SECTORS = 131072             # one OS slot: 64 MiB in both layouts


class CardLayoutError(ValueError):
    """The MBR is not a harness card's. The text is plain enough to show a user."""


NO_PARTITIONS = "This card has no harness partitions. Make it with the card tool first."


@dataclass(frozen=True)
class Part:
    """One partition: first sector and sector count."""

    type: int
    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length

    @property
    def mib(self) -> int:
        return self.length // MIB_SECTORS


def layout(store_mib: int = STORE_MIB_DEFAULT) -> tuple[int, int, int, int]:
    """Mirror of ``stage0_mkcard.layout``: ``(N_STORE, LBA_A, LBA_B, LBA_PERSIST)`` for a store
    of 96 MiB (v2, the default since mint 4) or 32 MiB (v1, every earlier card)."""
    if store_mib not in (STORE_MIB_V2, STORE_MIB_V1):
        raise CardLayoutError(f"the store must be {STORE_MIB_V2} (v2) or {STORE_MIB_V1} (v1) MiB")
    n_store = store_mib * MIB_SECTORS
    lba_a = LBA_STORE + n_store
    return n_store, lba_a, lba_a + SLOT_SECTORS, lba_a + 2 * SLOT_SECTORS


KNOWN = {1: layout(STORE_MIB_V1), 2: layout(STORE_MIB_V2)}


@dataclass(frozen=True)
class CardLayout:
    """A card's table. ``version`` is 1, 2 or 0 (unknown); ``store`` is ``None`` when the card
    has no type-0xDA entry (a compact test card, or an old card)."""

    slot_a: Part
    slot_b: Part
    persist: Part | None
    store: Part | None
    version: int

    @property
    def name(self) -> str:
        return f"v{self.version}" if self.version else "unknown"

    def require_store(self) -> Part:
        if self.store is None:
            raise CardLayoutError("This card has no design store partition (type 0xDA).")
        return self.store

    def as_dict(self) -> dict[str, object]:
        def part(p: Part | None) -> dict[str, int] | None:
            return None if p is None else {"start": p.start, "sectors": p.length, "mib": p.mib}
        return {"layout": self.name, "slot_a": part(self.slot_a), "slot_b": part(self.slot_b),
                "persist": part(self.persist), "store": part(self.store),
                "slot_mib": self.slot_a.mib}


def mbr_entries(sector: bytes) -> list[Part]:
    """The four entries, in table order (empty ones have type 0 and length 0)."""
    if len(sector) < SECTOR or sector[510:512] != b"\x55\xaa":
        raise CardLayoutError(NO_PARTITIONS + " (no MBR signature)")
    out = []
    for i in range(4):
        base = 446 + 16 * i
        start, length = struct.unpack_from("<II", sector, base + 8)
        out.append(Part(sector[base + 4], start, length))
    return out


def classify(slot_a: Part, slot_b: Part, store: Part | None) -> int:
    """1 or 2 when the slots (and the store, if present) sit exactly where that layout puts
    them; 0 otherwise."""
    for version, (n_store, lba_a, lba_b, _persist) in KNOWN.items():
        if (slot_a.start == lba_a and slot_b.start == lba_b
                and slot_a.length == SLOT_SECTORS and slot_b.length == SLOT_SECTORS
                and (store is None or (store.start == LBA_STORE and store.length == n_store))):
            return version
    return 0


def parse_mbr(sector: bytes, *, require_slots: bool = True) -> CardLayout:
    """Read the table. ``CardLayoutError`` for no signature, entries 1/2 not slots (unless
    ``require_slots`` is false: the caller reports them itself), or two partitions overlapping."""
    ents = mbr_entries(sector)
    a, b = ents[0], ents[1]
    for name, p in (("A", a), ("B", b)):
        if require_slots and (p.type != TYPE_SLOT or not p.start or not p.length):
            raise CardLayoutError(f"{NO_PARTITIONS} (slot {name} is missing)")
    persist = ents[2] if ents[2].type not in (0, TYPE_STORE) and ents[2].length else None
    stores = [p for p in ents if p.type == TYPE_STORE and p.length]
    store = stores[0] if stores else None
    used = [(n, p) for n, p in (("slot A", a), ("slot B", b)) if p.type == TYPE_SLOT and p.length]
    if persist is not None:
        used.append(("/persist", persist))
    if store is not None:
        used.append(("the store", store))
    for i, (n1, p1) in enumerate(used):
        for n2, p2 in used[i + 1:]:
            if p1.start < p2.end and p2.start < p1.end:
                raise CardLayoutError(f"This card's partition table is damaged: {n1} and {n2} "
                                      f"overlap. Make it again with the card tool.")
    return CardLayout(a, b, persist, store, classify(a, b, store))


def build_mbr(store_mib: int = STORE_MIB_DEFAULT, card_sectors: int | None = None) -> bytes:
    """The MBR sector for a fresh card of ``store_mib`` (v2 by default, v1 selectable), in
    stage0's entry order: 1 = slot A, 2 = slot B, 3 = /persist, 4 = store. ``card_sectors``
    sizes /persist to the end of the card (default: 256 MiB, stage0_mkcard's default)."""
    n_store, lba_a, lba_b, lba_p = layout(store_mib)
    persist_len = (card_sectors - lba_p) if card_sectors else 256 * MIB_SECTORS
    if persist_len <= 0:
        raise CardLayoutError("that card is too small for the harness partitions")
    sec = bytearray(SECTOR)
    struct.pack_into("<I", sec, 440, 0x53304C42)
    table = [(TYPE_SLOT, lba_a, SLOT_SECTORS), (TYPE_SLOT, lba_b, SLOT_SECTORS),
             (TYPE_PERSIST, lba_p, persist_len), (TYPE_STORE, LBA_STORE, n_store)]
    for i, (typ, lba, n) in enumerate(table):
        sec[446 + 16 * i:462 + 16 * i] = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", typ,
                                                     b"\xfe\xff\xff", lba, n)
    sec[510], sec[511] = 0x55, 0xAA
    return bytes(sec)
