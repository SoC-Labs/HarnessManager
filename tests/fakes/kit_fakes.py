"""Fakes for the DUT build kit (KIT-CORE): a fixture kit, loose mint files, synthetic
bitstreams, build receipts and a fake Vivado. Nothing here runs Vivado.

- ``fake_dcp(static_id)``: a tiny zip with a real-shaped ``dcp.xml`` whose zlib CRC-32 IS
  ``static_id`` (a 4-byte zip comment is solved for it: CRC-32 is affine over GF(2), so 32
  probes and a Gaussian elimination give the bytes). It is not a checkpoint; Vivado would
  refuse it. Only the CRC, the size and ``dcp.xml`` are real-shaped.
- ``FIXTURE``: the committed fixture kit ``tests/fakes/kit_fixture/0x72BB0A36/`` (kit.json,
  the fake DCP, the stamp). ``build_fixture`` regenerates it byte for byte, and a test
  holds it to that. Its ``rp.frames`` are the facts measured on the fielded 0x72BB0A36
  pairs (docs/design/DUT_BUILD_GUIDE.md §4).
- ``stream(...)``: a synthetic configuration stream with the shapes measured on the real
  partials (a partial: GRESTORE+START; a clearing: AGHIGH; IDCODE 0x0390D093; the FAR
  0x03BE0000 sentinel).
- ``passed_build(dir, ...)``: what ``build_rm.tcl`` leaves in ``out/`` after a pass.
"""

from __future__ import annotations

import json
import struct
import subprocess
import zipfile
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from harness_manager.services.kit.schema import DEFAULT_LICENCE_NOTE, hex32, sha256_file
from harness_manager_mps3 import bitcheck as bc

HERE = Path(__file__).resolve().parent
FIXTURE_ROOT = HERE / "kit_fixture"
STATIC_ID = "0x72BB0A36"
USERCODE = "0xC8551081"
FIXTURE = FIXTURE_ROOT / STATIC_ID
SPIKE_RM = FIXTURE_ROOT / "spike_rm.sv"

SLR0 = 0x0390D093
SLR1 = 0x03902093
CMD = {v: k for k, v in bc.CMDS.items()}
REG = {v: k for k, v in bc.REGS.items()}

#: rp.frames of 0x72BB0A36, measured on its fielded dbg_demo pair (and identical on the
#: nanosoc_ila pair) with harness_manager_mps3.bitcheck.frames_of_pair.
FRAMES_72BB0A36: dict[str, Any] = {
    "idcode": "0x0390D093",
    "partial": {"by_block": {"0": {"rows": [0, 1], "columns": [94, 200]},
                             "1": {"rows": [0, 1], "columns": [6, 11]}},
                "cmds": ["DESYNC", "GRESTORE", "LFRM", "MFW", "NULL", "RCRC", "SHUTDOWN",
                         "START", "WCFG"],
                "regs": ["CMD", "COR0", "CRC", "CTL0", "CTL1", "FAR", "FDRI", "IDCODE", "MASK",
                         "MFWR"]},
    "clearing": {"by_block": {"0": {"rows": [0, 1], "columns": [101, 193]}},
                 "cmds": ["AGHIGH", "DESYNC", "MFW", "NULL", "RCRC", "SHUTDOWN", "WCFG"],
                 "regs": ["CMD", "COR0", "CRC", "CTL0", "CTL1", "FAR", "FDRI", "IDCODE", "MASK",
                          "MFWR"]},
    "from": ["config_rm_dbg_demo_pblock_rp_dut_partial.bin",
             "config_rm_dbg_demo_pblock_rp_dut_partial_clear.bin"],
}


# --- a fake DCP with a chosen CRC-32 ---------------------------------------------------------------


def forge_crc(prefix: bytes, target: int) -> bytes:
    """4 bytes that make ``zlib.crc32(prefix + them) == target``."""
    base = zlib.crc32(prefix + b"\0\0\0\0")
    pivots: dict[int, tuple[int, int]] = {}
    for bit in range(32):
        v, m = zlib.crc32(prefix + (1 << bit).to_bytes(4, "little")) ^ base, 1 << bit
        while v:
            h = v.bit_length() - 1
            if h not in pivots:
                pivots[h] = (v, m)
                break
            pv, pm = pivots[h]
            v, m = v ^ pv, m ^ pm
    v, m = (target & 0xFFFFFFFF) ^ base, 0
    while v:
        pv, pm = pivots[v.bit_length() - 1]
        v, m = v ^ pv, m ^ pm
    out = m.to_bytes(4, "little")
    assert zlib.crc32(prefix + out) == target & 0xFFFFFFFF
    return out


