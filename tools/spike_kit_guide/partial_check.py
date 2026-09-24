#!/usr/bin/env python3
"""Spike (lane KIT-GUIDE): what Harness Manager can prove about a partial bitstream
with no Vivado and no board.

    partial_check.py PARTIAL [--clearing C] [--bin B] [--ref REF_PARTIAL]
                     [--part xcku115-flvb1760-1-c] [--clearing-max 262144] [--json]

PARTIAL and C may be a Vivado ``.bit`` (the ASCII header is read too) or the ICAP
``.bin`` that ``write_bitstream -bin_file`` writes beside it (the same bytes with
no header). ``--bin`` pairs a ``.bit`` with its ``.bin`` and proves they carry
one payload. ``--ref`` is a partial of the SAME static and partition, e.g. the
kit's greybox partial: every frame address the candidate writes must be inside
the reference's frame box.

How the packet stream is read. A configuration stream is a sync word
(0xAA995566) then packets: a Type-1 header names a register and a word count, a
Type-2 header carries a long word count for the register the Type-1 before it
named. The walker always skips a packet's payload by its count, so frame data
(megabytes of arbitrary words) is never read as headers: the failure mode that
``fpga/dfx/tools/bit_identity.py`` warns about is a walker that does not skip.
A stream it cannot walk to its end is reported as unparsed, never as clean.

Not wired into Harness Manager; the design doc (docs/design/DUT_BUILD_GUIDE.md,
"Validation before deploy") says where it would live.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SYNC = 0xAA995566
DUMMY = 0xFFFFFFFF
BUS_WIDTH = (0x000000BB, 0x11220044)

#: UltraScale configuration registers (UG570, "Configuration Registers").
REGS = {
    0x00: "CRC", 0x01: "FAR", 0x02: "FDRI", 0x03: "FDRO", 0x04: "CMD", 0x05: "CTL0",
    0x06: "MASK", 0x07: "STAT", 0x08: "LOUT", 0x09: "COR0", 0x0A: "MFWR", 0x0B: "CBC",
    0x0C: "IDCODE", 0x0D: "AXSS", 0x0E: "COR1", 0x10: "WBSTAR", 0x11: "TIMER",
    0x13: "RBCRC_SW", 0x16: "BOOTSTS", 0x18: "CTL1", 0x1F: "BSPI",
}
CMDS = {
    0x00: "NULL", 0x01: "WCFG", 0x02: "MFW", 0x03: "LFRM", 0x04: "RCFG", 0x05: "START",
    0x06: "RCAP", 0x07: "RCRC", 0x08: "AGHIGH", 0x09: "SWITCH", 0x0A: "GRESTORE",
    0x0B: "SHUTDOWN", 0x0C: "GCAPTURE", 0x0D: "DESYNC", 0x0F: "IPROG", 0x10: "CRCC",
    0x11: "LTIMER", 0x12: "BSPI_READ", 0x13: "FALL_EDGE",
}
#: Writes no partial or clearing of this platform makes (measured over 7 RMs on 3
#: statics, spike 2026-09-24): IPROG reboots the FPGA, AXSS/WBSTAR are the full
#: image's stamps, and register 0x1E passes a stream through to ANOTHER SLR (the
#: full image uses it for SLR1; the partition is in SLR0 only). SHUTDOWN, AGHIGH,
#: GRESTORE and COR0 are NOT on the list: every UltraScale partial or clearing
#: here writes them (the clearing asserts GHIGH_B while it blanks the region).
FORBIDDEN_CMDS = {"IPROG"}
FORBIDDEN_REGS = {"AXSS", "WBSTAR", "REG_0x1E"}

#: The two roles tell apart by their commands: a partial ends with GRESTORE +
#: START (it brings the region up), a clearing asserts AGHIGH and never starts.
ROLE_PARTIAL = {"GRESTORE", "START"}
ROLE_CLEARING = {"AGHIGH"}

#: Per-SLR IDCODEs of the parts the packs know, read out of the fielded full
#: image (config_rm_greybox.bit of 0x72BB0A36): SLR0 is the master and holds
#: the partition. Bits [31:28] are the silicon revision and are masked off.
IDCODES = {"xcku115": {"SLR0": 0x0390D093, "SLR1": 0x03902093}}
IDCODE_MASK = 0x0FFFFFFF

#: Block type 7 is not a frame: every stream here ends with FAR 0x03BE0000.
FAR_SENTINEL_BLOCK = 7


@dataclass
class Section:
    """One synchronised stream (an SSI device carries one per SLR)."""

    offset: int
    idcodes: list[int] = field(default_factory=list)
    fars: list[int] = field(default_factory=list)
    cmds: list[str] = field(default_factory=list)
    regs: dict[str, int] = field(default_factory=dict)
    fdri_words: int = 0
    mfwr_writes: int = 0
    crc_writes: int = 0
    axss: list[int] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    end: str = ""           # "desync" | "eof" | "error: ..."


def far_fields(far: int) -> tuple[int, int, int, int]:
    """(block type, row, column, minor) of an UltraScale frame address (UG570)."""
    return (far >> 23) & 0x7, (far >> 17) & 0x3F, (far >> 7) & 0x3FF, far & 0x7F


def read_header(data: bytes) -> tuple[dict[str, str], int]:
    """Parse a .bit file's header -> (fields, payload offset). ({}, 0) for a .bin."""
    if not data.startswith(b"\x00\x09\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00\x00\x01"):
        return {}, 0
    i = 13
    fields: dict[str, str] = {}
    while i < len(data):
        key = chr(data[i])
        i += 1
        if key == "e":
            (n,) = struct.unpack_from(">I", data, i)
            fields["e_len"] = str(n)
            return fields, i + 4
        (n,) = struct.unpack_from(">H", data, i)
        fields[key] = data[i + 2:i + 2 + n].rstrip(b"\x00").decode("ascii", "replace")
        i += 2 + n
    raise ValueError("truncated .bit header")


