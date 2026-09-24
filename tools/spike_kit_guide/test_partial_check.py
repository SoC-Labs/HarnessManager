"""Synthetic golden cases for the spike partial validator (lane KIT-GUIDE).

Not collected by ``make check`` (testpaths = tests). Run explicitly:

    .venv/bin/python -m pytest tools/spike_kit_guide/test_partial_check.py -q

The streams are built from the packet rules only (sync word, Type-1/Type-2
headers), with the shapes measured on the real partials: a partial issues
GRESTORE+START, a clearing AGHIGH, both write IDCODE 0x0390D093 (KU115 SLR0) and
end at FAR 0x03BE0000. The load-bearing case is ``test_frame_data_is_skipped``:
FDRI payload stuffed with words that LOOK like headers (the sync word, an AXSS
write, IPROG) must be skipped by count, never parsed.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import partial_check as pc  # noqa: E402

SLR0 = 0x0390D093
SLR1 = 0x03902093
CMD = {v: k for k, v in pc.CMDS.items()}
REG = {v: k for k, v in pc.REGS.items()}


def t1(reg: int, words: list[int]) -> list[int]:
    return [(1 << 29) | (2 << 27) | (reg << 13) | len(words), *words]


def t2(words: list[int]) -> list[int]:
    return [(2 << 29) | (2 << 27) | len(words), *words]


def far(block: int, row: int, col: int, minor: int = 0) -> int:
    return (block << 23) | (row << 17) | (col << 7) | minor


def stream(*, idcode: int = SLR0, cmds_before=("RCRC",), cmds_after=("GRESTORE", "START"),
           fars=((0, 0, 100), (0, 1, 150), (1, 0, 8)), fdri: list[int] | None = None,
           extra: list[int] | None = None) -> bytes:
    w = [pc.DUMMY] * 8 + [0x000000BB, 0x11220044, pc.DUMMY, pc.DUMMY, pc.SYNC, 0x20000000]
    for c in cmds_before:
        w += t1(REG["CMD"], [CMD[c]])
    w += t1(REG["IDCODE"], [idcode])
    for b, r, c in fars:
        w += t1(REG["FAR"], [far(b, r, c)])
        w += t1(REG["FDRI"], []) + t2(fdri if fdri is not None else [0x12345678] * 93)
    w += extra or []
    for c in cmds_after:
        w += t1(REG["CMD"], [CMD[c]])
    w += t1(REG["FAR"], [0x03BE0000])
    w += t1(REG["CMD"], [CMD["DESYNC"]]) + [0x20000000] * 4
    return struct.pack(f">{len(w)}I", *w)


def write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


def states(v: pc.Verdict) -> dict[str, str]:
    return {i["check"]: i["state"] for i in v.items}


def test_partial_and_clearing_pass(tmp_path):
    part = write(tmp_path, "p.bin", stream())
    clr = write(tmp_path, "c.bin", stream(cmds_before=("RCRC", "AGHIGH"), cmds_after=(),
                                          fars=((0, 0, 110), (0, 1, 140))))
    v, facts = pc.check(part, clearing=clr, ref=part, ref_clearing=clr)
    assert not v.refused, v.items
    s = states(v)
    assert s["partial: role"] == "ok" and s["clearing: role"] == "ok"
    assert s["static_binding"] == "unchecked"
    assert facts["partial"]["frames"]["by_block"]["0"] == {"rows": [0, 1], "columns": [100, 150]}


def test_frame_data_is_skipped(tmp_path):
    poison = [pc.SYNC, (1 << 29) | (2 << 27) | (0x0D << 13) | 1, 0xDEADBEEF,
              *t1(REG["CMD"], [CMD["IPROG"]])] * 20
    part = write(tmp_path, "p.bin", stream(fdri=poison))
    v, facts = pc.check(part)
    s = states(v)
    assert s["partial: stream"] == "ok"
    assert s["partial: device_global_writes"] == "ok", "frame data was parsed as packets"
    assert facts["partial"]["sections"][0]["axss"] == []


def test_swapped_roles_refused(tmp_path):
    part = write(tmp_path, "p.bin", stream())
    clr = write(tmp_path, "c.bin", stream(cmds_before=("RCRC", "AGHIGH"), cmds_after=()))
    v, _ = pc.check(clr, clearing=part)
    s = states(v)
    assert v.refused
    assert s["partial: role"] == "mismatch" and s["clearing: role"] == "mismatch"


def test_truncated_refused(tmp_path):
    data = stream()
    part = write(tmp_path, "p.bin", data[: len(data) // 2])
    v, _ = pc.check(part)
    assert states(v)["partial: stream"] == "mismatch"


def test_other_slr_and_device_global_writes_refused(tmp_path):
    part = write(tmp_path, "p.bin", stream(idcode=SLR1, extra=t1(REG["AXSS"], [0x01000001])))
    v, _ = pc.check(part)
    s = states(v)
    assert s["partial: idcode"] == "ok"                 # SLR1 is still the KU115...
    assert s["partial: device_global_writes"] == "mismatch"   # ...but AXSS is never a partial's
    other = write(tmp_path, "o.bin", stream(idcode=0x04B31093))
    assert states(pc.check(other)[0])["partial: idcode"] == "mismatch"


def test_frame_box_outside_reference_refused(tmp_path):
    ref = write(tmp_path, "r.bin", stream())
    part = write(tmp_path, "p.bin", stream(fars=((0, 2, 100), (0, 1, 150))))
    v, _ = pc.check(part, ref=ref)
    assert states(v)["partial: frame_box"] == "mismatch"


def test_bit_header_and_bin_pair(tmp_path):
    payload = stream()
    a = b"shell_top;UserID=0XFFFFFFFF;PARTIAL=TRUE;COMPRESS=TRUE;Version=2024.1\x00"
    hdr = (b"\x00\x09\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00\x00\x01"
           + b"a" + struct.pack(">H", len(a)) + a
           + b"b" + struct.pack(">H", 21) + b"xcku115-flvb1760-1-c\x00"
           + b"c" + struct.pack(">H", 11) + b"2026/09/24\x00"
           + b"d" + struct.pack(">H", 9) + b"12:00:00\x00"
           + b"e" + struct.pack(">I", len(payload)))
    bit = write(tmp_path, "p.bit", hdr + payload)
    good = write(tmp_path, "p.bin", payload)
    bad = write(tmp_path, "q.bin", payload[:-4] + b"\x00\x00\x00\x01")
    v, facts = pc.check(bit, bin_path=good)
    assert not v.refused, v.items
    assert facts["partial"]["header"]["tokens"]["Version"] == "2024.1"
    assert states(pc.check(bit, bin_path=bad)[0])["bit_bin_pair"] == "mismatch"
    full = write(tmp_path, "f.bit", hdr.replace(b"PARTIAL=TRUE", b"PARTIAL=FALS") + payload)
    assert states(pc.check(full)[0])["partial: partial_flag"] == "mismatch"


def test_clearing_over_arena_refused(tmp_path):
    part = write(tmp_path, "p.bin", stream())
    clr = write(tmp_path, "c.bin", stream(cmds_before=("RCRC", "AGHIGH"), cmds_after=(),
                                          fars=((0, 0, 110),)))
    v, _ = pc.check(part, clearing=clr, clearing_max=64)
    assert states(v)["clearing_fits"] == "mismatch"