def dcp_xml(*, release: str = "2024.1", build: int = 5076996, cpver: int = 22,
            part: str = "xcku115-flvb1760-1-c", rp_inst: str = "u_rp_dut") -> str:
    return (f'<?xml version="1.0"?>\n<Checkpoint Version="{cpver}" Minor="0">\n'
            f'\t<BUILD_NUMBER Name="{build}"/>\n'
            f'\t<PRODUCT Name="Vivado v{release} (64-bit)"/>\n'
            f'\t<Part Name="{part}"/>\n\t<Top Name="shell_top"/>\n'
            f'\t<HDBlackboxInfo Name="{rp_inst} HD.RECONFIGURABLE"/>\n</Checkpoint>\n')


def fake_dcp(static_id: str | int = STATIC_ID, **xml: Any) -> bytes:
    """A zip whose CRC-32 is ``static_id`` (the forged bytes are its 4-byte comment)."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        info = zipfile.ZipInfo("dcp.xml", (2026, 9, 24, 0, 0, 0))
        zf.writestr(info, dcp_xml(**xml))
        info = zipfile.ZipInfo("README.txt", (2026, 9, 24, 0, 0, 0))
        zf.writestr(info, "FAKE: a Harness Manager test fixture, not a Vivado checkpoint.\n")
        zf.comment = b"\0\0\0\0"
    data = buf.getvalue()
    target = int(static_id, 0) if isinstance(static_id, str) else static_id
    return data[:-4] + forge_crc(data[:-4], target)


def stamp(static_id: str = STATIC_ID, usercode: str = USERCODE) -> dict[str, Any]:
    return {"schema": "mps3-static-stamp", "schema_version": "1",
            "generated_by": "tests/fakes/kit_fakes.py", "static_id": static_id,
            "stamped": True, "harness_version": "1.0.0", "usercode": usercode}


def _file(root: Path, rel: str, role: str, crc: str = "") -> dict[str, Any]:
    p = root / rel
    out: dict[str, Any] = {"path": rel, "role": role, "size": p.stat().st_size,
                           "sha256": sha256_file(p)}
    if crc:
        out["crc32"] = crc
    return out


def kit_doc(root: Path, static_id: str = STATIC_ID, *, release: str = "2024.1",
            build: int = 5076996, usercode: str = USERCODE, frames: bool = True,
            **over: Any) -> dict[str, Any]:
    doc = {
        "schema": "hm-rm-kit", "schema_version": 1,
        "board_type": "mps3", "part": "xcku115-flvb1760-1-c",
        "static_id": static_id, "static_usercode": usercode, "harness_impl": "bare-metal",
        "vivado": {"release": release, "build": build, "checkpoint_version": 22},
        "rp": {"inst": "u_rp_dut", "pblock": "pblock_rp_dut", "ports": 47, "bits": 148,
               "clr_max": 262144, **({"frames": FRAMES_72BB0A36} if frames else {})},
        "pr_verify_ref": "static/static_routed_locked.dcp",
        "access": "public", "ip_class": "open", "licence_note": DEFAULT_LICENCE_NOTE,
        "files": [_file(root, "static/static_routed_locked.dcp", "locked_static", static_id),
                  _file(root, "static/static_stamp.json", "stamp")],
        "source": {"fixture": True},
        "generated_by": "tests/fakes/kit_fakes.py (a FAKE DCP: not a Vivado checkpoint)",
    }
    doc.update(over)
    return doc


def build_fixture(dest: Path, static_id: str = STATIC_ID, *, release: str = "2024.1",
                  usercode: str = USERCODE, frames: bool = True, **over: Any) -> Path:
    """A kit directory: kit.json + static/{static_routed_locked.dcp, static_stamp.json}."""
    dest = Path(dest)
    (dest / "static").mkdir(parents=True, exist_ok=True)
    (dest / "static" / "static_routed_locked.dcp").write_bytes(
        fake_dcp(static_id, release=release))
    (dest / "static" / "static_stamp.json").write_text(
        json.dumps(stamp(static_id, usercode), indent=2) + "\n", encoding="utf-8")
    doc = kit_doc(dest, static_id, release=release, usercode=usercode, frames=frames, **over)
    (dest / "kit.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                                   encoding="utf-8")
    return dest


def fielded_dir(dest: Path, static_id: str = STATIC_ID, *, release: str = "2024.1",
                pair: bool = True, static_id_txt: str | None = None) -> Path:
    """Loose mint files, as ``fielded/<sid>/`` holds them after ``fetch_fielded.sh``."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "static_routed_locked.dcp").write_bytes(fake_dcp(static_id, release=release))
    (dest / "static_stamp.json").write_text(json.dumps(stamp(static_id)) + "\n", encoding="utf-8")
    (dest / "static_id.txt").write_text((static_id_txt or static_id) + "\n", encoding="utf-8")
    (dest / "mint.json").write_text(json.dumps({
        "schema": "mps3-mint-record", "static_id": static_id,
        "tools": {"vivado": {"reason": None, "value": release}},
        "sources": {"repo": {"reason": None, "value": {"sha": "c855108" + "0" * 33,
                                                       "dirty": True}}}}) + "\n",
        encoding="utf-8")
    (dest / "config_rm_greybox.bit").write_bytes(b"not in the kit")
    if pair:
        (dest / "config_rm_x_pblock_rp_dut_partial.bin").write_bytes(stream())
        (dest / "config_rm_x_pblock_rp_dut_partial_clear.bin").write_bytes(clearing_stream())
    return dest


