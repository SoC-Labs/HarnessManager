"""The Xilinx ``.bit`` file header: design name, USERID, part, date, payload length.

Layout (Vivado ``write_bitstream``; read the same way by ``fpga/dfx/tools/bit_identity.py``)::

    00 09  0f f0 0f f0 0f f0 0f f0 00     magic field
    00 01                                 length of the next key (1)
    'a' u16 len  "design;UserID=0XD46FCDCB;Version=2024.1\\0"
    'b' u16 len  "xcku115-flvb2104-2-e\\0"   part
    'c' u16 len  "2026/09/23\\0"            date
    'd' u16 len  "13:16:00\\0"              time
    'e' u32 len  <configuration data>

The USERID in the header is what ``BITSTREAM.CONFIG.USERID`` stamped: the
``static_usercode`` every overlay built against this static must carry. An
unstamped bitstream says ``UserID=0XFFFFFFFF``.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = bytes.fromhex("0ff00ff00ff00ff000")
UNSTAMPED_USERID = "0xffffffff"
_USERID_RE = re.compile(r"UserID=0[xX]([0-9A-Fa-f]{1,8})")


class BitHeaderError(ValueError):
    """Not a Xilinx .bit file, or a truncated one."""


@dataclass(frozen=True)
class BitHeader:
    design: str
    part: str
    date: str
    time: str
    data_len: int
    data_offset: int
    userid: str = ""          # "0xd46fcdcb", or "" when the header carries none

    @property
    def stamped(self) -> bool:
        return bool(self.userid) and self.userid != UNSTAMPED_USERID

    def part_matches(self, want: str) -> bool:
        """``want`` is a device ("xcku115") or a full part; compared case-insensitively."""
        return bool(want) and self.part.lower().startswith(want.lower())


def parse_bit_header(data: bytes, *, need_data: bool = True) -> BitHeader:
    """Parse a .bit header. ``need_data=False`` accepts a prefix of the file (the header only)."""
    try:
        (n,) = struct.unpack_from(">H", data, 0)
        if n != len(MAGIC) or data[2:2 + n] != MAGIC:
            raise BitHeaderError("no Xilinx .bit magic")
        pos = 2 + n
        (klen,) = struct.unpack_from(">H", data, pos)
        pos += 2
        if klen != 1:
            raise BitHeaderError("unexpected .bit header layout")
        fields: dict[str, str] = {}
        while True:
            key = chr(data[pos])
            pos += 1
            if key == "e":
                (dlen,) = struct.unpack_from(">I", data, pos)
                pos += 4
                if need_data and pos + dlen > len(data):
                    raise BitHeaderError(f"truncated: the header declares {dlen} bytes of data, "
                                         f"the file has {len(data) - pos}")
                design = fields.get("a", "")
                m = _USERID_RE.search(design)
                return BitHeader(design=design, part=fields.get("b", ""),
                                 date=fields.get("c", ""), time=fields.get("d", ""),
                                 data_len=dlen, data_offset=pos,
                                 userid=f"0x{int(m.group(1), 16):08x}" if m else "")
            if key not in "abcd":
                raise BitHeaderError(f"unexpected .bit header key {key!r}")
            (flen,) = struct.unpack_from(">H", data, pos)
            pos += 2
            raw = data[pos:pos + flen]
            if len(raw) != flen:
                raise BitHeaderError("truncated .bit header field")
            fields[key] = raw.rstrip(b"\x00").decode("ascii", "replace")
            pos += flen
    except (struct.error, IndexError):
        raise BitHeaderError("truncated .bit header") from None


def read_bit_header(path: Path) -> BitHeader:
    """The header of a .bit file on disk, checked against the file's real length."""
    with open(path, "rb") as fh:
        hdr = parse_bit_header(fh.read(4096), need_data=False)
    size = Path(path).stat().st_size
    if hdr.data_offset + hdr.data_len != size:
        raise BitHeaderError(f"{Path(path).name}: the header declares {hdr.data_len} bytes of "
                             f"data but the file holds {size - hdr.data_offset}")
    return hdr


def build_bit(design: str, part: str, data: bytes, *, userid: str | None = None,
              date: str = "2026/09/23", time: str = "12:00:00") -> bytes:
    """A syntactically real .bit (tests and fixtures). ``userid`` like "0xD46FCDCB"."""
    name = design
    if userid is not None:
        name = f"{design};UserID=0X{int(userid, 16):08X};Version=2024.1"

    def field_(key: str, text: str) -> bytes:
        raw = text.encode("ascii") + b"\x00"
        return key.encode("ascii") + struct.pack(">H", len(raw)) + raw

    return (struct.pack(">H", len(MAGIC)) + MAGIC + struct.pack(">H", 1)
            + field_("a", name) + field_("b", part) + field_("c", date) + field_("d", time)
            + b"e" + struct.pack(">I", len(data)) + data)
