"""Curated MPS3 facts for tools/gen_mps3_pins.py: what the collateral says in prose.

Every fact here cites a file in the platform repo and a PATTERN that must appear in
that file at the generator's ref. The generator checks each pattern and records the
line it found, so a fact whose source moved or changed fails the generation instead
of going stale silently. Nothing here states a package pin: pins, banks, standards
and directions come from the constraint files and the Xilinx package file.
"""

from __future__ import annotations

#: Short ids for the Arm pinmap's groups (fpga/monolithic/nanosoc_mps3.xdc banner titles,
#: matched by prefix), with the peripheral each group reaches on the board.
GROUPS: list[tuple[str, str, str]] = [
    # (title prefix, id, what it reaches)
    ("LAN9220 Ethernet SMC", "eth_smc", "SMSC LAN9220 10/100 Ethernet controller (chip selects, IRQ)"),
    ("SMBF_* static memory bus", "smbf", "static memory bus shared by the LAN9220 and the USB debug FIFO"),
    ("USB debug/trace FIFO", "usb_fifo", "USB debug/trace FIFO on the SMBF bus"),
    ("UART_TX_F/UART_RX_F", "uart_f", "the four FPGA UART lanes to the FT4232 USB serial bridge"),
    ("CoreSight debug", "coresight", "20-pin CoreSight debug header (JTAG + 16-bit trace)"),
    ("USER_nLED / USER_SW / USER_nPB", "user_io", "user LEDs, DIP switches and push-buttons"),
    ("OSCCLK[5:0]", "oscclk", "MCC-programmed board oscillators OSC0..OSC5"),
    ("Quad SPI boot/overlay flash", "qspi", "SST26VF064B quad SPI flash"),
    ("User microSD interface", "usd", "user microSD card slot"),
    ("Board-level reset/run signals", "mcc_ctrl", "MCC reset/run and IO-FPGA supervisor signals"),
    ("SCC (System Configuration Controller)", "scc", "MCC serial configuration (SCC) bus"),
    ("MCC SMB backdoor bus", "smbm", "MCC static memory backdoor bus (DEPOSIT/EXAM)"),
    ("Arduino-style shield header", "shield", "Arduino-style shield headers SH0/SH1 (and the Pmods under them), shield ADC SPI"),
    ("CLCD (character/graphics LCD)", "clcd", "on-board QVGA CLCD (HX8347-D, 8080 bus) and its touch controller"),
    ("HDMI CEC/DDC", "hdmi", "HDMI CEC/DDC side channel"),
    ("Audio codec", "audio", "audio codec I2S + I2C control"),
    ("eMMC interface", "emmc", "on-board eMMC"),
    ("MMB (motherboard multimedia bus", "mmb", "MMB parallel video-out bus"),
]

PKG_NOTE = ("Xilinx IBIS package model (ships with Vivado; licence-free): the pin's IO "
            "function name carries its bank and clock capability (_GC_ = global clock "
            "capable, _QBC_/_DBC_ = byte-lane clock capable).")

#: Board oscillators: the value the MCC programs (fpga/mps3_sd/templates/nanosoc.txt).
OSCILLATORS: list[dict] = [
    {"osc": "OSC0", "net": "OSCCLK[0]", "cite": ("fpga/mps3_sd/templates/nanosoc.txt", "OSC0: 25.0")},
    {"osc": "OSC1", "net": "OSCCLK[1]", "cite": ("fpga/mps3_sd/templates/nanosoc.txt", "OSC1: 50.0")},
    {"osc": "OSC2", "net": "OSCCLK[2]", "cite": ("fpga/mps3_sd/templates/nanosoc.txt", "OSC2: 50.0")},
    {"osc": "OSC3", "net": "OSCCLK[3]", "cite": ("fpga/mps3_sd/templates/nanosoc.txt", "OSC3: 50.0")},
    {"osc": "OSC4", "net": "OSCCLK[4]", "cite": ("fpga/mps3_sd/templates/nanosoc.txt", "OSC4: 24.576")},
    {"osc": "OSC5", "net": "OSCCLK[5]", "cite": ("fpga/mps3_sd/templates/nanosoc.txt", "OSC5: 23.75")},
]

