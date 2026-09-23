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

from socharness.core.model import Candidate, Link, LinkKind
from socharness.core.transport import register_fake_serial, unregister_fake_serial

from .fake_mcc import FakeMcc
from .fake_sd import FakeSdVolume

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


class VirtualMps3:
    """Start with ``with VirtualMps3(tmp_path) as vb:``; the parts are attributes."""

    def __init__(self, tmp_path: Path, profile: FirmwareProfile = FIELDED_3F1A560F,
                 *, boot_rm_id: int = 0, usb: bool = False) -> None:
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
        # boot_s=3 so a reboot is observable over Ethernet (the witness needs
        # two failed 1 s pings) when a test runs on the real clock.
        self.mcc = FakeMcc(on_reboot=self._on_reboot, on_boot=self._on_boot, boot_s=3.0)
        self.boot_rm_id = boot_rm_id
        self.boots = 0
        self.sd = FakeSdVolume(tmp_path / "sd")
        self.reboots = 0
        # usb=True registers the MCC as fake://<name> so the pack's USB adapters
        # (Team T3) can open it through socharness.core.transport.open_serial.
        self.usb = usb
        self.mcc_url = ""
        self._fake_name = f"mcc-{id(self):x}"

    def _on_reboot(self) -> None:
        # A real REBOOT power-cycles the board (proven 2026-08-04): the shell goes away.
        self.reboots += 1
        self.shell.stop()

    def _on_boot(self) -> None:
        # ...and the MCC reloads the base bitstream from the SD, so the shell comes back
        # on the same address with the boot design (greybox) loaded, not the last swap.
        self.boots += 1
        self.shell.current_rm_id = 0
        self.shell.start()

    @property
    def shell_endpoint(self) -> str:
        return f"{self.shell.host}:{self.shell.control_port}"

    @property
    def console_ports(self) -> dict[str, int]:
        return dict(self.shell.console_ports)

    def candidate(self, *, ethernet: bool = True, usb: bool | None = None) -> Candidate:
        """A candidate with the links this virtual board exposes."""
        links: list[Link] = []
        if ethernet:
            links.append(Link(LinkKind.ETHERNET, self.shell_endpoint, "shell control channel"))
        if self.usb if usb is None else usb:
            links.append(Link(LinkKind.USB_SERIAL, self.mcc_url, "MCC console (fake)"))
            links.append(Link(LinkKind.USB_MSD, str(self.sd.root), "V2M-MPS3 (fake)"))
        return Candidate(pack="mps3", board_id=f"mps3@{self.shell_endpoint}", links=tuple(links),
                         label="virtual MPS3", evidence="test fixture")

    def __enter__(self) -> VirtualMps3:
        self.shell.start()
        if self.usb:
            self.mcc_url = register_fake_serial(self._fake_name, self.mcc)
        return self

    def __exit__(self, *exc: object) -> None:
        self.shell.stop()
        unregister_fake_serial(self._fake_name)
