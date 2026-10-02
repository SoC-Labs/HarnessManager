"""Lane WINDOWS: finding an MPS3's Debug USB on Windows, board-free. pyserial's Windows
listing (the FTDI VCP driver's COM ports with channel letters; usbser's ``x.<MI>``
locations) and kernel32's drive letters are fakes; ``platform`` says Windows. Each
behaviour has its negative twin."""

from __future__ import annotations

import pytest

from harness_manager.core.errors import UsageError
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.pack import ProbeHints
from harness_manager.services import bringup
from harness_manager_mps3 import sd as sdmod
from harness_manager_mps3 import usb as usbmod
from harness_manager_mps3.usb import UsbEnv, probe_usb
from tests.fakes.fake_sd import FakeSdVolume
from tests.fakes.t3_usb import FakePortInfo, FakeWinVolumes, windows_ftdi_vcp

SCAN = ProbeHints(scan_usb=True, scan_network=False)


class KindedWinVolumes(FakeWinVolumes):
    """kernel32 with GetDriveTypeW: 2 removable, 3 fixed, 4 a network drive, 1 no root."""

    def __init__(self, labels, kinds) -> None:
        super().__init__(labels)
        self.kinds = kinds
        self.asked: list[str] = []

    def drive_type(self, root: str) -> int:
        return self.kinds.get(root, 3)

    def label(self, root: str):
        self.asked.append(root)
        return super().label(root)


def win_env(ports, volumes) -> UsbEnv:
    return UsbEnv(list_ports=lambda: list(ports), list_volumes=lambda: list(volumes),
                  platform=lambda: "win32")


def test_windows_finds_the_mcc_com_port_and_the_v2m_mps3_drive_letter(tmp_path):
    sd = FakeSdVolume(tmp_path / "E")
    vols = sdmod.windows_volumes(KindedWinVolumes({"C:\\": "Windows", "E:\\": "V2M-MPS3",
                                                   "F:\\": "MBED"}, {"E:\\": 2, "F:\\": 2}))
    vols = [sdmod.VolumeInfo(v.label, str(sd.root) if v.root == "E:\\" else v.root, v.device)
            for v in vols]
    (cand,) = probe_usb(SCAN, [], env=win_env(windows_ftdi_vcp(), vols))
    serial = [lk for lk in cand.links if lk.kind == LinkKind.USB_SERIAL]
    assert serial[0].address == "serial://COM7" and "if00: MCC console" in serial[0].detail
    assert [lk.address for lk in serial[1:]] == ["serial://COM8", "serial://COM9",
                                                  "serial://COM10"]
    (msd,) = [lk for lk in cand.links if lk.kind == LinkKind.USB_MSD]
    assert msd.address == str(sd.root) and "on E:" in msd.detail
    assert cand.board_id == "mps3@usb:serial://COM7"
    assert "channel letter" in cand.evidence
    row = bringup.usb_links(cand)
    assert row["mcc"]["port"] == "COM7" and row["volume"]["path"] == str(sd.root)


def test_windows_usbser_locations_number_the_interfaces_too():
    ports = [FakePortInfo(device=f"COM{3 + n}", vid=0x0403, pid=0x6011, serial_number="FT9ABC",
                          location=f"1-4:x.{n}") for n in range(4)]
    (cand,) = probe_usb(SCAN, [], env=win_env(ports, []))
    assert cand.links[0].address == "serial://COM3" and "if00" in cand.links[0].detail


def test_twin_a_drive_and_no_com_ports_on_windows_names_the_ftdi_driver(tmp_path):
    sd = FakeSdVolume(tmp_path / "E")
    vols = [sdmod.VolumeInfo("V2M-MPS3", str(sd.root), "E:")]
    (cand,) = probe_usb(SCAN, [], env=win_env([], vols))
    assert usbmod.WINDOWS_NO_PORTS in cand.evidence
    assert "Device Manager, Ports (COM & LPT)" in cand.evidence and "FTDI VCP" in cand.evidence
    linux = UsbEnv(list_ports=lambda: [], list_volumes=lambda: vols, platform=lambda: "linux")
    (cand2,) = probe_usb(SCAN, [], env=linux)
    assert "FTDI" not in cand2.evidence                          # Linux: no Windows words


