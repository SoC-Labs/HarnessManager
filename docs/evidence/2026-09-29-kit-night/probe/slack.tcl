open_checkpoint /tmpdir/claude-74755/kit-night/home/builds/minimal/out/minimal_routed.dcp
source /tmpdir/claude-74755/kit-night/probe/rp_slack_new.tcl
puts "KN_NEW_RP_SLACK [list [rp_slack u_rp_dut]]"
# the lsearch output must still be usable by get_timing_paths: take static flops
set s [lrange [get_cells -quiet -hierarchical -filter {NAME =~ u_shell/* && IS_SEQUENTIAL}] 0 199]
set f [lsearch -all -inline -not -glob $s "*/HD_PR_Connection_*"]
puts "KN_STATIC_REGS [llength $s] after_filter [llength $f]"
set p1 [get_timing_paths -quiet -setup -max_paths 1 -nworst 1 -to $s]
set p2 [get_timing_paths -quiet -setup -max_paths 1 -nworst 1 -to $f]
puts "KN_SLACK_OBJS [get_property SLACK $p1] KN_SLACK_FILTERED [get_property SLACK $p2]"
exit 0
