set D /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_floor
set O /tmpdir/claude-74755/kit-interactive/runs/place
open_checkpoint $D/out/lfsr_floor_routed.dcp
puts "PLACE pblocks=[get_pblocks]"
foreach pb [get_pblocks] { puts "PLACE pblock $pb PARENT=[get_property PARENT $pb] GRID=[get_property GRID_RANGES $pb] SNAPPING_MODE=[get_property SNAPPING_MODE $pb] cells=[llength [get_cells -quiet -of_objects $pb]]" }
set pb [get_pblocks u_rp_dut_pblock_lfsr]
set assigned [get_cells -of_objects $pb]
set in 0; set outside {}; set sites {}
foreach c $assigned {
    set loc [get_property LOC $c]
    lappend sites $loc
    if { [regexp {^SLICE_X(\d+)Y(\d+)$} $loc -> x y] && $x >= 80 && $x <= 87 && $y >= 90 && $y <= 104 } { incr in } else { lappend outside "$c=$loc" }
}
puts "PLACE assigned=[llength $assigned] inside SLICE_X80Y90:SLICE_X87Y104=$in outside=[llength $outside] [lrange $outside 0 9]"
puts "PLACE slices used by the child's cells: [lsort -unique $sites]"
# every RM register (not Vivado's inserted ones): where it landed
set regs [get_cells -hierarchical -filter {NAME =~ u_rp_dut/* && IS_SEQUENTIAL && NAME !~ *HD_PR_Connection*}]
set rin 0
set fh [open $O/lfsr_floor_register_sites.txt w]
foreach r [lsort -dictionary $regs] {
    set loc [get_property LOC $r]
    puts $fh "$r $loc [get_property BEL $r] pblock=[get_pblocks -of_objects $r]"
    if { [regexp {^SLICE_X(\d+)Y(\d+)$} $loc -> x y] && $x >= 80 && $x <= 87 && $y >= 90 && $y <= 104 } { incr rin }
}
close $fh
puts "PLACE RM registers=[llength $regs] inside the child's range=$rin"
report_utilization -pblocks $pb -file $O/lfsr_floor_util_child_pblock.rpt
report_utilization -pblocks [get_pblocks pblock_rp_dut] -file $O/lfsr_floor_util_rp_pblock.rpt
set fh [open $O/lfsr_floor_child_cells.txt w]
foreach c [lsort -dictionary $assigned] { puts $fh "$c [get_property REF_NAME $c] [get_property LOC $c] [get_property BEL $c]" }
close $fh
puts "PLACE done"
exit
