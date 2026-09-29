open_checkpoint /tmpdir/claude-74755/kit-night/home/builds/minimal/out/minimal_routed.dcp
set wp [get_timing_paths -setup -max_paths 20 -nworst 1]
set s [lsort -unique [get_cells -of_objects [get_property ENDPOINT_PIN $wp]]]
set f [lsearch -all -inline -not -glob $s "*/HD_PR_Connection_*"]
puts "KN_REGS [llength $s] after_filter [llength $f] class_of_first [get_property CLASS [lindex $f 0]]"
set p1 [get_timing_paths -quiet -setup -max_paths 1 -nworst 1 -to $s]
set p2 [get_timing_paths -quiet -setup -max_paths 1 -nworst 1 -to $f]
set p3 [get_timing_paths -quiet -hold -max_paths 1 -nworst 1 -from $f]
puts "KN_SLACK_OBJS [get_property SLACK $p1] KN_SLACK_FILTERED [get_property SLACK $p2] KN_HOLD_FROM_FILTERED [get_property SLACK $p3]"
exit 0
