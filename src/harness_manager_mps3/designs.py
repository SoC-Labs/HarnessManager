"""The design manifest: one immutable row per design, keyed by ``rm_id & 0xFFFF``.

Everything Harness Manager knows about a design by number lives here once: its name, what its
console does, how to debug it, whether its own boot code writes the DUT's flash, how big its
partial bitstream is and how long it takes to load. The older per-topic tables
(``constants.KNOWN_DESIGNS`` / ``WRITES_DUT_FLASH`` / ``DAP_DESIGN_CONFIGS`` and
``uart.DESIGN_UART``) are views computed from this one, kept under their old names.

The overlay store's manifests stay the authority for a loaded card; this table is what the
program knows without one. Imports nothing from this package, so ``constants`` can build on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

#: The debug port's JTAG identity (``nanosoc_mps3_jtag.cfg``: ``_DAP_TAPID``, silicon-verified).
DAP_TAP_IDCODE = "0x6ba00477"
#: nanosoc_multicore: two cores, one access port each.
MULTICORE_DESIGN = 0x0003

#: The shell's clear buffer (``clr_max``), bytes: the most one partial can clear in one go.
CLEAR_BUFFER_BYTES = 4 * 1024 * 1024
#: How fast a partial loads over the wire and the ICAP, bytes a second. Measured on mint 4
#: (shape A): the 7.0 MB multicore partial takes about 3 minutes (180 s), so 7.0 MB / 180 s.
SWAP_LOAD_BPS = 39_000

_GDB_ONE_CORE = (("nanosoc.cpu0", "nanosoc_halt_examine"),)
_GDB_TWO_CORES = (("nanosoc.cpu0", "nanosoc_halt_examine"),
                  ("nanosoc.cpu1", "nanosoc_halt_examine1"))


@dataclass(frozen=True)
class UartInfo:
    """What a design does with uart0. ``baud`` None: no serial line (see ``why``)."""

    baud: int | None
    source: str                    # the file (and line) that fixes it, platform-repo relative
    why: str = ""                  # for ``baud`` None: why there is no rate


@dataclass(frozen=True)
class DesignRow:
    design_id: int
    name: str
    uart: UartInfo | None = None
    #: OpenOCD target-half files for the single-core debug port (``DAP_DESIGN_CONFIGS``).
    dap_configs: tuple[str, ...] | None = None
    #: The same for a design with one access port per core (host path only).
    multi_ap_configs: tuple[str, ...] | None = None
    #: (OpenOCD target, Tcl proc) run when GDB attaches.
    gdb_attach: tuple[tuple[str, str], ...] = _GDB_ONE_CORE
    #: Why its boot code writes the DUT's flash, and the word the user types to go on.
    writes_flash_why: str = ""
    writes_flash_word: str = ""
    #: Partial bitstream size in bytes (mint 4, measured); None when not measured.
    partial_bytes: int | None = None
    #: In the older ``constants.KNOWN_DESIGNS`` name table. False for designs only the console
    #: map knew; flipping them to True is a deliberate later change (it widens that table).
    in_known_names: bool = True

    @property
    def debug_configs(self) -> tuple[str, ...] | None:
        return self.dap_configs or self.multi_ap_configs


_JTAG = ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl")


def _row(design_id: int, name: str, **kw: Any) -> tuple[int, DesignRow]:
    return design_id, DesignRow(design_id, name, **kw)


DESIGNS: Mapping[int, DesignRow] = MappingProxyType(dict([
    _row(0x0000, "greybox", partial_bytes=3_450_000,
         uart=UartInfo(None, "fpga/dfx/rms/rm_greybox/rm_greybox.sv",
                       "no design is loaded (greybox): nothing drives uart0")),
    _row(0x0001, "nanosoc", dap_configs=_JTAG,
         uart=UartInfo(76800, "fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:54 (UART_BAUD -> "
                              "uart_axis_shim .BAUD, 382-384)")),
    _row(0x0002, "eth_ss", in_known_names=False, partial_bytes=3_970_000,
         uart=UartInfo(None, "fpga/rp/eth_ss/rp_eth_ss_wrapper.sv:193-195",
                       "eth_ss ties its console off (rp_eth_ss_wrapper.sv:193-195)")),
    _row(0x0003, "nanosoc_multicore", partial_bytes=7_000_000,
         multi_ap_configs=("nanosoc_mps3_multicore_jtag.cfg",), gdb_attach=_GDB_TWO_CORES,
         uart=UartInfo(76800, "fpga/rp/nanosoc_multicore/rp_nanosoc_multicore_wrapper.sv:49 "
                              "(UART_BAUD -> uart_axis_shim .BAUD, 229-231)"),
         writes_flash_word="MULTICORE",
         writes_flash_why="nanosoc_multicore's boot code writes the DUT's QSPI flash (one byte "
                          "at 0x20000 onward) on every boot: it damages a MicroPython image "
                          "there (nanosoc_upy)."),
    _row(0x0004, "uart_echo", in_known_names=False,
         uart=UartInfo(None, "fpga/dfx/rms/rm_uart_echo/rm_uart_echo.sv:273-274",
                       "uart_echo echoes bytes on the AXI-Stream itself: there is no serial "
                       "line, so there is no rate (rm_uart_echo.sv:273-274)")),
    _row(0x0005, "nanosoc_upy", dap_configs=_JTAG,
         uart=UartInfo(76800, "fpga/rp/nanosoc_upy/rp_nanosoc_upy_wrapper.sv:62 (passed to "
                              "rp_nanosoc_wrapper, 164)")),
    _row(0x0007, "clcd_demo", in_known_names=False,
         uart=UartInfo(None, "fpga/rp/clcd_demo/rp_clcd_demo_wrapper.sv:331-333",
                       "clcd_demo ties its console off (rp_clcd_demo_wrapper.sv:331-333)")),
    _row(0x0008, "nanosoc_iice", dap_configs=("nanosoc_iice_chain.cfg", "nanosoc_ops.tcl"),
         uart=UartInfo(76800, "fpga/rp/nanosoc_iice/rp_nanosoc_iice_core.sv:165-182 (forwards "
                              "only NGPIO, so rp_nanosoc_wrapper.sv:54's default applies)")),
    _row(0x0009, "dbg_demo", in_known_names=False,
         uart=UartInfo(None, "fpga/rp/dbg_demo/rp_dbg_demo_wrapper.sv:148-150",
                       "dbg_demo ties its console off (rp_dbg_demo_wrapper.sv:148-150)")),
    _row(0x000A, "nanosoc_ila", in_known_names=False,
         uart=UartInfo(76800, "fpga/rp/nanosoc_ila/rp_nanosoc_ila_wrapper.sv:40 (passed to "
                              "rp_nanosoc_wrapper, 107)")),
    _row(0x000C, "nanosoc_motor", dap_configs=_JTAG),       # the nanosoc DAP
    _row(0x001E, "led"),
]))


def design_row(rm_id: Any) -> DesignRow | None:
    """The row for an rm_id (or a bare design number): its low 16 bits. None when unknown or
    when ``rm_id`` does not parse."""
    try:
        key = (int(rm_id, 0) if isinstance(rm_id, str) else int(rm_id)) & 0xFFFF
    except (TypeError, ValueError):
        return None
    return DESIGNS.get(key)


def partial_bytes(rm_id: Any) -> int | None:
    """The design's partial bitstream size in bytes; None when not measured."""
    row = design_row(rm_id)
    return row.partial_bytes if row else None


def expected_load_s(rm_id: Any) -> float | None:
    """Seconds a swap of this design takes (size / ``SWAP_LOAD_BPS``); None when the size is
    not known. The 300 s swap wait (``deploy.SWAP_TIMEOUT_S``) covers every measured design."""
    size = partial_bytes(rm_id)
    return None if size is None else size / SWAP_LOAD_BPS


def card_commit_s(rm_id: Any) -> float | None:
    """Seconds to keep the design on the user card: written, then read back, at the card's
    rates (``os_slots.CARD_WRITE_BPS`` / ``CARD_READ_BPS``); None when the size is not known."""
    size = partial_bytes(rm_id)
    if size is None:
        return None
    from . import os_slots  # late: os_slots imports constants, which imports this
    return size / os_slots.CARD_WRITE_BPS + size / os_slots.CARD_READ_BPS