#: Nets a design must not use, or must use with care.
RESERVED: list[dict] = [
    {"nets": ["UART_TX_F[0]", "UART_RX_F[0]"],
     "reason": "FPGA UART lane 0 is the MCC's own console (reserved, do not use)",
     "cite": ("fpga/shell/constraints/mps3_harness.xdc", "[0] AF27/AF28 = the MCC's OWN internal console")},
]
CAUTIONS: list[dict] = [
    {"nets": ["UART_TX_F[1]", "UART_RX_F[1]"],
     "text": "lane 1 reaches the host only through the MCC's UART mux (UARTMODE in config.txt)",
     "cite": ("fpga/shell/constraints/mps3_harness.xdc", "[1] AE30/AE31 = muxed")},
    {"nets": ["SH0_IO[16]", "SH0_IO[17]", "SH1_IO[16]", "SH1_IO[17]"],
     "text": "TRM caution (harness handover C2): SH*_IO16/17 connect through 4K7 to IO14/15; hold them high-Z",
     "cite": ("docs/planning/BOARD_MANAGER_HARNESS_HANDOVER.md", "SH*_IO16/17 connect through 4K7 to IO14/15")},
]

#: Connector facts that are bench reports, not a pinmap (verified: inferred).
CONNECTORS: list[dict] = [
    {"id": "SH0", "title": "Arduino-style shield header, channel 0 (bank 84, 3.3 V)",
     "nets": "SH0_IO[17:0]", "shared_with": ["J28 (Pmod0/1)", "J34 pins 1-4"],
     "verified": "inferred",
     "cite": ("docs/CONNECTOR_SURVEY.md", "J28 (Pmod0/1), J34 (Pmod2/3), J38")},
    {"id": "SH1", "title": "Arduino-style shield header, channel 1 (bank 94, 3.3 V)",
     "nets": "SH1_IO[17:0]", "shared_with": ["J36"], "verified": "inferred",
     "cite": ("docs/CONNECTOR_SURVEY.md", "SH1 is fully pinned and free")},
    {"id": "J28", "title": "Pmod J28 (Pmod0/1) under the shield headers: the SH0 channel",
     "verified": "inferred",
     "pins": {"3": "SH0_IO[0]", "2": "SH0_IO[1]", "8": "SH0_IO[2]", "9": "SH0_IO[15]",
              "7": "SH0_IO[4]", "1": "SH0_IO[3]", "4": "SH0_IO[9]", "10": "SH0_IO[14]"},
     "cite": ("docs/CONNECTOR_SURVEY.md", "| phy_rmii_ref_clk | AW14 | SH0_IO[0]  | J28 pin 3 |")},
    {"id": "J34", "title": "Pmod J34 (Pmod2/3): pins 1-4 are SH0 channels", "verified": "inferred",
     "pins": {"3": "SH0_IO[12]"},
     "cite": ("docs/CONNECTOR_SURVEY.md", "**J34 pin 3** (overflow; J34 pins 1-4 are SH0 channels)")},
]
CONNECTOR_NOTE = ("docs/CONNECTOR_SURVEY.md is marked HISTORICAL (2026-08-07); the Pmod/connector "
                  "pin numbers are bench reports, not a pinmap. Harness Lane C2 transcribes the TRM "
                  "connector tables (Appendix A) into the real model.")

