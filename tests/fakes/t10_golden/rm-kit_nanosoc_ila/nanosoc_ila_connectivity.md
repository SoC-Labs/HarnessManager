# nanosoc_ila: boundary connectivity (static 0xA5856D30)

The partition boundary of static `0xA5856D30`: 54 ports, 260 bits, 24 decoupler interfaces.
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
| `dut_gpio_o[16]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 16 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[0] | AW14 |
| `dut_gpio_o[17]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 17 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[1] | AW13 |
| `dut_gpio_o[18]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 18 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[2] | AW15 |
| `dut_gpio_o[19]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 19 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[3] | AY15 |
| `dut_gpio_o[20]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 20 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[4] | AY13 |
| `dut_gpio_o[21]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 21 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[5] | AY12 |
| `dut_gpio_o[22]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 22 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[6] | BA15 |
| `dut_gpio_o[23]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 23 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[7] | BB14 |
| `dut_gpio_o[24]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 24 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[8] | BA12 |
| `dut_gpio_o[25]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 25 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[9] | BB12 |
| `dut_gpio_o[26]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 26 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[10] | BA14 |
| `dut_gpio_o[27]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 27 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[11] | BA13 |
| `dut_gpio_o[28]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 28 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[12] | BB15 |
| `dut_gpio_o[29]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 29 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[13] | AU12 |
| `dut_gpio_o[30]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 30 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[14] | AV12 |
| `dut_gpio_o[31]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 31 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[15] | AV17 |
| `dut_gpio_o[32]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 32 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[0] | AT17 |
| `dut_gpio_o[33]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 33 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[1] | AU17 |
| `dut_gpio_o[34]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 34 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[2] | AV19 |
| `dut_gpio_o[35]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 35 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[3] | AW19 |
| `dut_gpio_o[36]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 36 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[4] | AW20 |
| `dut_gpio_o[37]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 37 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[5] | BA19 |
| `dut_gpio_o[38]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 38 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[6] | BA18 |
| `dut_gpio_o[39]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 39 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[7] | AY20 |
| `dut_gpio_o[40]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 40 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[8] | BA20 |
| `dut_gpio_o[41]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 41 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[9] | BA17 |
| `dut_gpio_o[42]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 42 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[10] | BB17 |
| `dut_gpio_o[43]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 43 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[11] | BB20 |
| `dut_gpio_o[44]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 44 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[12] | BB19 |
| `dut_gpio_o[45]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 45 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[13] | AW16 |
| `dut_gpio_o[46]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 46 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[14] | AY16 |
| `dut_gpio_o[47]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, drive: lane 47 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[15] | AY18 |
| `dut_gpio_oe[0]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[0] | AU32 |
| `dut_gpio_oe[1]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[1] | AU30 |
| `dut_gpio_oe[2]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[2] | AU31 |
| `dut_gpio_oe[3]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[3] | AR32 |
| `dut_gpio_oe[4]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[4] | AT33 |
| `dut_gpio_oe[5]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[5] | AW30 |
| `dut_gpio_oe[6]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[6] | AW31 |
| `dut_gpio_oe[7]` | gpio | out | yes | false path | 0 | board_gpio pads [7:0] output enable (LED lit when o & oe) | the user LEDs | USER_nLED[7] | AR30 |
| `dut_gpio_oe[15:8]` | gpio | out | yes | false path | 0 | clcd_kvm display tunnel: control byte (cs, wr, rs, rd, pd_oe, busy, req, spare) | `harness-manager lab display dut` | - | - |
| `dut_gpio_oe[16]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 16 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[0] | AW14 |
| `dut_gpio_oe[17]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 17 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[1] | AW13 |
| `dut_gpio_oe[18]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 18 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[2] | AW15 |
| `dut_gpio_oe[19]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 19 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[3] | AY15 |
| `dut_gpio_oe[20]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 20 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[4] | AY13 |
| `dut_gpio_oe[21]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 21 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[5] | AY12 |
| `dut_gpio_oe[22]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 22 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[6] | BA15 |
| `dut_gpio_oe[23]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 23 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[7] | BB14 |
| `dut_gpio_oe[24]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 24 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[8] | BA12 |
| `dut_gpio_oe[25]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 25 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[9] | BB12 |
| `dut_gpio_oe[26]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 26 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[10] | BA14 |
| `dut_gpio_oe[27]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 27 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[11] | BA13 |
| `dut_gpio_oe[28]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 28 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[12] | BB15 |
| `dut_gpio_oe[29]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 29 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[13] | AU12 |
| `dut_gpio_oe[30]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 30 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[14] | AV12 |
| `dut_gpio_oe[31]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 31 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[15] | AV17 |
| `dut_gpio_oe[32]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 32 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[0] | AT17 |
| `dut_gpio_oe[33]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 33 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[1] | AU17 |
| `dut_gpio_oe[34]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 34 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[2] | AV19 |
| `dut_gpio_oe[35]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 35 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[3] | AW19 |
| `dut_gpio_oe[36]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 36 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[4] | AW20 |
| `dut_gpio_oe[37]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 37 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[5] | BA19 |
| `dut_gpio_oe[38]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 38 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[6] | BA18 |
| `dut_gpio_oe[39]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 39 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[7] | AY20 |
| `dut_gpio_oe[40]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 40 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[8] | BA20 |
| `dut_gpio_oe[41]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 41 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[9] | BA17 |
| `dut_gpio_oe[42]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 42 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[10] | BB17 |
| `dut_gpio_oe[43]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 43 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[11] | BB20 |
| `dut_gpio_oe[44]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 44 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[12] | BB19 |
| `dut_gpio_oe[45]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 45 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[13] | AW16 |
| `dut_gpio_oe[46]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 46 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[14] | AY16 |
| `dut_gpio_oe[47]` | gpio | out | yes | false path | 0 | fixed 1:1 lane map, output enable: lane 47 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[15] | AY18 |
| `dut_gpio_i[7:0]` | gpio | in | yes | false path | - | board_gpio pads [7:0] read back the LED drive (no input path) | - | - | - |
| `dut_gpio_i[8]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[0] | BA29 |
| `dut_gpio_i[9]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[1] | BB29 |
| `dut_gpio_i[10]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[2] | BA32 |
| `dut_gpio_i[11]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[3] | BA33 |
| `dut_gpio_i[12]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[4] | BA30 |
| `dut_gpio_i[13]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[5] | BB30 |
| `dut_gpio_i[14]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[6] | AY33 |
| `dut_gpio_i[15]` | gpio | in | yes | false path | - | board_gpio pads [15:8] <- USER_SW[7:0] (DIP switches) | the DIP switches | USER_SW[7] | AY31 |
| `dut_gpio_i[16]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 16 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[0] | AW14 |
| `dut_gpio_i[17]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 17 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[1] | AW13 |
| `dut_gpio_i[18]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 18 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[2] | AW15 |
| `dut_gpio_i[19]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 19 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[3] | AY15 |
| `dut_gpio_i[20]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 20 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[4] | AY13 |
| `dut_gpio_i[21]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 21 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[5] | AY12 |
| `dut_gpio_i[22]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 22 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[6] | BA15 |
| `dut_gpio_i[23]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 23 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[7] | BB14 |
| `dut_gpio_i[24]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 24 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[8] | BA12 |
| `dut_gpio_i[25]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 25 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[9] | BB12 |
| `dut_gpio_i[26]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 26 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[10] | BA14 |
| `dut_gpio_i[27]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 27 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[11] | BA13 |
| `dut_gpio_i[28]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 28 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[12] | BB15 |
| `dut_gpio_i[29]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 29 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[13] | AU12 |
| `dut_gpio_i[30]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 30 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[14] | AV12 |
| `dut_gpio_i[31]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 31 <-> SH0_IO[{i-16}] (gated by SH_HI_EN) | Arduino shield 0 / SH0 Pmod pins | SH0_IO[15] | AV17 |
| `dut_gpio_i[32]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 32 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[0] | AT17 |
| `dut_gpio_i[33]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 33 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[1] | AU17 |
| `dut_gpio_i[34]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 34 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[2] | AV19 |
| `dut_gpio_i[35]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 35 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[3] | AW19 |
| `dut_gpio_i[36]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 36 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[4] | AW20 |
| `dut_gpio_i[37]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 37 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[5] | BA19 |
| `dut_gpio_i[38]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 38 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[6] | BA18 |
| `dut_gpio_i[39]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 39 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[7] | AY20 |
| `dut_gpio_i[40]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 40 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[8] | BA20 |
| `dut_gpio_i[41]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 41 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[9] | BA17 |
| `dut_gpio_i[42]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 42 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[10] | BB17 |
| `dut_gpio_i[43]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 43 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[11] | BB20 |
| `dut_gpio_i[44]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 44 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[12] | BB19 |
| `dut_gpio_i[45]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 45 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[13] | AW16 |
| `dut_gpio_i[46]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 46 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[14] | AY16 |
| `dut_gpio_i[47]` | gpio | in | yes | false path | - | fixed 1:1 lane map, pad sample: lane 47 <-> SH1_IO[{i-32}] (gated by SH_HI_EN) | Arduino shield 1 / SH1 Pmod pins | SH1_IO[15] | AY18 |
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
| `hostio_ioreq1_o` | hostio | out | tied off | false path | 0 | hostio_bridge_0 (AXI4-Lite 0x44BD_0000) hostio4 target: IOREQ1 from the RM | hostio4 link (`harness-manager` hostio mode) | - | - |
| `hostio_ioreq2_o` | hostio | out | tied off | false path | 0 | hostio_bridge_0 (AXI4-Lite 0x44BD_0000) hostio4 target: IOREQ2 from the RM | hostio4 link (`harness-manager` hostio mode) | - | - |
| `hostio_iodata_o[3:0]` | hostio | out | tied off | false path | 0 | hostio_bridge_0 (AXI4-Lite 0x44BD_0000) hostio4 target: IODATA drive from the RM | hostio4 link (`harness-manager` hostio mode) | - | - |
| `hostio_iodata_oe_o[3:0]` | hostio | out | tied off | false path | 0 | hostio_bridge_0 (AXI4-Lite 0x44BD_0000) hostio4 target: IODATA output enable from the RM | hostio4 link (`harness-manager` hostio mode) | - | - |
| `hostio_ioack_i` | hostio | in | tied off | false path | - | hostio_bridge_0 (AXI4-Lite 0x44BD_0000) hostio4 target: IOACK to the RM | hostio4 link (`harness-manager` hostio mode) | - | - |
| `hostio_iodata_i[3:0]` | hostio | in | tied off | false path | - | hostio_bridge_0 (AXI4-Lite 0x44BD_0000) hostio4 target: IODATA sampled to the RM | hostio4 link (`harness-manager` hostio mode) | - | - |
| `hostio_mode_i` | hostio | in | tied off | false path | - | hostio_bridge_0 (AXI4-Lite 0x44BD_0000) hostio4 target: HOSTIO.CTRL.MODE, latched while rp_resetn is low | hostio4 link (`harness-manager` hostio mode) | - | - |

