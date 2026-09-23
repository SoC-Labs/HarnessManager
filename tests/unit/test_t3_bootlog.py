"""T3: the MCC boot-log parser on the real board's console capture.

Fixture: tests/fixtures/t3/pB_mcc_log_20260923.txt, a byte-exact copy of
mps3-nanosoc-platform docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt (MCC
console tty_00 across the 2026-09-23 power cycles; seven power-ons; mixed
CR / LF / CRLF line ends; ``Cmd>`` prompts stripped).
"""

from __future__ import annotations

from pathlib import Path

from socharness_board_mps3.mcc import BootWatch, parse_boot_log
from tests.fakes.fake_mcc import BOOT_BANNER

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "t3" / "pB_mcc_log_20260923.txt"
REAL_OSC = {0: 25.0, 1: 50.0, 2: 50.0, 3: 50.0, 4: 24.576, 5: 23.75}


def real_log() -> str:
    return FIXTURE.read_bytes().decode("ascii")


def test_fixture_is_the_raw_capture():
    raw = FIXTURE.read_bytes()
    assert b"\rAddress: 0x00020000" in raw          # CR-separated progress records survive git


def test_real_log_parses_into_seven_power_ons():
    boots = parse_boot_log(real_log())
    assert len(boots) == 7
    for b in boots:
        assert b.complete and b.fpga_configured
        assert (b.bootloader, b.firmware, b.build_date) == ("v1.0.0", "v1.3.2", "Apr 20 2018")
        assert b.hbi_build == "HBI0309 build 567" and b.board == "rev C, var A"
        assert b.board_file == "\\MB\\HBI0309C\\Nanosoc\\nanosoc.txt"
        assert b.fpga_file == "\\MB\\HBI0309C\\Nanosoc\\nanosoc.bit"
        assert b.fpga_config_records == 92 and b.fpga_last_address == 0x00B60000
        assert b.osc_mhz == REAL_OSC and b.osc_setup == "PASSED" and b.gtxclk == "PASSED"
        assert b.uart_map == {"UART0": "MCC", "UART1": "FPGA0"}
        assert b.usb_serial == "0000000000000"


def test_real_log_errors_and_warnings():
    first = parse_boot_log(real_log())[0]
    assert first.errors[0] == "File not found \\MB\\HBI0309C\\mbb_v141.ebf"
    assert "File not found \\MB\\HBI0309C\\Nanosoc\\images.txt" in first.errors
    assert any("SMSC9220 bus is in 16-bit mode" in e for e in first.errors)
    assert "SMSC9220 initialisation failed." in first.errors
    assert first.warnings == ["Image file not found"]


def test_external_reset_is_attached_to_the_boot_it_caused():
    boots = parse_boot_log(real_log())
    assert boots[-1].preceded_by == ["External reset request...", "Resets released..."]
    assert all(b.preceded_by == [] for b in boots[:-1])


def test_truncated_boot_is_neither_configured_nor_complete():
    # Negative twin: cut the capture before the FPGA finished configuring.
    text = real_log()
    cut = text[: text.index("FPGA configuration complete.")]
    last = parse_boot_log(cut)[-1]
    assert not last.fpga_configured and not last.complete and last.osc_mhz == {}


def test_text_without_a_banner_has_no_boots():
    assert parse_boot_log("Cmd> HELP\r\nCAP COPY DEBUG\r\nCmd> ") == []
    assert parse_boot_log("") == []


def test_boot_seen_from_the_middle_is_still_a_boot():
    # The port dropped and came back mid-banner: the header was missed.
    text = real_log()
    tail = text[text.rindex("Powering up system..."):]
    (boot,) = parse_boot_log(tail)
    assert boot.bootloader == "" and boot.fpga_configured and boot.complete


def test_the_fake_banner_matches_the_real_one():
    # Keeps FakeMcc honest: its banner parses to the real board's facts.
    fake = parse_boot_log("\r\n".join(BOOT_BANNER) + "\r\nCmd> ")[0]
    real = parse_boot_log(real_log())[0]
    for attr in ("firmware", "bootloader", "board", "board_file", "fpga_file", "osc_mhz",
                 "uart_map", "errors", "warnings", "fpga_configured", "complete"):
        assert getattr(fake, attr) == getattr(real, attr), attr
    assert fake.at_prompt


def test_boot_watch_tracks_progress():
    watch = BootWatch()
    assert not watch.started and not watch.in_progress
    watch.feed(b"ARM V2M-MPS3 Boot loader v1.0.0\r\nPress Enter to stop auto boot...\r\n")
    assert watch.started and watch.in_progress
    watch.feed(b"FPGA configuration complete.\r\nEnabling debug USB.\r\n")
    assert not watch.in_progress and watch.settling
    watch.feed(b"USB Serial Number = 0000000000000\r\nCmd> ")
    assert not watch.settling and watch.record.at_prompt and watch.record.fpga_configured