#: What each boundary signal reaches in the fielded static. `bits` narrows a fact to a
#: slice of a vector; `board_net` names the board net per bit (`{i}` = the bit index,
#: `{i-8}` = index minus 8). `static` is the block; `via` the host-visible service.
CONNECTIVITY: list[dict] = [
    {"signal": "dut_clk", "static": "clk_wiz_dut MMCM clk_out1 (DRP-reconfigurable, default 50 MHz)",
     "via": "MMCM DRP at 0x44AB_0000; `harness-manager clock`", "board_net": "OSCCLK[1]",
     "cite": ("docs/contracts/partition-timing.md", "clk_out1_shell_bd_clk_wiz_dut_0")},
    {"signal": "dut_resetn", "static": "dut_clkrst: DUT system reset (host register)",
     "via": "`harness-manager reset dut`",
     "cite": ("docs/contracts/partition-pins.md", "reset #1 — DUT system reset (host register)")},
    {"signal": "rp_resetn", "static": "dfx_ctl: reconfiguration reset, held through a swap",
     "via": "every partial load",
     "cite": ("docs/contracts/partition-pins.md", "reset #2 — reconfig reset, held through swap")},
    {"signal": "dbg_resetn", "static": "debug/SRST from the OpenOCD path",
     "via": "OpenOCD reset over TCP 6921",
     "cite": ("docs/contracts/partition-pins.md", "reset #3 — debug/SRST from OpenOCD path")},
    {"signal": "jtag_tck", "static": "jtag_bb CSR bit-banger (JTAGBB at 0x44A7_0000)",
     "via": "OpenOCD remote_bitbang on TCP 6921",
     "cite": ("docs/contracts/net-protocol.md", "| 6921 | TCP | **JTAG** — OpenOCD `remote_bitbang`")},
    {"signal": "jtag_tms", "static": "jtag_bb CSR bit-banger (JTAGBB at 0x44A7_0000)",
     "via": "OpenOCD remote_bitbang on TCP 6921",
     "cite": ("docs/contracts/shell-regmap.md", "| `0x44A7_0000` | JTAGBB |")},
    {"signal": "jtag_tdi", "static": "jtag_bb CSR bit-banger (JTAGBB at 0x44A7_0000)",
     "via": "OpenOCD remote_bitbang on TCP 6921",
     "cite": ("docs/contracts/shell-regmap.md", "| `0x44A7_0000` | JTAGBB |")},
    {"signal": "jtag_tdo", "static": "jtag_bb CSR bit-banger (sampled TDO; decoupler clamps 0)",
     "via": "OpenOCD remote_bitbang on TCP 6921",
     "cite": ("docs/contracts/shell-regmap.md", "| `0x44A7_0000` | JTAGBB |")},
    {"group": "dbgbscan", "static": "debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master",
     "via": "Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs)",
     "cite": ("docs/contracts/net-protocol.md", "| 2542 | TCP | **XVC** — Xilinx Virtual Cable → Debug Bridge (ILA)")},
    {"signal": "phy_rmii_ref_clk", "static": "clk_wiz_shell clk_out2, fixed 50 MHz RMII reference",
     "via": "always running (also the RM debug-hub clock)",
     "cite": ("docs/contracts/partition-timing.md", "clk_out2_shell_bd_clk_wiz_shell_0")},
    {"signal": "phy_rmii_crs_dv", "static": "the shell's in-fabric virtual PHY (eth_mac_test_subsystem_0)",
     "via": "shell lab tools (`harness-manager lab link|macgen|dutrx`)",
     "cite": ("fpga/shell/bd/shell_bd.tcl", "eth_mac_test_subsystem_0")},
    {"signal": "phy_rmii_rxd", "static": "the shell's in-fabric virtual PHY (eth_mac_test_subsystem_0)",
     "via": "shell lab tools (`harness-manager lab macgen`)",
     "cite": ("fpga/shell/bd/shell_bd.tcl", "eth_mac_test_subsystem_0")},
    {"signal": "phy_rmii_txd", "static": "the shell's in-fabric virtual PHY (eth_mac_test_subsystem_0)",
     "via": "shell lab tools (`harness-manager lab dutrx`)",
     "cite": ("fpga/shell/bd/shell_bd.tcl", "eth_mac_test_subsystem_0")},
    {"signal": "phy_rmii_tx_en", "static": "the shell's in-fabric virtual PHY (eth_mac_test_subsystem_0)",
     "via": "shell lab tools (`harness-manager lab dutrx`)",
     "cite": ("fpga/shell/bd/shell_bd.tcl", "eth_mac_test_subsystem_0")},
    {"signal": "mdc", "static": "the virtual PHY's MDIO register model (mdio_phy_model)",
     "via": "-", "cite": ("docs/contracts/partition-pins.md", "MDIO in to DUT (virtual-PHY register model reply)")},
    {"signal": "mdio_o", "static": "the virtual PHY's MDIO register model (mdio_phy_model)",
     "via": "-", "cite": ("docs/contracts/partition-pins.md", "MDIO in to DUT (virtual-PHY register model reply)")},
    {"signal": "mdio_oe", "static": "the virtual PHY's MDIO register model (mdio_phy_model)",
     "via": "-", "cite": ("docs/contracts/partition-pins.md", "MDIO in to DUT (virtual-PHY register model reply)")},
    {"signal": "mdio_i", "static": "the virtual PHY's MDIO register model (mdio_phy_model)",
     "via": "-", "cite": ("docs/contracts/partition-pins.md", "MDIO in to DUT (virtual-PHY register model reply)")},
    {"signal": "uart_tx_tdata", "static": "uart_bridge_0 async FIFO -> MicroBlaze", "via": "console uart0 on TCP 6930",
     "cite": ("docs/contracts/net-protocol.md", "| 6930 | TCP | **UART0** (boot monitor) — raw byte stream")},
    {"signal": "uart_tx_tvalid", "static": "uart_bridge_0 async FIFO -> MicroBlaze", "via": "console uart0 on TCP 6930",
     "cite": ("fpga/shell/bd/shell_bd.tcl", "uart_bridge_0")},
    {"signal": "uart_tx_tready", "static": "uart_bridge_0 async FIFO -> MicroBlaze", "via": "console uart0 on TCP 6930",
     "cite": ("fpga/shell/bd/shell_bd.tcl", "uart_bridge_0")},
    {"signal": "uart_rx_tdata", "static": "uart_bridge_0 async FIFO <- MicroBlaze", "via": "console uart0 on TCP 6930",
     "cite": ("fpga/shell/bd/shell_bd.tcl", "uart_bridge_0")},
    {"signal": "uart_rx_tvalid", "static": "uart_bridge_0 async FIFO <- MicroBlaze", "via": "console uart0 on TCP 6930",
     "cite": ("fpga/shell/bd/shell_bd.tcl", "uart_bridge_0")},
    {"signal": "uart_rx_tready", "static": "uart_bridge_0 async FIFO <- MicroBlaze", "via": "console uart0 on TCP 6930",
     "cite": ("fpga/shell/bd/shell_bd.tcl", "uart_bridge_0")},
    {"signal": "swo", "static": "swo_uart_rx in uart_bridge_0", "via": "console swo on TCP 6932",
     "cite": ("docs/contracts/net-protocol.md", "| 6932 | TCP | **SWO/ITM** trace — raw byte stream")},
    {"signal": "rm_id", "static": "dfx_ctl: DFXCTL.RM_ID (0x44A1_0010), read back after every load",
     "via": "`harness-manager info` (rm_id)",
     "cite": ("docs/contracts/shell-regmap.md", "| 0x10 | `RM_ID` | [31:0] rm_id |")},
    {"signal": "dut_lockup", "static": "dfx_ctl: DFXCTL.RM_STATUS[1] (0x44A1_0014)", "via": "shell diag",
     "cite": ("docs/contracts/shell-regmap.md", "| 0x14 | `RM_STATUS` | [0] rm_id_valid, [1] dut_lockup, [2] dut_eth_irq |")},
    {"signal": "irq_out", "static": "dfx_ctl: DFXCTL.RM_STATUS[2] (0x44A1_0014, 2-FF synced)", "via": "shell diag",
     "cite": ("docs/contracts/shell-regmap.md", "| 0x14 | `RM_STATUS` | [0] rm_id_valid, [1] dut_lockup, [2] dut_eth_irq |")},
    {"signal": "dut_gpio_o", "bits": [7, 0], "static": "board_gpio pads [7:0] -> USER_nLED[7:0] (lit when o & oe)",
     "via": "the user LEDs", "board_net": "USER_nLED[{i}]",
     "cite": ("fpga/shell/shell_top.sv", "assign USER_nLED = ~led_drive;")},
    {"signal": "dut_gpio_oe", "bits": [7, 0], "static": "board_gpio pads [7:0] output enable (LED lit when o & oe)",
     "via": "the user LEDs", "board_net": "USER_nLED[{i}]",
     "cite": ("fpga/shell/shell_top.sv", "wire [7:0] led_drive = board_gpio_pad_o[7:0] & board_gpio_pad_oe[7:0];")},
    {"signal": "dut_gpio_i", "bits": [7, 0], "static": "board_gpio pads [7:0] read back the LED drive (no input path)",
     "via": "-", "cite": ("fpga/shell/shell_top.sv", "assign board_gpio_pad_i = { USER_SW, led_drive };")},
    {"signal": "dut_gpio_i", "bits": [15, 8], "static": "board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches)",
     "via": "the DIP switches", "board_net": "USER_SW[{i-8}]",
     "cite": ("fpga/shell/shell_top.sv", "assign board_gpio_pad_i = { USER_SW, led_drive };")},
    {"signal": "dut_gpio_o", "bits": [15, 8], "static": "clcd_kvm display tunnel: 8080 data PD[7:0] -> CLCD_PD[17:10] while the DUT owns the panel",
     "via": "`harness-manager lab display dut`", "board_net": "CLCD_PD[{i+2}]",
     "cite": ("docs/contracts/dut-display-tunnel.md", "**`dut_gpio_o[15:8] = PD[7:0]`**")},
    {"signal": "dut_gpio_oe", "bits": [15, 8], "static": "clcd_kvm display tunnel: control byte (cs, wr, rs, rd, pd_oe, busy, req, spare)",
     "via": "`harness-manager lab display dut`",
     "cite": ("docs/contracts/dut-display-tunnel.md", "### `dut_gpio_oe[15:8]` — the control/status byte")},
    {"signal": "qspi_sclk", "static": "decoupler clamp (0) -> shell_top IOBUF -> SST26VF064B SCLK",
     "via": "the QSPI flash", "board_net": "QSPI_SCLK",
     "cite": ("fpga/shell/shell_top.sv", "IOBUF u_qspi_sclk_iobuf (.IO(QSPI_SCLK)")},
    {"signal": "qspi_csn", "static": "decoupler clamp (1 = deselected) -> shell_top IOBUF -> SST26VF064B nCS",
     "via": "the QSPI flash", "board_net": "QSPI_nCS",
     "cite": ("fpga/shell/shell_top.sv", "IOBUF u_qspi_ncs_iobuf  (.IO(QSPI_nCS)")},
    {"signal": "qspi_io_o", "static": "decoupler clamp (0) -> shell_top IOBUF -> SST26VF064B IO{i}",
     "via": "the QSPI flash", "board_net": "QSPI_D{i}",
     "cite": ("fpga/shell/shell_top.sv", "IOBUF u_qspi_d0_iobuf   (.IO(QSPI_D0)")},
    {"signal": "qspi_io_oe", "static": "decoupler clamp (0) -> shell_top IOBUF tristate for IO{i}",
     "via": "the QSPI flash", "board_net": "QSPI_D{i}",
     "cite": ("fpga/shell/shell_top.sv", "IOBUF u_qspi_d0_iobuf   (.IO(QSPI_D0)")},
    {"signal": "qspi_io_i", "static": "pad sample of SST26VF064B IO{i} (non-CDC pass-through, not decoupled)",
     "via": "the QSPI flash", "board_net": "QSPI_D{i}",
     "cite": ("fpga/shell/shell_top.sv", "the pad sample driven straight back to the RP")},
]

