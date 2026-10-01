# The "user at the console" after build_rm.tcl stopped at link in the GUI (-script runs after -source)
puts "A3 after-script start [clock format [clock seconds] -format %T] mode=[expr {[info exists ::rdi::mode] ? $::rdi::mode : "?"}]"
puts "A3 projects={[get_projects -quiet]} current_project={[current_project -quiet]} current_design={[current_design -quiet]} pblocks={[get_pblocks]}"
set rpc [get_cells u_rp_dut]
puts "A3 rp_cell=$rpc HD.RECONFIGURABLE=[get_property HD.RECONFIGURABLE $rpc] REF_NAME=[get_property REF_NAME $rpc] stage=$::stage"
# --- the floorplan loop: a child pblock inside pblock_rp_dut ---
set rmcells [get_cells -hierarchical -filter {NAME =~ u_rp_dut/* && IS_PRIMITIVE && REF_NAME != GND && REF_NAME != VCC && NAME !~ *HD_PR_Connection* && NAME !~ *HD_Inserted*}]
create_pblock pblock_lfsr
resize_pblock [get_pblocks pblock_lfsr] -add {SLICE_X80Y90:SLICE_X87Y104}
set_property PARENT pblock_rp_dut [get_pblocks pblock_lfsr]
add_cells_to_pblock [get_pblocks pblock_lfsr] $rmcells
puts "A3 pblock_lfsr PARENT=[get_property PARENT [get_pblocks pblock_lfsr]] cells=[llength [get_cells -of_objects [get_pblocks pblock_lfsr]]]"
set rc [catch {hm_save_floorplan {/tmpdir/claude-74755/kit-interactive/design/lfsr_floor_floorplan.xdc}} e]; puts "A3 hm_save_floorplan rc=$rc {$e}"
# the same file read back as the next build reads it
delete_pblocks [get_pblocks pblock_lfsr]
puts "A3 after delete: pblocks={[get_pblocks]} pblock_rp_dut cells=[llength [get_cells -quiet -of_objects [get_pblocks pblock_rp_dut]]]"
set rc [catch {read_xdc -cell [get_cells u_rp_dut] {/tmpdir/claude-74755/kit-interactive/design/lfsr_floor_floorplan.xdc}} e]; puts "A3 read_xdc -cell rc=$rc {$e}"
foreach pb [get_pblocks] { puts "A3 pblock $pb PARENT=[get_property PARENT $pb] GRID=[get_property GRID_RANGES $pb] SNAP=[get_property SNAPPING_MODE $pb] cells=[llength [get_cells -quiet -of_objects $pb]]" }
puts "A3 lfsr_q_reg\[0\] in [get_pblocks -of_objects [get_cells {u_rp_dut/lfsr_q_reg[0]}]]"
set s [report_drc -checks [get_drc_checks HDPR*] -return_string]
puts "A3 hdpr violations=[llength [get_drc_violations -quiet]]"
set rc [catch {hm_save_floorplan {/tmpdir/claude-74755/kit-interactive/runs/a3/floorplan_roundtrip.xdc}} e]; puts "A3 round-trip save rc=$rc {$e}"
# --- source again with this design still open, argv unset (the script's own STOP_AFTER=link) ---
puts "A3 re-source: design open, argv unset"
unset ::argv
set t0 [clock seconds]
set rc [catch {source build_rm.tcl} e]
puts "A3 re-source rc=$rc err={$e} took=[expr {[clock seconds] - $t0}]s stage=$::stage"
puts "A3 projects={[get_projects -quiet]} current_project={[current_project -quiet]} current_design={[current_design -quiet]} pblocks={[get_pblocks]}"
puts "A3 after-script end [clock format [clock seconds] -format %T]"
exit
