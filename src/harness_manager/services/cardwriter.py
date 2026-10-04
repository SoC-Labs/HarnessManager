"""Write SD cards in THIS PC's card reader, safely (lane SD-FLASH, the bring-up wave).

The "SD flashing" door of bring-up: the alternative to writing the board's configuration SD
over its Debug USB. Board-agnostic: it lists this computer's card readers, and writes one of
two things onto the card in one of them:

- ``files``: a configuration-SD file tree (a harness bundle laid out like the SD) onto the
  mounted FAT volume of a card taken out of the board (the MPS3's ``V2M-MPS3`` card). The
  writer is the board pack's own configuration-SD writer (``<pack package>.sd:
  make_storage_adapter``, the module convention ``pack.py`` uses), so its rules hold here
  too: a verified backup of the card as it is NOW first (taken by the job, or given), never
  an ``.ebf`` (it reflashes the MCC), never an MCC command file, journaled, read back. And
  the card's MCC firmware selection is never changed (the card's ``MBBIOS:`` line in
  board.txt is kept: the pack's rule through the pack hook, see "the MCC firmware selection"
  below).
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
- **The typed phrases.** ``WRITE <model> <size>`` exactly as listed (``confirm``), AND, since
  what is written is unsigned (a bundle folder or zip, a whole-card image), ``INSTALL UNSIGNED
  <first 8 hex of its sha256>`` (``services/unsigned.py``: a zip's or an image's own sha256, a
  folder's manifest). ``run`` refuses a plan without it, and the sha256 is checked again just
  before the first byte (a source changed since its phrase was typed is refused).
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

Windows (lane WINDOWS, HM v1.1.0): the disks come from PowerShell (``Get-Disk``,
``Get-Partition``/``Get-Volume``, ``Win32_DiskDrive``: ``windows_disks``), read only. The
same rails, in Windows' words: only BusType USB/SD/MMC with removable media; never the
system or boot disk (``IsSystem``/``IsBoot``, or a volume on ``%SystemDrive%``); never the
MPS3's MCC drive (``V2M-MPS3`` on an ARM/V2M device) or DAPLink. ``files`` writes the
card-reader volume's drive letter (``E:``) through the pack's writer, no Administrator
needed. ``card`` (a raw whole-card image onto ``PhysicalDriveN``) needs Administrator,
and HM NEVER writes a raw disk on Windows, even when it runs elevated: the job ends
``needs_privilege`` with copy-pasteable Admin PowerShell steps (check the disk number is the
card, ``diskpart clean``, write every sector but the first, then the first, so Windows mounts
nothing mid-write) and the standard imager (Raspberry Pi Imager, "Use custom"), the image's
sha256 to check first, and an Admin PowerShell read-back that prints the sha256
(``privileged_commands``). Only proven on a real Windows laptop: the checklist in the lane's
hand-back.
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
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from harness_manager.core import winps
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

from . import unsigned as _unsigned
from .update import s0lb

# --- settings ------------------------------------------------------------------------------

SETTING = "bringup.sd_flash"
MAX_SETTING = "bringup.sd_flash_max"
ENABLE_ENV = "HARNESS_MANAGER_BRINGUP_SD_FLASH"          # the rows' variables (settings/rows.py)
MAX_ENV = "HARNESS_MANAGER_BRINGUP_SD_FLASH_MAX"
DEFAULT_MAX = 256_000_000_000
CAPABILITY = "sd_flash"
DISABLED_REASON = "SD flashing is turned off (Settings → Bring-up, bringup.sd_flash)"
#: Kept for callers of the old name: Windows is supported now (lane WINDOWS); only a platform
#: with no lister (not Linux, macOS or Windows) is refused.
WINDOWS_REASON = UNSUPPORTED_REASON = (
    "writing SD cards in this PC's card reader is not supported on this operating system "
    "(Linux, macOS and Windows only): write the configuration SD over the board's Debug USB "
    "instead")
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
    system: bool = False             # Windows: Get-Disk IsSystem/IsBoot (holds Windows)
    number: int = -1                 # Windows: the disk number (Get-Disk -Number N)

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
    """Run a read-only listing command (lsblk, diskutil list/info, PowerShell Get-Disk).
    Never a shell; no console window on Windows."""
    from harness_manager.core.proc import no_window

    try:
        cp = subprocess.run(list(argv), capture_output=True, timeout=timeout, check=False,
                            stdin=subprocess.DEVNULL, **no_window())
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


# --- Windows: PowerShell (read only) -------------------------------------------------------------

#: One read-only PowerShell question: every disk, its partitions with their volumes, and
#: Win32_DiskDrive's media type (Get-Disk has no "removable"). Strings are forced where a
#: CIM enum would arrive as a number on one host and a name on another.
WINDOWS_DISKS_PS = (
    "$disks = @(Get-Disk | ForEach-Object { [pscustomobject]@{ Number = [int]$_.Number; "
    "FriendlyName = [string]$_.FriendlyName; Model = [string]$_.Model; "
    "Manufacturer = [string]$_.Manufacturer; SerialNumber = [string]$_.SerialNumber; "
    "Size = [uint64]$_.Size; BusType = [string]$_.BusType; IsSystem = [bool]$_.IsSystem; "
    "IsBoot = [bool]$_.IsBoot; IsOffline = [bool]$_.IsOffline; "
    "PartitionStyle = [string]$_.PartitionStyle } }); "
    "$parts = @(Get-Partition -ErrorAction SilentlyContinue | ForEach-Object { $v = $null; "
    "try { $v = Get-Volume -Partition $_ -ErrorAction Stop } catch { }; "
    "[pscustomobject]@{ Disk = [int]$_.DiskNumber; Number = [int]$_.PartitionNumber; "
    "Letter = [string]$_.DriveLetter; Size = [uint64]$_.Size; "
    "Label = $(if ($v) { [string]$v.FileSystemLabel } else { '' }); "
    "Fs = $(if ($v) { [string]$v.FileSystem } else { '' }); "
    "Id = $(if ($v) { [string]$v.UniqueId } else { '' }) } }); "
    "$drives = @(Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue | "
    "ForEach-Object { [pscustomobject]@{ Index = [int]$_.Index; "
    "MediaType = [string]$_.MediaType; InterfaceType = [string]$_.InterfaceType; "
    "PNPDeviceID = [string]$_.PNPDeviceID } }); "
    "[pscustomobject]@{ disks = $disks; parts = $parts; drives = $drives; "
    "system = [string]$env:SystemDrive } | ConvertTo-Json -Depth 5 -Compress")

#: MSFT_Disk.BusType (a number from Windows PowerShell 5.1's CIM, a name elsewhere).
WIN_BUS_TYPES = {0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA",
                 6: "Fibre Channel", 7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA",
                 12: "SD", 13: "MMC", 14: "Virtual", 15: "File Backed Virtual",
                 16: "Storage Spaces", 17: "NVMe"}
WIN_PARTITION_STYLES = {0: "RAW", 1: "MBR", 2: "GPT"}
_WIN_FS = {"fat32": "vfat", "fat": "vfat", "fat16": "vfat", "fat12": "vfat", "exfat": "exfat",
           "ntfs": "ntfs", "refs": "refs"}


def windows_disk_path(number: int) -> str:
    return f"\\\\.\\PhysicalDrive{number}"


def parse_windows_disks(doc: Any) -> list[Disk]:
    """``WINDOWS_DISKS_PS``'s answer -> whole disks with their volumes (``platform`` win32)."""
    doc = doc if isinstance(doc, dict) else {}
    system_drive = winps.text(doc.get("system")).rstrip("\\").upper()       # "C:"
    parts: dict[int, list[dict[str, Any]]] = {}
    for p in winps.as_list(doc.get("parts")):
        if isinstance(p, dict):
            parts.setdefault(_int(p.get("Disk")), []).append(p)
    drives = {_int(d.get("Index")): d for d in winps.as_list(doc.get("drives"))
              if isinstance(d, dict)}
    disks: list[Disk] = []
    for d in winps.as_list(doc.get("disks")):
        if not isinstance(d, dict):
            continue
        n = _int(d.get("Number"))
        drive = drives.get(n, {})
        pnp = winps.text(drive.get("PNPDeviceID"))
        m = re.search(r"VEN_([^&\\]*)&PROD_([^&\\]*)", pnp, re.IGNORECASE)
        pnp_vendor, pnp_model = ((m.group(1).replace("_", " ").strip(),
                                  m.group(2).replace("_", " ").strip()) if m else ("", ""))
        bus = winps.enum_name(d.get("BusType"), WIN_BUS_TYPES)
        tran = {"USB": "usb", "SD": "sd", "MMC": "mmc"}.get(bus, bus.lower())
        media = winps.text(drive.get("MediaType")).lower()
        vols: list[Volume] = []
        letters: list[str] = []
        for p in sorted(parts.get(n, []), key=lambda q: _int(q.get("Number"))):
            pn = _int(p.get("Number"))
            letter = re.sub(r"[^A-Za-z]", "", winps.text(p.get("Letter")))[:1].upper()
            root = f"{letter}:\\" if letter else ""
            if letter:
                letters.append(f"{letter}:")
            fs = winps.text(p.get("Fs"))
            vols.append(Volume(
                name=f"PhysicalDrive{n}p{pn}", path=root or f"\\\\.\\PhysicalDrive{n}\\Partition{pn}",
                size=_int(p.get("Size")), fstype=_WIN_FS.get(fs.lower(), fs.lower()),
                label=winps.text(p.get("Label")), uuid=winps.text(p.get("Id")),
                mountpoints=(root,) if root else ()))
        system = bool(d.get("IsSystem")) or bool(d.get("IsBoot")) or \
            bool(system_drive and system_drive in letters)
        disks.append(Disk(
            name=f"PhysicalDrive{n}", path=windows_disk_path(n), size=_int(d.get("Size")),
            dtype="disk" if bus not in ("File Backed Virtual", "Virtual") else "loop",
            model=winps.text(d.get("FriendlyName")) or winps.text(d.get("Model")) or pnp_model,
            vendor=winps.text(d.get("Manufacturer")) or pnp_vendor,
            serial=winps.text(d.get("SerialNumber")), transport=tran,
            removable="removable" in media, hotplug=False,
            mmc_type="SD" if bus == "SD" else "", volumes=tuple(vols), platform="win32",
            system=system, number=n))
    return disks


