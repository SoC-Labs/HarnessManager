"""Fake OS views for T3's USB/volume discovery tests: no USB hardware needed.

``port()`` builds a pyserial-``ListPortInfo``-shaped object. The shapes follow
pyserial 3.5:

- Linux (``list_ports_linux.SysFS``): ``location`` is the sysfs interface
  name, ``"1-2.3.4.3:1.0"``, for multi-interface devices like the FT4232H;
  ``serial_number`` is the device's serial, the same on all four ports;
- Windows with the FTDI VCP driver (``list_ports_windows``, ``FTDIBUS``
  branch): ``location`` is None and ``serial_number`` carries the channel
  letter, ``"FT7XYZA"`` .. ``"FT7XYZD"``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FakePortInfo:
    device: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    location: str | None = None
    product: str | None = None
    interface: str | None = None


def linux_ft4232(prefix: str = "/dev/ttyUSB", first: int = 10, usb_path: str = "1-2.3.4.3",
                 serial: str = "") -> list[FakePortInfo]:
    """The four FT4232H ports of one MPS3 as Linux pyserial reports them (ttyUSB10-13 on the hub)."""
    return [FakePortInfo(device=f"{prefix}{first + n}", vid=0x0403, pid=0x6011,
                         serial_number=serial or None, location=f"{usb_path}:1.{n}",
                         product="Quad RS232-HS", interface="Quad RS232-HS")
            for n in range(4)]


def windows_ftdi_vcp(base: str = "FT7XYZ", first_com: int = 7) -> list[FakePortInfo]:
    """The four FT4232H ports as Windows pyserial reports them under the FTDI VCP driver."""
    return [FakePortInfo(device=f"COM{first_com + n}", vid=0x0403, pid=0x6011,
                         serial_number=f"{base}{'ABCD'[n]}", location=None)
            for n in range(4)]


def unrelated_ports() -> list[FakePortInfo]:
    """Ports that must never be taken for an MPS3: DAPLink CDC, an FT2232H, a built-in UART."""
    return [
        FakePortInfo(device="/dev/ttyACM0", vid=0x0D28, pid=0x0204, serial_number="0000000000000",
                     location="1-2.3.4.2:1.1", product="DAPLink CMSIS-DAP"),
        FakePortInfo(device="/dev/ttyUSB0", vid=0x0403, pid=0x6010, serial_number="FT2232",
                     location="1-1:1.0"),
        FakePortInfo(device="/dev/ttyS0"),
    ]


class FakeWinVolumes:
    """Stands in for kernel32 GetLogicalDrives + GetVolumeInformationW."""

    def __init__(self, labels: dict[str, str | None]) -> None:
        self.labels = labels            # "E:\\" -> "V2M-MPS3"; None = no medium

    def drive_roots(self) -> list[str]:
        return list(self.labels)

    def label(self, root: str) -> str | None:
        return self.labels.get(root)
