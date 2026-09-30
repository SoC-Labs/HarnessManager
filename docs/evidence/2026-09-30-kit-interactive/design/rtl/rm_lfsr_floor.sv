// rm_lfsr_floor.sv -- KIT-INTERACTIVE's floorplan test RM for static 0x44EE76D5.
// The port list is HM's wrapper skeleton (xdc/lfsr_floor_wrapper_skeleton.sv), unchanged.
// A 32-bit Galois LFSR and a 24-bit counter on dut_clk (real flops and LUTs), mixed with
// dut_gpio_i and driven out on dut_gpio_o; unused groups tied as the skeleton ties them.
`timescale 1ns / 1ps

module rm_lfsr_floor #(parameter int NGPIO = 16) (
  input  logic              dut_clk,
  input  logic              dut_resetn,
  input  logic              rp_resetn,
  input  logic              dbg_resetn,
  input  logic              jtag_tck,
  input  logic              jtag_tms,
  input  logic              jtag_tdi,
  output logic              jtag_tdo,
  input  logic              dbg_bscan_bscanid_en,
  input  logic              dbg_bscan_capture,
  input  logic              dbg_bscan_drck,
  input  logic              dbg_bscan_reset,
  input  logic              dbg_bscan_runtest,
  input  logic              dbg_bscan_sel,
  input  logic              dbg_bscan_shift,
  input  logic              dbg_bscan_tck,
  input  logic              dbg_bscan_tdi,
  input  logic              dbg_bscan_tms,
  input  logic              dbg_bscan_update,
  output logic              dbg_bscan_tdo,
  input  logic              phy_rmii_ref_clk,
  input  logic              phy_rmii_crs_dv,
  input  logic [1:0]        phy_rmii_rxd,
  output logic [1:0]        phy_rmii_txd,
  output logic              phy_rmii_tx_en,
  output logic              mdc,
  output logic              mdio_o,
  output logic              mdio_oe,
  input  logic              mdio_i,
  output logic [7:0]        uart_tx_tdata,
  output logic              uart_tx_tvalid,
  input  logic              uart_tx_tready,
  input  logic [7:0]        uart_rx_tdata,
  input  logic              uart_rx_tvalid,
  output logic              uart_rx_tready,
  output logic              swo,
  output logic [31:0]       rm_id,
  output logic              dut_lockup,
  output logic              irq_out,
  output logic [NGPIO-1:0]  dut_gpio_o,
  output logic [NGPIO-1:0]  dut_gpio_oe,
  input  logic [NGPIO-1:0]  dut_gpio_i,
  output logic              qspi_sclk,
  output logic              qspi_csn,
  output logic [3:0]        qspi_io_o,
  output logic [3:0]        qspi_io_oe,
  input  logic [3:0]        qspi_io_i
);

  // --- the logic: an LFSR, a counter, and a registered output stage, all on dut_clk ---
  logic [31:0]      lfsr_q;
  logic [23:0]      count_q;
  logic [NGPIO-1:0] gpio_i_q;
  logic [NGPIO-1:0] gpio_q;
  logic             irq_q;

  always_ff @(posedge dut_clk) begin
    if (!dut_resetn) begin
      lfsr_q   <= 32'hACE1_2468;
      count_q  <= '0;
      gpio_i_q <= '0;
      gpio_q   <= '0;
      irq_q    <= 1'b0;
    end else begin
      // Galois LFSR, taps 32,22,2,1 (x^32 + x^22 + x^2 + x + 1)
      lfsr_q   <= {1'b0, lfsr_q[31:1]} ^ ({32{lfsr_q[0]}} & 32'h8020_0003);
      count_q  <= count_q + 24'd1;
      gpio_i_q <= dut_gpio_i;
      gpio_q   <= lfsr_q[NGPIO-1:0] ^ count_q[23 -: NGPIO] ^ gpio_i_q;
      irq_q    <= &count_q;
    end
  end

  // jtag: not used: tied to the decoupler's safe-idle value
  assign jtag_tdo = 1'b0;
  // dbgbscan: not used: tied to the decoupler's safe-idle value
  assign dbg_bscan_tdo = 1'b0;
  // eth: not used: tied to the decoupler's safe-idle value
  assign phy_rmii_txd = '0;
  assign phy_rmii_tx_en = 1'b0;
  assign mdc = 1'b0;
  assign mdio_o = 1'b0;
  assign mdio_oe = 1'b0;
  // uart: not used: tied to the decoupler's safe-idle value
  assign uart_tx_tdata = '0;
  assign uart_tx_tvalid = 1'b0;
  assign uart_rx_tready = 1'b0;
  assign swo = 1'b0;
  // status: used by this design: drive these
  assign rm_id = 32'h0100B8C6;  // this RM's identity (the overlay manifest's rm_id)
  assign dut_lockup = 1'b0;
  assign irq_out = irq_q;
  // gpio: used by this design: drive these
  assign dut_gpio_o = gpio_q;
  assign dut_gpio_oe = {NGPIO{1'b1}};
  // qspi: not used: tied to the decoupler's safe-idle value
  assign qspi_sclk = 1'b0;
  assign qspi_csn = 1'b1;
  assign qspi_io_o = '0;
  assign qspi_io_oe = '0;

endmodule
