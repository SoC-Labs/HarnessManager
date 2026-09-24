# nanosoc_ila: boundary connectivity (static 0x72BB0A36)

The partition boundary of static `0x72BB0A36`: 47 ports, 148 bits, 20 decoupler interfaces.
Directions are the RM's. `swap clamp` is what the shell's DFX decoupler holds the
RM's output at during a partial reconfiguration (`-` = not decoupled: the shell drives it).
`reaches` is the block in the static; `board net`/`pin` is where it lands on the board.

Model: DERIVED (harness Lane C's board_pins.yaml does not exist yet).

| signal | group | RM dir | used | timing | swap clamp | reaches in the static | via | board net | pin |
|---|---|---|---|---|---|---|---|---|---|
| `dut_clk` | clkrst | in | yes | clock | - | clk_wiz_dut MMCM clk_out1 (DRP-reconfigurable, default 50 MHz) | MMCM DRP at 0x44AB_0000; `harness-manager clock` | OSCCLK[1] | AK16 |
| `dut_resetn` | clkrst | in | yes | false path | - | dut_clkrst: DUT system reset (host register) | `harness-manager reset dut` | - | - |
| `rp_resetn` | clkrst | in | yes | false path | - | dfx_ctl: reconfiguration reset, held through a swap | every partial load | - | - |
| `dbg_resetn` | clkrst | in | yes | false path | - | debug/SRST from the OpenOCD path | OpenOCD reset over TCP 6921 | - | - |
| `jtag_tck` | jtag | in | yes | clock | - | jtag_bb CSR bit-banger (JTAGBB at 0x44A7_0000) | OpenOCD remote_bitbang on TCP 6921 | - | - |
| `jtag_tms` | jtag | in | yes | false path | - | jtag_bb CSR bit-banger (JTAGBB at 0x44A7_0000) | OpenOCD remote_bitbang on TCP 6921 | - | - |
| `jtag_tdi` | jtag | in | yes | false path | - | jtag_bb CSR bit-banger (JTAGBB at 0x44A7_0000) | OpenOCD remote_bitbang on TCP 6921 | - | - |
| `jtag_tdo` | jtag | out | yes | false path | 0 | jtag_bb CSR bit-banger (sampled TDO; decoupler clamps 0) | OpenOCD remote_bitbang on TCP 6921 | - | - |
| `dbg_bscan_bscanid_en` | dbgbscan | in | yes | false path | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_capture` | dbgbscan | in | yes | false path | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_drck` | dbgbscan | in | yes | clock | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_reset` | dbgbscan | in | yes | false path | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_runtest` | dbgbscan | in | yes | false path | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_sel` | dbgbscan | in | yes | false path | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_shift` | dbgbscan | in | yes | false path | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_tck` | dbgbscan | in | yes | clock | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_tdi` | dbgbscan | in | yes | false path | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_tms` | dbgbscan | in | yes | false path | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_update` | dbgbscan | in | yes | false path | - | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `dbg_bscan_tdo` | dbgbscan | out | yes | false path | 0 | debug_bridge_0 (mode 2, AXI-to-BSCAN) m0_bscan master | Xilinx Virtual Cable on TCP 2542 (an RM with a mode-1 debug_bridge hub + ILAs) | - | - |
| `phy_rmii_ref_clk` | eth | in | tied off | clock | - | clk_wiz_shell clk_out2, fixed 50 MHz RMII reference | always running (also the RM debug-hub clock) | - | - |
| `phy_rmii_crs_dv` | eth | in | tied off | false path | - | the shell's in-fabric virtual PHY (eth_mac_test_subsystem_0) | shell lab tools (`harness-manager lab link|macgen|dutrx`) | - | - |
| `phy_rmii_rxd[1:0]` | eth | in | tied off | false path | - | the shell's in-fabric virtual PHY (eth_mac_test_subsystem_0) | shell lab tools (`harness-manager lab macgen`) | - | - |
| `phy_rmii_txd[1:0]` | eth | out | tied off | false path | 0 | the shell's in-fabric virtual PHY (eth_mac_test_subsystem_0) | shell lab tools (`harness-manager lab dutrx`) | - | - |
| `phy_rmii_tx_en` | eth | out | tied off | false path | 0 | the shell's in-fabric virtual PHY (eth_mac_test_subsystem_0) | shell lab tools (`harness-manager lab dutrx`) | - | - |
| `mdc` | eth | out | tied off | false path | 0 | the virtual PHY's MDIO register model (mdio_phy_model) | - | - | - |
| `mdio_o` | eth | out | tied off | false path | 0 | the virtual PHY's MDIO register model (mdio_phy_model) | - | - | - |
| `mdio_oe` | eth | out | tied off | false path | 0 | the virtual PHY's MDIO register model (mdio_phy_model) | - | - | - |
| `mdio_i` | eth | in | tied off | false path | - | the virtual PHY's MDIO register model (mdio_phy_model) | - | - | - |
| `uart_tx_tdata[7:0]` | uart | out | yes | false path | 0 | uart_bridge_0 async FIFO -> MicroBlaze | console uart0 on TCP 6930 | - | - |
| `uart_tx_tvalid` | uart | out | yes | false path | 0 | uart_bridge_0 async FIFO -> MicroBlaze | console uart0 on TCP 6930 | - | - |
| `uart_tx_tready` | uart | in | yes | false path | - | uart_bridge_0 async FIFO -> MicroBlaze | console uart0 on TCP 6930 | - | - |
| `uart_rx_tdata[7:0]` | uart | in | yes | false path | - | uart_bridge_0 async FIFO <- MicroBlaze | console uart0 on TCP 6930 | - | - |
| `uart_rx_tvalid` | uart | in | yes | false path | - | uart_bridge_0 async FIFO <- MicroBlaze | console uart0 on TCP 6930 | - | - |
| `uart_rx_tready` | uart | out | yes | false path | 0 | uart_bridge_0 async FIFO <- MicroBlaze | console uart0 on TCP 6930 | - | - |
| `swo` | uart | out | yes | false path | 0 | swo_uart_rx in uart_bridge_0 | console swo on TCP 6932 | - | - |
| `rm_id[31:0]` | status | out | yes | false path | 0 | dfx_ctl: DFXCTL.RM_ID (0x44A1_0010), read back after every load | `harness-manager info` (rm_id) | - | - |
| `dut_lockup` | status | out | yes | false path | 0 | dfx_ctl: DFXCTL.RM_STATUS[1] (0x44A1_0014) | shell diag | - | - |
| `irq_out` | status | out | yes | false path | 0 | dfx_ctl: DFXCTL.RM_STATUS[2] (0x44A1_0014, 2-FF synced) | shell diag | - | - |
| `dut_gpio_o[0]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] -> USER_nLED[7:0] (lit when o & oe) | the user LEDs | USER_nLED[0] | AU32 |
| `dut_gpio_o[1]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] -> USER_nLED[7:0] (lit when o & oe) | the user LEDs | USER_nLED[1] | AU30 |
| `dut_gpio_o[2]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] -> USER_nLED[7:0] (lit when o & oe) | the user LEDs | USER_nLED[2] | AU31 |
| `dut_gpio_o[3]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] -> USER_nLED[7:0] (lit when o & oe) | the user LEDs | USER_nLED[3] | AR32 |
| `dut_gpio_o[4]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] -> USER_nLED[7:0] (lit when o & oe) | the user LEDs | USER_nLED[4] | AT33 |
| `dut_gpio_o[5]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] -> USER_nLED[7:0] (lit when o & oe) | the user LEDs | USER_nLED[5] | AW30 |
| `dut_gpio_o[6]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] -> USER_nLED[7:0] (lit when o & oe) | the user LEDs | USER_nLED[6] | AW31 |
| `dut_gpio_o[7]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] -> USER_nLED[7:0] (lit when o & oe) | the user LEDs | USER_nLED[7] | AR30 |
| `dut_gpio_o[8]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: 8080 data PD[7:0] -> CLCD_PD[17:10] while the DUT owns the panel | `harness-manager lab display dut` | CLCD_PD[10] | AN17 |
| `dut_gpio_o[9]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: 8080 data PD[7:0] -> CLCD_PD[17:10] while the DUT owns the panel | `harness-manager lab display dut` | CLCD_PD[11] | AP16 |
| `dut_gpio_o[10]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: 8080 data PD[7:0] -> CLCD_PD[17:10] while the DUT owns the panel | `harness-manager lab display dut` | CLCD_PD[12] | AP18 |
| `dut_gpio_o[11]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: 8080 data PD[7:0] -> CLCD_PD[17:10] while the DUT owns the panel | `harness-manager lab display dut` | CLCD_PD[13] | AR18 |
| `dut_gpio_o[12]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: 8080 data PD[7:0] -> CLCD_PD[17:10] while the DUT owns the panel | `harness-manager lab display dut` | CLCD_PD[14] | AM16 |
| `dut_gpio_o[13]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: 8080 data PD[7:0] -> CLCD_PD[17:10] while the DUT owns the panel | `harness-manager lab display dut` | CLCD_PD[15] | AN16 |
| `dut_gpio_o[14]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: 8080 data PD[7:0] -> CLCD_PD[17:10] while the DUT owns the panel | `harness-manager lab display dut` | CLCD_PD[16] | AR17 |
| `dut_gpio_o[15]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: 8080 data PD[7:0] -> CLCD_PD[17:10] while the DUT owns the panel | `harness-manager lab display dut` | CLCD_PD[17] | AR16 |
| `dut_gpio_oe[0]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[0] | AU32 |
| `dut_gpio_oe[1]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[1] | AU30 |
| `dut_gpio_oe[2]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[2] | AU31 |
| `dut_gpio_oe[3]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[3] | AR32 |
| `dut_gpio_oe[4]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[4] | AT33 |
| `dut_gpio_oe[5]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[5] | AW30 |
| `dut_gpio_oe[6]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[6] | AW31 |
| `dut_gpio_oe[7]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[7] | AR30 |
| `dut_gpio_oe[15:8]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: control byte (cs, wr, rs, rd, pd_oe, busy, req, spare) | `harness-manager lab display dut` | - | - |
| `dut_gpio_i[7:0]` | gpio | in | yes | false path | - | board_gpio pads [7:0] read back the LED drive (no input path) | - | - | - |
| `dut_gpio_i[8]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[0] | BA29 |
| `dut_gpio_i[9]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[1] | BB29 |
| `dut_gpio_i[10]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[2] | BA32 |
| `dut_gpio_i[11]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[3] | BA33 |
| `dut_gpio_i[12]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[4] | BA30 |
| `dut_gpio_i[13]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[5] | BB30 |
| `dut_gpio_i[14]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[6] | AY33 |
| `dut_gpio_i[15]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[7] | AY31 |
| `qspi_sclk` | qspi | out | yes | timed | 0 | decoupler clamp (0) -> shell_top IOBUF -> SST26VF064B SCLK | the QSPI flash | QSPI_SCLK | AT25 |
| `qspi_csn` | qspi | out | yes | timed | 1 | decoupler clamp (1 = deselected) -> shell_top IOBUF -> SST26VF064B nCS | the QSPI flash | QSPI_nCS | AT24 |
| `qspi_io_o[0]` | qspi | out | yes | timed | 0 | decoupler clamp (0) -> shell_top IOBUF -> SST26VF064B IO0 | the QSPI flash | QSPI_D0 | AU24 |
| `qspi_io_o[1]` | qspi | out | yes | timed | 0 | decoupler clamp (0) -> shell_top IOBUF -> SST26VF064B IO1 | the QSPI flash | QSPI_D1 | AV24 |
| `qspi_io_o[2]` | qspi | out | yes | timed | 0 | decoupler clamp (0) -> shell_top IOBUF -> SST26VF064B IO2 | the QSPI flash | QSPI_D2 | AV21 |
| `qspi_io_o[3]` | qspi | out | yes | timed | 0 | decoupler clamp (0) -> shell_top IOBUF -> SST26VF064B IO3 | the QSPI flash | QSPI_D3 | AV22 |
| `qspi_io_oe[0]` | qspi | out | yes | timed | 0 | decoupler clamp (0) -> shell_top IOBUF tristate for IO0 | the QSPI flash | QSPI_D0 | AU24 |
| `qspi_io_oe[1]` | qspi | out | yes | timed | 0 | decoupler clamp (0) -> shell_top IOBUF tristate for IO1 | the QSPI flash | QSPI_D1 | AV24 |
| `qspi_io_oe[2]` | qspi | out | yes | timed | 0 | decoupler clamp (0) -> shell_top IOBUF tristate for IO2 | the QSPI flash | QSPI_D2 | AV21 |
| `qspi_io_oe[3]` | qspi | out | yes | timed | 0 | decoupler clamp (0) -> shell_top IOBUF tristate for IO3 | the QSPI flash | QSPI_D3 | AV22 |
| `qspi_io_i[0]` | qspi | in | yes | timed | - | pad sample of SST26VF064B IO0 (non-CDC pass-through, not decoupled) | the QSPI flash | QSPI_D0 | AU24 |
| `qspi_io_i[1]` | qspi | in | yes | timed | - | pad sample of SST26VF064B IO1 (non-CDC pass-through, not decoupled) | the QSPI flash | QSPI_D1 | AV24 |
| `qspi_io_i[2]` | qspi | in | yes | timed | - | pad sample of SST26VF064B IO2 (non-CDC pass-through, not decoupled) | the QSPI flash | QSPI_D2 | AV21 |
| `qspi_io_i[3]` | qspi | in | yes | timed | - | pad sample of SST26VF064B IO3 (non-CDC pass-through, not decoupled) | the QSPI flash | QSPI_D3 | AV22 |

## Sources

- docs/contracts/dut-display-tunnel.md:68 @ e5436302
- docs/contracts/dut-display-tunnel.md:72 @ e5436302
- docs/contracts/net-protocol.md:270 @ e5436302
- docs/contracts/net-protocol.md:272 @ e5436302
- docs/contracts/net-protocol.md:273 @ e5436302
- docs/contracts/net-protocol.md:275 @ e5436302
- docs/contracts/partition-pins.md:117 @ e5436302
- docs/contracts/partition-pins.md:48 @ e5436302
- docs/contracts/partition-pins.md:49 @ e5436302
- docs/contracts/partition-pins.md:50 @ e5436302
- docs/contracts/partition-timing.md:48 @ e5436302
- docs/contracts/partition-timing.md:49 @ e5436302
- docs/contracts/shell-regmap.md:146 @ e5436302
- docs/contracts/shell-regmap.md:220 @ e5436302
- docs/contracts/shell-regmap.md:221 @ e5436302
- fpga/shell/bd/shell_bd.tcl:243 @ e5436302
- fpga/shell/bd/shell_bd.tcl:77 @ e5436302
- fpga/shell/shell_top.sv:194 @ e5436302
- fpga/shell/shell_top.sv:444 @ e5436302
- fpga/shell/shell_top.sv:445 @ e5436302
- fpga/shell/shell_top.sv:446 @ e5436302
- fpga/shell/shell_top.sv:503 @ e5436302
- fpga/shell/shell_top.sv:504 @ e5436302
- fpga/shell/shell_top.sv:505 @ e5436302
