// tb_lcdm_snoop.sv -- drives lcdm_snoop with an 8080 bus-functional model replaying a
// stimulus file, then dumps GRAM, the register shadow, every dirty-map snapshot and the
// counters for tests/spikes/lcd_mirror_rtl.py to compare against the Python model.
//
// Stimulus: one record per line, three hex fields "C T BB":
//   C  timing class: 0 = harness clcd_0 (SETUP 2 / WR low 4 / WR high 4 / idle 1 cycles,
//      clcd.sv reset TIMING, CLCD_PANEL_FACTS.md 6); 1 = DUT through the KVM tunnel
//      (slower, and uneven: 3 / 6 / 5 / 7)
//   T  0 = index byte (RS=0), 1 = data byte (RS=1), 2 = pad levels (BB[2] CLCD_RST,
//      BB[1] CLCD_BL), 3 = SNAP the dirty map and log it
//   BB the byte
`timescale 1ns / 1ps

module tb_lcdm_snoop;
  logic clk = 0, rst_n = 0;
  always #5 clk = ~clk;                          // 100 MHz

  logic cs_n = 1, wr_n = 1, rs = 0, lcd_rst_n = 0, bl = 0, snap = 0;
  logic [7:0] pd = 8'h00;
  logic [16:0] fb_addr = 0; logic [15:0] fb_rdata;
  logic [7:0] reg_addr = 0; logic [7:0] reg_rdata;
  logic [299:0] dirty_snap;
  logic [31:0] cnt_bytes, cnt_pixels, cnt_changed, cnt_ramwr, cnt_resets, cnt_anom;

  lcdm_snoop dut (.*);

  task automatic cycles(input int n); repeat (n) @(posedge clk); endtask

  task automatic strobe(input logic r, input logic [7:0] b, input int cls);
    int su, lo, hi, idle;
    if (cls == 0) begin su = 2; lo = 4; hi = 4; idle = 1; end
    else          begin su = 3; lo = 6; hi = 5; idle = 7; end
    cs_n <= 0; rs <= r; pd <= b;  cycles(su);
    wr_n <= 0;                    cycles(lo);
    wr_n <= 1;                    cycles(hi);
    cs_n <= 1; pd <= 8'hA5;       cycles(idle);   // bus garbage while deselected
  endtask

  integer fd, fo, n, c, t, b, k, recs;
  string path, outdir;
  initial begin
    if (!$value$plusargs("stim=%s", path)) $fatal(1, "+stim=<file> required");
    if (!$value$plusargs("out=%s", outdir)) outdir = ".";
    fd = $fopen(path, "r");
    fo = $fopen({outdir, "/snaps.txt"}, "w");
    cycles(4); rst_n <= 1; cycles(4);
    recs = 0;
    while (!$feof(fd)) begin
      n = $fscanf(fd, "%h %h %h\n", c, t, b);
      if (n != 3) continue;
      recs++;
      case (t)
        0, 1: strobe(t[0], b[7:0], c);
        2: begin lcd_rst_n <= b[2]; bl <= b[1]; cycles(20); end
        3: begin
             cycles(4);                          // let the compare/write pipeline drain
             snap <= 1; cycles(1); snap <= 0; cycles(1);
             $fdisplay(fo, "%h", dirty_snap);
           end
      endcase
    end
    $fclose(fd); $fclose(fo);
    cycles(8);
    $writememh({outdir, "/gram.hex"}, dut.gram);
    $writememh({outdir, "/regs.hex"}, dut.regs);
    fo = $fopen({outdir, "/counters.txt"}, "w");
    $fdisplay(fo, "records %0d bytes %0d pixels %0d changed %0d ramwr %0d resets %0d anom %0d sim_ns %0t",
              recs, cnt_bytes, cnt_pixels, cnt_changed, cnt_ramwr, cnt_resets, cnt_anom, $time);
    $fclose(fo);
    // the CPU read port returns what the array holds (1-cycle latency)
    fb_addr <= 17'd1234; cycles(2);
    if (fb_rdata !== dut.gram[1234]) $fatal(1, "fb read port mismatch");
    $finish;
  end
endmodule