# --- synthetic configuration streams ---------------------------------------------------------------


def t1(reg: int, words: list[int]) -> list[int]:
    return [(1 << 29) | (2 << 27) | (reg << 13) | len(words), *words]


def t2(words: list[int]) -> list[int]:
    return [(2 << 29) | (2 << 27) | len(words), *words]


def far(block: int, row: int, col: int, minor: int = 0) -> int:
    return (block << 23) | (row << 17) | (col << 7) | minor


def stream(*, idcode: int = SLR0, cmds_before=("RCRC",), cmds_after=("GRESTORE", "START"),
           fars=((0, 0, 100), (0, 1, 150), (1, 0, 8)), fdri: list[int] | None = None,
           extra: list[int] | None = None) -> bytes:
    w = [bc.DUMMY] * 8 + [0x000000BB, 0x11220044, bc.DUMMY, bc.DUMMY, bc.SYNC, 0x20000000]
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


def clearing_stream(**kw: Any) -> bytes:
    kw.setdefault("cmds_before", ("RCRC", "AGHIGH"))
    kw.setdefault("cmds_after", ())
    kw.setdefault("fars", ((0, 0, 110), (0, 1, 140)))
    return stream(**kw)


def bit_file(payload: bytes, *, partial: bool = True, part: str = "xcku115-flvb1760-1-c",
             version: str = "2024.1") -> bytes:
    flag = b"PARTIAL=TRUE" if partial else b"PARTIAL=FALS"
    a = b"shell_top;UserID=0XFFFFFFFF;" + flag + b";COMPRESS=TRUE;Version=" + version.encode() \
        + b"\x00"
    b = part.encode() + b"\x00"
    return (b"\x00\x09\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00\x00\x01"
            + b"a" + struct.pack(">H", len(a)) + a
            + b"b" + struct.pack(">H", len(b)) + b
            + b"c" + struct.pack(">H", 11) + b"2026/09/24\x00"
            + b"d" + struct.pack(">H", 9) + b"12:00:00\x00"
            + b"e" + struct.pack(">I", len(payload)) + payload)


