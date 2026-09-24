#!/usr/bin/env python3
"""Spike (lane KIT-GUIDE): receipt -> overlay triple -> Harness Manager's catalogue.

    pack_receipt.py RECEIPT.json OUT_ROOT [--store DIR]

Reads the build receipt that build_rm.tcl writes, refuses it unless every gate
passed and the files still match the CRCs it recorded, then writes
``OUT_ROOT/<rm_name>/{manifest.json, <rm_name>.bin, <rm_name>_clear.bin[, .ltx]}``
in the schema of ``overlay-manifest.md`` (the fields ``fpga/dfx/gen_manifest.py
build`` writes). ``static_id`` and ``rm_id`` come from the receipt: the CRC of
the DCP the build opened and the id read out of the netlist, never typed.

With ``--store`` it imports the triple through ``overlays.import_overlay``
(which re-validates length and CRC) into a content store there, and lists the
catalogue the Program page would show.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zlib
from pathlib import Path


def crc32(path: Path) -> str:
    return f"0x{zlib.crc32(path.read_bytes()) & 0xFFFFFFFF:08x}"


def pack(receipt_path: Path, out_root: Path) -> Path:
    r = json.loads(receipt_path.read_text(encoding="utf-8"))
    problems = []
    if r.get("schema") != "harness-manager-rm-build":
        problems.append(f"not a build receipt (schema {r.get('schema')!r})")
    if r.get("state") != "passed":
        problems.append(f"the build did not pass (state {r.get('state')!r}, stage {r.get('stage')!r})")
    failed = [g["gate"] for g in r.get("gates", []) if g.get("verdict") == "FAIL"]
    if failed:
        problems.append(f"failed gates: {failed}")
    if r.get("rm_id") != r.get("rm_id_netlist"):
        problems.append(f"rm_id {r.get('rm_id')} != netlist {r.get('rm_id_netlist')}")
    base = receipt_path.parent
    files = {}
    for role in ("partial", "clearing"):
        f = base / str(r.get(f"{role}_bin", ""))
        if not r.get(f"{role}_bin") or not f.is_file():
            problems.append(f"{role} missing: {f}")
            continue
        if crc32(f).lower() != str(r.get(f"{role}_crc32", "")).lower():
            problems.append(f"{role} {f.name} crc {crc32(f)} != receipt {r.get(f'{role}_crc32')}")
        if f.stat().st_size != int(r.get(f"{role}_len", -1)):
            problems.append(f"{role} {f.name} is {f.stat().st_size} B, receipt says {r.get(f'{role}_len')}")
        files[role] = f
    if problems:
        raise SystemExit("REFUSED: " + "; ".join(problems))

    name = r["rm_name"]
    d = out_root / name
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(files["partial"], d / f"{name}.bin")
    shutil.copyfile(files["clearing"], d / f"{name}_clear.bin")
    manifest = {
        "schema": 1,
        "static_id": r["static_id"],
        "rm_id": r["rm_id"],
        "rm_name": name,
        "clearing": {"file": f"{name}_clear.bin", "len": int(r["clearing_len"]),
                     "crc32": r["clearing_crc32"].lower()},
        "partial": {"file": f"{name}.bin", "len": int(r["partial_len"]),
                    "crc32": r["partial_crc32"].lower()},
        "built": r.get("built", "")[:10],
        "vivado": r.get("vivado"),
        "static_usercode": r.get("static_usercode"),
        "build_receipt": receipt_path.name,
    }
    if r.get("ltx"):
        shutil.copyfile(base / r["ltx"], d / f"{name}.ltx")
        manifest["ltx"] = f"{name}.ltx"
        manifest["ltx_crc32"] = r["ltx_crc32"].lower()
    shutil.copyfile(receipt_path, d / receipt_path.name)
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return d


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("receipt", type=Path)
    ap.add_argument("out_root", type=Path)
    ap.add_argument("--store", type=Path)
    a = ap.parse_args(argv)
    d = pack(a.receipt, a.out_root)
    print(f"overlay: {d}")
    from pyverify.overlay import Overlay
    sid = int(json.loads((d / "manifest.json").read_text())["static_id"], 16)
    Overlay.load(d).validate(expected_static_id=sid)
    print(f"pyverify Overlay.validate(expected_static_id=0x{sid:08X}): ok")
    if a.store:
        from harness_manager.services.store import ContentStore
        from harness_manager_mps3.overlays import OverlayCatalogue, import_overlay
        store = ContentStore(a.store)
        sha = import_overlay(store, d)
        cat = OverlayCatalogue(use_env=False, store=store)
        for e in cat.entries():
            print(f"catalogue: {e.ref.name} rm_id {e.ref.rm_id} static {e.ref.static_id} "
                  f"usercode {e.ref.static_usercode} {e.ref.size_bytes} B, pair "
                  f"{e.pair_check.value}: {e.pair_detail} (manifest blob {sha[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
