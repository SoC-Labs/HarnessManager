"""Write SD cards in THIS PC's card reader, safely (lane SD-FLASH, the bring-up wave).

The "SD flashing" door of bring-up: the alternative to writing the board's configuration SD
over its Debug USB. Board-agnostic: it lists this computer's card readers, and writes one of
two things onto the card in one of them:

- ``files``: a configuration-SD file tree (a harness bundle laid out like the SD) onto the
  mounted FAT volume of a card taken out of the board (the MPS3's ``V2M-MPS3`` card). The
  writer is the board pack's own configuration-SD writer (``<pack package>.sd:
  make_storage_adapter``, the module convention ``pack.py`` uses), so its rules hold here
  too: a verified backup of the card as it is NOW first (taken by the job, or given), never
  an ``.ebf`` (it reflashes the MCC), never an MCC command file, journaled, read back.
- ``card``: a WHOLE-CARD image onto the whole device (the Linux harness's user microSD), as
  ``stage0_mkcard.py card --card-img`` builds it (STAGE0_CONTRACT.md §6: the MBR at LBA 0,
  the boot-select sectors at LBA 1-2, slot A at LBA 67584 and B at 198656, /persist p3, the
  D13 store p4). Checked first, the way stage0 reads a card (``services.update.s0lb`` for
  the slots): the MBR signature 0x55AA at bytes 510-511, then entries 1/2 (type 0x7F) and at
  least one slot whose S0LB boot table passes every CRC; never larger than the device.
  ``linux_slot.img`` (S0LB at byte 0) is ONE OS slot and is refused with how to build a card
  image: written at byte 0 it is "no MBR" to stage0, and the board sits in rescue.

Safety rails (code, not docs):

- **Only card readers are listed.** Whole, removable or hotplug disks on a USB or SD/MMC
  transport. Excluded, each with its reason: any disk holding a mounted ``/``, ``/boot``,
  ``/home``, ``/usr``, ``/var`` or swap; non-removable disks; loop, zram, optical; an
  internal eMMC; the board's own MCC drive (a ``V2M-MPS3`` volume on a disk the board's
  controller presents: vendor/model ARM, V2M, MPS, MBED, DAPLINK; that is the Debug USB
  door, never raw-written) and its DAPLink drive (``MBED*``); anything above the size cap
  (``bringup.sd_flash_max``, 256 GB); an empty reader. A card whose volume is labelled
  ``V2M-MPS3`` in a real card reader is listed for ``files`` only: a card image would erase
  the board's configuration.
- **A device id is a fingerprint** (name, path, model, vendor, serial, size, transport, the
  volumes' uuid/label/fs): a card swapped since the listing has another id, so the write
  is refused ("the device changed"). The device is listed again at write time, and again
  after the unmount, before the first byte.
- **The typed phrase** is ``WRITE <model> <size>`` exactly as listed (``confirm``).
- **HM never escalates.** If the device cannot be opened for writing, the job ends
  ``needs_privilege`` with the exact commands (``privileged_command``, ``verify_command``):
  Linux ``sudo dd if=IMG of=/dev/sdX bs=4M conv=fsync status=progress`` then ``sudo cmp -n
  <bytes> IMG /dev/sdX``; macOS ``diskutil unmountDisk`` + ``sudo dd of=/dev/rdiskN``. No
  udisks2 for the write (its ``OpenForRestore`` returns a file descriptor the ``udisksctl``
  /``gdbus`` CLIs cannot hand back, and HM adds no Python dependency): only for the unmount,
  ``udisksctl unmount -b`` when it is on PATH (else ``umount``); ``diskutil unmountDisk``
  on macOS.
- **Write, then prove it.** Large blocks (4 MiB), ``fsync`` every 32 MiB and at the end;
  then the page cache is dropped (``POSIX_FADV_DONTNEED``) and the bytes are read back and
  compared by sha256. On Linux the device is opened ``O_EXCL``: a mounted one refuses.
- **One write per device** (an ``flock`` in the state dir), whether from the app or the CLI.
- **Regular files are never devices.** ``RealAccess`` refuses anything that is not a block
  or character device. Only the test seam (``FileAccess``: tests, and ``--demo``'s
  simulated readers) writes temp files instead.
- **Off by default.** ``bringup.sd_flash`` (``$HARNESS_MANAGER_BRINGUP_SD_FLASH``) ``off``:
  nothing is listed (not even ``lsblk`` runs) and a write is refused.

Windows: listed as not supported yet (``Get-Disk`` is the later path).
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.pack import BackupRecord, Progress

from .update import s0lb

# --- settings ------------------------------------------------------------------------------

SETTING = "bringup.sd_flash"
MAX_SETTING = "bringup.sd_flash_max"
ENABLE_ENV = "HARNESS_MANAGER_BRINGUP_SD_FLASH"          # the rows' variables (settings/rows.py)
MAX_ENV = "HARNESS_MANAGER_BRINGUP_SD_FLASH_MAX"
DEFAULT_MAX = 256_000_000_000
CAPABILITY = "sd_flash"
DISABLED_REASON = "SD flashing is turned off (Settings → Bring-up, bringup.sd_flash)"
WINDOWS_REASON = ("writing SD cards in this PC's card reader is not supported on Windows yet "
                  "(Linux and macOS only): write the configuration SD over the board's Debug "
                  "USB instead")
KINDS = ("files", "card")
PHASES = ("backup", "unmount", "write", "verify")


def is_enabled(state_dir: Path | str | None = None) -> bool:
    """``bringup.sd_flash`` is ``on`` (its variable, then the settings; ``off`` by default)."""
    from harness_manager.settings import runtime

    return runtime.value(SETTING, state_dir=state_dir) == "on"


def size_cap(state_dir: Path | str | None = None) -> int:
    """``bringup.sd_flash_max``: the largest disk listed (bytes)."""
    from harness_manager.settings import runtime

    value = runtime.value(MAX_SETTING, state_dir=state_dir)
    return int(value) if isinstance(value, int) and value > 0 else DEFAULT_MAX


# --- sizes and phrases -------------------------------------------------------------------------


def human_size(n: int) -> str:
    """Decimal units, as a card's packaging counts: ``31914983424`` -> ``"31.9 GB"``."""
    value = float(max(0, int(n)))
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"  # pragma: no cover


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def confirm_phrase(model: str, size_bytes: int) -> str:
    """The phrase a write needs: ``WRITE <model> <size>``, both as the listing shows them."""
    return f"WRITE {_clean(model) or 'card'} {human_size(size_bytes)}"


# --- what discovery reads ------------------------------------------------------------------------


@dataclass(frozen=True)
class Volume:
    """A partition, a nested volume (LVM, crypt), or the disk itself when it carries a file
    system with no partition table (a "superfloppy")."""

    name: str
    path: str
    size: int = 0
    fstype: str = ""
    label: str = ""
    uuid: str = ""
    mountpoints: tuple[str, ...] = ()
    nested: bool = False             # under a partition (LVM, crypt): never the card's own FAT


@dataclass(frozen=True)
class Disk:
    name: str                        # "sdb", "mmcblk0", "disk4"
    path: str                        # "/dev/sdb", "/dev/disk4"
    size: int
    dtype: str = "disk"              # lsblk TYPE: disk | loop | rom | zram ...
    model: str = ""
    vendor: str = ""
    serial: str = ""
    transport: str = ""              # "usb" | "mmc" | "sd" | "sata" | "nvme" | ""
    removable: bool = False
    hotplug: bool = False
    internal: bool | None = None     # macOS: diskutil Internal
    mmc_type: str = ""               # Linux mmcblk: sysfs device/type ("SD", "MMC")
    raw_path: str = ""               # macOS: /dev/rdiskN, the path writes and reads use
    volumes: tuple[Volume, ...] = ()
    platform: str = "linux"

    @property
    def mountpoints(self) -> tuple[str, ...]:
        return tuple(m for v in self.volumes for m in v.mountpoints)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(v.label for v in self.volumes if v.label)

    @property
    def io_path(self) -> str:
        return self.raw_path or self.path

    def fingerprint(self) -> str:
        vols = sorted((v.name, v.uuid, v.label, v.fstype) for v in self.volumes)
        doc = [self.path, _clean(self.model), _clean(self.vendor), _clean(self.serial),
               int(self.size), self.transport, vols]
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:10]

    @property
    def device_id(self) -> str:
        return f"{self.name}-{self.fingerprint()}"

    @property
    def display_model(self) -> str:
        return _clean(self.model) or _clean(self.vendor) or "card"