def test_twin_no_pyserial_says_how_to_install_it_not_the_driver(tmp_path):
    sd = FakeSdVolume(tmp_path / "E")

    def no_pyserial() -> list:
        from harness_manager.transports.direct import _INSTALL_HINT

        raise UsageError("pyserial is not installed, so serial:// ports cannot be opened",
                         hint=_INSTALL_HINT)

    env = UsbEnv(list_ports=no_pyserial,
                 list_volumes=lambda: [sdmod.VolumeInfo("V2M-MPS3", str(sd.root), "E:")],
                 platform=lambda: "win32")
    (cand,) = probe_usb(SCAN, [], env=env)
    assert "install.ps1 -WithSerial" in cand.evidence
    assert usbmod.WINDOWS_NO_PORTS not in cand.evidence


def test_network_and_rootless_drives_are_never_asked_for_a_label():
    api = KindedWinVolumes({"C:\\": "Windows", "E:\\": "V2M-MPS3", "Z:\\": "share",
                            "Y:\\": None}, {"E:\\": 2, "Z:\\": 4, "Y:\\": 1})
    vols = sdmod.windows_volumes(api)
    assert [v.root for v in vols] == ["C:\\", "E:\\"]
    assert "Z:\\" not in api.asked and "Y:\\" not in api.asked   # a mapped drive can block


def test_twin_a_kernel32_without_drive_types_still_lists_every_label():
    vols = sdmod.windows_volumes(FakeWinVolumes({"E:\\": "V2M-MPS3", "Z:\\": "share"}))
    assert [v.label for v in vols] == ["V2M-MPS3", "share"]


@pytest.mark.parametrize(("given", "root"), [("E:", "E:\\"), ("e:", "E:\\"),
                                             ("E:\\", "E:\\"), ("E:\\MB", "E:\\MB"),
                                             ("/media/me/V2M-MPS3", "/media/me/V2M-MPS3"),
                                             ("label:V2M-MPS3", "label:V2M-MPS3")])
def test_a_bare_drive_letter_means_its_root(given, root):
    assert sdmod.drive_root(given) == root


def usb_cand(*, serial: bool = True, volume: str | None = "E:\\") -> Candidate:
    links = []
    if serial:
        links.append(Link(LinkKind.USB_SERIAL, "serial://COM7", "FT4232H X if00: MCC console"))
    if volume:
        links.append(Link(LinkKind.USB_MSD, volume, "V2M-MPS3 volume on E:"))
    return Candidate("mps3", f"mps3@usb:{links[0].address}", tuple(links))


def test_the_wizards_windows_words_when_the_mcc_port_is_missing(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    (p,) = bringup._problems(bringup.usb_links(usb_cand(serial=False)))
    assert p == bringup.WINDOWS_NO_MCC and "install the FTDI VCP driver" in p


def test_twin_the_wizards_linux_words_name_no_windows_tool(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    (p,) = bringup._problems(bringup.usb_links(usb_cand(serial=False)))
    assert "Device Manager" not in p and p.startswith("no MCC serial port with it")


class _NoBoards:
    def probe(self, hints):
        return []

    def open_boards(self):
        return []


def test_none_found_on_windows_says_where_windows_shows_the_drive_and_ports(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    out = bringup.scan(_NoBoards())
    assert out["empty"]["check"] == list(bringup.WINDOWS_NONE_FOUND_HINTS)
    assert any("File Explorer, This PC" in c for c in out["empty"]["check"])
    assert not any("udisksctl" in c for c in out["empty"]["check"])


def test_twin_none_found_on_linux_keeps_the_mount_hint(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    out = bringup.scan(_NoBoards())
    assert out["empty"]["check"] == list(bringup.NONE_FOUND_HINTS)


def test_the_mps3_packs_probe_finds_it_through_the_windows_listing(tmp_path, monkeypatch):
    # What `harness-manager probe` and the wizard's Over-USB scan call (scan_usb, no network).
    from harness_manager_mps3.pack import Mps3Pack

    sd = FakeSdVolume(tmp_path / "E")
    monkeypatch.setattr(usbmod, "DEFAULT_ENV",
                        win_env(windows_ftdi_vcp(first_com=11),
                                [sdmod.VolumeInfo("V2M-MPS3", str(sd.root), "E:")]))
    (cand,) = Mps3Pack().probe(SCAN)
    assert cand.board_id == "mps3@usb:serial://COM11"
    assert {lk.kind for lk in cand.links} == {LinkKind.USB_SERIAL, LinkKind.USB_MSD}
