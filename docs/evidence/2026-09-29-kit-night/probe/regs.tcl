open_checkpoint /tmpdir/claude-74755/kit-night/home/builds/minimal/out/minimal_routed.dcp
foreach c [get_cells -quiet -hierarchical -filter "NAME =~ u_rp_dut/* && IS_SEQUENTIAL"] {
  set clk [get_nets -quiet -of [get_pins -quiet $c/C]]
  set d   [get_nets -quiet -of [get_pins -quiet $c/D]]
  set q   [get_nets -quiet -of [get_pins -quiet $c/Q]]
  set clr [get_nets -quiet -of [get_pins -quiet $c/CLR]]
  puts "KN_REG $c ref=[get_property REF_NAME $c] C=$clk D=$d Q=$q CLR=$clr orig=[get_property -quiet ORIG_REF_NAME [get_cells -quiet [file dirname $c]]]"
}
puts "KN_RP_CELLS [llength [get_cells -quiet -hierarchical -filter {NAME =~ u_rp_dut/*}]]"
puts "KN_RP_PRIMS [lsort -unique [get_property REF_NAME [get_cells -quiet -hierarchical -filter {NAME =~ u_rp_dut/* && IS_PRIMITIVE}]]]"
exit 0
