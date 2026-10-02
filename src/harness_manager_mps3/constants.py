"""MPS3 facts the pack relies on, with their sources.

Keep this file factual. Every constant cites where it came from. When a fact
lives in the platform repo, prefer importing it from pyverify over copying it.
"""

from __future__ import annotations

from pathlib import Path

from pyverify.client import CONTROL_PORT  # noqa: F401 - re-exported; 6900
from pyverify.console import SWO_PORT, UART0_PORT, UART1_PORT  # 6930 / 6931 / 6932

DEFAULT_SHELL_HOST = "192.168.10.101"   # firmware/common/net_proto.h:35-38 (compile-time today)
JTAG_RBB_PORT = 6921                    # firmware jtag_server; OpenOCD remote_bitbang
XVC_PORT = 2542                         # firmware xvc_server (Debug Bridge target by default)
PUSH_PORT = 6910                        # raw/windowed bitstream push
#: DEBUG-ONBOARD: the Linux harness's on-board OpenOCD (``mps3-debug``, contract
#: ``mps3-debug/1``, confirmed by the Linux lead 2026-10-01) binds its gdb servers to the
#: board's 127.0.0.1 on these FIXED ports, core 0 then core 1. Its telnet (4444) and Tcl (6666)
#: ports are never forwarded off the board.
ONBOARD_GDB_PORTS = (3333, 3334)
ONBOARD_LAUNCHER = "mps3-debug"
#: The Linux harness's LCD mirror (mps3-lcdmirror, net-protocol v0.15): 127.0.0.1 ONLY on the
#: board, so it is reached through an SSH forward (``display.py``). ``version.lcd_mirror.port``
#: overrides it when the board says.
LCD_MIRROR_PORT = 6940

#: The firmware's clearing arena: the largest clearing the shell can hold for the
#: next swap-away (firmware/platform/Makefile:187,193 SWAP_CLEARING_ARENA_BYTES,
#: CLEARING_RAM_BYTES). A harness that reports ``clr_max`` (additive, in
#: ``version`` or ``stats``) overrides it; Linux keeps clearings in DDR.
CLEARING_ARENA_BYTES = 262144

CONSOLE_PORTS = {"uart0": UART0_PORT, "uart1": UART1_PORT, "swo": SWO_PORT}

#: Seconds per byte for input to the DUT UARTs. The nanoSoC UART has no receive FIFO:
#: an unpaced `print(1+1)` arrived as `p(1+1)` and a NameError (board window 2026-09-23,
#: platform docs/evidence/2026-09-w2/rf_flash_boot_20260923.txt). SWO is output only.
DUT_CONSOLE_PACE_S = 0.02
PACED_CONSOLES = ("uart0", "uart1")

# --- the two harness generations (T12) ----------------------------------------------
#
# Sources: the Linux harness plan §10/§10a (mps3-nanosoc-platform
# docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md) and the facts its owner agreed
# with the harness-manager lead on 2026-09-23.

#: ``version.impl`` values. The key is ADDITIVE: absent means the bare-metal
#: MicroBlaze firmware, ``"linux"`` means ``mps3-harnessd`` on the MicroBlaze V.
#: ``BoardIdentity.harness_impl`` is ``""`` only when the harness has no
#: ``version`` verb at all (net-protocol < v0.8, e.g. the July v0.7 daemons).
IMPL_BARE_METAL = "bare-metal"
IMPL_LINUX = "linux"

#: UDP identify probe (plan §10): one datagram in, one reply to the SENDER's
#: addr:port. Independent of the single-client 6900, so it answers while 6900
#: is held, parked by a swap, or (stage0 rescue) not there at all.
IDENTIFY_PORT = 6899
SSH_PORT = 22                           # dropbear on the Linux harness (DL5)

#: The reboot witness budget, per engine. Bare-metal: the MCC REBOOT witness
#: default (mcc.py). Linux: MCC config + stage0 reading the OS image over SPI +
#: a kernel boot. FIX-PACK-2 item 7 (2026-09-28): the Linux lead's stage0 cold-start
#: fix adds a 10 s DDR settle, so a cold MCC boot now answers 6900 after ~190 s
#: median (188.6 s max measured; 205 s to SSH), up from ~145 s: the old 180 s ran
#: out just before the board came up. 300 s leaves room; a failure only waits longer.
REBOOT_WAIT_S_BARE_METAL = 120.0
REBOOT_WAIT_S_LINUX = 300.0


