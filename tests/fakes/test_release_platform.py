"""RELEASE-PIPE fixtures: a fake PLATFORM tree, shaped like what a finished mint leaves on disk.

(The file name matches the lane's ``tests/**/test_release_*`` ownership; it holds no tests.)

``linux_platform(root)`` writes, under ``root``:

- ``prod/``: ``linux_bundle.json`` (FLOW_CONTRACT v1.6 §0.1, the fields the release tool
  reads), ``config_rm_greybox_stage0.bit`` (a real Xilinx header with the UserID; its data is
  T7's fake identity, so a VirtualMps3 booting it reports that identity), ``linux_slot.img``
  (a real S0LB v2 table), ``linux_legal_info.tar`` (a real tar), and a stage0 RE-BAKE
  (``config_rm_greybox_stage0_generic.bit`` + ``stage0_bake_generic.json``, schema
  mps3-stage0-bake v1);
- ``templates/``: the four config-SD templates, shaped like fpga/mps3_sd/templates;
- ``overlay_mbv/``: overlay triples keyed to the static: open ones (``greybox``, ``led``),
  Arm-IP ones (``nanosoc``, ``eth_ss``: harness.AAA_RMS), and the stray
  ``mps3_shell_static_id.c`` the real directory has;
- ``kit/mps3_<sid>_kit.zip``: an RM kit with its ``kit.json`` inside.

``bare_metal_platform(root)`` writes the same with a ``mint.json`` + ``firmware.json`` and no
Linux parts. Nothing here touches a real device, the hub or the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from tests.fakes.s0lb_image import linux_bundle_s0lb, make_s0lb
from tests.fakes.t2_overlays import make_overlay
from tests.fakes.t7_bundles import fake_bit
from tests.fakes.virtual_board import PRODUCT_V011_FEATURES

S_LNX, U_LNX = "0x44EE76D5", "0xFB1F8C76"       # RC2's numbers (a fake of its shape)
S_ILA, U_ILA = "0x72BB0A36", "0xC8551081"
PART = "xcku115-flvb1760-1-c"
OPEN_RMS = {"greybox": 0x0000_0000, "led": 0x0100_001E}
AAA_RMS = {"nanosoc": 0x0100_0001, "eth_ss": 0x0100_0002}

CONFIG_TXT = ("TITLE: nanoSoC MPS3 (HBI0309) FPGA prototyping board configuration file\n"
              "[CONFIGURATION]\nAUTORUN: TRUE\nAUTORUNDELAY: 3\nUARTMODE: 1\n"
              "USB_REMOTE: TRUE              ;Allow a remote reboot over USB\n")
BOARD_TXT = ("BOARD: @BOARD@\nTITLE: nanoSoC motherboard configuration file\n[MCCS]\n"
             "MBBIOS: mbb_v141.ebf           ;STOCK Arm MCC firmware already on the SD\n"
             "[APPLICATION NOTE]\nAPPFILE: Nanosoc\\nanosoc.txt   ;DOS separator\n")
NANOSOC_TXT = ("BOARD: HBI0309\nTITLE: AN522 application note configuration file\n[FPGAS]\n"
               "TOTALFPGAS: 1\nF0FILE: nanosoc.bit        ;FPGA0 filename\nF0MODE: FPGA\n"
               "[OSCCLKS]\nOSC0: 25.0\nOSC1: 50.0\n")
IMAGES_TXT = "TITLE: nanoSoC MPS3 images configuration file\n[IMAGES]\nTOTALIMAGES: 0\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Platform:
    root: Path
    prod: Path
    templates: Path
    overlays: Path
    kit: Path
    bit: Path
    static_id: str
    usercode: str
    impl: str
    rebake_bit: Path | None = None
    rebake_record: Path | None = None
    fw_sha: str = ""


def templates(root: Path) -> Path:
    t = root / "templates"
    t.mkdir(parents=True, exist_ok=True)
    for name, text in (("config.txt", CONFIG_TXT), ("board.txt", BOARD_TXT),
                       ("nanosoc.txt", NANOSOC_TXT), ("images.txt", IMAGES_TXT)):
        (t / name).write_text(text, encoding="utf-8")
    return t


def overlay_dir(root: Path, static_id: str, usercode: str) -> Path:
    d = root / "overlay_mbv"
    for name, rm_id in {**OPEN_RMS, **AAA_RMS}.items():
        make_overlay(d, name, rm_id=rm_id, static_id=int(static_id, 16),
                     static_usercode=int(usercode, 16),
                     partial=(name.encode() * 64)[:384], clearing=(name.encode() * 32)[:128])
    (d / "mps3_shell_static_id.c").write_text("uint32_t mps3_shell_static_id(void);\n")
    return d


def overlay_records(d: Path) -> list[dict]:
    """``targets.ethernet.overlays`` as linux_bundle.py writes it."""
    out = []
    for man in sorted(d.glob("*/manifest.json")):
        m = json.loads(man.read_text())
        out.append({"rm_name": m["rm_name"], "rm_id": m["rm_id"], "static_id": m["static_id"],
                    "static_usercode": m.get("static_usercode"),
                    "partial_crc32": m["partial"]["crc32"],
                    "clearing_crc32": m["clearing"]["crc32"]})
    return out


def kit_zip(root: Path, static_id: str, *, access: str = "github-token",
            distribution: str = "INTERNAL-ONLY") -> Path:
    name = f"mps3_{static_id}_kit"
    meta = {"schema": "hm-rm-kit", "schema_version": 1, "static_id": static_id,
            "access": access, "distribution": distribution, "contains_amd_ip": True,
            "ip_class": "unknown", "vivado": {"release": "2026.1"},
            "source": {"dirty": False}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/kit.json", json.dumps(meta))
        zf.writestr(f"{name}/static/static_routed_locked.dcp", b"\0" * 4096)
    p = root / "kit" / f"{name}.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(buf.getvalue())
    return p


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), 0
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def linux_platform(root: Path, *, static_id: str = S_LNX, usercode: str = U_LNX,
                   image_kind: str = "release", fieldable: bool = True,
                   fw_sha: str = "f9428f91") -> Platform:
    prod = root / "prod"
    prod.mkdir(parents=True, exist_ok=True)
    feats = [f for f in PRODUCT_V011_FEATURES if f != "windowed"]
    bit = fake_bit(static_id=static_id, harness="1.0.0", sha=fw_sha, usercode=usercode,
                   features=feats, part=PART)
    (prod / "config_rm_greybox_stage0.bit").write_bytes(bit)
    # a re-bake differs in BRAM content only: the same header, other data. The fake identity
    # is JSON in the data, so add a key the board ignores rather than corrupt it.
    generic = fake_bit(static_id=static_id, harness="1.0.0", sha=fw_sha, usercode=usercode,
                       features=[*feats, "generic_label"], part=PART)
    (prod / "config_rm_greybox_stage0_generic.bit").write_bytes(generic)
    img = make_s0lb(b"\x5a" * 65536)
    (prod / "linux_slot.img").write_bytes(img)
    legal = _tar({"legal-info/README": b"GPL sources and licences (fake)\n"})
    (prod / "linux_legal_info.tar").write_bytes(legal)
    tpl = templates(root)
    ovl = overlay_dir(root, static_id, usercode)
    stage0_elf = "e1a2172d" + "0" * 56
    lb = {
        "schema": "mps3-linux-bundle", "schema_version": "1", "mint_kind": "mint",
        "fieldable": fieldable, "shell_cpu": "mbv", "static_id": static_id,
        "static_usercode": usercode, "static_ver32": "0x01000000",
        "targets": {
            "mcc_sd": {"flashable_bit": {"name": "config_rm_greybox_stage0.bit",
                                         "sha256": sha(bit), "bytes": len(bit)},
                       "stage0": {"name": "stage0_field_b1.elf", "sha256": stage0_elf,
                                  "identity_checked": True}},
            "ethernet": {
                "slot_image": {"name": "linux_slot.img", "format": "s0lb", "sha256": sha(img),
                               "bytes": len(img), "s0lb": linux_bundle_s0lb(img)},
                "provisioned": {"static_id": static_id, "sidecar": "version"},
                "components": {"harness": "1.0.0", "image_kind": image_kind, "dirty": "0",
                               "sha": fw_sha, "harnessd_sha256": "ab" * 32,   # differs from sha, as on silicon
                               "stage0_sha256": stage0_elf, "kernel": "6.18.7",
                               "ver32": "0x01000000"},
                "legal_info": {"name": "linux_legal_info.tar", "sha256": sha(legal),
                               "bytes": len(legal)},
                "overlays": overlay_records(ovl)}}}
    (prod / "linux_bundle.json").write_text(json.dumps(lb, indent=1), encoding="utf-8")
    record = {"schema": "mps3-stage0-bake", "schema_version": "1", "mint_kind": "mint",
              "static_id": static_id, "static_usercode": usercode,
              "stage0_identity_checked": True,
              "baked_bit": {"name": "config_rm_greybox_stage0_generic.bit",
                            "sha256": sha(generic), "bytes": len(generic)},
              "stage0_elf": {"name": "stage0_generic_b1.elf", "sha256": "6bb0d0a0" + "0" * 56},
              "stage0_elf_check": {"baked_constants": {"mps3_stage0_static_id": static_id,
                                                       "mps3_stage0_ver32": "0x01000000"}},
              "note": "updatemem changed BRAM init frames only: UserID and USR_ACCESS are the "
                      "base's"}
    (prod / "stage0_bake_generic.json").write_text(json.dumps(record, indent=1))
    return Platform(root, prod, tpl, ovl, kit_zip(root, static_id), prod /
                    "config_rm_greybox_stage0.bit", static_id, usercode, "linux",
                    prod / "config_rm_greybox_stage0_generic.bit",
                    prod / "stage0_bake_generic.json", fw_sha)


def bare_metal_platform(root: Path, *, static_id: str = S_ILA, usercode: str = U_ILA,
                        fw_sha: str = "0e12a0b0", dirty: bool = False) -> Platform:
    prod = root / "prod"
    prod.mkdir(parents=True, exist_ok=True)
    feats = list(PRODUCT_V011_FEATURES)
    bit = fake_bit(static_id=static_id, harness="1.0.0", sha=fw_sha, usercode=usercode,
                   features=feats, part=PART)
    (prod / "config_rm_greybox.bit").write_bytes(bit)

    def wrap(v: object) -> dict:
        return {"reason": None, "value": v}

    (prod / "mint.json").write_text(json.dumps({
        "schema": "mps3-mint-record", "schema_version": "1.1", "record_kind": "mint",
        "static_id": static_id,
        "static_usercode": wrap({"usercode": usercode, "usr_access": wrap("0x01000000"),
                                 "harness_version": wrap("1.0.0")}),
        "sources": {"repo": wrap({"sha": "c85510817ae7d9cacc6533c1e2e37fadb0fd60bb",
                                  "dirty": dirty})},
        "tools": {"vivado": wrap("2026.1")}}, indent=1), encoding="utf-8")
    (root / "firmware.json").write_text(json.dumps({
        "version": "1.0.0", "sha": fw_sha, "dirty": False, "proto": "0.12",
        "features": feats}), encoding="utf-8")
    tpl = templates(root)
    ovl = overlay_dir(root, static_id, usercode)
    return Platform(root, prod, tpl, ovl, kit_zip(root, static_id),
                    prod / "config_rm_greybox.bit", static_id, usercode, "bare-metal",
                    fw_sha=fw_sha)


def release_args(p: Platform, out: Path, version: str, *extra: str) -> list[str]:
    """``python -m tools.release harness-release`` arguments for platform ``p``."""
    args = ["harness-release", "--out", str(out), "--version", version, "--from", str(p.prod),
            "--sd-templates", str(p.templates), "--overlays", str(p.overlays)]
    if p.impl == "bare-metal":
        args += ["--bit", str(p.bit), "--firmware-json", str(p.root / "firmware.json")]
    return [*args, *extra]