@dataclass(frozen=True)
class CardDevice:
    """One card reader's card, as ``GET /cardwriter/devices`` lists it."""

    disk: Disk
    kinds: dict[str, str]            # kind -> "" (can) or why not
    needs_privilege: bool = False
    files_root: str = ""             # the mounted FAT volume a ``files`` write goes to

    @property
    def id(self) -> str:
        return self.disk.device_id

    @property
    def writable(self) -> bool:
        return any(not why for why in self.kinds.values())

    @property
    def why_not(self) -> str:
        return "; ".join(f"{k}: {why}" for k, why in self.kinds.items() if why)

    @property
    def confirm(self) -> str:
        return confirm_phrase(self.disk.display_model, self.disk.size)

    def to_json(self) -> dict[str, Any]:
        d = self.disk
        fats = [v for v in d.volumes if v.fstype in FAT_TYPES]
        out: dict[str, Any] = {
            "id": self.id, "path": d.path, "model": d.display_model, "size_bytes": d.size,
            "removable": True, "mounted": list(d.mountpoints), "writable": self.writable,
            # additive to the contract
            "size": human_size(d.size), "vendor": _clean(d.vendor), "transport": d.transport,
            "labels": list(d.labels), "confirm": self.confirm,
            "kinds": {k: {"ok": not why, "why_not": why} for k, why in self.kinds.items()},
            "needs_privilege": self.needs_privilege, "files_root": self.files_root,
            "volumes": [{"path": v.path, "size_bytes": v.size, "fs": v.fstype,
                         "label": v.label, "mounted": list(v.mountpoints)}
                        for v in d.volumes if not v.nested],
        }
        fs = (fats[0].fstype if fats else next((v.fstype for v in d.volumes if v.fstype), ""))
        if fs:
            out["fs"] = fs
        if not self.writable:
            out["why_not"] = self.why_not
        return out


@dataclass(frozen=True)
class Excluded:
    """A disk that is not listed, and why (``GET /cardwriter/devices`` ``excluded``)."""

    name: str
    path: str
    model: str
    size_bytes: int
    why: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "model": self.model,
                "size_bytes": self.size_bytes, "size": human_size(self.size_bytes),
                "why": self.why}


@dataclass(frozen=True)
class Listing:
    devices: tuple[CardDevice, ...] = ()
    excluded: tuple[Excluded, ...] = ()

    def find(self, device_id: str) -> CardDevice | None:
        return next((c for c in self.devices if c.id == device_id), None)


# --- Linux: lsblk --------------------------------------------------------------------------------

LSBLK_COLUMNS = "NAME,PATH,RM,HOTPLUG,TRAN,TYPE,SIZE,MODEL,VENDOR,SERIAL,MOUNTPOINTS,FSTYPE,LABEL,UUID"
#: util-linux before 2.37 has no MOUNTPOINTS and before 2.33 no PATH (RHEL 8: 2.32).
LSBLK_COLUMNS_OLD = "NAME,RM,HOTPLUG,TRAN,TYPE,SIZE,MODEL,VENDOR,SERIAL,MOUNTPOINT,FSTYPE,LABEL,UUID"

Runner = Callable[[Sequence[str]], "tuple[int, bytes, bytes]"]


