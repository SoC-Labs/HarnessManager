"""The design manifest (designs.py): one row per design, and the old tables as views of it.

The SNAPSHOT_* literals below were copied from the four hand-kept tables BEFORE they were
replaced (constants.py KNOWN_DESIGNS / WRITES_DUT_FLASH / DAP_DESIGN_CONFIGS, uart.py
DESIGN_UART). The aliases must keep returning exactly these.
"""

from __future__ import annotations

import dataclasses

import pytest

from harness_manager.core.errors import NothingOnTargetError
from harness_manager_mps3 import constants, deploy, designs, openocd, uart

SNAPSHOT_KNOWN = {
    0x0000: "greybox", 0x0001: "nanosoc", 0x0003: "nanosoc_multicore", 0x0005: "nanosoc_upy",
    0x0008: "nanosoc_iice", 0x000C: "nanosoc_motor", 0x001E: "led",
}
SNAPSHOT_DAP = {
    0x0001: ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"),
    0x0005: ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"),
    0x0008: ("nanosoc_iice_chain.cfg", "nanosoc_ops.tcl"),
    0x000C: ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"),
}
SNAPSHOT_FLASH_KEYS = {0x0003}
SNAPSHOT_FLASH_WORD = "MULTICORE"
SNAPSHOT_UART = {
    0x0001: ("nanosoc", 76800), 0x0003: ("nanosoc_multicore", 76800),
    0x0005: ("nanosoc_upy", 76800), 0x0008: ("nanosoc_iice", 76800),
    0x000A: ("nanosoc_ila", 76800), 0x0000: ("greybox", None), 0x0002: ("eth_ss", None),
    0x0004: ("uart_echo", None), 0x0007: ("clcd_demo", None), 0x0009: ("dbg_demo", None),
}


def test_known_designs_alias_is_unchanged():
    assert constants.KNOWN_DESIGNS == SNAPSHOT_KNOWN


def test_dap_alias_is_unchanged():
    assert constants.DAP_DESIGN_CONFIGS == SNAPSHOT_DAP


def test_writes_dut_flash_alias_is_unchanged():
    assert set(constants.WRITES_DUT_FLASH) == SNAPSHOT_FLASH_KEYS
    why, word = constants.WRITES_DUT_FLASH[0x0003]
    assert word == SNAPSHOT_FLASH_WORD
    assert why.startswith("nanosoc_multicore's boot code writes the DUT's QSPI flash (one byte "
                          "at 0x20000 onward) on every boot: it damages a MicroPython image "
                          "there (nanosoc_upy).")


def test_uart_alias_is_unchanged():
    got = {k: (r.name, r.baud) for k, r in uart.DESIGN_UART.items()}
    assert got == SNAPSHOT_UART
    for row in uart.DESIGN_UART.values():
        assert isinstance(row, uart.DesignUart) and row.source
        assert (row.baud is None) == bool(row.why)


def test_manifest_covers_every_design_any_table_knows():
    ids = set(designs.DESIGNS)
    assert ids >= set(SNAPSHOT_KNOWN) | set(SNAPSHOT_UART) | set(SNAPSHOT_DAP)
    for key, row in designs.DESIGNS.items():
        assert key == row.design_id and 0 <= key <= 0xFFFF and row.name


def test_manifest_is_immutable():
    row = designs.DESIGNS[0x0003]
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.name = "x"                                  # type: ignore[misc]
    with pytest.raises(TypeError):
        designs.DESIGNS[0x9999] = row                   # type: ignore[index]


def test_lookup_by_rm_id_uses_low_16_bits():
    assert designs.design_row(0x0100_0003).name == "nanosoc_multicore"
    assert designs.design_row(0xFFFF_FFF0) is None
    assert designs.design_row("garbage") is None


def test_sizes():
    mb = 1_000_000
    assert designs.partial_bytes(0x0000) == 3_450_000
    assert designs.partial_bytes(0x0003) == 7_000_000
    assert designs.partial_bytes(0x0002) == 3_970_000
    assert designs.partial_bytes(0x001E) is None and designs.partial_bytes(0x7777) is None
    assert designs.expected_load_s(0x001E) is None and designs.expected_load_s(0x7777) is None
    assert designs.CLEAR_BUFFER_BYTES == 4 * 1024 * 1024
    assert designs.partial_bytes(0x0003) > designs.CLEAR_BUFFER_BYTES    # the 7 MB one is the big one
    assert mb == 10 ** 6


def test_load_time_budgets():
    multi = designs.expected_load_s(0x0003)
    assert 170 <= multi <= 190                       # "about 3 minutes"
    assert designs.expected_load_s(0x0000) < multi
    for row in designs.DESIGNS.values():
        t = designs.expected_load_s(row.design_id)
        if t is not None:
            assert t < deploy.SWAP_TIMEOUT_S         # every known design fits the swap wait


def test_card_commit_time_uses_the_card_rates():
    from harness_manager_mps3 import os_slots
    want = 7_000_000 / os_slots.CARD_WRITE_BPS + 7_000_000 / os_slots.CARD_READ_BPS
    assert designs.card_commit_s(0x0003) == pytest.approx(want)
    assert designs.card_commit_s(0x001E) is None


def test_multicore_debug_on_the_host_path():
    rec = openocd.design_debug(0x0003)
    assert rec.name == "nanosoc_multicore"
    assert rec.configs == ("nanosoc_mps3_multicore_jtag.cfg",)
    assert rec.gdb_attach == (("nanosoc.cpu0", "nanosoc_halt_examine"),
                              ("nanosoc.cpu1", "nanosoc_halt_examine1"))
    cfg = (constants.PACKAGED_OPENOCD_CFG / rec.configs[0]).read_text()
    for target, proc in rec.gdb_attach:              # every named target and proc is in the cfg
        assert f"target create $_CHIPNAME.{target.split('.')[1]}" in cfg
        assert f"proc {proc} " in cfg


def test_single_core_recipes_unchanged():
    rec = openocd.design_debug(0x0001)
    assert rec.configs == SNAPSHOT_DAP[0x0001]
    assert rec.gdb_attach == (("nanosoc.cpu0", "nanosoc_halt_examine"),)
    with pytest.raises(NothingOnTargetError):
        openocd.design_debug(0x001E)
    assert openocd.DAP_TAP_IDCODE == designs.DAP_TAP_IDCODE == "0x6ba00477"
    assert openocd.MULTICORE_DESIGN == designs.MULTICORE_DESIGN == 0x0003
