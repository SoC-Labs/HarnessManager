"""FakeSdVolume: the MPS3 configuration microSD as a USB mass-storage volume. Owned by T3.

The layout follows the real SD (mps3-nanosoc-platform fpga/mps3_sd/, the MCC
boot log docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt): ``config.txt`` at
the root, ``MB/HBI0309C/board.txt`` naming the application note
``Nanosoc\\nanosoc.txt``, which names ``nanosoc.bit``; and a stock MCC firmware
image (``.ebf``) that must never be written or deleted.

``FakeDapLinkVolume`` is the other drive on the Debug USB: the on-board
CMSIS-DAP's, labelled ``MBED MPS3`` (scripts/mps3_sd_update.sh, 2026-08-25),
with DAPLink's ``DETAILS.TXT`` and ``MBED.HTM``. Code must refuse it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class FakeSdVolume:
    """The configuration microSD as it appears over USB mass storage."""

    LABEL = "V2M-MPS3"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.label = self.LABEL
        (root / "MB" / "HBI0309C" / "Nanosoc").mkdir(parents=True, exist_ok=True)
        (root / "config.txt").write_text("TITLE: V2M-MPS3 config\nUSB_REMOTE: TRUE\nUARTMODE: 0\n")
        (root / "MB" / "HBI0309C" / "board.txt").write_text("APPFILE: Nanosoc\\nanosoc.txt\n")
        (root / "MB" / "HBI0309C" / "Nanosoc" / "nanosoc.txt").write_text(
            "F0FILE: nanosoc.bit\n[OSCCLKS]\nOSC0: 25.0\nOSC1: 50.0\n"
        )
        (root / "MB" / "HBI0309C" / "Nanosoc" / "nanosoc.bit").write_bytes(b"\x00" * 64)
        # Stock MCC firmware image. The installer must never write or delete .ebf files.
        (root / "MB" / "HBI0309C" / "mbb_v132.ebf").write_bytes(b"MCCBIOS")

    @property
    def ebf(self) -> Path:
        return self.root / "MB" / "HBI0309C" / "mbb_v132.ebf"

    @property
    def bit(self) -> Path:
        return self.root / "MB" / "HBI0309C" / "Nanosoc" / "nanosoc.bit"

    def snapshot(self) -> dict[str, str]:
        """relative POSIX path -> sha256 of every file (the journal excluded)."""
        out = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and p.name != ".harness-manager-journal.json":
                out[p.relative_to(self.root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out


class FakeDapLinkVolume:
    """The on-board CMSIS-DAP (DAPLink) drive on the same Debug USB connector."""

    LABEL = "MBED MPS3"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.label = self.LABEL
        root.mkdir(parents=True, exist_ok=True)
        (root / "DETAILS.TXT").write_text("# DAPLink Firmware - see https://daplink.io\n")
        (root / "MBED.HTM").write_text("<!-- mbed Microcontroller Website and Authentication Shortcut -->\n")
