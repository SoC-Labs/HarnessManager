"""Build real stage0 S0LB v2 boot images for the tests (lane LINUX-SLOTS).

The layout is STAGE0_CONTRACT v1.2 §5 (``stage0_pack.py`` is the platform's one writer):
a 32-byte header ``<8I`` (magic "S0LB", version 2, num_entries, entry_pc, a0, a1, flags,
header_crc32), one ``<4I`` entry per region (src_offset, dst, len, crc32), then the regions.
``header_crc32`` covers the header (that field zeroed) and the entry table.

``linux_bundle_s0lb(image)`` is what ``linux_bundle.py`` records for it
(``check_image_overlay_match.s0lb_info``'s shape), for a channel component's ``s0lb``.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

MAGIC = 0x424C3053
DDR = 0x80000000


def make_s0lb(*regions: bytes, pc: int = DDR, a0: int = 0, a1: int = 0,
              dsts: tuple[int, ...] = ()) -> bytes:
    """A valid S0LB v2 image carrying ``regions`` (default: one 4 KiB region)."""
    if not regions:
        regions = (bytes(range(256)) * 16,)
    n = len(regions)
    off = 32 + 16 * n
    entries = b""
    body = b""
    for i, r in enumerate(regions):
        dst = dsts[i] if i < len(dsts) else DDR + 0x100000 * i
        entries += struct.pack("<4I", off + len(body), dst, len(r), zlib.crc32(r) & 0xFFFFFFFF)
        body += r
    hdr0 = struct.pack("<8I", MAGIC, 2, n, pc, a0, a1, 0, 0)
    crc = zlib.crc32(hdr0 + entries) & 0xFFFFFFFF
    return struct.pack("<8I", MAGIC, 2, n, pc, a0, a1, 0, crc) + entries + body


def header_crc(image: bytes) -> str:
    return f"0x{struct.unpack_from('<I', image, 28)[0]:08x}"


def linux_bundle_s0lb(image: bytes) -> dict[str, Any]:
    """``targets.ethernet.slot_image.s0lb`` as ``linux_bundle.py`` writes it (upper hex)."""
    magic, ver, n, pc, a0, a1, _f, hcrc = struct.unpack_from("<8I", image, 0)
    regions = []
    for i in range(n):
        _src, dst, ln, crc = struct.unpack_from("<4I", image, 32 + 16 * i)
        regions.append({"dst": f"0x{dst:08X}", "len": ln, "crc32": f"0x{crc:08X}"})
    return {"version": ver, "num_entries": n, "entry_pc": f"0x{pc:08X}",
            "entry_a0": f"0x{a0:08X}", "entry_a1": f"0x{a1:08X}",
            "header_crc32": f"0x{hcrc:08X}", "regions": regions}
