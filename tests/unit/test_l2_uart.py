"""Lane L2: the MPS3 design -> UART rate table, and its citations.

Each row with a rate cites the wrapper line that fixes it. When the platform
repo is on this machine (pyverify is installed from it), the cited line must
really say ``UART_BAUD = <rate>``; rows whose file this checkout lacks are
skipped (the ILA RM lives on the mint branch). Every check has a negative twin.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import pyverify

from harness_manager_mps3 import uart
from harness_manager_mps3.constants import KNOWN_DESIGNS


def platform_root() -> Path | None:
    root = Path(pyverify.__file__).resolve().parents[3]
    return root if (root / "fpga" / "rp").is_dir() else None


def cited_line(source: str) -> tuple[str, int]:
    m = re.match(r"(\S+?):(\d+)", source)
    assert m, f"no file:line in {source!r}"
    return m.group(1), int(m.group(2))


def says_rate(root: Path, source: str, baud: int) -> bool:
    rel, line = cited_line(source)
    lines = (root / rel).read_text().splitlines()
    return bool(re.search(rf"UART_BAUD\s*=\s*{baud}\b", lines[line - 1]))


def test_every_design_with_a_console_has_a_rate_and_a_citation():
    rated = {did: row for did, row in uart.DESIGN_UART.items() if row.baud}
    assert set(rated) == {0x0001, 0x0003, 0x0005, 0x0008, 0x000A}
    assert all(row.baud == 76800 and ".sv:" in row.source for row in rated.values())
    # Names agree with the pack's own design table wherever both know the design.
    for did, row in uart.DESIGN_UART.items():
        if did in KNOWN_DESIGNS:
            assert KNOWN_DESIGNS[did] == row.name


def test_negative_twin_designs_without_a_console_say_why_not_zero():
    for did in (0x0000, 0x0002, 0x0004, 0x0007, 0x0009):
        row = uart.DESIGN_UART[did]
        assert row.baud is None and row.why and "0" != row.why


@pytest.mark.parametrize("did", [0x0001, 0x0003, 0x0005, 0x000A])
def test_the_cited_line_really_fixes_the_rate(did):
    root = platform_root()
    if root is None:
        pytest.skip("the platform repo is not next to pyverify here")
    row = uart.DESIGN_UART[did]
    rel, _ = cited_line(row.source)
    if not (root / rel).is_file():
        pytest.skip(f"{rel} is not in this platform checkout")
    assert says_rate(root, row.source, row.baud)


def test_negative_twin_a_wrong_line_is_caught():
    root = platform_root()
    if root is None or not (root / "fpga/rp/nanosoc/rp_nanosoc_wrapper.sv").is_file():
        pytest.skip("the platform repo is not next to pyverify here")
    assert not says_rate(root, "fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:53", 76800)
    assert not says_rate(root, uart.DESIGN_UART[1].source, 115200)


def test_design_row_for_nanosoc_names_the_fixed_rate_and_the_missing_feature():
    row = uart.design_row("uart0", "0x01000001")
    assert (row["kind"], row["baud"], row["source"], row["settable"]) == ("ethernet", 76800,
                                                                          "design", False)
    assert "nanosoc" in row["reason"] and "76800" in row["reason"] and "uart_baud" in row["reason"]
    assert row["choices"] == [76800] and row["design"] == "nanosoc"


def test_negative_twin_an_unrecorded_design_is_unknown_not_76800():
    row = uart.design_row("uart0", "0x01000006")           # socscope: no row
    assert row["baud"] is None and row["source"] == "unknown" and "0x0006" in row["reason"]
    garbage = uart.design_row("uart0", "not-an-id")
    assert garbage["baud"] is None and "the loaded design" in garbage["reason"]


def test_uart1_and_swo_come_from_the_shell_not_the_design():
    assert uart.design_row("uart1", "0x01000001")["baud"] is None
    assert "ties its DUT side off" in uart.design_row("uart1", "0x01000001")["reason"]
    swo = uart.design_row("swo", "0x01000001")
    assert (swo["baud"], swo["source"]) == (2_000_000, "harness") and "divisor 24" in swo["reason"]


def test_harness_row_reads_the_frozen_reply_shape():
    base = uart.design_row("uart0", "0x01000001")
    row = uart.harness_row("uart0", {"ok": True, "stream": "uart0", "baud": 115200, "mode": "set",
                                     "settable": True}, base)
    assert (row["baud"], row["source"], row["mode"], row["settable"]) == (115200, "harness",
                                                                          "set", True)
    assert 115200 in row["choices"] and row["reason"] == ""
    # Negative twins: a fixed stream and an ok:false reply are not settable, with a reason.
    fixed = uart.harness_row("uart0", {"ok": True, "baud": 76800, "mode": "fixed",
                                       "settable": False}, base)
    assert not fixed["settable"] and "fixed" in fixed["reason"]
    refused = uart.harness_row("uart0", {"ok": False, "err": "EBUSY"}, base)
    assert not refused["settable"] and "EBUSY" in refused["reason"] and refused["baud"] == 76800


def test_a_tcp_share_is_not_settable_and_says_so():
    row = uart.serial_row("tcp://hub:7001?baud=115200")
    assert (row["kind"], row["baud"], row["settable"], row["share"]) == ("serial", 115200,
                                                                         False, True)
    assert "share sets the rate" in row["reason"]
    assert uart.serial_row("tcp://hub:7001")["source"] == "unknown"


def test_console_baud_info_without_a_shell_reports_only_shares():
    info = uart.console_baud_info({"fpga_uart0": "serial:///dev/ttyUSB1",
                                   "fpga_uart1": "tcp://hub:7001"}, None)
    assert set(info) == {"fpga_uart1"}                      # the broker owns serial:// rates