def windows_disks(run: Runner | None = None) -> list[Disk]:
    """This PC's disks through PowerShell (read only); ``run`` is the test seam."""
    run = run or (lambda argv: run_command(argv, 45.0))   # looked up per call: the tests' guard
    doc = winps.run_json(WINDOWS_DISKS_PS, what="list the disks (Get-Disk)",
                         capability=CAPABILITY, run=run)
    return parse_windows_disks(doc)


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
    if disk.system:
        return "holds Windows (this PC's system or boot disk)"
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
    # "V2M_MPS3" (a Windows PNP id spells it with _) must still match the word V2M/MPS3
    who = " ".join((disk.vendor, disk.model)).replace("_", " ")
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
        """A device node under /dev: a block device (and on macOS the raw /dev/rdiskN). On
        Windows nothing is ever opened raw (``needs_privilege``)."""
        if winps.is_windows(self.platform):
            raise RefusedError(f"Harness Manager never writes a raw disk on Windows ({path})",
                               hint="run the Administrator steps the write printed")
        if not str(path).startswith("/dev/"):
            raise RefusedError(f"{path} is not under /dev: refusing to write it",
                               hint="only a listed card reader's device is ever written")
        try:
            st = os.stat(path)
        except FileNotFoundError as exc:
            raise AbsentError(f"{path} is gone (the card reader was unplugged?)",
                              hint="list the devices again") from exc
        raw_ok = self.platform == "darwin" and stat.S_ISCHR(st.st_mode)
        if not (stat.S_ISBLK(st.st_mode) or raw_ok):
            raise RefusedError(f"{path} is not a block device: refusing to write it",
                               hint="only a listed card reader's device is ever written")

    def can_write(self, path: str) -> bool:
        if winps.is_windows(self.platform):
            return False              # a raw disk needs Administrator: HM never escalates
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