#: The clocks the shell drives across the boundary (partition-timing.md table).
#: ``declare``: always (every RM OOC XDC declares it), when_timed (when the design times
#: that clock's group, e.g. a MAC on the RMII group), on_request (only when the design
#: asks: jtag_tck is not a clock in the static). ``async_group``: clocks that share a
#: group in set_clock_groups.
BOUNDARY_CLOCKS: list[dict] = [
    {"signal": "dut_clk", "period_ns": 20.0, "waveform": [0.0, 10.0], "drp": True,
     "declare": "always", "async_group": "dut_clk",
     "static_clock": "clk_out1_shell_bd_clk_wiz_dut_0",
     "cite": ("docs/contracts/partition-timing.md", "| `dut_clk` | `clk_out1_shell_bd_clk_wiz_dut_0` | **20.000 ns**")},
    {"signal": "phy_rmii_ref_clk", "period_ns": 20.0, "waveform": [0.0, 10.0], "drp": False,
     "declare": "when_timed", "async_group": "phy_rmii_ref_clk",
     "static_clock": "clk_out2_shell_bd_clk_wiz_shell_0",
     "cite": ("docs/contracts/partition-timing.md", "| `phy_rmii_ref_clk` | `clk_out2_shell_bd_clk_wiz_shell_0` | **20.000 ns**")},
    {"signal": "dbg_bscan_tck", "period_ns": 80.0, "waveform": [0.0, 40.0], "drp": False,
     "declare": "always", "async_group": "dbg_bscan",
     "static_clock": "debug_bridge_0 soft-BSCAN TCK (generated, static BUFGCE)",
     "cite": ("docs/contracts/partition-timing.md", "| `dbg_bscan_tck` |")},
    {"signal": "dbg_bscan_drck", "period_ns": 80.0, "waveform": [0.0, 40.0], "drp": False, "ooc_only": True,
     "declare": "always", "async_group": "dbg_bscan",
     "static_clock": "(gated TCK; no clock object in the static)",
     "cite": ("docs/contracts/partition-timing.md", "| `dbg_bscan_drck` |")},
    {"signal": "jtag_tck", "period_ns": 100.0, "waveform": [0.0, 50.0], "drp": False, "ooc_only": True,
     "declare": "on_request", "async_group": "jtag_tck",
     "static_clock": "(none: firmware bit-banged, kHz-class; an OOC-only clock for SW-DP logic)",
     "cite": ("docs/contracts/partition-timing.md", "| `jtag_tck` | *(none)* |")},
]

