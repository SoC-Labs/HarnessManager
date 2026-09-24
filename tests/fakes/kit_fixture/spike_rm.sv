// spike_rm.sv -- the "user RTL" for the KIT-GUIDE spike: the XDC kit's wrapper
// skeleton (design spike_rm, groups clkrst+status+gpio) with a counter on the
// shield GPIO. rm_id 0x010080F0 = v1.0, design_id 0x80F0 (an unallocated id).
`timescale 1ns / 1ps

module rm_spike #(parameter int NGPIO = 16) (
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

  logic [31:0] count;
  always_ff @(posedge dut_clk or negedge dut_resetn)
    if (!dut_resetn) count <= '0;
    else             count <= count + 32'd1;

  assign dut_gpio_o  = count[31:16] ^ dut_gpio_i;
  assign dut_gpio_oe = '1;
  assign rm_id       = 32'h0100_80F0;
  assign dut_lockup  = 1'b0;
  assign irq_out     = count[31];

  assign jtag_tdo = 1'b0;
  assign dbg_bscan_tdo = 1'b0;
  assign phy_rmii_txd = '0;
  assign phy_rmii_tx_en = 1'b0;
  assign mdc = 1'b0;
  assign mdio_o = 1'b0;
  assign mdio_oe = 1'b0;
  assign uart_tx_tdata = '0;
  assign uart_tx_tvalid = 1'b0;
  assign uart_rx_tready = 1'b0;
  assign swo = 1'b0;
  assign qspi_sclk = 1'b0;
  assign qspi_csn = 1'b1;
  assign qspi_io_o = '0;
  assign qspi_io_oe = '0;
endmodule
