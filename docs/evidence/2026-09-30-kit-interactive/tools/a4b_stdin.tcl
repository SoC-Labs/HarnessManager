puts "A4B start [clock format [clock seconds] -format %T]"
cd {/tmpdir/claude-74755/kit-interactive/home/builds/lfsr_a2}
set argv {STOP_AFTER=link}
set rc [catch {source build_rm.tcl} e]
puts "A4B link rc=$rc {$e} design=[current_design]"
proc pbstate {tag} {
    foreach pb [get_pblocks] {
        puts "$tag pblock $pb PARENT=[get_property PARENT $pb] GRID=[get_property GRID_RANGES $pb] SNAP=[get_property SNAPPING_MODE $pb] EXCL=[get_property EXCLUDE_PLACEMENT $pb] CONTAIN=[get_property CONTAIN_ROUTING $pb] cells=[llength [get_cells -quiet -of_objects $pb]]"
    }
    set c [get_cells {u_rp_dut/lfsr_q_reg[0]}]
    puts "$tag lfsr_q_reg\[0\] in [get_pblocks -of_objects $c]; count_q_reg\[5\] in [get_pblocks -of_objects [get_cells {u_rp_dut/count_q_reg[5]}]]; rp HD.RECONFIGURABLE=[get_property HD.RECONFIGURABLE [get_cells u_rp_dut]]"
    set v [report_drc -checks [get_drc_checks HDPR*] -return_string]
    set n 0
    foreach viol [get_drc_violations -quiet] { incr n; puts "$tag drc [get_property NAME $viol] [get_property SEVERITY $viol]" }
    puts "$tag hdpr_violations=$n"
}
proc cleanup {} {
    foreach pb [get_pblocks] { if {$pb ne "pblock_rp_dut"} { catch {delete_pblocks $pb} } }
    puts "cleanup: pblocks now [get_pblocks]"
}
pbstate V0
foreach {tag f scoped} [list V1 /tmpdir/claude-74755/kit-interactive/runs/a4/v1_scoped_min.xdc 1 V2 /tmpdir/claude-74755/kit-interactive/runs/a4/v2_scoped_parent.xdc 1 V3 /tmpdir/claude-74755/kit-interactive/runs/a4/fp_cell_excl.xdc 1 V4 /tmpdir/claude-74755/kit-interactive/runs/a4/v4_global_journal.xdc 0 V4c /tmpdir/claude-74755/kit-interactive/runs/a4/v4_global_journal.xdc 1] {
    puts "$tag ==== read [file tail $f] [expr {$scoped ? "-cell u_rp_dut" : "(no -cell)"}]"
    if {$scoped} { set rc [catch {read_xdc -cell [get_cells u_rp_dut] $f} e] } else { set rc [catch {read_xdc $f} e] }
    puts "$tag read rc=$rc {$e}"
    pbstate $tag
    cleanup
}
puts "A4B end [clock format [clock seconds] -format %T]"
exit
