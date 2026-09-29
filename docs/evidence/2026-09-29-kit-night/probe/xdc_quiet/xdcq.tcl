read_verilog -sv /tmpdir/claude-74755/kit-night/home/builds/minimal/xdc/minimal_wrapper_skeleton.sv
synth_design -top rm_minimal -part xcku115-flvb1760-1-c -mode out_of_context
puts "KN_PHASE original"
read_xdc -mode out_of_context /tmpdir/claude-74755/kit-night/home/builds/minimal/xdc/minimal_ooc.xdc
puts "KN_PHASE quiet"
reset_timing
read_xdc -mode out_of_context /tmpdir/claude-74755/kit-night/probe/xdcq/minimal_ooc_quiet.xdc
puts "KN_CLOCKS [get_clocks]"
puts "KN_DONE"
exit 0
