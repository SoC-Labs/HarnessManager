"""T3: USB discovery with injected port and volume listings (no USB hardware).

Every check has a negative twin: paired vs not paired, MCC identified vs not,
config SD vs DAPLink drive.
"""

from __future__ import annotations

import pytest

from socharness.core.errors import RefusedError, UsageError
from socharness.core.model import Candidate, Link, LinkKind
from socharness.core.pack import ProbeHints
from socharness_board_mps3 import usb as usbmod
from socharness_board_mps3.sd import VolumeInfo
from socharness_board_mps3.usb import UsbEnv, group_ft4232, probe_usb, serial_console_endpoints
from tests.fakes.fake_sd import FakeDapLinkVolume, FakeSdVolume
from tests.fakes.t3_usb import linux_ft4232, unrelated_ports, windows_ftdi_vcp

SCAN = ProbeHints(scan_usb=True, scan_network=False)


def usb_env(ports=(), volumes=()) -> UsbEnv:
    return UsbEnv(list_ports=lambda: list(ports), list_volumes=lambda: list(volumes))


def eth_candidate(host: str = "192.168.10.101:6900") -> Candidate:
    return Candidate(pack="mps3", board_id=f"mps3@{host}",
                     links=(Link(LinkKind.ETHERNET, host, "shell control channel"),),
                     label="MPS3 greybox on shell 0x3f1a560f", evidence="answered ping")


def kinds(c: Candidate) -> list[LinkKind]:
    return [lk.kind for lk in c.links]


# --- grouping and interface numbers -------------------------------------------------


def test_linux_ports_group_by_usb_location():
    (board,) = group_ft4232(linux_ft4232() + unrelated_ports())
    assert board.usb_path == "1-2.3.4.3"
    assert board.interfaces == {0: "/dev/ttyUSB10", 1: "/dev/ttyUSB11",
                                2: "/dev/ttyUSB12", 3: "/dev/ttyUSB13"}


def test_windows_ftdi_vcp_ports_group_by_channel_letter():
    (board,) = group_ft4232(windows_ftdi_vcp())
    assert board.serial == "FT7XYZ" and board.interfaces == {0: "COM7", 1: "COM8", 2: "COM9", 3: "COM10"}
    assert "channel letter" in board.how


def test_unknown_interfaces_give_no_mcc_link(tmp_path):
    # Negative twin: no location and no channel letters, so no interface can be trusted.
    ports = windows_ftdi_vcp()
    for p in ports:
        p.serial_number = "FT7XYZ"
    (board,) = group_ft4232(ports)
    assert board.interfaces == {} and len(board.unassigned) == 4
    assert probe_usb(SCAN, [], env=usb_env(ports)) == []      # nothing usable to open
    sd = FakeSdVolume(tmp_path / "sd")
    (cand,) = probe_usb(SCAN, [], env=usb_env(ports, [VolumeInfo("V2M-MPS3", str(sd.root))]))
    assert kinds(cand) == [LinkKind.USB_MSD]
    assert "interface 00 could not be identified" in cand.evidence


def test_a_lone_ftdi_port_with_a_letter_is_not_guessed():
    ports = windows_ftdi_vcp()[:1]            # one port whose serial happens to end in A
    (board,) = group_ft4232(ports)
    assert board.interfaces == {}


def test_other_usb_serial_devices_are_ignored():
    assert group_ft4232(unrelated_ports()) == []
    assert probe_usb(SCAN, [], env=usb_env(unrelated_ports())) == []


# --- candidates ---------------------------------------------------------------------


def test_scan_builds_one_board_with_mcc_first_and_the_sd(tmp_path):
    sd = FakeSdVolume(tmp_path / "sd")
    vols = [VolumeInfo("V2M-MPS3", str(sd.root), "/dev/sdb1", "1-2.3.4.1"),
            VolumeInfo("MBED MPS3", "/media/u/MBED MPS3", "/dev/sda", "1-2.3.4.2")]
    (cand,) = probe_usb(SCAN, [], env=usb_env(linux_ft4232(), vols))
    assert cand.links[0] == Link(LinkKind.USB_SERIAL, "serial:///dev/ttyUSB10",
                                 "FT4232H ? if00: MCC console")
    assert [lk.address for lk in cand.links if lk.kind == LinkKind.USB_SERIAL] == [
        f"serial:///dev/ttyUSB{n}" for n in (10, 11, 12, 13)]
    msd = [lk for lk in cand.links if lk.kind == LinkKind.USB_MSD]
    assert [lk.address for lk in msd] == [str(sd.root)]            # never the DAPLink drive
    assert "0403:6011" in cand.evidence and "the only FT4232H" in cand.evidence
    assert "none was found" in cand.evidence


