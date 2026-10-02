"""What Windows PowerShell answers to ``cardwriter.WINDOWS_DISKS_PS`` on a lab laptop (lane
WINDOWS), as a FIXTURE: never PowerShell. The shapes follow Windows PowerShell 5.1:
``ConvertTo-Json`` of ``[pscustomobject]``s; ``BusType`` as the Storage module's name
("USB") on some disks and the CIM number ("12" = SD) on others; a partition with no drive
letter has ``Letter`` "\\u0000" ([string] of [char]0)."""

from __future__ import annotations

import copy
import json
from typing import Any

CARD = 31_914_983_424         # a "32 GB" microSD in a USB reader
BLANK = 15_931_539_456        # a "16 GB" one
BUILTIN = 7_948_206_080       # an 8 GB SD card in the laptop's own reader


def _disk(n: int, name: str, size: int, bus: Any, *, system: bool = False, boot: bool = False,
          serial: str = "", maker: str = "") -> dict:
    return {"Number": n, "FriendlyName": name, "Model": name, "Manufacturer": maker,
            "SerialNumber": serial, "Size": size, "BusType": bus, "IsSystem": system,
            "IsBoot": boot, "IsOffline": False, "PartitionStyle": "MBR"}


def _part(disk: int, n: int, letter: str, size: int, fs: str, label: str) -> dict:
    return {"Disk": disk, "Number": n, "Letter": letter or "\u0000", "Size": size,
            "Label": label, "Fs": fs, "Id": f"\\\\?\\Volume{{{disk:08x}-{n:04x}}}\\"}


def _drive(n: int, media: str, pnp: str) -> dict:
    return {"Index": n, "MediaType": media, "InterfaceType": "USB", "PNPDeviceID": pnp}


def laptop() -> dict:
    """Disk 0 the system NVMe (C:); 1 the board's config card in a USB reader (E:, V2M-MPS3);
    2 a blank microSD in a USB reader (F:); 3 the MPS3's own MCC drive over the Debug USB (G:);
    4 the board's DAPLink drive (H:); 5 a 2 TB USB disk; 6 an SD card in the built-in reader
    (BusType 12, no drive letter); 7 a VHD; 8 an empty reader."""
    return {
        "system": "C:",
        "disks": [
            _disk(0, "Samsung SSD 980 PRO 512GB", 512_110_190_592, "NVMe", system=True,
                  boot=True),
            _disk(1, "Generic- SD/MMC USB Device", CARD, "USB"),
            _disk(2, "Generic- MicroSD/M2 USB Device", BLANK, "7"),
            _disk(3, "ARM V2M_MPS3 USB Device", 2_097_152_000, "USB"),
            _disk(4, "MBED VFS USB Device", 67_108_864, "USB"),
            _disk(5, "Seagate Expansion HDD USB Device", 2_000_398_934_016, "USB"),
            _disk(6, "SDXC Card", BUILTIN, "12"),
            _disk(7, "Msft Virtual Disk", 10_737_418_240, "15"),
            _disk(8, "Generic- SM/xD-Picture USB Device", 0, "USB"),
        ],
        "parts": [
            _part(0, 1, "", 104_857_600, "FAT32", ""),
            _part(0, 3, "C", 511_000_000_000, "NTFS", "Windows"),
            _part(1, 1, "E", CARD - 4_194_304, "FAT32", "V2M-MPS3"),
            _part(2, 1, "F", BLANK - 4_194_304, "FAT32", "NO NAME"),
            _part(3, 1, "G", 2_097_152_000, "FAT", "V2M-MPS3"),
            _part(4, 1, "H", 67_108_864, "FAT", "MBED"),
            _part(5, 1, "I", 2_000_398_934_016, "NTFS", "Expansion"),
            _part(6, 1, "", BUILTIN - 4_194_304, "FAT32", "BOOT"),
        ],
        "drives": [
            _drive(0, "Fixed hard disk media", "SCSI\\DISK&VEN_NVME&PROD_SAMSUNG\\5&1"),
            _drive(1, "Removable Media", "USBSTOR\\DISK&VEN_GENERIC-&PROD_SD/MMC&REV_1.00\\1"),
            _drive(2, "Removable Media", "USBSTOR\\DISK&VEN_GENERIC-&PROD_MICROSD/M2&REV_1.08\\2"),
            _drive(3, "Removable Media", "USBSTOR\\DISK&VEN_ARM&PROD_V2M_MPS3&REV_1.0\\3"),
            _drive(4, "Removable Media", "USBSTOR\\DISK&VEN_MBED&PROD_VFS&REV_0.1\\4"),
            _drive(5, "External hard disk media", "USBSTOR\\DISK&VEN_SEAGATE&PROD_EXP\\5"),
            _drive(6, "Removable Media", "SD\\DISK&GENERIC_SD&REV_1\\6"),
            _drive(7, "Fixed hard disk media", "SCSI\\DISK&VEN_MSFT&PROD_VIRTUAL_DISK\\7"),
            _drive(8, "Removable Media", "USBSTOR\\DISK&VEN_GENERIC-&PROD_SM/XD&REV_1\\8"),
        ],
    }


def answer(doc: dict | None = None) -> bytes:
    """The bytes PowerShell writes (UTF-8 with a BOM, as [Console]::OutputEncoding gives)."""
    return b"\xef\xbb\xbf" + json.dumps(doc if doc is not None else laptop()).encode()


def edited(**changes: Any) -> dict:
    doc = copy.deepcopy(laptop())
    doc.update(changes)
    return doc
