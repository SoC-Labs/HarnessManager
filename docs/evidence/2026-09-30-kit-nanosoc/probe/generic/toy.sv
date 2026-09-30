module toy #(parameter IMG = "nope.hex") (input logic clk, input logic [3:0] a, output logic [7:0] q);
  logic [7:0] mem [0:15];
  initial $readmemh(IMG, mem);
  always_ff @(posedge clk) q <= mem[a];
endmodule
