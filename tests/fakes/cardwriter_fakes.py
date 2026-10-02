"""Fakes for lane SD-FLASH (``services/cardwriter.py``): lsblk and diskutil answers as FIXTURES,
temp files standing in for devices (the ``FileAccess`` seam), and S0LB images.

Nothing here runs ``lsblk``/``diskutil`` or opens a block device. ``guard`` makes any real
listing command, or any write through ``RealAccess``, fail the test that tried it.
"""

from __future__ import annotations

import copy
import plistlib
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness_manager.services import cardwriter as cw

MCC = "V2M-MPS3"
CARD_SIZE = 31_914_983_424          # a "32 GB" microSD: 31.9 GB
BLANK_SIZE = 15_931_539_456         # a "16 GB" microSD: 15.9 GB


# --- lsblk (util-linux 2.37+: booleans, ints, PATH, MOUNTPOINTS) ------------------------------


def _part(name: str, size: int, fstype: str | None, label: str | None, uuid: str | None,
          mounts: list[str | None] | None = None, children: list | None = None) -> dict:
    d = {"name": name, "path": f"/dev/{name}", "rm": True, "hotplug": True, "tran": None,
         "type": "part", "size": size, "model": None, "vendor": None, "serial": None,
         "mountpoints": mounts if mounts is not None else [None], "fstype": fstype,
         "label": label, "uuid": uuid}
    if children:
        d["children"] = children
    return d


def _disk(name: str, size: int, *, rm: bool, hotplug: bool, tran: str | None, model: str | None,
          vendor: str | None = None, dtype: str = "disk", children: list | None = None,
          serial: str | None = None, fstype: str | None = None, label: str | None = None,
          mounts: list[str | None] | None = None) -> dict:
    d = {"name": name, "path": f"/dev/{name}", "rm": rm, "hotplug": hotplug, "tran": tran,
         "type": dtype, "size": size, "model": model, "vendor": vendor, "serial": serial,
         "mountpoints": mounts if mounts is not None else [None], "fstype": fstype,
         "label": label, "uuid": None}
    if children:
        d["children"] = children
    return d


def lsblk_doc(config_root: str | None = None) -> dict:
    """A laptop's lsblk: the system disk, a fixed SATA disk, loop/zram/optical, the MPS3's own
    Debug-USB drives (MCC + DAPLink), a 2 TB USB disk, an empty reader, an eMMC, and three
    card readers' cards: the board's config card (``sdb``, V2M-MPS3, mounted at
    ``config_root``), a blank microSD (``sdc``) and an SD card in a built-in reader
    (``mmcblk0``)."""
    root = config_root or "/media/u/V2M-MPS3"
    return {"blockdevices": [
        _disk("loop0", 328_093_696, rm=False, hotplug=False, tran=None, model=None,
              dtype="loop", fstype="squashfs", mounts=["/snap/core/1"]),
        _disk("zram0", 8_589_934_592, rm=False, hotplug=False, tran=None, model=None,
              mounts=["[SWAP]"]),
        _disk("nvme0n1", 512_110_190_592, rm=False, hotplug=False, tran="nvme",
              model="Samsung SSD 980 PRO 512GB", children=[
                  _part("nvme0n1p1", 536_870_912, "vfat", None, "AAAA-0001", ["/boot/efi"]),
                  _part("nvme0n1p2", 511_000_000_000, "ext4", None, "u-root", ["/"])]),
        _disk("sda", 1_000_204_886_016, rm=False, hotplug=False, tran="sata",
              model="WDC WD10EZEX", children=[
                  _part("sda1", 1_000_000_000_000, "ext4", "data", "u-data", ["/data"])]),
        _disk("sdb", CARD_SIZE, rm=True, hotplug=True, tran="usb", model="SD/MMC   ",
              vendor="Generic-", serial="000000000819", children=[
                  _part("sdb1", CARD_SIZE - 4_194_304, "vfat", MCC, "4A1B-0C2D", [root])]),
        _disk("sdc", BLANK_SIZE, rm=True, hotplug=True, tran="usb", model="MicroSD/M2",
              vendor="Generic-", serial="000000000820", children=[
                  _part("sdc1", BLANK_SIZE - 4_194_304, "vfat", "NO NAME", "1C2E-3F40")]),
        _disk("sdd", 2_013_265_920, rm=True, hotplug=True, tran="usb", model="V2M-MPS3",
              vendor="ARM", children=[
                  _part("sdd1", 2_013_200_000, "vfat", MCC, "5555-0001", ["/media/u/V2M-MPS3_1"])]),
        _disk("sde", 67_108_864, rm=True, hotplug=True, tran="usb", model="VFS",
              vendor="MBED", fstype="vfat", label="MBED MPS3", mounts=["/media/u/MBED MPS3"]),
        _disk("sdf", 2_000_398_934_016, rm=False, hotplug=True, tran="usb",
              model="Expansion HDD", vendor="Seagate", children=[
                  _part("sdf1", 2_000_000_000_000, "exfat", "Backup", "u-bk")]),
        _disk("sdg", 0, rm=True, hotplug=True, tran="usb", model="SD/MMC", vendor="Generic-"),
        _disk("sr0", 1_073_741_312, rm=True, hotplug=True, tran="sata", model="DVD-RW",
              dtype="rom"),
        _disk("mmcblk0", 63_864_569_856, rm=False, hotplug=False, tran=None, model=None,
              children=[_part("mmcblk0p1", 63_860_000_000, "exfat", "SDXC", "u-sdxc")]),
        _disk("mmcblk1", 31_268_536_320, rm=False, hotplug=False, tran=None, model=None,
              children=[_part("mmcblk1p1", 31_000_000_000, "ext4", None, "u-emmc", ["/srv"])]),
        _disk("mmcblk1boot0", 4_194_304, rm=False, hotplug=False, tran=None, model=None),
    ]}