#: RP pblock facts for the fielded static.
PBLOCK: list[dict] = [
    {"key": "name", "value": "pblock_rp_dut",
     "cite": ("fpga/dfx/dfx_floorplan.xdc", 'set rp_pblock_name "pblock_rp_dut"')},
    {"key": "rp_instance", "value": "u_rp_dut",
     "cite": ("fpga/dfx/dfx_floorplan.xdc", 'the RP cell is the top-level "u_rp_dut"')},
    {"key": "clock_regions", "value": ["X2Y0", "X3Y0", "X2Y1", "X3Y1"],
     "cite": ("fpga/dfx/dfx_floorplan.xdc", "set rp_regions {X2Y0 X3Y0 X2Y1 X3Y1}")},
    {"key": "slr", "value": "SLR0",
     "cite": ("fpga/dfx/prod_results_2026-07-06-realshell/dryrun_slr.txt", "RP region X3Y1 -> SLR SLR0")},
    {"key": "site_types", "value": ["SLICE", "DSP48E2", "RAMB18", "RAMB36"],
     "cite": ("fpga/dfx/dfx_floorplan.xdc", "foreach site_type {SLICE DSP48E2 RAMB18 RAMB36} {")},
    {"key": "slice_range", "value": "SLICE_X48Y0:SLICE_X95Y119",
     "cite": ("src/linux_soc/hw/build.tcl", "The existing DFX RP pblock (SLICE_X48Y0:SLICE_X95Y119")},
    {"key": "snapping_mode", "value": "ON",
     "cite": ("fpga/dfx/dfx_floorplan.xdc", "set_property SNAPPING_MODE ON [get_pblocks $rp_pblock_name]")},
    {"key": "exclude_placement_contain_routing", "value": True,
     "cite": ("docs/planning/SERVICES_PARTITION.md", "with `EXCLUDE_PLACEMENT=1`, `CONTAIN_ROUTING=1`, `SNAPPING_MODE=ON`")},
    {"key": "capacity", "value": {"LUT": 42824, "FF": 85648, "CLB": 5353},
     "cite": ("docs/planning/SERVICES_PARTITION.md", "Capacity **42,824 LUT / 85,648 FF / 5,353 CLB**")},
    {"key": "bram_tiles", "value": 144,
     "cite": ("fpga/dfx/dfx_floorplan.xdc", "16.5/144 BRAM tiles (11.5%)")},
    {"key": "reference_use", "value": "rm_nanosoc: 7,903 LUT (18.5%), 16.5 BRAM tiles (11.5%)",
     "cite": ("fpga/dfx/dfx_floorplan.xdc", "nanosoc config uses 7,903/42,824")},
    {"key": "io_sites", "value": 0,
     "cite": ("fpga/dfx/dfx_floorplan.xdc", "The RP Pblock is built from SLICE/DSP/BRAM SITE ranges, NOT whole clock")},
    {"key": "no_clock_or_bscan_sites", "value": "no BUFG, BSCAN or IOB site: the RM may hold no clock buffer, BSCANE2 or pad",
     "cite": ("docs/planning/HANDOVER_RM_ILA_OVER_XVC.md", "There is no BUFG or BSCAN site")},
    {"key": "dut_clk_hd_clk_src", "value": "BUFGCE_X2Y24",
     "cite": ("fpga/rp/nanosoc/nanosoc_ooc.xdc", "set_property HD.CLK_SRC BUFGCE_X2Y24 [get_ports -quiet dut_clk]")},
    {"key": "icap", "value": "CONFIG_SITE_X0Y0 (clock region X5Y1, SLR0)",
     "cite": ("fpga/dfx/prod_results_2026-07-06-realshell/dryrun_slr.txt", "CONFIG/ICAP site CONFIG_SITE_X0Y0: clock region X5Y1 SLR SLR0")},
]

#: IO standard -> the VCCO it needs (UltraScale SelectIO, UG571). Inputs of a few
#: standards are VCCO-independent; the generator only uses what the board's own
#: constraint files use, so the table stays small.
IOSTANDARD_VCCO: dict[str, float] = {
    "LVCMOS12": 1.2, "LVCMOS15": 1.5, "LVCMOS18": 1.8, "LVCMOS25": 2.5, "LVCMOS33": 3.3,
    "LVTTL": 3.3, "SSTL12": 1.2, "POD12": 1.2, "SSTL135": 1.35, "SSTL15": 1.5, "SSTL18_I": 1.8,
    "HSTL_I_18": 1.8, "HSTL_I": 1.5, "LVDS": 1.8, "LVDS_25": 2.5, "DIFF_SSTL12": 1.2,
    "DIFF_POD12": 1.2, "DIFF_HSTL_I_18": 1.8,
}
