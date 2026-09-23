"""Team T2 test helpers: synthetic overlay triples, a non-windowed firmware profile, a store.

The overlay builder follows pyverify's own ``tests/test_e2e_deploy.py``
``make_synthetic_overlay``: the manifest has gen_manifest.py's shape (same keys,
same contract file names, "0x"-hex crc32 strings). Payloads are tiny and word
aligned. ``*.bin`` is gitignored here, so every triple is built in ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import os
import zlib
from dataclasses import replace
from pathlib import Path

from socharness.core.pack import OverlayRef
from tests.fakes.virtual_board import FIELDED_3F1A560F, FirmwareProfile, VirtualMps3

FIELDED_STATIC_ID = FIELDED_3F1A560F.static_id            # 0x3F1A560F
OTHER_STATIC_ID = 0xDEADBEEF                              # a shell that is not on the board
FIELDED_USERCODE = 0xD46FCDCB                              # fpga/dfx/overlay/*/manifest.json

#: Synthetic designs sit OUTSIDE fpga/dfx/rm_list.tcl's allocated range, like
#: pyverify's SYNTHETIC_RM_ID (0x0100_7A57), so a fixture never poses as a real design.
SYNTH_RM_ID = 0x0100_7A57
SYNTH2_RM_ID = 0x0100_7A58
GREYBOX_RM_ID = 0x0000_0000

CLEARING = b"\xC1\x0E\xA2\x00" * 32      # 128 B
PARTIAL = b"\xB1\x75\x00\x0D" * 96       # 384 B

#: The fielded firmware minus "windowed": the pre-2026-07-17 push mode (tftp).
NON_WINDOWED = replace(
    FIELDED_3F1A560F,
    name="fielded-0x3F1A560F-without-windowed",
    features=tuple(f for f in FIELDED_3F1A560F.features if f != "windowed"),
)


def make_overlay(
    root: Path,
    rm_name: str = "synth",
    *,
    rm_id: int = SYNTH_RM_ID,
    static_id: int = FIELDED_STATIC_ID,
    clearing: bytes = CLEARING,
    partial: bytes = PARTIAL,
    static_usercode: int | None = None,
    ip_class: str | None = None,
    corrupt_partial: bool = False,
    clearing_name: str | None = None,
) -> Path:
    """Write ``<root>/<rm_name>/{manifest.json, <rm>.bin, <rm>_clear.bin}``; return the dir.

    ``corrupt_partial`` flips one payload byte AFTER the manifest records the
    good CRC: the length still matches, the CRC does not.
    """
    directory = root / rm_name
    directory.mkdir(parents=True, exist_ok=True)
    partial_name = f"{rm_name}.bin"
    clearing_name = clearing_name or f"{rm_name}_clear.bin"
    (directory / clearing_name).write_bytes(clearing)
    (directory / partial_name).write_bytes(partial)
    manifest: dict = {
        "schema": 1,
        "static_id": f"0x{static_id:08X}",
        "rm_id": f"0x{rm_id:08x}",
        "rm_name": rm_name,
        "clearing": {"file": clearing_name, "len": len(clearing),
                     "crc32": f"0x{zlib.crc32(clearing) & 0xFFFFFFFF:08x}"},
        "partial": {"file": partial_name, "len": len(partial),
                    "crc32": f"0x{zlib.crc32(partial) & 0xFFFFFFFF:08x}"},
        "built": "2026-09-23",
        "vivado": "2024.1",
    }
    if static_usercode is not None:
        manifest["static_usercode"] = f"0x{static_usercode:08X}"
    if ip_class is not None:
        manifest["ip_class"] = ip_class
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if corrupt_partial:
        bad = bytearray(partial)
        bad[0] ^= 0xFF
        (directory / partial_name).write_bytes(bytes(bad))
    return directory


def ref_named(refs: list[OverlayRef], name: str) -> OverlayRef:
    matches = [r for r in refs if r.name == name]
    assert len(matches) == 1, f"expected one overlay named {name!r}, got {matches}"
    return matches[0]


def point_pushes_at(monkeypatch, vb: VirtualMps3) -> None:
    """Aim the adapter's push ports at the virtual board's ephemeral FakeShell ports."""
    monkeypatch.setenv("SOCHARNESS_MPS3_PUSH_PORT", str(vb.shell.raw_tcp_port))
    monkeypatch.setenv("SOCHARNESS_MPS3_TFTP_PORT", str(vb.shell.tftp_port))


def use_overlay_dirs(monkeypatch, *dirs: Path) -> None:
    monkeypatch.setenv("SOCHARNESS_MPS3_OVERLAY_DIRS", os.pathsep.join(str(d) for d in dirs))


def profile(name: str, **changes) -> FirmwareProfile:
    return replace(FIELDED_3F1A560F, name=name, **changes)


class FakeStore:
    """The ``ContentStore`` protocol, minimally: sha256-addressed blobs with meta, in a dir."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index: dict[str, tuple[str, dict[str, str]]] = {}

    def put_bytes(self, data: bytes, *, kind: str, meta: dict[str, str]) -> str:
        sha = hashlib.sha256(data).hexdigest()
        self.path(sha).write_bytes(data)
        self.index[sha] = (kind, dict(meta))
        return sha

    def put_file(self, path: Path, *, kind: str, meta: dict[str, str]) -> str:
        return self.put_bytes(Path(path).read_bytes(), kind=kind, meta=meta)

    def path(self, sha256: str) -> Path:
        return self.root / sha256

    def verify(self, sha256: str) -> bool:
        return hashlib.sha256(self.path(sha256).read_bytes()).hexdigest() == sha256

    def find(self, kind: str, **meta: str) -> list[tuple[str, dict[str, str]]]:
        return [(sha, dict(m)) for sha, (k, m) in self.index.items()
                if k == kind and all(m.get(key) == val for key, val in meta.items())]