def old_lsblk(doc: dict) -> dict:
    """The same answer as util-linux 2.32 gives it (RHEL 8): strings, MOUNTPOINT, no PATH."""
    def conv(n: dict) -> dict:
        out = {k: v for k, v in n.items() if k not in ("path", "mountpoints", "children")}
        out["rm"] = "1" if n["rm"] else "0"
        out["hotplug"] = "1" if n["hotplug"] else "0"
        out["size"] = str(n["size"])
        mounts = [m for m in n.get("mountpoints") or [] if m]
        out["mountpoint"] = mounts[0] if mounts else None
        if n.get("children"):
            out["children"] = [conv(c) for c in n["children"]]
        return out
    return {"blockdevices": [conv(n) for n in doc["blockdevices"]]}


SYSFS = {"/sys/block/mmcblk0/device/type": "SD", "/sys/block/mmcblk1/device/type": "MMC"}


def sysfs(path: str) -> str:
    return SYSFS.get(path, "")


# --- macOS diskutil plists --------------------------------------------------------------------


def diskutil_list() -> bytes:
    return plistlib.dumps({
        "AllDisks": ["disk4", "disk4s1", "disk5", "disk5s1", "disk6", "disk6s1"],
        "WholeDisks": ["disk4", "disk5", "disk6"],
        "AllDisksAndPartitions": [
            {"DeviceIdentifier": "disk4", "Size": CARD_SIZE, "Content": "FDisk_partition_scheme",
             "Partitions": [{"DeviceIdentifier": "disk4s1", "Size": CARD_SIZE - 4_194_304,
                             "Content": "DOS_FAT_32", "VolumeName": MCC,
                             "MountPoint": "/Volumes/V2M-MPS3", "VolumeUUID": "4A1B-0C2D"}]},
            {"DeviceIdentifier": "disk5", "Size": 1_000_204_886_016,
             "Content": "GUID_partition_scheme",
             "Partitions": [{"DeviceIdentifier": "disk5s1", "Size": 1_000_000_000_000,
                             "Content": "Apple_APFS"}]},
            {"DeviceIdentifier": "disk6", "Size": BLANK_SIZE, "Content": "FDisk_partition_scheme",
             "Partitions": [{"DeviceIdentifier": "disk6s1", "Size": BLANK_SIZE - 4_194_304,
                             "Content": "Windows_FAT_32", "VolumeName": "NO NAME"}]},
        ],
    })