def run_command(argv: Sequence[str], timeout: float = 15.0) -> tuple[int, bytes, bytes]:
    """Run a read-only listing command (lsblk, diskutil list/info). Never a shell."""
    try:
        cp = subprocess.run(list(argv), capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, b"", f"{argv[0]}: not found".encode()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 126, b"", str(exc).encode()
    return cp.returncode, cp.stdout, cp.stderr


def read_sysfs(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _flag(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true")


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _mounts(node: Mapping[str, Any]) -> tuple[str, ...]:
    many = node.get("mountpoints")
    if isinstance(many, list):
        return tuple(str(m) for m in many if m)
    one = node.get("mountpoint")
    return (str(one),) if one else ()


def parse_lsblk(doc: Mapping[str, Any], *,
                sysfs: Callable[[str], str] = read_sysfs) -> list[Disk]:
    """``lsblk -J -b -o ...`` (old or new util-linux) -> whole disks with their volumes."""
    disks: list[Disk] = []
    for node in doc.get("blockdevices") or []:
        name = str(node.get("name") or "")
        if not name:
            continue
        vols: list[Volume] = []

        def walk(n: Mapping[str, Any], depth: int, out: list[Volume]) -> None:
            for child in n.get("children") or []:
                cname = str(child.get("name") or "")
                out.append(Volume(
                    name=cname, path=str(child.get("path") or f"/dev/{cname}"),
                    size=_int(child.get("size")), fstype=str(child.get("fstype") or ""),
                    label=str(child.get("label") or ""), uuid=str(child.get("uuid") or ""),
                    mountpoints=_mounts(child), nested=depth > 0))
                walk(child, depth + 1, out)

        if node.get("fstype") or _mounts(node):           # a file system on the whole disk
            vols.append(Volume(name=name, path=str(node.get("path") or f"/dev/{name}"),
                               size=_int(node.get("size")), fstype=str(node.get("fstype") or ""),
                               label=str(node.get("label") or ""),
                               uuid=str(node.get("uuid") or ""), mountpoints=_mounts(node)))
        walk(node, 0, vols)
        tran = str(node.get("tran") or "")
        mmc_type = ""
        if re.fullmatch(r"mmcblk\d+", name):
            tran = tran or "mmc"
            mmc_type = sysfs(f"/sys/block/{name}/device/type")
        disks.append(Disk(
            name=name, path=str(node.get("path") or f"/dev/{name}"), size=_int(node.get("size")),
            dtype=str(node.get("type") or ""), model=str(node.get("model") or ""),
            vendor=str(node.get("vendor") or ""), serial=str(node.get("serial") or ""),
            transport=tran, removable=_flag(node.get("rm")), hotplug=_flag(node.get("hotplug")),
            mmc_type=mmc_type, volumes=tuple(vols), platform="linux"))
    return disks


def linux_disks(run: Runner | None = None,
                sysfs: Callable[[str], str] | None = None) -> list[Disk]:
    run = run or run_command                  # looked up at each call: the tests' guard
    sysfs = sysfs or read_sysfs
    rc, out, err = run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    if rc != 0 and b"unknown column" in err:
        rc, out, err = run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS_OLD])
    if rc != 0:
        raise UnavailableError(CAPABILITY, f"lsblk failed: {err.decode(errors='replace').strip()}",
                               hint="install util-linux (lsblk)")
    try:
        doc = json.loads(out.decode("utf-8", errors="replace") or "{}")
    except ValueError as exc:
        raise UnavailableError(CAPABILITY, f"lsblk answered something that is not JSON ({exc})") \
            from exc
    return parse_lsblk(doc, sysfs=sysfs)


# --- macOS: diskutil -----------------------------------------------------------------------------


def _plist(data: bytes) -> dict[str, Any]:
    try:
        doc = plistlib.loads(data)
    except Exception as exc:  # noqa: BLE001 - plistlib raises several types
        raise UnavailableError(CAPABILITY, f"diskutil answered something that is not a plist "
                                           f"({exc})") from exc
    return doc if isinstance(doc, dict) else {}


def parse_diskutil(list_plist: bytes, info: Callable[[str], bytes]) -> list[Disk]:
    """``diskutil list -plist external physical`` plus ``diskutil info -plist`` per disk."""
    doc = _plist(list_plist)
    disks: list[Disk] = []
    for entry in doc.get("AllDisksAndPartitions") or []:
        ident = str(entry.get("DeviceIdentifier") or "")
        if not ident:
            continue
        di = _plist(info(ident))
        vols: list[Volume] = []
        for part in entry.get("Partitions") or []:
            pid = str(part.get("DeviceIdentifier") or "")
            fs = str(part.get("Content") or "")
            if part.get("MountPoint"):
                pinfo = _plist(info(pid))
                fs = str(pinfo.get("FilesystemType") or fs)
            vols.append(Volume(name=pid, path=f"/dev/{pid}", size=_int(part.get("Size")),
                               fstype=_mac_fs(fs), label=str(part.get("VolumeName") or ""),
                               uuid=str(part.get("VolumeUUID") or part.get("DiskUUID") or ""),
                               mountpoints=(str(part["MountPoint"]),) if part.get("MountPoint")
                               else (), nested=False))
            # an APFS container's volumes are on a synthesized disk: name the content
            if fs in ("Apple_APFS", "Apple_HFS", "Apple_Boot"):
                vols[-1] = replace(vols[-1], fstype=fs)
        if entry.get("MountPoint"):                       # a file system on the whole disk
            vols.append(Volume(name=ident, path=f"/dev/{ident}", size=_int(entry.get("Size")),
                               fstype=_mac_fs(str(di.get("FilesystemType") or "")),
                               label=str(entry.get("VolumeName") or ""),
                               mountpoints=(str(entry["MountPoint"]),)))
        bus = str(di.get("BusProtocol") or "")
        tran = {"USB": "usb", "Secure Digital": "sd"}.get(bus, bus.lower())
        disks.append(Disk(
            name=ident, path=str(di.get("DeviceNode") or f"/dev/{ident}"),
            size=_int(di.get("TotalSize") or di.get("Size") or entry.get("Size")),
            dtype="disk", model=str(di.get("MediaName") or di.get("IORegistryEntryName") or ""),
            vendor="", serial="", transport=tran,
            removable=bool(di.get("Removable") or di.get("RemovableMedia")
                           or di.get("Ejectable")),
            hotplug=bool(di.get("Ejectable")), internal=bool(di.get("Internal", False)),
            raw_path=f"/dev/r{ident}", volumes=tuple(vols), platform="darwin"))
    return disks


def _mac_fs(fs: str) -> str:
    low = fs.lower()
    if low in ("msdos", "dos_fat_32", "dos_fat_16", "dos_fat_12", "windows_fat_32",
               "windows_fat_16", "fat32", "fat16"):
        return "vfat"
    return fs


def mac_disks(run: Runner | None = None) -> list[Disk]:
    run = run or run_command
    rc, out, err = run(["diskutil", "list", "-plist", "external", "physical"])
    if rc != 0:
        raise UnavailableError(CAPABILITY, f"diskutil failed: "
                                           f"{err.decode(errors='replace').strip()}")

    def info(ident: str) -> bytes:
        rc2, out2, _ = run(["diskutil", "info", "-plist", ident])
        return out2 if rc2 == 0 else plistlib.dumps({})

    return parse_diskutil(out, info)


# --- which disks are card readers ----------------------------------------------------------------

FAT_TYPES = ("vfat", "msdos", "fat", "fat16", "fat32")
SYSTEM_MOUNTS = ("/", "/home", "/usr", "/var")
CARD_TRANSPORTS = ("usb", "mmc", "sd")
MCC_LABEL = "V2M-MPS3"          # harness_manager_mps3.constants.MSD_VOLUME_LABEL
#: The board-controller side of a Debug USB: the MCC's drive and the CMSIS-DAP (DAPLink)'s.
MCC_DEVICE = re.compile(r"\b(arm|v2m|mps[23]?|mbed|daplink|keil)\b", re.IGNORECASE)
DAPLINK_LABELS = ("MBED", "DAPLINK", "MAINTENANCE")   # harness_manager_mps3.sd


def system_mount(mount: str) -> str:
    """The system mount ``mount`` is ("" when it is none): / /boot* /home /usr /var, swap."""
    m = mount.strip()
    if m in ("[SWAP]", "swap") or m in SYSTEM_MOUNTS:
        return "swap" if m in ("[SWAP]", "swap") else m
    if m == "/boot" or m.startswith("/boot/"):
        return m
    if m.startswith("/System/Volumes") or m == "/private/var/vm":   # macOS's system volumes
        return m
    return ""


def exclusion(disk: Disk, cap: int) -> str:
    """Why ``disk`` is not a card reader's card ("" when it is one)."""
    if disk.dtype != "disk":
        return {"loop": "a loop device (a file attached as a disk), not a card reader",
                "zram": "zram (compressed RAM), not a card reader",
                "rom": "an optical drive, not a card reader"}.get(
            disk.dtype, f"not a whole disk ({disk.dtype or 'unknown type'})")
    if disk.name.startswith("zram"):
        return "zram (compressed RAM), not a card reader"
    if re.fullmatch(r"mmcblk\d+(boot\d+|rpmb)", disk.name):
        return "an eMMC boot or RPMB area, not a card"
    system = sorted({s for s in (system_mount(m) for m in disk.mountpoints) if s})
    if system:
        return f"holds this computer's {', '.join(system)}: the system disk"
    if any(v.fstype in ("Apple_APFS", "Apple_HFS", "Apple_Boot") for v in disk.volumes):
        return "holds a macOS volume (APFS/HFS), not a card"
    if disk.internal:
        return "an internal disk"
    if disk.transport == "mmc" and disk.mmc_type and disk.mmc_type.upper() != "SD":
        return f"an internal eMMC ({disk.mmc_type}), not an SD card"
    if disk.transport not in CARD_TRANSPORTS:
        bus = disk.transport or "an internal bus"
        return f"on {bus}, not a USB or SD card reader"
    if not (disk.removable or disk.hotplug or disk.mmc_type.upper() == "SD"):
        return "not removable (a fixed disk)"
    labels = [lb.upper() for lb in disk.labels]
    who = " ".join((disk.vendor, disk.model))
    if any(lb.startswith(DAPLINK_LABELS) for lb in labels):
        return "the board's CMSIS-DAP (DAPLink) drive, not a card"
    if MCC_LABEL in labels and MCC_DEVICE.search(who):
        return ("the MPS3's own MCC drive over the Debug USB: write it with the board's Debug "
                "USB door (Bring-up), never raw")
    if MCC_DEVICE.search(who):
        return "a board controller's drive (Debug USB), not a card reader"
    if disk.size <= 0:
        return "no card in this reader"
    if disk.size > cap:
        return (f"{human_size(disk.size)} is above the {human_size(cap)} cap "
                f"(bringup.sd_flash_max): not an SD card")
    return ""


# --- the files writer (a board pack's configuration-SD writer) -----------------------------------


class _VolumeSession:
    """Just enough of a board session for a pack's ``make_storage_adapter``: one USB_MSD link."""

    def __init__(self, root: str) -> None:
        from harness_manager.core.model import Candidate, Link, LinkKind

        self._link = Link(LinkKind.USB_MSD, root, "a card in this PC's reader")
        self.candidate = Candidate(pack="", board_id=f"card@{root}", links=(self._link,))

    def link(self, kind: Any) -> Any:
        return self._link if kind == self._link.kind else None


def pack_storage(root: str) -> Any:
    """The installed board packs' configuration-SD writer for the volume at ``root``: the
    first pack whose ``<package>.sd`` module has ``make_storage_adapter`` (the MPS3's
    ``Mps3Storage``, label ``V2M-MPS3``). ``UnavailableError`` when no pack has one."""
    from harness_manager.core.registry import load_packs

    for pack in load_packs().values():
        package = type(pack).__module__.rsplit(".", 1)[0]
        try:
            mod = importlib.import_module(f"{package}.sd")
        except ImportError:
            continue
        make = getattr(mod, "make_storage_adapter", None)
        if callable(make):
            storage = make(_VolumeSession(root))
            if storage is not None:
                return storage
    raise UnavailableError(CAPABILITY, "no installed board pack writes a configuration SD")


def files_check(storage_for: Callable[[str], Any], root: str) -> str:
    """Why the volume at ``root`` cannot take a ``files`` write ("" when it can): the pack's
    own check (its label, its layout: config.txt or MB/, never the DAPLink drive)."""
    try:
        storage_for(root).locate()
    except HarnessError as exc:
        return exc.message
    return ""


# --- the device access seam ----------------------------------------------------------------------


class RealAccess:
    """How HM touches a real device. Never a regular file; never as root unless HM already is."""

    simulated = False

    def __init__(self, run: Runner | None = None, platform: str | None = None) -> None:
        self.run = run or (lambda argv: run_command(argv))
        self.platform = platform or sys.platform

    def check_device(self, path: str) -> None:
        try:
            st = os.stat(path)
        except FileNotFoundError as exc:
            raise AbsentError(f"{path} is gone (the card reader was unplugged?)",
                              hint="list the devices again") from exc
        if not (stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode)):
            raise RefusedError(f"{path} is not a block device: refusing to write it",
                               hint="only a listed card reader's device is ever written")

    def can_write(self, path: str) -> bool:
        return os.access(path, os.W_OK)

    def open_write(self, path: str) -> int:
        self.check_device(path)
        flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        if self.platform.startswith("linux"):
            flags |= os.O_EXCL       # a block device opened O_EXCL refuses while mounted (EBUSY)
        return os.open(path, flags)

    def open_read(self, path: str) -> int:
        self.check_device(path)
        return os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))

    def aligned(self, path: str) -> bool:
        """A character (raw) device takes whole sectors only (macOS /dev/rdiskN)."""
        try:
            return stat.S_ISCHR(os.stat(path).st_mode)
        except OSError:
            return False

    def drop_cache(self, fd: int) -> None:
        advise = getattr(os, "posix_fadvise", None)
        if advise is not None:
            with contextlib.suppress(OSError):
                advise(fd, 0, 0, os.POSIX_FADV_DONTNEED)

    def unmount(self, disk: Disk) -> None:
        mounted = [v for v in disk.volumes if v.mountpoints]
        if not mounted:
            return
        if disk.platform == "darwin":
            rc, _, err = self.run(["diskutil", "unmountDisk", disk.path])
            if rc != 0:
                raise RefusedError(f"cannot unmount {disk.path}: "
                                   f"{err.decode(errors='replace').strip()}",
                                   hint=f"eject its volumes first (`diskutil unmountDisk "
                                        f"{disk.path}`), then retry")
            return
        tool = ["udisksctl", "unmount", "-b"] if shutil.which("udisksctl") else ["umount"]
        for vol in mounted:
            rc, _, err = self.run([*tool, vol.path])
            if rc != 0:
                raise RefusedError(f"cannot unmount {vol.path} ({', '.join(vol.mountpoints)}): "
                                   f"{err.decode(errors='replace').strip()}",
                                   hint=f"unmount it yourself (`udisksctl unmount -b {vol.path}` "
                                        f"or `sudo umount {vol.path}`), then retry")