#: How a Windows user opens the shell the steps need (HM never opens it for them).
WIN_ADMIN_HOW = ("open PowerShell as Administrator (Start, type PowerShell, right-click "
                 "Windows PowerShell, Run as administrator, Yes), then paste each step in turn")
#: A raw-disk handle for Windows PowerShell 5.1 (.NET Framework's FileStream refuses a
#: \\.\ path; a handle from CreateFile is allowed).
WIN_RAW_TYPE = ("Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
                "using Microsoft.Win32.SafeHandles;public static class HmRawDisk{"
                "[DllImport(\"kernel32.dll\",SetLastError=true,CharSet=CharSet.Unicode)]"
                "public static extern SafeFileHandle CreateFile(string n,uint a,uint s,IntPtr p,"
                "uint c,uint f,IntPtr t);}'")
RASPBERRY_PI_IMAGER = "https://www.raspberrypi.com/software/"


def _ps(text: str) -> str:
    """A PowerShell single-quoted string (a ' inside is doubled)."""
    return "'" + str(text).replace("'", "''") + "'"


def windows_privileged_commands(disk: Disk, image: str, nbytes: int,
                                sha256: str) -> dict[str, Any]:
    """Windows: the Admin PowerShell steps (and the standard imager) for a whole-card image.

    1. check that disk N is still the card HM listed (its size; never the system or boot
       disk), else stop;
    2. ``diskpart clean``: the card's partitions go, so Windows holds none of its volumes;
    3. write every sector but the first, then the first (the MBR): Windows sees no
       partition table, so mounts nothing, until the last write;
    4. ``Update-Disk``: Windows reads the new partition table.

    The read-back (``verify_command``) prints the sha256 of the card's first ``nbytes``.
    """
    n = disk.number if disk.number >= 0 else int(re.sub(r"\D", "", disk.name) or -1)
    dev = _ps(f"\\\\.\\PhysicalDrive{n}")
    img = _ps(image)
    want = f"{disk.display_model} {human_size(disk.size)}"
    clean = f"$env:TEMP\\hm-clean-disk{n}.txt"
    steps = [
        f"$d = Get-Disk -Number {n}; if ($d.Size -ne {disk.size} -or $d.IsSystem -or "
        f"$d.IsBoot) {{ throw {_ps(f'Disk {n} is not the card Harness Manager listed ({want}): stop')} }}; "
        f"$d | Format-Table Number, FriendlyName, BusType, Size",
        f"Set-Content -Path \"{clean}\" -Value 'select disk {n}', 'clean'; "
        f"diskpart /s \"{clean}\"",
        WIN_RAW_TYPE,
        f"$h = [HmRawDisk]::CreateFile({dev}, 3221225472, 3, [IntPtr]::Zero, 3, 0, "
        f"[IntPtr]::Zero); if ($h.IsInvalid) {{ throw 'cannot open disk {n}: is this "
        f"PowerShell running as Administrator?' }}; "
        f"$dst = New-Object IO.FileStream($h, [IO.FileAccess]::ReadWrite); "
        f"$src = [IO.File]::OpenRead({img}); $buf = New-Object byte[] 4194304; "
        f"[void]$src.Seek(512, 'Begin'); [void]$dst.Seek(512, 'Begin'); "
        f"while (($k = $src.Read($buf, 0, $buf.Length)) -gt 0) {{ if ($k % 512) {{ "
        f"[Array]::Clear($buf, $k, 512 - $k % 512); $k += 512 - $k % 512 }}; "
        f"$dst.Write($buf, 0, $k) }}; [void]$src.Seek(0, 'Begin'); "
        f"[void]$src.Read($buf, 0, 512); [void]$dst.Seek(0, 'Begin'); "
        f"$dst.Write($buf, 0, 512); $dst.Flush(); $dst.Close(); $src.Close(); 'written'",
        f"Update-Disk -Number {n}",
    ]
    verify = (
        f"{WIN_RAW_TYPE}; $h = [HmRawDisk]::CreateFile({dev}, 2147483648, 3, [IntPtr]::Zero, "
        f"3, 0, [IntPtr]::Zero); $f = New-Object IO.FileStream($h, [IO.FileAccess]::Read); "
        f"$sha = [Security.Cryptography.SHA256]::Create(); $buf = New-Object byte[] 4194304; "
        f"$left = [long]{nbytes}; while ($left -gt 0) {{ "
        f"$want = [int][Math]::Min([long]$buf.Length, $left); "
        f"$ask = [int]([Math]::Ceiling($want / 512) * 512); $got = $f.Read($buf, 0, $ask); "
        f"if ($got -le 0) {{ break }}; $use = [int][Math]::Min($got, $want); "
        f"[void]$sha.TransformBlock($buf, 0, $use, $null, 0); $left -= $use }}; "
        f"[void]$sha.TransformFinalBlock($buf, 0, 0); $f.Close(); "
        f"-join ($sha.Hash | ForEach-Object {{ $_.ToString('x2') }})")
    imager = {
        "name": "Raspberry Pi Imager", "url": RASPBERRY_PI_IMAGER,
        "check_command": f"Get-FileHash -Algorithm SHA256 {img}",
        "check_expect": f"its Hash is {sha256.upper()}",
        "steps": [
            f"check the image first (any PowerShell): Get-FileHash -Algorithm SHA256 {img} "
            f"shows Hash {sha256.upper()}",
            "open Raspberry Pi Imager (it asks for Administrator itself: Yes)",
            f"Choose OS: Use custom, then {image}",
            f"Choose Storage: {want} (nothing else)",
            "Next; No to OS customisation; Yes to erase the card. It writes, then verifies "
            "what it wrote",
        ],
    }
    return {"privileged_command": "\n".join(steps), "privileged_steps": steps,
            "privileged_shell": "powershell_admin", "privileged_how": WIN_ADMIN_HOW,
            "verify_command": verify, "verify_expect": f"prints {sha256}",
            "verify_how": "in the same Administrator PowerShell", "imager": imager,
            "disk_number": n, "image": image, "bytes": nbytes, "sha256": sha256}


