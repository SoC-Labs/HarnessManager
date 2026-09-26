"""The stage0 boot image (S0LB v2) as far as an install needs it: its frames, checked.

A Linux harness's OS slot image (``linux_slot.img``) is stage0's boot table (STAGE0_CONTRACT
v1.2 §5; ``stage0_pack.py`` is the one writer): a 32-byte header, then one 16-byte entry
per region, then the regions::

    header  <8I  magic "S0LB" (0x424C3053), version 2, num_entries (1..8), entry_pc,
                 entry_a0, entry_a1, flags, header_crc32
    entry   <4I  src_offset, dst, len, crc32          (one per region)

``header_crc32`` is the CRC-32 of the header (with that field zeroed) plus the entry table:
the image's identity everywhere (the board's slot ``hdr_crc``, stage0's
``image_hdr_crc``). Every region must lie inside the file and inside the DDR window
``[0x8000_0000, 0xB000_0000)``, and carry its CRC. stage0 checks the same at boot and the
board checks it again on a push; this module lets an install refuse a bad download
before it touches a board, and compare it with what ``linux_bundle.json`` declared.

Stdlib only (like ``bitheader``): the update service does not import a board pack's codec.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

MAGIC = 0x424C3053            # "S0LB"
VERSION = 2
MAX_ENTRIES = 8
IMAGE_MAX = 64 * 1024 * 1024
DDR_BASE, DDR_END = 0x80000000, 0xB0000000
_HDR = struct.Struct("<8I")
_ENT = struct.Struct("<4I")


@dataclass(frozen=True)
class Region:
    src_offset: int
    dst: int
    length: int
    crc32: int


@dataclass(frozen=True)
class BootTable:
    header_crc32: int
    num_entries: int
    entry_pc: int
    entry_a0: int
    entry_a1: int
    regions: tuple[Region, ...] = ()
    problems: tuple[str, ...] = field(default=())   # region faults (the table itself parsed)

    @property
    def ok(self) -> bool:
        return not self.problems


class S0lbError(ValueError):
    """Not an S0LB v2 boot table stage0 would read."""


def _h(v: int) -> str:
    return f"0x{v & 0xFFFFFFFF:08x}"


def parse(image: bytes) -> BootTable:
    """Parse and check ``image`` as stage0 will. Raises ``S0lbError`` when the header or
    the entry table is unusable; region faults land in ``BootTable.problems``."""
    if len(image) < _HDR.size:
        raise S0lbError(f"shorter than the {_HDR.size}-byte header ({len(image)} B)")
    magic, ver, n, pc, a0, a1, flags, hcrc = _HDR.unpack_from(image, 0)
    if magic != MAGIC:
        raise S0lbError(f"no boot table (magic {_h(magic)}, want {_h(MAGIC)} 'S0LB')")
    if ver != VERSION:
        raise S0lbError(f"S0LB version {ver}; stage0 takes {VERSION}")
    if not 1 <= n <= MAX_ENTRIES:
        raise S0lbError(f"num_entries {n} (1..{MAX_ENTRIES})")
    end = _HDR.size + _ENT.size * n
    if len(image) < end:
        raise S0lbError("the entry table is truncated")
    table = _HDR.pack(magic, ver, n, pc, a0, a1, flags, 0) + image[_HDR.size:end]
    if zlib.crc32(table) & 0xFFFFFFFF != hcrc:
        raise S0lbError("the table CRC fails (the header or an entry is corrupt)")
    problems: list[str] = []
    if len(image) > IMAGE_MAX:
        problems.append(f"{len(image)} B is over stage0's 64 MiB")
    regions = []
    for i in range(n):
        src, dst, ln, crc = _ENT.unpack_from(image, _HDR.size + _ENT.size * i)
        regions.append(Region(src, dst, ln, crc))
        if src + ln > len(image):
            problems.append(f"region {i} runs past the end of the image (truncated)")
        elif zlib.crc32(image[src:src + ln]) & 0xFFFFFFFF != crc:
            problems.append(f"region {i} fails its CRC")
        if dst < DDR_BASE or dst + ln > DDR_END:
            problems.append(f"region {i} dst {_h(dst)}+{ln:#x} is outside the DDR window")
    return BootTable(header_crc32=hcrc, num_entries=n, entry_pc=pc, entry_a0=a0, entry_a1=a1,
                     regions=tuple(regions), problems=tuple(problems))


def _u32(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v & 0xFFFFFFFF
    if isinstance(v, str):
        try:
            return int(v, 0) & 0xFFFFFFFF
        except ValueError:
            return None
    return None


def compare(table: BootTable, declared: dict[str, Any]) -> list[str]:
    """Where the parsed frames differ from ``linux_bundle.json``'s ``slot_image.s0lb``
    (``check_image_overlay_match.s0lb_info``'s shape). ``[]`` when they agree."""
    out: list[str] = []
    for key, have in (("header_crc32", table.header_crc32), ("entry_pc", table.entry_pc),
                      ("entry_a0", table.entry_a0), ("entry_a1", table.entry_a1)):
        if key in declared:
            want = _u32(declared.get(key))
            if want != have:
                out.append(f"{key} is {_h(have)}, the bundle declares {declared.get(key)}")
    if "num_entries" in declared and declared.get("num_entries") != table.num_entries:
        out.append(f"{table.num_entries} region(s), the bundle declares "
                   f"{declared.get('num_entries')}")
    regions = declared.get("regions")
    if isinstance(regions, list):
        if len(regions) != len(table.regions):
            out.append(f"{len(table.regions)} region(s), the bundle declares {len(regions)}")
        for i, (want, have) in enumerate(zip(regions, table.regions, strict=False)):
            if not isinstance(want, dict):
                out.append(f"region {i}: the bundle's entry is not an object")
                continue
            for key, value in (("dst", have.dst), ("crc32", have.crc32)):
                if _u32(want.get(key)) != value:
                    out.append(f"region {i} {key} is {_h(value)}, the bundle declares "
                               f"{want.get(key)}")
            if want.get("len") != have.length:
                out.append(f"region {i} len is {have.length}, the bundle declares "
                           f"{want.get('len')}")
    return out


def header_crc(image: bytes) -> str:
    """The image's ``hdr_crc`` as the board reports it (``0x`` + 8 hex), or "" if unparsable."""
    try:
        return _h(parse(image).header_crc32)
    except S0lbError:
        return ""
