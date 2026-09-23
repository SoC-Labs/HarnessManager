"""MPS3 facts the pack relies on, with their sources.

Keep this file factual. Every constant cites where it came from. When a fact
lives in the platform repo, prefer importing it from pyverify over copying it.
"""

from __future__ import annotations

import os
from pathlib import Path

from pyverify.client import CONTROL_PORT  # noqa: F401 - re-exported; 6900
from pyverify.console import SWO_PORT, UART0_PORT, UART1_PORT  # 6930 / 6931 / 6932

DEFAULT_SHELL_HOST = "192.168.10.101"   # firmware/common/net_proto.h:35-38 (compile-time today)
JTAG_RBB_PORT = 6921                    # firmware jtag_server; OpenOCD remote_bitbang
XVC_PORT = 2542                         # firmware xvc_server (Debug Bridge target by default)
PUSH_PORT = 6910                        # raw/windowed bitstream push

CONSOLE_PORTS = {"uart0": UART0_PORT, "uart1": UART1_PORT, "swo": SWO_PORT}

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

# Designs that expose the SoC-400 debug port on the RP jtag_* pins, mapped to
# the OpenOCD target-half config in mps3-nanosoc-platform/host/openocd/.
# nanosoc_multicore needs a two-AP config that does not exist yet (harness
# handover B4).
DAP_DESIGN_CONFIGS = {
    0x0001: ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"),
    0x0005: ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"),
    0x0008: ("nanosoc_iice_chain.cfg", "nanosoc_ops.tcl"),
}

# Where the OpenOCD target-half configs live (mps3-nanosoc-platform/host/openocd).
# Harness bundles will ship them (harness Lane G1); until then, point at the platform repo.
_default_cfg = Path(__file__).resolve().parents[3] / "mps3-nanosoc-platform" / "host" / "openocd"
OPENOCD_CFG_DIR: Path | None = (
    Path(os.environ["SOCHARNESS_MPS3_OPENOCD_DIR"]) if os.environ.get("SOCHARNESS_MPS3_OPENOCD_DIR")
    else (_default_cfg if _default_cfg.is_dir() else None)
)
