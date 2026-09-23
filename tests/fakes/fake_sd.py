"""FakeSdVolume: the MPS3 configuration microSD as a USB mass-storage volume. Owned by T3."""

from __future__ import annotations

from pathlib import Path


class FakeSdVolume:
    """The configuration microSD as it appears over USB mass storage."""

    LABEL = "V2M-MPS3"

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "MB" / "HBI0309C" / "Nanosoc").mkdir(parents=True, exist_ok=True)
        (root / "config.txt").write_text("TITLE: V2M-MPS3 config\nUSB_REMOTE: TRUE\nUARTMODE: 0\n")
        (root / "MB" / "HBI0309C" / "board.txt").write_text("APPFILE: Nanosoc\\nanosoc.txt\n")
        (root / "MB" / "HBI0309C" / "Nanosoc" / "nanosoc.txt").write_text(
            "F0FILE: nanosoc.bit\n[OSCCLKS]\nOSC0: 25.0\nOSC1: 50.0\n"
        )
        (root / "MB" / "HBI0309C" / "Nanosoc" / "nanosoc.bit").write_bytes(b"\x00" * 64)
        # Stock MCC firmware image. The installer must never write or delete .ebf files.
        (root / "MB" / "HBI0309C" / "mbb_v132.ebf").write_bytes(b"MCCBIOS")