def walk(data: bytes, start: int = 0, depth: int = 0) -> list[Section]:
    """Every synchronised section of ``data`` (and of streams nested inside it)."""
    sections: list[Section] = []
    n = len(data) - len(data) % 4
    sync = struct.pack(">I", SYNC)
    i = start
    while True:
        i = data.find(sync, i)
        if i < 0 or i + 4 > n:
            break
        sec = Section(offset=i)
        i += 4
        last_reg = None
        while True:
            if i + 4 > n:
                sec.end = "eof"
                break
            (h,) = struct.unpack_from(">I", data, i)
            i += 4
            kind = h >> 29
            if kind == 1:
                op, reg, cnt = (h >> 27) & 3, (h >> 13) & 0x3FFF, h & 0x7FF
                last_reg = reg
            elif kind == 2:
                op, reg, cnt = (h >> 27) & 3, last_reg, h & 0x7FFFFFF
                if reg is None:
                    sec.end = f"error: Type-2 packet with no register at byte {i - 4}"
                    break
            else:
                sec.end = f"error: not a packet header 0x{h:08X} at byte {i - 4}"
                break
            if i + 4 * cnt > n:
                sec.end = f"error: packet at byte {i - 4} runs past the end ({cnt} words)"
                break
            words = struct.unpack_from(f">{cnt}I", data, i) if cnt and op == 2 and cnt <= 64 else ()
            payload_at = i
            i += 4 * cnt
            if op != 2:            # NOOP / read: nothing written
                continue
            name = REGS.get(reg, f"REG_0x{reg:02X}")
            sec.regs[name] = sec.regs.get(name, 0) + 1
            if name == "FDRI":
                sec.fdri_words += cnt
            elif name == "MFWR":
                sec.mfwr_writes += 1
            elif name == "CRC":
                sec.crc_writes += 1
            elif name == "FAR" and words:
                sec.fars.append(words[0])
            elif name == "IDCODE" and words:
                sec.idcodes.append(words[0])
            elif name == "AXSS" and words:
                sec.axss.append(words[0])
            elif name == "CMD" and words:
                sec.cmds.append(CMDS.get(words[0] & 0x1F, f"0x{words[0]:X}"))
                if words[0] & 0x1F == 0x0D:     # DESYNC
                    sec.end = "desync"
            elif name.startswith("REG_"):
                sec.unknown.append(f"{name} x{cnt}")
                # an SSI device passes the next SLR's whole stream through one register
                blob = data[payload_at:payload_at + 4 * cnt]
                if cnt > 16 and struct.pack(">I", SYNC) in blob:
                    for inner in walk(blob, 0, depth + 1):
                        inner.offset += payload_at
                        sections.append(inner)
            if sec.end == "desync":
                break
        sections.append(sec)
        if sec.end.startswith("error"):
            break
    sections.sort(key=lambda s: s.offset)
    return sections