def test_unmounted_sd_is_noted_not_linked():
    vols = [VolumeInfo("V2M-MPS3", None, "/dev/sdb1")]
    (cand,) = probe_usb(SCAN, [], env=usb_env(linux_ft4232(), vols))
    assert LinkKind.USB_MSD not in kinds(cand) and "not mounted" in cand.evidence


def test_two_boards_pair_their_sds_by_usb_hub(tmp_path):
    a = FakeSdVolume(tmp_path / "a")
    b = FakeSdVolume(tmp_path / "b")
    ports = linux_ft4232(first=10, usb_path="1-2.3.4.3") + linux_ft4232(first=20, usb_path="1-5.1.3")
    vols = [VolumeInfo("V2M-MPS3", str(b.root), "/dev/sdc1", "1-5.1.1"),
            VolumeInfo("V2M-MPS3", str(a.root), "/dev/sdb1", "1-2.3.4.1")]
    cands = probe_usb(SCAN, [], env=usb_env(ports, vols))
    assert len(cands) == 2
    by_mcc = {c.links[0].address: c for c in cands}
    msd = {k: [lk.address for lk in c.links if lk.kind == LinkKind.USB_MSD] for k, c in by_mcc.items()}
    assert msd == {"serial:///dev/ttyUSB10": [str(a.root)], "serial:///dev/ttyUSB20": [str(b.root)]}


def test_two_boards_with_unknown_hubs_stay_separate(tmp_path):
    # Negative twin: no USB path for the volumes, so nothing is guessed.
    ports = linux_ft4232(first=10, usb_path="1-2.3.4.3") + linux_ft4232(first=20, usb_path="1-5.1.3")
    vols = [VolumeInfo("V2M-MPS3", str(tmp_path / "x"), "E:")]
    cands = probe_usb(SCAN, [], env=usb_env(ports, vols))
    assert len(cands) == 3
    lone = [c for c in cands if kinds(c) == [LinkKind.USB_MSD]]
    assert len(lone) == 1 and "could not be told apart" in lone[0].evidence


# --- pairing with Ethernet ----------------------------------------------------------


def test_one_usb_and_one_ethernet_board_are_merged(tmp_path):
    sd = FakeSdVolume(tmp_path / "sd")
    eth = eth_candidate()
    found = [eth]
    (merged,) = probe_usb(SCAN, found, env=usb_env(linux_ft4232(), [VolumeInfo("V2M-MPS3", str(sd.root))]))
    assert merged.board_id == eth.board_id
    assert kinds(merged)[0] == LinkKind.ETHERNET and LinkKind.USB_SERIAL in kinds(merged)
    assert LinkKind.USB_MSD in kinds(merged)
    assert "paired: the only MPS3 shell on Ethernet" in merged.evidence
    assert found == []          # the Ethernet-only candidate is superseded, not listed twice


def test_two_ethernet_boards_are_not_paired():
    found = [eth_candidate("192.168.10.101:6900"), eth_candidate("192.168.10.102:6900")]
    (usb,) = probe_usb(SCAN, found, env=usb_env(linux_ft4232()))
    assert LinkKind.ETHERNET not in kinds(usb) and len(found) == 2
    assert "2 MPS3 shell(s) on Ethernet and 1 on USB" in usb.evidence
    assert "firmware A0" in usb.evidence


def test_two_usb_boards_are_not_paired_with_one_shell():
    found = [eth_candidate()]
    ports = linux_ft4232(first=10, usb_path="1-2.3.4.3") + linux_ft4232(first=20, usb_path="1-5.1.3")
    cands = probe_usb(SCAN, found, env=usb_env(ports))
    assert len(cands) == 2 and len(found) == 1
    assert all("1 MPS3 shell(s) on Ethernet and 2 on USB" in c.evidence for c in cands)


def test_other_packs_are_never_paired():
    other = Candidate(pack="haps", board_id="haps@x", links=(Link(LinkKind.ETHERNET, "x"),))
    found = [other]
    (usb,) = probe_usb(SCAN, found, env=usb_env(linux_ft4232()))
    assert found == [other] and LinkKind.ETHERNET not in kinds(usb)


# --- explicit hints bypass scanning ---------------------------------------------------