class FileAccess(RealAccess):
    """THE TEST SEAM (and ``--demo``'s simulated readers): each device path is a temp file.

    ``files`` maps a device path to the regular file standing in for it; ``after_write``
    (tests) runs between the write and the read-back; ``on_unmount`` is told what was
    unmounted (the fake lister drops those mounts). Never used for a real device.
    """

    simulated = True

    def __init__(self, files: Mapping[str, Path], *, writable: bool = True,
                 after_write: Callable[[Path], None] | None = None,
                 on_unmount: Callable[[Disk], None] | None = None) -> None:
        super().__init__(run=_refuse_run, platform="linux")
        self.files = {k: Path(v) for k, v in files.items()}
        self.writable = writable
        self.after_write = after_write
        self.on_unmount = on_unmount
        self.unmounted: list[str] = []

    def _file(self, path: str) -> Path:
        if path not in self.files:
            raise AbsentError(f"{path} is not a simulated device", hint="list the devices again")
        return self.files[path]

    def check_device(self, path: str) -> None:
        self._file(path)

    def can_write(self, path: str) -> bool:
        return self.writable

    def open_write(self, path: str) -> int:
        f = self._file(path)
        if not self.writable:
            raise PermissionError(13, "Permission denied", path)
        return os.open(f, os.O_WRONLY | os.O_CREAT, 0o600)

    def open_read(self, path: str) -> int:
        return os.open(self._file(path), os.O_RDONLY)

    def aligned(self, path: str) -> bool:
        return False

    def unmount(self, disk: Disk) -> None:
        self.unmounted.append(disk.path)
        if self.on_unmount is not None:
            self.on_unmount(disk)

    def written(self, path: str) -> None:
        if self.after_write is not None:
            self.after_write(self._file(path))


def _refuse_run(argv: Sequence[str]) -> tuple[int, bytes, bytes]:
    raise AssertionError(f"the test seam ran a real command: {' '.join(argv)}")


# --- card images: stage0's user microSD (STAGE0_CONTRACT.md §6; stage0_mkcard.py) ----------------

BLOCK = 512
TYPE_SLOT = 0x7F                 # MBR entries 1 and 2: stage0's slots A and B
LBA_A, LBA_B = 67584, 198656     # where stage0_mkcard puts them (stage0 reads the entries)
MIB = 1 << 20
SLOT_IMAGE_REFUSAL = ("that is a single OS slot (linux_slot.img), not a whole-card image: "
                      "build one with stage0_mkcard.py card --card-img")


def _slot_entries(sector: bytes) -> list[tuple[int, int, int]]:
    return [(sector[446 + 16 * i + 4], *struct.unpack_from("<II", sector, 446 + 16 * i + 8))
            for i in range(4)]


def _read_slot(f: Any, lba: int, nblocks: int) -> bytes:
    """A slot's boot image, read by its own header, bounded by the partition (stage0_mkcard)."""
    limit = min(nblocks * BLOCK, s0lb.IMAGE_MAX)
    f.seek(lba * BLOCK)
    head = f.read(BLOCK)
    if len(head) < 32:
        return head
    n = min(struct.unpack_from("<I", head, 8)[0], s0lb.MAX_ENTRIES)
    end = 32 + 16 * n
    for i in range(n):
        if 32 + 16 * i + 16 <= len(head):
            so, _d, ln, _c = struct.unpack_from("<IIII", head, 32 + 16 * i)
            end = max(end, so + ln)
    f.seek(lba * BLOCK)
    return f.read(min(end, limit))


@dataclass(frozen=True)
class CardImage:
    """A whole-card image that passed ``inspect_card``."""

    source: str
    size: int
    slots: tuple[str, ...]           # the bootable ones: "A", "B"
    hdr_crc: str                     # the first bootable slot's (the board's ``hdr_crc``)
    notes: tuple[str, ...] = ()      # a slot that is not bootable, and why

    def describe(self) -> str:
        return (f"a stage0 card image: slot{'s' if len(self.slots) > 1 else ''} "
                f"{' and '.join(self.slots)} bootable (hdr_crc {self.hdr_crc})")