def privileged_commands(disk: Disk, image: str, nbytes: int, sha256: str) -> dict[str, Any]:
    """The exact commands for a device HM cannot open: write, then verify."""
    q = shlex.quote
    if winps.is_windows(disk.platform):
        return windows_privileged_commands(disk, image, nbytes, sha256)
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
# HM never changes which MCC firmware a configuration card selects. ``MB/HBI0309C/board.txt``'s
# ``MBBIOS:`` line names the .ebf the MCC flashes ITSELF with when that file is on the card.
# The rule is the BOARD PACK's, not this module's: the MPS3's is
# ``harness_manager_mps3.mbbios.keep_mbbios`` (the card's line is kept; a card whose .ebf the
# bundle's line would select is refused unless ``allow_mcc_update``; a board.txt is never
# written with the line removed).
#
# THE PACK HOOK, found by the same module convention as the pack's configuration-SD writer
# (``<pack package>.sd:make_storage_adapter``): ``<pack package>.mbbios:keep_mbbios(files, *,
# card_board_txt, card_files, workdir, allow_mcc_update=False) -> (files, decision | None)``
# (``pack_mbbios``). When the same module has ``card_boards_of(root)`` (the MPS3's, FIX-PACK-9:
# every ``MB/HBI*/board.txt`` on the card), the rule is called with ``card_boards=`` instead of
# ``card_board_txt=``, so each revision folder's line is kept against the card's own; the result
# shows one decision per revision and each warning (``decisions_json``). A storage adapter whose
# ``install`` takes ``allow_mcc_update`` applies the rule itself (the MPS3's ``Mps3Storage``; its
# notes in ``install_notes``). A pack WITHOUT an ``mbbios`` module has no rule: its files are
# written as given.