def _no_scan() -> list:
    raise AssertionError("scanned although explicit ports/volumes were given")


def test_hints_bypass_scanning(tmp_path):
    sd = FakeSdVolume(tmp_path / "sd")
    hints = ProbeHints(serial_ports=("fake://mcc-1",), volumes=(str(sd.root),), scan_network=False)
    (cand,) = probe_usb(hints, [], env=UsbEnv(list_ports=_no_scan, list_volumes=lambda: []))
    assert [(lk.kind, lk.address) for lk in cand.links] == [
        (LinkKind.USB_SERIAL, "fake://mcc-1"), (LinkKind.USB_MSD, str(sd.root))]
    assert "given explicitly" in cand.evidence


def test_explicit_device_path_becomes_a_serial_url():
    hints = ProbeHints(serial_ports=("/dev/ttyUSB10", "COM7"))
    cands = probe_usb(hints, [], env=UsbEnv(list_ports=_no_scan, list_volumes=lambda: []))
    assert [c.links[0].address for c in cands] == ["serial:///dev/ttyUSB10", "serial://COM7"]


def test_explicit_daplink_volume_is_refused(tmp_path):
    dap = FakeDapLinkVolume(tmp_path / "dap")
    with pytest.raises(RefusedError, match="DAPLink"):
        probe_usb(ProbeHints(volumes=(str(dap.root),)), [],
                  env=UsbEnv(list_ports=_no_scan, list_volumes=lambda: []))


def test_no_scan_and_no_hints_finds_nothing():
    assert probe_usb(ProbeHints(scan_usb=False), [], env=UsbEnv(list_ports=_no_scan,
                                                                  list_volumes=_no_scan)) == []


def test_missing_pyserial_still_finds_the_sd(tmp_path):
    sd = FakeSdVolume(tmp_path / "sd")

    def no_pyserial() -> list:
        raise UsageError("pyserial is not installed")

    (cand,) = probe_usb(SCAN, [], env=UsbEnv(list_ports=no_pyserial,
                                             list_volumes=lambda: [VolumeInfo("V2M-MPS3", str(sd.root))]))
    assert kinds(cand) == [LinkKind.USB_MSD] and "serial ports not scanned" in cand.evidence


# --- console endpoints ---------------------------------------------------------------


def _usb_candidate(tmp_path, uartmode: str | None):
    sd = FakeSdVolume(tmp_path / "sd")
    if uartmode is None:
        (sd.root / "config.txt").write_text("TITLE: no uart mode here\n")
    else:
        (sd.root / "config.txt").write_text(f"UARTMODE: {uartmode}    ;0-MCC:FPGA0, 1-MCC:FPGA1\n")
    (cand,) = probe_usb(SCAN, [], env=usb_env(linux_ft4232(), [VolumeInfo("V2M-MPS3", str(sd.root))]))
    return cand


def test_lanes_follow_the_sd_uartmode(tmp_path):
    eps = serial_console_endpoints(_usb_candidate(tmp_path / "m0", "0"))
    assert eps == {"fpga_uart0": "serial:///dev/ttyUSB11", "fpga_uart2": "serial:///dev/ttyUSB12",
                   "fpga_uart3": "serial:///dev/ttyUSB13"}
    eps1 = serial_console_endpoints(_usb_candidate(tmp_path / "m1", "1"))
    assert eps1["fpga_uart1"] == "serial:///dev/ttyUSB11" and "fpga_uart0" not in eps1


def test_interface_01_is_left_out_when_the_mux_is_unknown(tmp_path):
    eps = serial_console_endpoints(_usb_candidate(tmp_path, None))
    assert set(eps) == {"fpga_uart2", "fpga_uart3"}
    assert all("ttyUSB10" not in url for url in eps.values())      # never the MCC


def test_ethernet_only_and_fake_candidates_have_no_lanes():
    assert serial_console_endpoints(eth_candidate()) == {}
    fake = Candidate("mps3", "b", (Link(LinkKind.USB_SERIAL, "fake://mcc", "MCC console (fake)"),))
    assert serial_console_endpoints(fake) == {}


def test_uartmode_parser_ignores_comments(tmp_path):
    (tmp_path / "CONFIG.TXT").write_text("; UARTMODE: 2\nUARTMODE: 1 ;comment\n")
    assert usbmod.uartmode_of(str(tmp_path)) == 1
    assert usbmod.uartmode_of(str(tmp_path / "missing")) is None