def inspect_card(path: Path | str) -> CardImage:
    """Check ``path`` as a whole-card image the way stage0 reads a card: the MBR (0x55AA),
    entries 1/2 type 0x7F, and a slot whose S0LB boot table passes. ``RefusedError`` says why
    not; a single OS slot (S0LB at byte 0) is refused with how to build a card image."""
    path = Path(path)
    if not path.is_file():
        raise AbsentError(f"no card image at {path}",
                          hint="give the card image stage0_mkcard.py card --card-img wrote")
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(BLOCK)
        if len(head) >= 4 and struct.unpack_from("<I", head, 0)[0] == s0lb.MAGIC:
            raise RefusedError(f"{path.name}: {SLOT_IMAGE_REFUSAL}",
                               hint="nothing was written: a slot image at byte 0 is \"no MBR\" "
                                    "to stage0, and the board would sit in rescue")
        if len(head) < BLOCK or head[510:512] != b"\x55\xaa":
            raise RefusedError(f"{path.name} is not a whole-card image: no MBR (0x55AA at bytes "
                               f"510-511)",
                               hint="build one with stage0_mkcard.py card --card-img")
        good: list[str] = []
        crc = ""
        notes: list[str] = []
        for (typ, lba, n), name in zip(_slot_entries(head)[:2], ("A", "B"), strict=True):
            if typ != TYPE_SLOT or not lba or not n:
                notes.append(f"slot {name}: MBR entry {'1' if name == 'A' else '2'} is not a "
                             f"type-0x7F slot")
                continue
            try:
                table = s0lb.parse(_read_slot(f, lba, n))
            except s0lb.S0lbError as exc:
                notes.append(f"slot {name} at LBA {lba}: {exc}")
                continue
            if table.problems:
                notes.append(f"slot {name} at LBA {lba}: {'; '.join(table.problems)}")
                continue
            good.append(name)
            crc = crc or f"0x{table.header_crc32:08x}"
    if not good:
        raise RefusedError(f"{path.name} has an MBR but no bootable stage0 slot "
                           f"({'; '.join(notes)})",
                           hint="the board would sit in rescue; build the card image with "
                                "stage0_mkcard.py card --card-img")
    return CardImage(str(path), size, tuple(good), crc, tuple(notes))


