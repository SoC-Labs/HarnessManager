// lcdm_snoop.sv -- LCD-MIRROR spike 3 (feasibility SKETCH, not a deliverable).
//
// A static-shell 8080 bus SNOOPER for the MPS3 HX8347-D panel. It taps the pads AFTER the
// CLCD KVM mux (clcd_kvm.sv's clcd_*_o outputs, which are in s_axi_aclk -- the DUT tunnel is
// already synchronised and stability-filtered inside the KVM), so ONE decoder sees whatever
// the panel sees, whoever owns it. It never drives anything.
//
// It keeps what a mirror needs and nothing else:
//   * GRAM: a physical 240x320 RGB565 copy (the controller's native layout), written through
//     the same address counter + MADCTL (0x16) MY/MX/MV mapping the controller applies, so a
//     picture drawn under any MADCTL lands where the panel scans it out.
//   * REGS: a 256 x 8 shadow of every register index's last datum (0x22 excluded). Software
//     turns GRAM + REGS into the viewer picture (anchor transform, display on/off, standby,
//     and a flag for anything it has not been calibrated for).
//   * DIRTY: one bit per 16x16 GRAM tile, set only when a write CHANGES a pixel (a DUT that
//     repaints every frame then costs nothing when the picture is static). SNAP moves the
//     live map into a readable copy and clears it in the same cycle: no update is lost.
//   * counters: bytes, pixels, changed pixels, RAMWR commands, panel resets, anomalies.
//
// Semantics are those of tests/spikes/lcd_mirror_decoder.py (Hx8347dShadow); the bench
// (tb_lcdm_snoop.sv) proves the two agree bit for bit on the real firmware stream. The
// AXI side is deliberately absent: here the CPU port is a plain synchronous read port.
`timescale 1ns / 1ps

module lcdm_snoop #(
  parameter int GW = 240,               // native GRAM columns (source)
  parameter int GH = 320,               // native GRAM rows (gate)
  parameter int TILE_LOG2 = 4
) (
  input  logic        clk,              // s_axi_aclk, 100 MHz: the KVM's own domain
  input  logic        rst_n,            // shell reset

  // ---- taps: the KVM's pad outputs (clcd_kvm.sv:803-839), read-only ----------------
  input  logic        cs_n,
  input  logic        wr_n,
  input  logic        rs,               // 0 = index, 1 = data
  input  logic [7:0]  pd,
  input  logic        lcd_rst_n,        // CLCD_RST pad (KVM S_RST included)
  input  logic        bl,               // CLCD_BL pad

  // ---- CPU side (an AXI BRAM controller + CSR block in the real thing) --------------
  input  logic [16:0] fb_addr,          // pixel index gy*GW + gx
  output logic [15:0] fb_rdata,         // 1-cycle latency
  input  logic [7:0]  reg_addr,
  output logic [7:0]  reg_rdata,
  input  logic        snap,             // pulse: snapshot + clear the dirty map
  output logic [(GW>>TILE_LOG2)*(GH>>TILE_LOG2)-1:0] dirty_snap,
  output logic [31:0] cnt_bytes, cnt_pixels, cnt_changed, cnt_ramwr, cnt_resets, cnt_anom
);
  localparam int NPIX   = GW * GH;
  localparam int TW     = GW >> TILE_LOG2;           // 15
  localparam int TH     = GH >> TILE_LOG2;           // 20
  localparam int NTILES = TW * TH;                   // 300

  // ---- storage ------------------------------------------------------------------------
  logic [15:0] gram [0:NPIX-1];                      // 38 x RAMB36 as 2K x 18 (inferred TDP)
  logic [7:0]  regs [0:255];                         // distributed RAM (or half a RAMB18)
  logic [NTILES-1:0] dirty_live;
  // BRAM INIT values: a known all-black GRAM at configuration (the model starts there too).
  initial for (int j = 0; j < NPIX; j++) gram[j] = 16'h0000;

  // ---- 1. strobe detect: the panel latches on WR rising with CS low ------------------
  logic cs_n_q, wr_n_q, rs_q, lrst_q;
  logic [7:0] pd_q;
  always_ff @(posedge clk) begin
    cs_n_q <= cs_n; wr_n_q <= wr_n; rs_q <= rs; pd_q <= pd; lrst_q <= lcd_rst_n;
  end
  // wr_n was low last cycle, is high now, CS was low while it was low: one byte, and
  // rs_q/pd_q are the values held across the low phase (both sources hold them stable).
  wire strobe = !wr_n_q && wr_n && !cs_n_q;

  // ---- 2. the controller's state ------------------------------------------------------
  logic [7:0]  index_q;
  logic        have_index_q, have_hi_q;
  logic [7:0]  hi_q;
  logic [8:0]  col_q, page_q;                        // logical counters (<= 319)

  wire [8:0] sc = {regs[8'h02][0], regs[8'h03]};
  wire [8:0] ec = {regs[8'h04][0], regs[8'h05]};
  wire [8:0] sp = {regs[8'h06][0], regs[8'h07]};
  wire [8:0] ep = {regs[8'h08][0], regs[8'h09]};
  wire [7:0] madctl = regs[8'h16];
  wire [7:0] colmod = regs[8'h17];

  // logical (col, page) -> physical (gx, gy): MV exchanges, then MX/MY mirror the physical
  // axes (the convention the decoder model assumes; calibrated on the board, see design).
  function automatic logic [17:0] phys(input logic [8:0] c, input logic [8:0] p, input logic [7:0] m);
    logic [8:0] gx, gy;
    logic oob;
    gx = m[5] ? p : c;
    gy = m[5] ? c : p;
    oob = (gx >= GW[8:0]) || (gy >= GH[8:0]);
    if (m[6]) gx = GW[8:0] - 9'd1 - gx;
    if (m[7]) gy = GH[8:0] - 9'd1 - gy;
    return {oob, gy, gx[7:0]};                       // {oob, gy[8:0], gx[7:0]}
  endfunction
  wire [17:0] a_now = phys(col_q, page_q, madctl);

  // ---- 3. read-compare-write pipeline (events are >= 11 cycles apart) ----------------
  logic        wr_req_q, cmp_q;
  logic [16:0] wr_addr_q;
  logic [15:0] wr_px_q, old_px;
  logic [8:0]  tile_q;

  // reset of the REGISTER shadow follows the panel pad (the controller resets too); GRAM
  // is left alone (the panel keeps it; every writer repaints after a reset anyway).
  integer i;
  always_ff @(posedge clk) begin
    wr_req_q <= 1'b0;
    if (!rst_n || !lcd_rst_n) begin
      for (i = 0; i < 256; i = i + 1) regs[i] <= 8'h00;
      regs[8'h04] <= 8'h00; regs[8'h05] <= 8'hEF;    // native full window (ASSUMED defaults,
      regs[8'h08] <= 8'h01; regs[8'h09] <= 8'h3F;    //  as the model's _defaults())
      regs[8'h17] <= 8'h06;
      have_index_q <= 1'b0; have_hi_q <= 1'b0; index_q <= 8'h00;
      col_q <= 9'd0; page_q <= 9'd0;
    end else if (strobe) begin
      if (!rs_q) begin                                // ---- index write
        index_q <= pd_q; have_index_q <= 1'b1; have_hi_q <= 1'b0;
        if (pd_q == 8'h22) begin col_q <= sc; page_q <= sp; end
      end else if (have_index_q && index_q != 8'h22) begin   // ---- register datum
        regs[index_q] <= pd_q;
        // a start-register write reloads the address counter with the NEW value
        case (index_q)
          8'h02: col_q  <= {pd_q[0], regs[8'h03]};
          8'h03: col_q  <= {regs[8'h02][0], pd_q};
          8'h06: page_q <= {pd_q[0], regs[8'h07]};
          8'h07: page_q <= {regs[8'h06][0], pd_q};
          default: ;
        endcase
      end else if (have_index_q) begin                // ---- GRAM data (RGB565 only here)
        if (!have_hi_q) begin
          hi_q <= pd_q; have_hi_q <= 1'b1;
        end else begin
          have_hi_q <= 1'b0;
          if (!a_now[17]) begin
            wr_req_q  <= 1'b1;
            wr_addr_q <= {8'd0, a_now[16:8]} * GW[16:0] + {9'd0, a_now[7:0]};  // gy*240+gx (shift-add)
            wr_px_q   <= {hi_q, pd_q};
            tile_q    <= (a_now[16:8] >> TILE_LOG2) * TW[8:0] + {1'b0, a_now[7:0] >> TILE_LOG2};
          end
          // advance: column first, wrap to SC and step the page, wrap to SP
          if (col_q >= ec) begin
            col_q <= sc;
            page_q <= (page_q >= ep) ? sp : page_q + 9'd1;
          end else begin
            col_q <= col_q + 9'd1;
          end
        end
      end
    end
  end

  // stage 2: read the old pixel; stage 3: compare and write only a change
  always_ff @(posedge clk) begin
    old_px <= gram[wr_addr_q];
    cmp_q  <= wr_req_q;
  end
  logic [16:0] w_addr2; logic [15:0] w_px2; logic [8:0] tile2;
  always_ff @(posedge clk) begin
    w_addr2 <= wr_addr_q; w_px2 <= wr_px_q; tile2 <= tile_q;
  end
  wire changed = cmp_q && (old_px != w_px2);
  always_ff @(posedge clk) begin
    if (changed) gram[w_addr2] <= w_px2;
  end

  // ---- dirty map with an atomic snapshot ----------------------------------------------
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      dirty_live <= {NTILES{1'b0}}; dirty_snap <= {NTILES{1'b0}};
    end else begin
      if (snap) begin
        dirty_snap <= dirty_live | (changed ? ({{(NTILES-1){1'b0}}, 1'b1} << tile2) : {NTILES{1'b0}});
        dirty_live <= {NTILES{1'b0}};
      end else if (changed) begin
        dirty_live[tile2] <= 1'b1;
      end
    end
  end

  // ---- counters ------------------------------------------------------------------------
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      cnt_bytes <= 0; cnt_pixels <= 0; cnt_changed <= 0; cnt_ramwr <= 0; cnt_resets <= 0; cnt_anom <= 0;
    end else begin
      if (strobe && lcd_rst_n) cnt_bytes <= cnt_bytes + 1;
      if (strobe && lcd_rst_n && !rs_q && pd_q == 8'h22) cnt_ramwr <= cnt_ramwr + 1;
      if (strobe && lcd_rst_n && rs_q && have_index_q && index_q == 8'h22 && have_hi_q) cnt_pixels <= cnt_pixels + 1;
      if (strobe && lcd_rst_n && !rs_q && have_hi_q) cnt_anom <= cnt_anom + 1;   // half pixel dropped
      if (changed) cnt_changed <= cnt_changed + 1;
      if (lrst_q && !lcd_rst_n) cnt_resets <= cnt_resets + 1;
    end
  end

  // ---- CPU read ports ----------------------------------------------------------------------
  always_ff @(posedge clk) begin
    fb_rdata  <= gram[fb_addr];
    reg_rdata <= regs[reg_addr];
  end
endmodule
