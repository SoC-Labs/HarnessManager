puts "A4C start [clock format [clock seconds] -format %T]"
cd {/tmpdir/claude-74755/kit-interactive/home/builds/lfsr_a2}
set argv {STOP_AFTER=link}
set rc [catch {source build_rm.tcl} e]
puts "A4C link rc=$rc"
proc pbstate {tag} {
    foreach pb [get_pblocks] { puts "$tag pblock $pb PARENT=[get_property PARENT $pb] GRID=[get_property GRID_RANGES $pb] cells=[llength [get_cells -quiet -of_objects $pb]]" }
    puts "$tag lfsr_q_reg\[0\] in {[get_pblocks -of_objects [get_cells {u_rp_dut/lfsr_q_reg[0]}]]}"
    report_drc -checks [get_drc_checks HDPR*] -return_string
    puts "$tag hdpr violations=[llength [get_drc_violations -quiet]]"
}
foreach {tag scoped} {J1 1 J2 0} {
    puts "$tag ==== read j_journal_fullnames.xdc [expr {$scoped ? "-cell u_rp_dut" : "(no -cell)"}]"
    if {$scoped} { set rc [catch {read_xdc -cell [get_cells u_rp_dut] /tmpdir/claude-74755/kit-interactive/runs/a4/j_journal_fullnames.xdc} e] } else { set rc [catch {read_xdc /tmpdir/claude-74755/kit-interactive/runs/a4/j_journal_fullnames.xdc} e] }
    puts "$tag read rc=$rc {$e}"
    pbstate $tag
    foreach pb [get_pblocks] { if {$pb ne "pblock_rp_dut"} { catch {delete_pblocks $pb} } }
    puts "$tag cleanup: pblocks [get_pblocks] pblock_rp_dut cells=[llength [get_cells -quiet -of_objects [get_pblocks pblock_rp_dut]]]"
}
puts "A4C end [clock format [clock seconds] -format %T]"
exit