def summarise(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    header, off = read_header(data)
    payload = data[off:]
    tokens = {}
    if header.get("a"):
        parts = header["a"].split(";")
        tokens["design"] = parts[0]
        for p in parts[1:]:
            k, _, v = p.partition("=")
            tokens[k] = v
    secs = walk(payload)
    fars = sorted({f for s in secs for f in s.fars if far_fields(f)[0] != FAR_SENTINEL_BLOCK})
    boxes = [far_fields(f) for f in fars]
    by_block: dict[str, Any] = {}
    for b in sorted({x[0] for x in boxes}):
        sel = [x for x in boxes if x[0] == b]
        by_block[str(b)] = {"rows": sorted({x[1] for x in sel}),
                            "columns": [min(x[2] for x in sel), max(x[2] for x in sel)]}
    cmds = sorted({c for s in secs for c in s.cmds})
    regs = sorted({r for s in secs for r in s.regs})
    role = ("partial" if ROLE_PARTIAL <= set(cmds) and not (ROLE_CLEARING & set(cmds))
            else "clearing" if ROLE_CLEARING <= set(cmds) and not (ROLE_PARTIAL & set(cmds))
            else "unknown")
    return {
        "file": str(path),
        "kind": "bit" if header else "bin",
        "bytes": len(data),
        "payload_bytes": len(payload),
        "sha256": hashlib.sha256(data).hexdigest(),
        "crc32": f"0x{zlib.crc32(data) & 0xFFFFFFFF:08x}",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "header": {"design": tokens.get("design"), "part": header.get("b"),
                   "date": header.get("c"), "time": header.get("d"),
                   "declared_len": int(header["e_len"]) if "e_len" in header else None,
                   "tokens": {k: v for k, v in tokens.items() if k != "design"}},
        "sections": [{
            "offset": s.offset, "idcodes": [f"0x{x:08X}" for x in s.idcodes],
            "far_count": len(s.fars), "fdri_words": s.fdri_words, "mfwr_writes": s.mfwr_writes,
            "crc_writes": s.crc_writes, "cmds": s.cmds, "regs": s.regs,
            "axss": [f"0x{x:08X}" for x in s.axss], "unknown": s.unknown, "end": s.end,
        } for s in secs],
        "frames": {
            "far_distinct": len(fars),
            "block_types": sorted({b[0] for b in boxes}),
            "rows": sorted({b[1] for b in boxes}),
            "columns": [min(b[2] for b in boxes), max(b[2] for b in boxes)] if boxes else [],
            "by_block": by_block,
        },
        "role": role,
        "cmds": cmds,
        "regs": regs,
        "_fars": fars,
    }


@dataclass
class Verdict:
    items: list[dict[str, str]] = field(default_factory=list)

    def add(self, check: str, state: str, detail: str) -> None:
        self.items.append({"check": check, "state": state, "detail": detail})

    @property
    def refused(self) -> bool:
        return any(i["state"] == "mismatch" for i in self.items)


def _inside(box: dict[str, Any], ref: dict[str, Any]) -> bool:
    """Every block type's rows and column range lie inside the reference's."""
    for blk, b in box.items():
        r = ref.get(blk)
        if r is None or not set(b["rows"]) <= set(r["rows"]):
            return False
        if not (r["columns"][0] <= b["columns"][0] and b["columns"][1] <= r["columns"][1]):
            return False
    return True


def _stream_checks(v: Verdict, s: dict[str, Any], role: str, part: str,
                   ref: dict[str, Any] | None) -> None:
    label = f"{role}: "
    h = s["header"]
    if s["kind"] == "bit":
        ok = h["declared_len"] == s["payload_bytes"]
        v.add(label + "bit_header_length", "ok" if ok else "mismatch",
              f"header declares {h['declared_len']} B, payload is {s['payload_bytes']} B")
        v.add(label + "part", "ok" if h["part"] == part else "mismatch", f"{h['part']} (want {part})")
        is_partial = h["tokens"].get("PARTIAL") == "TRUE"
        v.add(label + "partial_flag", "ok" if is_partial else "mismatch",
              "PARTIAL=TRUE" if is_partial else "not a partial bitstream: a full image "
              "reconfigures the whole FPGA, shell included")
    secs = s["sections"]
    unparsed = [x["end"] for x in secs if x["end"].startswith("error")]
    if not secs:
        v.add(label + "stream", "mismatch", "no sync word: not a configuration stream")
    elif unparsed:
        v.add(label + "stream", "mismatch", f"packet walk failed: {unparsed[0]}")
    else:
        v.add(label + "stream", "ok", f"{len(secs)} sections, every packet walked to DESYNC, "
              f"{sum(x['fdri_words'] for x in secs)} FDRI words, {s['frames']['far_distinct']} frames")
    dev = part.split("-")[0]
    slrs = IDCODES.get(dev)
    ids = sorted({int(x, 16) for sec in secs for x in sec["idcodes"]})
    if slrs is None:
        v.add(label + "idcode", "unchecked", f"no IDCODE known for {dev}")
    else:
        names = [next((n for n, c in slrs.items() if (c & IDCODE_MASK) == (i & IDCODE_MASK)), None)
                 for i in ids]
        ok = bool(ids) and None not in names and len(set(names)) == 1
        v.add(label + "idcode", "ok" if ok else "mismatch",
              ", ".join(f"0x{i:08X}={n or 'not ' + dev}" for i, n in zip(ids, names, strict=True))
              + ("" if ok else " (a partial here writes exactly one SLR)"))
    bad = sorted((set(s["cmds"]) & FORBIDDEN_CMDS) | (set(s["regs"]) & FORBIDDEN_REGS))
    v.add(label + "device_global_writes", "mismatch" if bad else "ok",
          f"writes {bad}" if bad else "no IPROG, AXSS, WBSTAR or SLR pass-through")
    v.add(label + "role", "ok" if s["role"] == role else "mismatch",
          f"the commands say {s['role']!r}" + ("" if s["role"] == role else f", expected {role!r}"
                                                " (are the two files swapped?)"))
    if ref is not None:
        inside = _inside(s["frames"]["by_block"], ref["frames"]["by_block"])
        v.add(label + "frame_box", "ok" if inside else "mismatch",
              f"{s['frames']['by_block']} inside the reference's {ref['frames']['by_block']}"
              if inside else f"{s['frames']['by_block']} is OUTSIDE the reference's "
              f"{ref['frames']['by_block']}: another partition or another device")
        extra = sorted((set(s["cmds"]) - set(ref["cmds"])) | (set(s["regs"]) - set(ref["regs"])))
        v.add(label + "vocabulary", "mismatch" if extra else "ok",
              f"writes {extra}, which the reference {role} never does" if extra
              else f"commands and registers are a subset of the reference {role}'s")


def check(partial: Path, *, clearing: Path | None = None, bin_path: Path | None = None,
          ref: Path | None = None, ref_clearing: Path | None = None,
          part: str = "xcku115-flvb1760-1-c",
          clearing_max: int = 262144) -> tuple[Verdict, dict[str, Any]]:
    v = Verdict()
    s = summarise(partial)
    r = summarise(ref) if ref is not None else None
    facts: dict[str, Any] = {"partial": s}
    if r is not None:
        facts["ref"] = {k: r[k] for k in ("file", "frames", "header", "role")}
    _stream_checks(v, s, "partial", part, r)

    if bin_path is not None:
        b = summarise(bin_path)
        same = b["payload_sha256"] == s["payload_sha256"]
        v.add("bit_bin_pair", "ok" if same else "mismatch",
              f"{bin_path.name} {'carries' if same else 'does NOT carry'} the payload of {partial.name}")

    if clearing is not None:
        c = summarise(clearing)
        rc = summarise(ref_clearing) if ref_clearing is not None else None
        facts["clearing"] = {k: c[k] for k in ("file", "bytes", "frames", "crc32", "role")}
        _stream_checks(v, c, "clearing", part, rc)
        pay = c["payload_bytes"]
        v.add("clearing_fits", "ok" if pay <= clearing_max else "mismatch",
              f"clearing payload {pay} B of the {clearing_max} B the harness holds")
        inside = _inside(c["frames"]["by_block"], s["frames"]["by_block"])
        v.add("clearing_pairs_partial", "ok" if inside else "mismatch",
              f"the clearing's frames {c['frames']['by_block']} lie "
              f"{'inside' if inside else 'OUTSIDE'} the partial's {s['frames']['by_block']}")
    v.add("static_binding", "unchecked",
          "a partial carries no static identity: every -cell write says UserID=0XFFFFFFFF and "
          "the frame box is the same on every static of this partition; the build receipt "
          "(static_id = CRC-32 of the DCP the build opened) and pr_verify bind it")
    return v, facts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("partial", type=Path)
    ap.add_argument("--clearing", type=Path)
    ap.add_argument("--bin", dest="bin_path", type=Path)
    ap.add_argument("--ref", type=Path, help="a partial of the same partition (the kit's greybox)")
    ap.add_argument("--ref-clearing", type=Path, help="that partial's clearing")
    ap.add_argument("--part", default="xcku115-flvb1760-1-c")
    ap.add_argument("--clearing-max", type=int, default=262144)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    v, facts = check(a.partial, clearing=a.clearing, bin_path=a.bin_path, ref=a.ref,
                     ref_clearing=a.ref_clearing,
                     part=a.part, clearing_max=a.clearing_max)
    for f in (facts.get("partial"), facts.get("ref")):
        if isinstance(f, dict):
            f.pop("_fars", None)
    if a.json:
        print(json.dumps({"refused": v.refused, "checks": v.items, "facts": facts}, indent=1))
    else:
        for i in v.items:
            print(f"{i['state']:>9}  {i['check']:<32} {i['detail']}")
        print("REFUSED" if v.refused else "PASSED (unchecked is not a pass: see the list)")
    return 15 if v.refused else 0


if __name__ == "__main__":
    sys.exit(main())