BOARD_TXT = "MB/HBI0309C/board.txt"
KeepMbbios = Callable[..., "tuple[dict[str, Path], Any]"]


def pack_mbbios() -> KeepMbbios | None:
    """THE PACK HOOK: the first installed pack's ``<package>.mbbios:keep_mbbios`` (the MPS3's
    ``harness_manager_mps3.mbbios.keep_mbbios``); None when no pack has one (no rule: the
    files are written as given)."""
    from harness_manager.core.registry import load_packs

    for pack in load_packs().values():
        package = type(pack).__module__.rsplit(".", 1)[0]
        try:
            mod = importlib.import_module(f"{package}.mbbios")
        except ImportError:
            continue
        keep = getattr(mod, "keep_mbbios", None)
        if callable(keep):
            return keep
    return None


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


def card_state(root: str) -> tuple[bytes | None, list[str]]:
    """The card's board.txt (None: none) and every file on it (SD-relative), read now."""
    board = _resolve_ci(Path(root), BOARD_TXT)
    text = board.read_bytes() if board is not None and board.is_file() else None
    files = sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file())
    return text, files


def decision_json(decision: Any) -> dict[str, str]:
    """A decision (ours or the pack's) as the result shows it."""
    return {"file": str(getattr(decision, "path", "") or BOARD_TXT),
            "action": str(getattr(decision, "action", "")),
            "value": str(getattr(decision, "value", "")),
            "note": str(getattr(decision, "note", ""))}