DISKUTIL_INFO = {
    "disk4": {"DeviceIdentifier": "disk4", "DeviceNode": "/dev/disk4", "MediaName":
              "Built In SDXC Reader", "BusProtocol": "Secure Digital", "Internal": False,
              "Removable": True, "RemovableMedia": True, "Ejectable": True,
              "TotalSize": CARD_SIZE, "WholeDisk": True, "VirtualOrPhysical": "Physical"},
    "disk4s1": {"DeviceIdentifier": "disk4s1", "FilesystemType": "msdos",
                "MountPoint": "/Volumes/V2M-MPS3", "VolumeName": MCC},
    "disk5": {"DeviceIdentifier": "disk5", "DeviceNode": "/dev/disk5", "MediaName":
              "Samsung Portable SSD T7", "BusProtocol": "USB", "Internal": False,
              "Removable": False, "Ejectable": True, "TotalSize": 1_000_204_886_016},
    "disk6": {"DeviceIdentifier": "disk6", "DeviceNode": "/dev/disk6", "MediaName":
              "USB3.0 CRW-SD/MS", "BusProtocol": "USB", "Internal": False,
              "Removable": True, "RemovableMedia": True, "Ejectable": True,
              "TotalSize": BLANK_SIZE},
}


def diskutil_info(ident: str) -> bytes:
    return plistlib.dumps(DISKUTIL_INFO.get(ident, {}))


# --- S0LB images ------------------------------------------------------------------------------


def slot_image(payload: bytes = b"linux" * 4000, *, dst: int = 0x80000000) -> bytes:
    """A valid S0LB v2 boot table with one region (what stage0_pack.py writes)."""
    hdr = struct.Struct("<8I")
    off = hdr.size + 16
    entry = struct.pack("<4I", off, dst, len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
    head = hdr.pack(0x424C3053, 2, 1, dst, 0, 0x80001000, 0, 0)
    crc = zlib.crc32(head + entry) & 0xFFFFFFFF
    return hdr.pack(0x424C3053, 2, 1, dst, 0, 0x80001000, 0, crc) + entry + payload


# --- whole-card images (stage0_mkcard.py card --card-img) ---------------------------------------

BLOCK = 512
LBA_A, LBA_B, N_SLOT = 67584, 198656, 131072
LBA_STORE, N_STORE, LBA_PERSIST = 2048, 65536, 329728


def mbr(entries: list[tuple[int, int, int]]) -> bytes:
    """An MBR with ``(type, start LBA, blocks)`` entries in order (stage0_mkcard's CHS form)."""
    sec = bytearray(BLOCK)
    struct.pack_into("<I", sec, 440, 0x53304C42)
    for i, (typ, lba, n) in enumerate(entries):
        sec[446 + 16 * i:462 + 16 * i] = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", typ,
                                                     b"\xfe\xff\xff", lba, n)
    sec[510], sec[511] = 0x55, 0xAA
    return bytes(sec)


def bootsel(default: int = 1, seq: int = 1) -> bytes:
    sec = bytearray(BLOCK)
    struct.pack_into("<IIII", sec, 0, 0x43423053, 1, seq, default)
    struct.pack_into("<I", sec, 0x1FC, zlib.crc32(bytes(sec[:0x1FC])) & 0xFFFFFFFF)
    return bytes(sec)