#: The shell's DISTINCT refusal of ``swap``/``commit`` when the card's image and
#: the running fabric disagree (plan §10a S1: ``ping.shell_id`` stays the FABRIC
#: value, ``version`` shows the skew, swap/commit are refused). The exact ``err``
#: string is TBD in net-protocol.md; until it is published these substrings are
#: matched case-insensitively against the reply's ``err``. Replace the tuple with
#: the one published string when it lands.
FABRIC_MISMATCH_ERRS = ("fabric mismatch", "static mismatch", "card mismatch",
                        "efabric", "eskew", "skew",
                        # the Linux harness's fabric identity lock (net-protocol "Identity
                        # lock": the card image vs the FPGA static), LINUX-CLAIM
                        "identity lock")


def reboot_wait_s(identity: object | None) -> float:
    """How long a reboot witness waits for this harness to come back.

    ``identity`` is a ``BoardIdentity`` (or anything with ``harness_impl``);
    ``None`` or an unknown engine gets the bare-metal budget. The controller
    (mcc.py, Team T3) owns the witness; this is the number it should use.
    """
    impl = getattr(identity, "harness_impl", "") if identity is not None else ""
    return REBOOT_WAIT_S_LINUX if impl == IMPL_LINUX else REBOOT_WAIT_S_BARE_METAL

# USB identities on the MPS3 Debug USB connector (TRM 100765 §2.18).
FT4232H_VID_PID = (0x0403, 0x6011)      # 4x UART: MCC + FPGA UART lanes
MSD_VOLUME_LABEL = "V2M-MPS3"           # configuration microSD over USB mass storage
MCC_CHAR_PACE_S = 0.05                  # MCC drops burst writes; ≥50 ms/char (fpgahub 30ae4f3)

# rm_id design numbers (rm_id & 0xFFFF). The overlay store's manifests are the
# authority; this table only gives a readable name when no manifest is loaded.
# Source: fpga/dfx/rm_list.tcl, docs/evidence/2026-09-w2/summary_20260922T113359Z.txt.
KNOWN_DESIGNS = {
    0x0000: "greybox",
    0x0001: "nanosoc",
    0x0003: "nanosoc_multicore",
    0x0005: "nanosoc_upy",
    0x0008: "nanosoc_iice",
    0x001E: "led",
}

# Designs whose own boot code writes the DUT's QSPI flash (FIX-PACK-8; core.pack.DutFlashWrite:
# Harness Manager warns before EVERY program of one and asks for the word, typed; --yes never
# implies it). {design id (rm_id & 0xFFFF): (why, word)}; a manifest naming the design matches
# too (overlays.writes_dut_flash). nanosoc_multicore: CPU1's boot ROM writes one 0x00 byte at
# DUT flash 0x20000 onward on every boot, until the v2.1 ROM fix (Linux v2.0.0 release notes,
# known issue 15; docs/HIL_LINUX.md OCD8).
WRITES_DUT_FLASH = {
    0x0003: ("nanosoc_multicore's boot code writes the DUT's QSPI flash (one byte at 0x20000 "
             "onward) on every boot, until Linux v2.1: it damages a MicroPython image there "
             "(nanosoc_upy).", "MULTICORE"),
}

# Designs that expose the SoC-400 debug port on the RP jtag_* pins, mapped to
# the OpenOCD target-half config in mps3-nanosoc-platform/host/openocd/.
# nanosoc_multicore needs a two-AP config that does not exist yet (harness
# handover B4).
DAP_DESIGN_CONFIGS = {
    0x0001: ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"),
    0x0005: ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"),
    0x0008: ("nanosoc_iice_chain.cfg", "nanosoc_ops.tcl"),
}

# Where the OpenOCD target-half configs live when the setting mps3.openocd_cfg_dir (its
# variable, then the Settings menu: openocd.config_dir(), read at each use) names none: a
# platform repo checked out next to this one (a developer's live copy), else the copy
# shipped in this package (openocd_cfg/, vendored from the platform commit named in
# vendor/README.md). Never the environment at import (SETTINGS.md §12.9, lane SET-WIRE).
_default_cfg = Path(__file__).resolve().parents[3] / "mps3-nanosoc-platform" / "host" / "openocd"
PACKAGED_OPENOCD_CFG = Path(__file__).resolve().parent / "openocd_cfg"
OPENOCD_CFG_DIR: Path | None = (
    _default_cfg if _default_cfg.is_dir()
    else PACKAGED_OPENOCD_CFG if PACKAGED_OPENOCD_CFG.is_dir()
    else None
)