## Sources

- docs/contracts/dut-display-tunnel.md:68 @ 89a8dcdf
- docs/contracts/dut-display-tunnel.md:72 @ 89a8dcdf
- docs/contracts/net-protocol.md:606 @ 89a8dcdf
- docs/contracts/net-protocol.md:608 @ 89a8dcdf
- docs/contracts/net-protocol.md:609 @ 89a8dcdf
- docs/contracts/net-protocol.md:612 @ 89a8dcdf
- docs/contracts/partition-pins.md:124 @ 89a8dcdf
- docs/contracts/partition-pins.md:171 @ 89a8dcdf
- docs/contracts/partition-pins.md:211 @ 89a8dcdf
- docs/contracts/partition-pins.md:55 @ 89a8dcdf
- docs/contracts/partition-pins.md:56 @ 89a8dcdf
- docs/contracts/partition-pins.md:57 @ 89a8dcdf
- docs/contracts/partition-timing.md:82 @ 89a8dcdf
- docs/contracts/partition-timing.md:83 @ 89a8dcdf
- docs/contracts/shell-regmap.md:200 @ 89a8dcdf
- docs/contracts/shell-regmap.md:294 @ 89a8dcdf
- docs/contracts/shell-regmap.md:295 @ 89a8dcdf
- fpga/shell/bd/shell_bd.tcl:113 @ 89a8dcdf
- fpga/shell/bd/shell_bd.tcl:318 @ 89a8dcdf
- fpga/shell/shell_top.sv:306 @ 89a8dcdf
- fpga/shell/shell_top.sv:682 @ 89a8dcdf
- fpga/shell/shell_top.sv:683 @ 89a8dcdf
- fpga/shell/shell_top.sv:684 @ 89a8dcdf
- fpga/shell/shell_top.sv:755 @ 89a8dcdf
- fpga/shell/shell_top.sv:756 @ 89a8dcdf
- fpga/shell/shell_top.sv:757 @ 89a8dcdf