#: The golden bitstreams: name -> bytes, a few KB in all. Regenerated by the tests (*.bit and
#: *.bin never go in git) and pinned by sha256 in tests/fixtures/kit/bitstreams.sha256.
def golden_bitstreams() -> dict[str, bytes]:
    p, c = stream(), clearing_stream()
    return {
        "good_partial.bin": p,
        "good_partial_clear.bin": c,
        "good_partial.bit": bit_file(p),
        "full_image.bit": bit_file(stream(extra=t1(REG["AXSS"], [0x01000001])
                                          + t1(REG["CMD"], [CMD["IPROG"]])), partial=False),
        "truncated_partial.bin": p[: len(p) // 2],
        "wrong_part.bit": bit_file(p, part="xcvu9p-flga2104-2-i"),
        "other_device_partial.bin": stream(idcode=0x04B31093),
        "other_partition_partial.bin": stream(fars=((0, 2, 100), (0, 1, 150))),
    }


# --- a build, as build_rm.tcl leaves it -------------------------------------------------------------


def crc(data: bytes) -> str:
    return hex32(zlib.crc32(data))


def passed_build(build_dir: Path, *, name: str = "spike_rm", rm_id: str = "0x010080F0",
                 static_id: str = STATIC_ID, usercode: str = USERCODE, state: str = "passed",
                 netlist_rm_id: str | None = None, partial: bytes | None = None,
                 clearing: bytes | None = None, ltx: bytes | None = None,
                 gates: list[dict[str, str]] | None = None, **fields: str) -> Path:
    """``<build_dir>/out/<name>_build.json`` and the pair it names. Returns the receipt."""
    out = Path(build_dir) / "out"
    out.mkdir(parents=True, exist_ok=True)
    partial = stream() if partial is None else partial
    clearing = clearing_stream() if clearing is None else clearing
    (out / f"{name}_partial.bin").write_bytes(partial)
    (out / f"{name}_partial_clear.bin").write_bytes(clearing)
    (out / f"{name}_partial.bit").write_bytes(bit_file(partial))
    (out / f"{name}_partial_clear.bit").write_bytes(bit_file(clearing))
    doc: dict[str, Any] = {
        "schema": "harness-manager-rm-build", "schema_version": 1,
        "state": state, "stage": "bitstream" if state == "passed" else "synth",
        "kit_id": f"mps3/{static_id}/vivado-2024.1",
        "rm_name": name, "rm_top": f"rm_{name}", "part": "xcku115-flvb1760-1-c",
        "rp_inst": "u_rp_dut", "vivado": "2024.1", "static_id": static_id,
        "static_usercode": usercode, "rm_id": rm_id,
        "rm_id_netlist": netlist_rm_id or rm_id, "rm_wns": "18.057", "rm_whs": "0.079",
        "pr_verify_ref": "static_routed_locked.dcp",
        "partial_bin": f"{name}_partial.bin", "partial_len": str(len(partial)),
        "partial_crc32": crc(partial),
        "clearing_bin": f"{name}_partial_clear.bin", "clearing_len": str(len(clearing)),
        "clearing_crc32": crc(clearing), "built": "2026-09-24T12:00:00Z",
        "gates": gates if gates is not None else [
            {"gate": "static_id", "verdict": "PASS", "detail": f"CRC-32 is {static_id}"},
            {"gate": "pr_verify", "verdict": "PASS", "detail": "compatible"}],
    }
    if ltx is not None:
        (out / f"{name}.ltx").write_bytes(ltx)
        doc["ltx"] = f"{name}.ltx"
        doc["ltx_crc32"] = crc(ltx)
    doc.update(fields)
    path = out / f"{name}_build.json"
    path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return path


# --- a fake vivado ------------------------------------------------------------------------------------


@dataclass
class FakeVivado:
    """A ``runner`` for ``vivado.discover``: answers ``-version`` like Vivado, runs nothing."""

    release: str = "2024.1"
    build: int = 5076996
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        out = (f"vivado v{self.release} (64-bit)\nTool Version Limit: 2024.05\n"
               f"SW Build {self.build} on Wed May 22 18:36:09 MDT 2024\n")
        return subprocess.CompletedProcess(argv, 0, out, "")


def fake_vivado_script(dest: Path, release: str = "2024.1", build: int = 5076996) -> Path:
    """An executable ``vivado`` that prints a version and exits: for the CLI and API tests
    (``HARNESS_MANAGER_VIVADO=<it>``). POSIX sh; the tests that use it skip on Windows."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    exe = dest / "vivado"
    exe.write_text("#!/bin/sh\n"
                   f"echo 'vivado v{release} (64-bit)'\n"
                   f"echo 'SW Build {build} on Wed May 22 18:36:09 MDT 2024'\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe
