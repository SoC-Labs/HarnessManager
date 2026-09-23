"""VirtualMps3: one fake MPS3, assembled from the fakes, for integration tests.

Firmware profiles pin the fake to a real harness release. ``FIELDED_3F1A560F``
is what is on the lab board today: harness 1.0.0; the five features it
reports; ``reset`` accepts only ``dut`` (coordinator.c:246); USR_ACCESS
unreadable, so the build check is "unchecked". Features added by the harness
handover (stats, identify, reboot, log, mcc) belong in new profiles, added
when FakeShell grows them, never by editing this one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pyverify.testing.fakeshell import FakeShell

from .fake_mcc import FakeMcc

SHELL_0x3F1A560F = 0x3F1A560F


@dataclass(frozen=True)
class FirmwareProfile:
    name: str
    static_id: int
    harness_version: str
    harness_sha: str
    features: tuple[str, ...]
    reset_targets: tuple[str, ...]
    usr_access: int | None
    extra: dict = field(default_factory=dict)


FIELDED_3F1A560F = FirmwareProfile(
    name="fielded-0x3F1A560F",
    static_id=SHELL_0x3F1A560F,
    harness_version="1.0.0",
    harness_sha="cb31b0f2",
    features=("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed"),
    reset_targets=("dut",),
    usr_access=None,
)


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


class VirtualMps3:
    """Start with ``with VirtualMps3(tmp_path) as vb:``; the parts are attributes."""

    def __init__(self, tmp_path: Path, profile: FirmwareProfile = FIELDED_3F1A560F,
                 *, boot_rm_id: int = 0) -> None:
        self.profile = profile
        self.shell = FakeShell.ephemeral(
            static_id=profile.static_id,
            boot_rm_id=boot_rm_id,
            reset_targets=profile.reset_targets,
            harness_version=profile.harness_version,
            harness_sha=profile.harness_sha,
            harness_usr_access=profile.usr_access,
            features=profile.features,
        )
        self.mcc = FakeMcc(on_reboot=self._on_reboot)
        self.sd = FakeSdVolume(tmp_path / "sd")
        self.reboots = 0

    def _on_reboot(self) -> None:
        # A real REBOOT power-cycles the board and reloads the SD bitstream
        # (proven 2026-08-04). The fake counts it; T3 will model the reload.
        self.reboots += 1

    @property
    def shell_endpoint(self) -> str:
        return f"{self.shell.host}:{self.shell.control_port}"

    @property
    def console_ports(self) -> dict[str, int]:
        return dict(self.shell.console_ports)

    def __enter__(self) -> VirtualMps3:
        self.shell.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shell.stop()