def decisions_json(decision: Any) -> list[dict[str, str]]:
    """The pack's decision as ``result.mbbios`` lists it: one entry per revision folder (a
    combined FIX-PACK-9 decision's ``per_rev()``; a decision that is not combined is one), then
    one ``action: "warning"`` entry per warning ("WARNING: this card had no HBI0309B or
    HBI0309C folder: …")."""
    per_rev = getattr(decision, "per_rev", None)
    each = list(per_rev()) if callable(per_rev) else [decision]
    out = [decision_json(d) for d in each]
    out += [{"file": "", "action": "warning", "value": "", "note": f"WARNING: {w}"}
            for w in getattr(decision, "warnings", ()) or ()]
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
    mbbios: list[dict[str, str]] = field(default_factory=list)   # decision_json each
    unsigned: _unsigned.Unsigned | None = None   # the INSTALL UNSIGNED <sha8> it was typed for

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
            out["mbbios"] = list(self.mbbios)
        if self.unsigned is not None:
            out["unsigned"] = self.unsigned.as_dict()
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
                 mbbios_for: Callable[[], KeepMbbios | None] = pack_mbbios,
                 platform: str | None = None,
                 enabled: Callable[[], bool] | None = None,
                 cap: Callable[[], int] | None = None,
                 publish: Publish | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.platform = platform or sys.platform
        self.access = access or RealAccess(platform=self.platform)
        self.storage_for = storage_for
        self.mbbios_for = mbbios_for
        self._lister = lister
        self._enabled = enabled or (lambda: is_enabled(self.state_dir))
        self._cap = cap or (lambda: size_cap(self.state_dir))
        self.publish = publish

    # -- the gate ---------------------------------------------------------------------------

    @property
    def supported(self) -> bool:
        return self.platform.startswith("linux") or self.platform == "darwin" \
            or winps.is_windows(self.platform) or self.access.simulated

    def refusal(self) -> UnavailableError | None:
        """Why nothing can be listed or written now (the setting, the platform); else None."""
        if not self._enabled():
            return UnavailableError(CAPABILITY, DISABLED_REASON,
                                    hint="turn it on: harness-manager config set "
                                         "bringup.sd_flash on")
        if not self.supported:
            return UnavailableError(CAPABILITY, UNSUPPORTED_REASON)
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
        if winps.is_windows(self.platform):
            return windows_disks()
        return linux_disks()

    def listing(self) -> Listing:
        self.require()
        cap = self._cap()
        devices: list[CardDevice] = []
        excluded: list[Excluded] = []
        for disk in self.disks():
            why = exclusion(disk, cap)
            if why:
                excluded.append(Excluded(disk.name, disk.path,
                                         _clean(disk.model) or _clean(disk.vendor), disk.size,
                                         why))
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
        elif not mounted and winps.is_windows(disk.platform):
            kinds["files"] = ("its FAT volume has no drive letter: give it one in Disk "
                              "Management (Start, type diskmgmt.msc; right-click the volume, "
                              "Change Drive Letter and Paths, Add)")
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
        try:
            listing = self.listing()
        except UnavailableError as exc:            # lsblk/diskutil missing or failing: say so
            return {**base, "enabled": True, "reason": exc.reason, "cap_bytes": self._cap()}
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
                allow_mcc_update: bool = False, confirm_unsigned: Any = None,
                unsigned: _unsigned.Unsigned | None = None) -> WritePlan:
        """Every check a write needs before it starts, then the typed phrases; ``WritePlan``
        or the refusal (``plan``, then ``check_unsigned``, then ``check_confirm``).
        ``unsigned``: a caller that already took the phrase for what it names (the bring-up's
        card-reader route, for the bundle it checked) passes it instead of
        ``confirm_unsigned``."""
        plan = self.plan(device_id, kind, source, backup_path=backup_path,
                         backup_dir=backup_dir, allow_mcc_update=allow_mcc_update)
        if unsigned is not None:
            plan.unsigned = unsigned
        else:
            self.check_unsigned(plan, confirm_unsigned)
        self.check_confirm(plan, confirm)
        return plan

    @staticmethod
    def unsigned_of(source: Path | str) -> _unsigned.Unsigned:
        """What ``INSTALL UNSIGNED <sha8>`` names for ``source`` (a folder with a symbolic link
        is refused: its sha256 would not cover what the link points to)."""
        info = _unsigned.of_path(source)
        err = _unsigned.links_refusal(info)
        if err is not None:
            raise err
        return info

    def check_unsigned(self, plan: WritePlan, typed: Any) -> None:
        """The typed ``INSTALL UNSIGNED <sha8>`` for the plan's source as it is now; the plan
        remembers it (``run`` checks the sha256 again before the first byte)."""
        info = self.unsigned_of(plan.source)
        _unsigned.require(info, typed)
        plan.unsigned = info

    def check(self, kind: str, source: Path | str) -> dict[str, Any]:
        """``POST /cardwriter/check``: what a write of ``source`` as ``kind`` would write and
        the unsigned phrase it needs, before any device is chosen. Refused as the write would
        be (a slot image as a card, an .ebf in a bundle, a symbolic link)."""
        self.require()
        if kind not in KINDS:
            raise UsageError(f"kind must be files or card, not {kind!r}")
        src = Path(source)
        out: dict[str, Any] = {"kind": kind, "source": str(src)}
        if kind == "card":
            image = inspect_card(src)
            out["card"] = {"bytes": image.size, "slots": list(image.slots),
                           "describe": image.describe(), "notes": list(image.notes)}
        else:
            files = self._bundle(src)
            out["files"] = sorted(files)
            out["count"] = len(files)
        out["unsigned"] = self.unsigned_of(src).as_dict()
        return out

    @staticmethod
    def check_confirm(plan: WritePlan, confirm: Any) -> None:
        """The typed phrase must be exactly the device's ``WRITE <model> <size>``."""
        want = plan.confirm
        if not isinstance(confirm, str) or confirm.strip() != want:
            err = RefusedError(f"not confirmed: type exactly {want!r} to write "
                               f"{plan.device.disk.path}",
                               hint="the phrase names the card's model and size as listed; "
                                    "nothing was written")
            err.data = {"confirm": want, "device_id": plan.device.id}  # type: ignore[attr-defined]
            raise err

    def plan(self, device_id: str, kind: str, source: Path | str, *,
             backup_path: Path | str | None = None, backup_dir: Path | str | None = None,
             allow_mcc_update: bool = False) -> WritePlan:
        """Every check a write needs except the typed phrase (the CLI asks for it after
        showing this): ``WritePlan`` or the refusal."""
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
        plan = WritePlan(device=card, kind=kind, source=source, confirm=card.confirm)
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
        with tempfile.TemporaryDirectory(prefix="hm-mbbios-check-") as tmp:   # refused: 409 now
            _, plan.mbbios = self._mbbios(plan.files, card.files_root, plan.allow_mcc_update,
                                          Path(tmp))
        return plan

    def _bundle(self, source: Path) -> dict[str, Path]:
        """The configuration-SD files of a bundle folder or ``.zip`` (unpacked safely under the
        state dir, ``bundle.safe_extract``); a release bundle's ``sd/`` when it has one."""
        from harness_manager.cli.cmd_board import bundle_files

        root = source
        if source.is_file() and source.suffix.lower() == ".zip":
            root = self._unzip(source)
        if (root / "sd").is_dir() and not (root / "config.txt").exists():
            root = root / "sd"                  # a release bundle: its config-SD tree
        files = bundle_files(root)             # AbsentError / RefusedError (.ebf) as `sd install`
        ebf = sorted(k for k in files if k.lower().endswith(".ebf"))
        if ebf:                                 # belt and braces: never an .ebf
            raise RefusedError(f"{source} contains board-controller firmware ({', '.join(ebf)})",
                               hint=".ebf files are never written to the SD; remove them")
        return files

    def _unzip(self, source: Path) -> Path:
        """A zip's contents under ``<state>/cardwriter/bundles`` (kept for the job; the newest
        8), its one top folder when it holds nothing else. The "complete" marker sits beside
        the folder, never in it (every file in it is written to the card)."""
        from harness_manager.services.update.bundle import safe_extract

        base = (self.state_dir if self.state_dir is not None else _default_state_dir()) / \
            "cardwriter" / "bundles"
        dest = base / f"{source.stem[:40]}-{_unsigned.file_sha256(source)[:16]}"
        done = base / f"{dest.name}.complete"
        if not done.is_file():
            base.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(dest, ignore_errors=True)
            safe_extract(source, dest)
            done.write_text("ok\n", encoding="ascii")
            olds = sorted((d for d in base.iterdir() if d.is_dir() and d != dest),
                          key=lambda d: d.stat().st_mtime)
            for old in olds[:-7]:
                shutil.rmtree(old, ignore_errors=True)
                (base / f"{old.name}.complete").unlink(missing_ok=True)
        tops = [d for d in dest.iterdir() if d.name != "__MACOSX"]
        if len(tops) == 1 and tops[0].is_dir() and tops[0].name.lower() not in ("mb", "sd"):
            return tops[0]
        return dest

    # -- the job ----------------------------------------------------------------------------

    def run(self, plan: WritePlan, progress: Progress | None = None) -> dict[str, Any]:
        """Write ``plan`` (on a job's thread). The device is listed again first. Never a plan
        whose unsigned phrase was not typed; the sha256 it named is checked again."""
        if plan.unsigned is None:
            _unsigned.require(self.unsigned_of(plan.source), None)      # refused: never typed
        emit = self._emitter(plan, progress)
        with self._device_lock(plan.device.disk):
            card = self.find(plan.device.id)            # still the card the user confirmed
            if card.confirm != plan.confirm:
                raise RefusedError(f"{card.disk.path} changed since it was confirmed",
                                   hint="list the devices again")
            if plan.kind == "files":
                _unsigned.still_same(plan.unsigned)     # a card image's: in _run_card, once
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
            except ImportError:            # Windows: msvcrt's byte-range lock instead
                fcntl = None  # type: ignore[assignment]
            if fcntl is None and os.name == "nt":
                fh = _win_lock(lock_dir, key, disk.path)
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

    def _mbbios(self, files: Mapping[str, Path], root: str, allow: bool,
                workdir: Path) -> tuple[dict[str, Path], list[dict[str, str]]]:
        """The board pack's MBBIOS rule (``pack_mbbios``) on ``files`` against the card at
        ``root`` as it is now: the files to write and the decision. No pack rule: ``files``
        as given, no decision."""
        keep = self.mbbios_for()
        if keep is None:
            return dict(files), []
        card_board, card_files = card_state(root)
        boards_of = getattr(sys.modules.get(getattr(keep, "__module__", "") or ""),
                            "card_boards_of", None)
        if callable(boards_of):           # FIX-PACK-9: every revision's board.txt on the card
            out, decision = keep(files, card_boards=boards_of(root), card_files=card_files,
                                 workdir=workdir, allow_mcc_update=allow)
        else:
            out, decision = keep(files, card_board_txt=card_board, card_files=card_files,
                                 workdir=workdir, allow_mcc_update=allow)
        return dict(out), ([] if decision is None else decisions_json(decision))

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
        # The card as it is now decides board.txt's MBBIOS line (refused: nothing written).
        base = self.state_dir if self.state_dir is not None else _default_state_dir()
        staged = base / "cardwriter" / "staged" / f"{os.getpid()}-{time.monotonic_ns()}"
        staged.mkdir(parents=True, exist_ok=True)
        try:
            files, plan.mbbios = self._mbbios(plan.files, card.files_root, plan.allow_mcc_update,
                                              staged)
            if _takes(storage.install, "allow_mcc_update"):
                # The pack's writer applies the same rule itself (Mps3Storage): give it the
                # bundle as it is, and the override.
                storage.install(plan.files, backup=record, progress=relay,
                                allow_mcc_update=plan.allow_mcc_update)
            else:
                storage.install(files, backup=record, progress=relay)
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        written = {rel: _resolve_ci(Path(card.files_root), rel) for rel in sorted(files)}
        digest = hashlib.sha256("\n".join(
            f"{rel}  {file_sha256(p) if p else '-'}" for rel, p in written.items()).encode()
        ).hexdigest()
        result = {"outcome": "written", "verified": True, "sha256": digest,
                  "files": sorted(files), "root": card.files_root,
                  "mbbios": list(plan.mbbios), "backup": {
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
        assert plan.unsigned is not None
        if plan.unsigned.of == "file" and Path(plan.unsigned.path) == src:
            if sha != plan.unsigned.sha256:                 # the image its phrase named, still
                raise RefusedError(
                    f"{src} changed since its phrase was typed: its sha256 now starts "
                    f"{sha[:8]}, not {plan.unsigned.sha256[:8]}",
                    hint="check it again and type its new phrase; nothing was written")
        else:
            _unsigned.still_same(plan.unsigned)
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


def _win_lock(lock_dir: Path, key: str, path: str) -> Any:  # pragma: no cover - Windows
    """Windows: one write per device across processes (the app and the CLI), a non-blocking
    ``msvcrt.locking`` on the lock file's first byte; released when the file closes."""
    import msvcrt  # type: ignore[import-not-found]

    lock_dir.mkdir(parents=True, exist_ok=True)
    fh = open(lock_dir / f"{key}.lock", "a+b")   # noqa: SIM115 - held for the write
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        fh.close()
        raise HeldError(f"another Harness Manager is writing {path}",
                        hint="wait for it to finish; never write a card twice at once") from exc
    return fh


def _takes(fn: Callable[..., Any], name: str) -> bool:
    """Does ``fn`` take the keyword ``name`` (a storage adapter's ``install(allow_mcc_update=)``)?"""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


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