def file_sha256(path: Path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    left = limit
    with open(path, "rb") as f:
        while left is None or left > 0:
            chunk = f.read(CHUNK if left is None else min(CHUNK, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


# --- the commands a user runs when HM may not write the device -------------------------------------


def privileged_commands(disk: Disk, image: str, nbytes: int, sha256: str) -> dict[str, Any]:
    """The exact commands for a device HM cannot open: write, then verify."""
    q = shlex.quote
    if disk.platform == "darwin":
        raw = disk.raw_path or disk.path.replace("/dev/disk", "/dev/rdisk")
        steps = [f"diskutil unmountDisk {q(disk.path)}", f"sudo dd if={q(image)} of={q(raw)} bs=4m",
                 "sync"]
        count = -(-nbytes // MIB)
        verify = (f"sudo dd if={q(raw)} bs=1m count={count} 2>/dev/null | head -c {nbytes} "
                  f"| shasum -a 256")
        expect = f"prints {sha256}"
    else:
        steps = [f"sudo umount {q(v.path)}" for v in disk.volumes
                 if v.mountpoints and not v.nested]
        steps.append(f"sudo dd if={q(image)} of={q(disk.path)} bs=4M conv=fsync status=progress")
        verify = f"sudo cmp -n {nbytes} {q(image)} {q(disk.path)} && echo verified"
        expect = "prints verified (cmp says nothing when the bytes match)"
    return {"privileged_command": " && ".join(steps), "privileged_steps": steps,
            "verify_command": verify, "verify_expect": expect, "image": image,
            "bytes": nbytes, "sha256": sha256}


# --- the MCC firmware selection in board.txt (MBBIOS) --------------------------------------------
#
# HM never changes which MCC firmware a configuration card selects. An ``MBBIOS:`` line names
# the .ebf the MCC flashes itself with when that file is on the card (a copied mbb_v141.ebf
# silently reflashes the MCC). The rule, for each board.txt a ``files`` write carries:
#
# - the card's board.txt HAS an MBBIOS line: it is kept (it replaces the bundle's; it is
#   added when the bundle's board.txt has none);
# - the card has none (or no board.txt): the bundle's line is written unchanged when the .ebf
#   it names is NOT on the card (the MCC finds nothing to flash); when it IS on the card the
#   write is refused, unless ``allow_mcc_update`` (``--allow-mcc-update``);
# - a board.txt is never written with an MBBIOS line removed.
#
# TODO(integration): use FIX-PACK-7's shared MBBIOS function (the sd writer path, rc2) here and
# delete this local copy; the rule is the same.

MBBIOS_LINE = re.compile(r"^[ \t]*MBBIOS[ \t]*:[ \t]*([^;\r\n]*)", re.IGNORECASE)
MCCS_HEADER = re.compile(r"^[ \t]*\[MCCS\]", re.IGNORECASE)
MCC_UPDATE_REFUSAL = ("this card would make the MCC update itself to {file}: remove {file} "
                      "from the card, or add --allow-mcc-update")


@dataclass(frozen=True)
class MbbiosDecision:
    file: str                  # the board.txt's SD path ("MB/HBI0309C/board.txt")
    action: str                # "kept" | "bundle" | "mcc-update" | "none"
    value: str                 # the MBBIOS value written ("" for none)
    note: str                  # what the CLI prints ("MBBIOS kept: mbb_v141.ebf")

    def to_json(self) -> dict[str, str]:
        return {"file": self.file, "action": self.action, "value": self.value, "note": self.note}


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")):] or "\n"


def mbbios_merge(bundle_text: str, card_text: str | None, ebf_on_card: Callable[[str], bool],
                 *, allow_mcc_update: bool = False, file: str = "board.txt"
                 ) -> tuple[str, MbbiosDecision]:
    """The board.txt to write, and what happened to its MBBIOS line (the rule above). Texts are
    latin-1 strings (byte for byte). ``RefusedError`` when the card would update its MCC."""
    bundle = _lines(bundle_text)
    card_lines = [ln for ln in _lines(card_text or "") if MBBIOS_LINE.match(ln)]
    at = [i for i, ln in enumerate(bundle) if MBBIOS_LINE.match(ln)]
    if card_lines:
        value = MBBIOS_LINE.match(card_lines[0]).group(1).strip()   # type: ignore[union-attr]
        if at:
            end = _ending(bundle[at[0]])
            keep = [ln.rstrip("\r\n") + end for ln in card_lines]
            out = [ln for i, ln in enumerate(bundle) if i not in at[1:]]
            first = at[0]
            out[first:first + 1] = keep
        else:
            end = _ending(bundle[0]) if bundle else "\n"
            keep = [ln.rstrip("\r\n") + end for ln in card_lines]
            hdr = next((i for i, ln in enumerate(bundle) if MCCS_HEADER.match(ln)), None)
            out = list(bundle)
            if out and not out[-1].endswith(("\n", "\r")):
                out[-1] += end
            if hdr is None:
                out += [f"[MCCS]{end}", *keep]
            else:
                out[hdr + 1:hdr + 1] = keep
        return "".join(out), MbbiosDecision(file, "kept", value, f"MBBIOS kept: {value}")
    if not at:
        return bundle_text, MbbiosDecision(file, "none", "",
                                           "MBBIOS: none (neither the card nor the bundle "
                                           "names one)")
    value = MBBIOS_LINE.match(bundle[at[0]]).group(1).strip()        # type: ignore[union-attr]
    if value and ebf_on_card(value):
        if not allow_mcc_update:
            raise RefusedError(MCC_UPDATE_REFUSAL.format(file=value),
                               hint=f"nothing was written: the card has no MBBIOS line of its "
                                    f"own and {value} is on it, so the MCC would flash it at "
                                    f"the next boot")
        return bundle_text, MbbiosDecision(
            file, "mcc-update", value,
            f"MBBIOS: {value} from the bundle (--allow-mcc-update: the MCC updates itself to "
            f"{value} at its next boot)")
    return bundle_text, MbbiosDecision(
        file, "bundle", value,
        f"MBBIOS: {value} from the bundle (the card had none, and {value} is not on the card: "
        f"the MCC does not update itself)")


def _resolve_ci(root: Path, rel: str) -> Path | None:
    """``rel`` under ``root`` the way FAT matches names (any case); None when it is not there."""
    cur = root
    for part in rel.replace("\\", "/").split("/"):
        try:
            match = next((e for e in cur.iterdir() if e.name.lower() == part.lower()), None)
        except OSError:
            return None
        if match is None:
            return None
        cur = match
    return cur


def mbbios_plan(files: Mapping[str, Path], root: str, *, allow_mcc_update: bool = False
                ) -> list[tuple[str, str, MbbiosDecision]]:
    """For each board.txt in ``files``: ``(sd path, the text to write, the decision)``, reading
    the card at ``root`` as it is now. ``RefusedError`` for an MCC self-update."""
    out = []
    for rel, src in sorted(files.items()):
        if rel.replace("\\", "/").rsplit("/", 1)[-1].lower() != "board.txt":
            continue
        on_card = _resolve_ci(Path(root), rel)
        card_text = on_card.read_bytes().decode("latin-1") if on_card and on_card.is_file() \
            else None
        where = rel.replace("\\", "/").rsplit("/", 1)[0] if "/" in rel.replace("\\", "/") else ""

        def ebf_on_card(name: str, where: str = where) -> bool:
            target = f"{where}/{name}" if where else name
            found = _resolve_ci(Path(root), target.replace("\\", "/"))
            return bool(found and found.is_file())

        text, decision = mbbios_merge(Path(src).read_bytes().decode("latin-1"), card_text,
                                      ebf_on_card, allow_mcc_update=allow_mcc_update, file=rel)
        out.append((rel, text, decision))
    return out


# --- the writer ----------------------------------------------------------------------------------

CHUNK = 4 * MIB
SYNC_EVERY = 32 * MIB
PROGRESS_EVERY_S = 0.1
Publish = Callable[[str, dict[str, Any]], None]


@dataclass
class WritePlan:
    """A write that passed every check before the job (``CardWriter.prepare``)."""

    device: CardDevice
    kind: str
    source: Path
    confirm: str
    card: CardImage | None = None
    files: dict[str, Path] = field(default_factory=dict)
    backup_path: Path | None = None
    backup_dir: Path | None = None
    allow_mcc_update: bool = False
    mbbios: list[MbbiosDecision] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"device_id": self.device.id, "path": self.device.disk.path,
                               "kind": self.kind, "source": str(self.source)}
        if self.card is not None:
            out["card"] = {"bytes": self.card.size, "slots": list(self.card.slots),
                           "hdr_crc": self.card.hdr_crc, "describe": self.card.describe(),
                           "notes": list(self.card.notes)}
        if self.files:
            out["files"] = sorted(self.files)
        if self.mbbios:
            out["mbbios"] = [d.to_json() for d in self.mbbios]
        return out


class CardWriter:
    """Lists this PC's card readers and writes a card (``files`` or ``card``).

    Every OS touch is injectable: ``lister`` (-> ``[Disk]``), ``access`` (the device seam),
    ``storage_for`` (root -> the pack's configuration-SD writer), ``platform``, ``enabled``
    and ``cap`` (the settings, read at each call), ``publish`` (events).
    """

    def __init__(self, *, state_dir: Path | str | None = None,
                 lister: Callable[[], list[Disk]] | None = None,
                 access: RealAccess | None = None,
                 storage_for: Callable[[str], Any] = pack_storage,
                 platform: str | None = None,
                 enabled: Callable[[], bool] | None = None,
                 cap: Callable[[], int] | None = None,
                 publish: Publish | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.platform = platform or sys.platform
        self.access = access or RealAccess(platform=self.platform)
        self.storage_for = storage_for
        self._lister = lister
        self._enabled = enabled or (lambda: is_enabled(self.state_dir))
        self._cap = cap or (lambda: size_cap(self.state_dir))
        self.publish = publish

    # -- the gate ---------------------------------------------------------------------------

    @property
    def supported(self) -> bool:
        return self.platform.startswith("linux") or self.platform == "darwin" \
            or self.access.simulated

    def refusal(self) -> UnavailableError | None:
        """Why nothing can be listed or written now (the setting, the platform); else None."""
        if not self._enabled():
            return UnavailableError(CAPABILITY, DISABLED_REASON,
                                    hint="turn it on: harness-manager config set "
                                         "bringup.sd_flash on")
        if not self.supported:
            return UnavailableError(CAPABILITY, WINDOWS_REASON)
        return None

    def require(self) -> None:
        err = self.refusal()
        if err is not None:
            raise err

    # -- listing ----------------------------------------------------------------------------

    def disks(self) -> list[Disk]:
        if self._lister is not None:
            return self._lister()
        if self.platform == "darwin":
            return mac_disks()
        return linux_disks()

    def listing(self) -> Listing:
        self.require()
        cap = self._cap()
        devices: list[CardDevice] = []
        excluded: list[Excluded] = []
        for disk in self.disks():
            why = exclusion(disk, cap)
            if why:
                excluded.append(Excluded(disk.name, disk.path, disk.display_model, disk.size, why))
                continue
            devices.append(self._card(disk))
        return Listing(tuple(devices), tuple(excluded))

    def _card(self, disk: Disk) -> CardDevice:
        kinds: dict[str, str] = {}
        fats = [v for v in disk.volumes if v.fstype in FAT_TYPES and not v.nested]
        mounted = [v for v in fats if v.mountpoints]
        root = ""
        if not fats:
            kinds["files"] = "no FAT volume on it (a configuration SD is FAT)"
        elif not mounted:
            kinds["files"] = (f"its FAT volume ({fats[0].path}) is not mounted: open it in your "
                              f"file manager, or `udisksctl mount -b {fats[0].path}`")
        elif len(mounted) > 1:
            kinds["files"] = (f"{len(mounted)} FAT volumes are mounted "
                              f"({', '.join(v.mountpoints[0] for v in mounted)}): HM writes a "
                              f"card with one")
        else:
            root = mounted[0].mountpoints[0]
            kinds["files"] = files_check(self.storage_for, root)
        if MCC_LABEL in [lb.upper() for lb in disk.labels]:
            kinds["card"] = (f"this card is an MPS3 configuration SD ({MCC_LABEL}): a card image "
                             f"would erase the board's configuration")
        else:
            kinds["card"] = ""
        needs = not self.access.can_write(disk.io_path)
        return CardDevice(disk=disk, kinds=kinds, needs_privilege=needs, files_root=root)

    def devices_json(self) -> dict[str, Any]:
        """``GET /cardwriter/devices``: ``{enabled, reason?, supported, devices, excluded}``."""
        base: dict[str, Any] = {"enabled": False, "supported": self.supported,
                                "platform": self.platform, "simulated": self.access.simulated,
                                "devices": [], "excluded": []}
        err = self.refusal()
        if err is not None:
            return {**base, "reason": err.reason}
        listing = self.listing()
        return {**base, "enabled": True, "cap_bytes": self._cap(),
                "devices": [c.to_json() for c in listing.devices],
                "excluded": [e.to_json() for e in listing.excluded]}

    def find(self, device_id: str) -> CardDevice:
        """The listed device with this id NOW. A changed or swapped card has another id."""
        listing = self.listing()
        card = listing.find(device_id)
        if card is not None:
            return card
        name = device_id.rsplit("-", 1)[0]
        same = next((c for c in listing.devices if c.disk.name == name), None)
        if same is not None:
            raise RefusedError(
                f"{same.disk.path} changed since it was listed (now {same.disk.display_model} "
                f"{human_size(same.disk.size)}, id {same.id}): a different card?",
                hint="list the devices again, check it is the card you mean, and type its "
                     "phrase again")
        gone = next((e for e in listing.excluded if e.name == name), None)
        if gone is not None:
            raise RefusedError(f"{gone.path} is not a card reader's card now: {gone.why}",
                               hint="list the devices again")
        raise AbsentError(f"no card reader device {device_id!r} is attached now",
                          hint="list the devices again (`harness-manager flash devices`)")

    # -- before the job ---------------------------------------------------------------------

    def prepare(self, device_id: str, kind: str, source: Path | str, confirm: str, *,
                backup_path: Path | str | None = None,
                backup_dir: Path | str | None = None,
                allow_mcc_update: bool = False) -> WritePlan:
        """Every check a write needs before it starts; ``WritePlan`` or the refusal."""
        self.require()
        if kind not in KINDS:
            raise UsageError(f"kind must be files or card, not {kind!r}")
        if not isinstance(device_id, str) or not device_id:
            raise UsageError("device_id must name a listed device")
        source = Path(source)
        card = self.find(device_id)
        why = card.kinds.get(kind, "")
        if why:
            raise RefusedError(f"cannot write {kind} to {card.disk.path}: {why}",
                               hint="pick another card, or the other kind")
        want = card.confirm
        if not isinstance(confirm, str) or confirm.strip() != want:
            err = RefusedError(f"not confirmed: type exactly {want!r} to write {card.disk.path}",
                               hint="the phrase names the card's model and size as listed; "
                                    "nothing was written")
            err.data = {"confirm": want, "device_id": card.id}  # type: ignore[attr-defined]
            raise err
        plan = WritePlan(device=card, kind=kind, source=source, confirm=want)
        if kind == "card":
            image = inspect_card(source)
            if image.size > card.disk.size:
                raise RefusedError(
                    f"the card image is {human_size(image.size)}; {card.disk.path} holds "
                    f"{human_size(card.disk.size)}", hint="use a bigger card; nothing was written")
            plan.card = image
            return plan
        plan.files = self._bundle(source)
        if backup_path is None and backup_dir is None:
            raise RefusedError("writing a configuration SD needs a backup of it first",
                               hint="give a backup of this card (--backup ZIP), or a directory "
                                    "HM backs it up into first (--backup-dir DIR)")
        plan.backup_path = Path(backup_path) if backup_path is not None else None
        plan.backup_dir = Path(backup_dir) if backup_dir is not None else None
        if plan.backup_path is not None and not plan.backup_path.is_file():
            raise AbsentError(f"no backup archive at {plan.backup_path}",
                              hint="give the zip `harness-manager sd ... backup` (or a "
                                   "previous card write) made")
        if plan.backup_dir is not None and plan.backup_dir.exists() \
                and not plan.backup_dir.is_dir():
            raise UsageError(f"{plan.backup_dir} exists and is not a directory",
                             hint="give a directory for the backup archive")
        plan.allow_mcc_update = bool(allow_mcc_update)
        plan.mbbios = [d for _, _, d in mbbios_plan(plan.files, card.files_root,
                                                     allow_mcc_update=plan.allow_mcc_update)]
        return plan

    @staticmethod
    def _bundle(source: Path) -> dict[str, Path]:
        from harness_manager.cli.cmd_board import bundle_files

        files = bundle_files(source)           # AbsentError / RefusedError (.ebf) as `sd install`
        ebf = sorted(k for k in files if k.lower().endswith(".ebf"))
        if ebf:                                 # belt and braces: never an .ebf
            raise RefusedError(f"{source} contains board-controller firmware ({', '.join(ebf)})",
                               hint=".ebf files are never written to the SD; remove them")
        return files

    # -- the job ----------------------------------------------------------------------------

    def run(self, plan: WritePlan, progress: Progress | None = None) -> dict[str, Any]:
        """Write ``plan`` (on a job's thread). The device is listed again first."""
        emit = self._emitter(plan, progress)
        with self._device_lock(plan.device.disk):
            card = self.find(plan.device.id)            # still the card the user confirmed
            if card.confirm != plan.confirm:
                raise RefusedError(f"{card.disk.path} changed since it was confirmed",
                                   hint="list the devices again")
            if plan.kind == "files":
                return self._run_files(plan, card, emit)
            return self._run_card(plan, card, emit)

    def _emitter(self, plan: WritePlan, progress: Progress | None) -> Progress:
        last = {"phase": "", "at": 0.0}

        def emit(phase: str, done: int, total: int) -> None:
            if progress is not None:
                progress(phase, done, total)
            if self.publish is None:
                return
            now = time.monotonic()
            due = phase != last["phase"] or (total and done >= total) \
                or now - last["at"] >= PROGRESS_EVERY_S
            if due:
                last["phase"], last["at"] = phase, now
                self.publish("cardwriter.progress", {"device_id": plan.device.id,
                                                     "kind": plan.kind, "phase": phase,
                                                     "bytes": int(done), "total": int(total)})
        return emit

    def _done(self, plan: WritePlan, **data: Any) -> None:
        if self.publish is not None:
            self.publish("cardwriter.done", {"device_id": plan.device.id, "kind": plan.kind,
                                             **data})

    @contextlib.contextmanager
    def _device_lock(self, disk: Disk) -> Iterator[None]:
        """One write per device, across this process and others (the CLI and the app)."""
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", disk.path)
        with _LOCAL_MU:
            if key in _LOCAL:
                raise HeldError(f"a card write to {disk.path} is already running here",
                                hint="wait for it to finish; a slow write is still a write")
            _LOCAL.add(key)
        fh = None
        try:
            base = self.state_dir if self.state_dir is not None else _default_state_dir()
            lock_dir = base / "cardwriter" / "locks"
            try:
                import fcntl
            except ImportError:            # Windows: not supported anyway (one process)
                fcntl = None  # type: ignore[assignment]
            if fcntl is not None:
                lock_dir.mkdir(parents=True, exist_ok=True)
                fh = open(lock_dir / f"{key}.lock", "a+")   # noqa: SIM115 - held for the write
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise HeldError(f"another Harness Manager is writing {disk.path}",
                                    hint="wait for it to finish; never write a card twice at "
                                         "once") from exc
            yield
        finally:
            if fh is not None:
                fh.close()
            with _LOCAL_MU:
                _LOCAL.discard(key)

    # -- files ------------------------------------------------------------------------------

    def _run_files(self, plan: WritePlan, card: CardDevice, emit: Progress) -> dict[str, Any]:
        storage = self.storage_for(card.files_root)
        storage.locate()                                  # the pack's own checks, again

        def relay(phase: str, done: int, total: int) -> None:
            emit({"install": "write"}.get(phase, phase), done, total)

        if plan.backup_path is not None:
            record: BackupRecord = storage.load_backup(plan.backup_path)
            took = False
        else:
            assert plan.backup_dir is not None
            plan.backup_dir.mkdir(parents=True, exist_ok=True)
            record = storage.backup(plan.backup_dir, progress=relay)
            took = True
        # The card as it is now decides each board.txt's MBBIOS line (refused: nothing written).
        merged = mbbios_plan(plan.files, card.files_root, allow_mcc_update=plan.allow_mcc_update)
        plan.mbbios = [d for _, _, d in merged]
        files = dict(plan.files)
        base = self.state_dir if self.state_dir is not None else _default_state_dir()
        staged = base / "cardwriter" / "staged" / f"{os.getpid()}-{time.monotonic_ns()}"
        try:
            for rel, text, _ in merged:
                dest = staged.joinpath(*rel.replace("\\", "/").split("/"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(text.encode("latin-1"))
                files[rel] = dest
            storage.install(files, backup=record, progress=relay)
            digest = hashlib.sha256("\n".join(
                f"{rel}  {file_sha256(src)}" for rel, src in sorted(files.items())).encode()
            ).hexdigest()
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        result = {"outcome": "written", "verified": True, "sha256": digest,
                  "files": sorted(files), "root": card.files_root,
                  "mbbios": [d.to_json() for d in plan.mbbios], "backup": {
                      "path": record.path, "sha256": record.sha256, "files": record.files,
                      "taken": took},
                  "note": "written and read back; the board runs it after it boots from this "
                          "card (put it back, then reboot or power-cycle the board)"}
        self._done(plan, verified=True, sha256=digest, outcome="written")
        return {**plan.summary(), **result}

    # -- image ------------------------------------------------------------------------------

    def _run_card(self, plan: WritePlan, card: CardDevice, emit: Progress) -> dict[str, Any]:
        disk = card.disk
        assert plan.card is not None
        again = inspect_card(plan.card.source)              # the file may have changed since
        src, nbytes, sha = Path(again.source), again.size, file_sha256(Path(again.source))
        if nbytes > disk.size:
            raise RefusedError(f"{human_size(nbytes)} does not fit on {disk.path} "
                               f"({human_size(disk.size)})", hint="use a bigger card")
        if not self.access.can_write(disk.io_path):
            cmds = privileged_commands(disk, str(src), nbytes, sha)
            why = (f"this user may not write {disk.io_path} (Harness Manager never asks for "
                   f"root): run the command yourself")
            self._done(plan, verified=False, sha256=sha, outcome="needs_privilege", **{
                k: cmds[k] for k in ("privileged_command", "verify_command", "verify_expect")})
            return {**plan.summary(), "outcome": "needs_privilege", "verified": False,
                    "why": why, **cmds}
        emit("unmount", 0, 1)
        self.access.unmount(disk)
        emit("unmount", 1, 1)
        now = self.find(plan.device.id)                    # still the same card, now unmounted
        if now.disk.mountpoints:
            raise RefusedError(f"{disk.path} is still mounted ({', '.join(now.disk.mountpoints)})",
                               hint="unmount it, then retry; nothing was written")
        written = self._write(disk, src, nbytes, emit)
        if isinstance(self.access, FileAccess):
            self.access.written(disk.io_path)
        got = self._read_back(disk, nbytes, emit)
        if got != sha:
            self._done(plan, verified=False, sha256=sha, read_back=got, outcome="verify_failed")
            raise ActionFailedError(
                f"read-back mismatch on {disk.path}: wrote sha256 {sha[:12]}…, read {got[:12]}…",
                hint="the card or the reader is failing: try another card, then write again")
        result = {"outcome": "written", "verified": True, "sha256": sha, "bytes": written,
                  "written_from": str(src),
                  "note": "written and read back; put the card in the board's USER microSD "
                          "slot and power it on"}
        self._done(plan, verified=True, sha256=sha, outcome="written")
        return {**plan.summary(), **result}

    def _write(self, disk: Disk, src: Path, nbytes: int, emit: Progress) -> int:
        path = disk.io_path
        aligned = self.access.aligned(path)
        try:
            fd = self.access.open_write(path)
        except PermissionError as exc:
            raise RefusedError(f"cannot open {path} for writing: {exc.strerror}",
                               hint="run the privileged command instead (list the device "
                                    "again: the job says it)") from exc
        except OSError as exc:
            hint = ("it is still mounted or in use: unmount it, then retry"
                    if exc.errno == 16 else "check the card reader")
            raise ActionFailedError(f"cannot open {path}: {exc.strerror}", hint=hint) from exc
        done = 0
        since_sync = 0
        try:
            emit("write", 0, nbytes)
            with src.open("rb") as f:
                while done < nbytes:
                    chunk = f.read(min(CHUNK, nbytes - done))
                    if not chunk:
                        raise ActionFailedError(f"{src} ended at {done} of {nbytes} bytes",
                                                hint="the image changed while it was written")
                    data = chunk
                    if aligned and len(data) % BLOCK:
                        data = data + b"\0" * (BLOCK - len(data) % BLOCK)
                    view = memoryview(data)
                    while view:
                        n = os.write(fd, view)
                        view = view[n:]
                    done += len(chunk)
                    since_sync += len(chunk)
                    if since_sync >= SYNC_EVERY:
                        os.fsync(fd)
                        since_sync = 0
                    emit("write", done, nbytes)
            os.fsync(fd)
        except OSError as exc:
            raise ActionFailedError(f"writing {path} failed at {done} of {nbytes} bytes: "
                                    f"{exc.strerror}",
                                    hint="the card is now partly written: write it again") from exc
        finally:
            os.close(fd)
        return done

    def _read_back(self, disk: Disk, nbytes: int, emit: Progress) -> str:
        path = disk.io_path
        aligned = self.access.aligned(path)
        fd = self.access.open_read(path)
        h = hashlib.sha256()
        got = 0
        try:
            self.access.drop_cache(fd)
            emit("verify", 0, nbytes)
            while got < nbytes:
                want = min(CHUNK, nbytes - got)
                ask = want + (-want % BLOCK) if aligned else want
                chunk = os.read(fd, ask)
                if not chunk:
                    break
                chunk = chunk[:want]
                h.update(chunk)
                got += len(chunk)
                emit("verify", got, nbytes)
        finally:
            os.close(fd)
        return h.hexdigest()


_LOCAL: set[str] = set()
_LOCAL_MU = threading.Lock()


def _default_state_dir() -> Path:
    from harness_manager.settings.files import config_dir

    return Path(config_dir(None))


# --- --demo: simulated readers (never a real device) ----------------------------------------------


def demo_writer(state_dir: Path, *, publish: Publish | None = None) -> CardWriter:
    """``harness-manager app --demo``: two simulated card readers (temp files under the demo's
    own state dir) and the disks a laptop would exclude. Never lists or writes a real device."""
    root = Path(state_dir) / "cardwriter-demo"
    cfg = root / "V2M-MPS3"
    (cfg / "MB").mkdir(parents=True, exist_ok=True)
    if not (cfg / "config.txt").exists():
        (cfg / "config.txt").write_text("TITLE: Versatile Express Images Configuration File\n",
                                        encoding="utf-8")
    files = {"/dev/sdb": root / "sdb.img", "/dev/sdc": root / "sdc.img"}
    for f in files.values():
        if not f.exists():
            with f.open("wb") as fh:
                fh.truncate(4 * MIB)
    disks = [
        Disk("nvme0n1", "/dev/nvme0n1", 512_110_190_592, model="Samsung SSD 980 PRO 512GB",
             transport="nvme", volumes=(Volume("nvme0n1p2", "/dev/nvme0n1p2", fstype="ext4",
                                               mountpoints=("/",)),)),
        Disk("sdb", "/dev/sdb", 31_914_983_424, model="SD/MMC", vendor="Generic-",
             transport="usb", removable=True, hotplug=True,
             volumes=(Volume("sdb1", "/dev/sdb1", 31_910_789_120, "vfat", "V2M-MPS3", "4A1B-0C2D",
                             (str(cfg),)),)),
        Disk("sdc", "/dev/sdc", 15_931_539_456, model="MicroSD/M2", vendor="Generic-",
             transport="usb", removable=True, hotplug=True,
             volumes=(Volume("sdc1", "/dev/sdc1", 15_927_345_152, "vfat", "NO NAME", "1C2E-3F40",
                             ()),)),
        Disk("sdd", "/dev/sdd", 2_000_398_934_016, model="Expansion HDD", vendor="Seagate",
             transport="usb", hotplug=True),
    ]

    def storage_for(mount: str) -> Any:
        from harness_manager_mps3.sd import Mps3Storage, SdEnv, VolumeInfo

        return Mps3Storage(mount, env=SdEnv(list_volumes=lambda: [VolumeInfo("V2M-MPS3", str(cfg))]))

    return CardWriter(state_dir=state_dir, lister=lambda: list(disks),
                      access=FileAccess(files), storage_for=storage_for, platform="linux",
                      publish=publish)