def card_image(path: Path, slot: bytes | None = None, *, canonical: bool = False,
               card_bytes: int = 512 * 1024 * 1024, slot_b: bool = True) -> Path:
    """A whole-card image. ``canonical``: stage0_mkcard.py's layout and size (sparse;
    byte-identical to ``card --slot-a IMG --card-img F --card-mib 512``). Otherwise a
    COMPACT card stage0 reads the same way (slots A/B at LBA 8 and 120): small, fast tests."""
    img = slot if slot is not None else slot_image()
    if canonical:
        total = card_bytes // BLOCK
        entries = [(0x7F, LBA_A, N_SLOT), (0x7F, LBA_B, N_SLOT),
                   (0x83, LBA_PERSIST, total - LBA_PERSIST), (0xDA, LBA_STORE, N_STORE)]
        a, b, size = LBA_A, LBA_B, card_bytes
    else:
        n = -(-len(img) // BLOCK) + 8
        a, b = 8, 8 + n
        entries = [(0x7F, a, n), (0x7F, b, n) if slot_b else (0, 0, 0)]
        size = (b + n) * BLOCK
    with path.open("wb") as f:
        f.truncate(size)
        f.seek(0)
        f.write(mbr(entries))
        f.write(bootsel())
        f.write(bootsel())
        f.seek(a * BLOCK)
        f.write(img)
        if slot_b:
            f.seek(b * BLOCK)
            f.write(img)
    return path


# board.txt as the card has it, as a bundle carries it, and one with no MBBIOS line (CRLF,
# and an em dash: byte for byte).
CARD_BOARD = (b"BOARD: HBI0309C\r\nTITLE: stock\r\n\r\n[MCCS]\r\n"
              b"MBBIOS: mbb_v141.ebf           ;MB BIOS image \xe2\x80\x94 stock\r\n\r\n"
              b"[APPLICATION NOTE]\r\nAPPFILE: AN547\\an547.txt\r\n")
BUNDLE_BOARD = (b"BOARD: HBI0309C\r\nTITLE: nanoSoC\r\n\r\n[MCCS]\r\n"
                b"MBBIOS: mbb_v999.ebf ;the bundle's\r\n\r\n"
                b"[APPLICATION NOTE]\r\nAPPFILE: Nanosoc\\nanosoc.txt\r\n")
NO_LINE_BOARD = (b"BOARD: HBI0309C\r\n[MCCS]\r\n\r\n[APPLICATION NOTE]\r\n"
                 b"APPFILE: Nanosoc\\nanosoc.txt\r\n")



# --- the rig: a CardWriter over fixtures and temp files ----------------------------------------


_AUTO = object()


class RigWriter(cw.CardWriter):
    """The rig's ``CardWriter``. Its ``prepare`` called WITHOUT ``confirm_unsigned`` types the
    right ``INSTALL UNSIGNED <sha8>`` itself, so the tests of OTHER behaviours (discovery, the
    device checks, MBBIOS, the write and read-back) stay about those. Every test of the phrase
    passes it explicitly (``None`` or a wrong one is refused as in the product), or sets
    ``auto_unsigned = False``; the doors (the API route, ``flash write``) always pass it."""

    auto_unsigned = True

    def prepare(self, *args, confirm_unsigned=_AUTO, **kw):  # noqa: ANN001, ANN002, ANN003
        if confirm_unsigned is _AUTO:
            confirm_unsigned = None
            if self.auto_unsigned and kw.get("unsigned") is None:
                src = args[2] if len(args) > 2 else kw.get("source")
                try:
                    confirm_unsigned = self.unsigned_of(src).phrase
                except Exception:  # noqa: BLE001 - a bad source: plan() says why, first
                    confirm_unsigned = None
        return super().prepare(*args, confirm_unsigned=confirm_unsigned, **kw)


def phrase(source: Path) -> str:
    """The typed INSTALL UNSIGNED <sha8> for a bundle folder, zip or card image."""
    from harness_manager.services import unsigned

    return unsigned.of_path(source).phrase      # not cw.CardWriter: tests monkeypatch it


class Rig:
    """A ``CardWriter`` whose lsblk answer is ``self.doc`` (mutable: swap a card by editing
    it), whose devices are temp files (``FileAccess``), and whose config card is a temp dir
    with ``config.txt`` and ``MB/`` (the MPS3 pack's real ``Mps3Storage`` writes it)."""

    def __init__(self, tmp: Path, *, enabled: bool = True, cap: int = cw.DEFAULT_MAX,
                 writable: bool = True, old: bool = False) -> None:
        self.tmp = tmp
        self.root = tmp / "V2M-MPS3"
        (self.root / "MB" / "HBI0309C").mkdir(parents=True)
        (self.root / "config.txt").write_text("TITLE: config\n", encoding="utf-8")
        (self.root / "MB" / "HBI0309C" / "images.txt").write_text("old\n", encoding="utf-8")
        self.doc = lsblk_doc(str(self.root))
        self.old = old
        self.enabled = enabled
        self.cap = cap
        self.events: list[tuple[str, dict]] = []
        self.listed = 0
        self.devices = {f"/dev/{n}": tmp / f"dev-{n}.img" for n in ("sdb", "sdc", "mmcblk0")}
        for f in self.devices.values():
            f.write_bytes(b"\xee" * 4096)
        self.access = cw.FileAccess(self.devices, writable=writable, on_unmount=self.unmounted)
        self.writer = RigWriter(
            state_dir=tmp / "state", lister=self.lister, access=self.access,
            storage_for=self.storage_for, platform="linux", enabled=lambda: self.enabled,
            cap=lambda: self.cap, publish=lambda t, d: self.events.append((t, d)))

    def lister(self) -> list[cw.Disk]:
        self.listed += 1
        doc = old_lsblk(self.doc) if self.old else copy.deepcopy(self.doc)
        return cw.parse_lsblk(doc, sysfs=sysfs)

    def storage_for(self, root: str) -> Any:
        from harness_manager_mps3.sd import Mps3Storage, SdEnv, VolumeInfo

        env = SdEnv(list_volumes=lambda: [VolumeInfo(MCC, str(self.root))])
        return Mps3Storage(root, env=env)

    def node(self, name: str) -> dict:
        return next(n for n in self.doc["blockdevices"] if n["name"] == name)

    def unmounted(self, disk: cw.Disk) -> None:
        for child in self.node(disk.name).get("children") or []:
            child["mountpoints"] = [None]

    def card(self, name: str) -> cw.CardDevice:
        return next(c for c in self.writer.listing().devices if c.disk.name == name)

    def bundle(self, *, ebf: bool = False, board_txt: bytes | None = None,
               name: str = "") -> Path:
        b = self.tmp / (name or ("bundle-ebf" if ebf else "bundle"))
        (b / "MB" / "HBI0309C").mkdir(parents=True)
        (b / "MB" / "HBI0309C" / "images.txt").write_text("new harness\n", encoding="utf-8")
        (b / "MB" / "HBI0309C" / "shell.bit").write_bytes(b"\x00\x09bit" * 100)
        if ebf:
            (b / "MB" / "mbb_v141.ebf").write_bytes(b"bios")
        if board_txt is not None:
            (b / "MB" / "HBI0309C" / "board.txt").write_bytes(board_txt)
        return b

    def card_board_txt(self, data: bytes | None, *, ebf: str = "") -> None:
        """The card's own MB/HBI0309C/board.txt (None: none) and an .ebf beside it."""
        p = self.root / "MB" / "HBI0309C" / "board.txt"
        if data is None:
            p.unlink(missing_ok=True)
        else:
            p.write_bytes(data)
        if ebf:
            (self.root / "MB" / "HBI0309C" / ebf).write_bytes(b"MCC firmware")

    def slot(self, data: bytes | None = None, name: str = "linux_slot.img") -> Path:
        p = self.tmp / name
        p.write_bytes(slot_image() if data is None else data)
        return p

    def card_image(self, name: str = "card.img", **kw: Any) -> Path:
        return card_image(self.tmp / name, **kw)

    def topics(self, topic: str) -> list[dict]:
        return [d for t, d in self.events if t == topic]


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No real lsblk/diskutil, no real device open: a test that tries fails."""
    def refuse_run(argv, timeout=15.0):  # noqa: ANN001, ARG001
        raise AssertionError(f"a test ran a real listing command: {' '.join(argv)}")

    def refuse_open(self, path):  # noqa: ANN001
        raise AssertionError(f"a test opened a real device: {path}")

    monkeypatch.setattr(cw, "run_command", refuse_run)
    monkeypatch.setattr(cw.RealAccess, "open_write", refuse_open)
    monkeypatch.setattr(cw.RealAccess, "open_read", refuse_open)
    monkeypatch.setattr(cw.RealAccess, "unmount", lambda self, disk: refuse_open(self, disk.path))
    yield
