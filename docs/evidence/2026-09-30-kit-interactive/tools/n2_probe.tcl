# after build_rm.tcl stopped at link: what is clocked, and is the boundary timed?
set tag $::N2TAG
set o /tmpdir/claude-74755/kit-interactive/runs/n2
puts "N2 $tag stage=$::stage clocks=[llength [get_clocks]]"
foreach c [lsort [get_clocks]] { puts "N2 $tag clock $c period=[get_property PERIOD $c] src=[get_property SOURCE_PINS $c]" }
report_timing_summary -check_timing_verbose -max_paths 1 -file $o/${tag}_link_timing.rpt
check_timing -file $o/${tag}_check_timing.rpt
set rp [get_cells u_rp_dut]
set paths [get_timing_paths -quiet -max_paths 10 -nworst 1 -through [get_pins -of_objects $rp]]
puts "N2 $tag boundary_paths=[llength $paths]"
foreach p $paths { puts "N2 $tag path [get_property STARTPOINT_PIN $p] -> [get_property ENDPOINT_PIN $p] clocks [get_property STARTPOINT_CLOCK $p]/[get_property ENDPOINT_CLOCK $p] slack [get_property SLACK $p]" }
report_timing -max_paths 10 -nworst 1 -through [get_pins -of_objects $rp] -file $o/${tag}_boundary.rpt
puts "N2 $tag done"
exit
