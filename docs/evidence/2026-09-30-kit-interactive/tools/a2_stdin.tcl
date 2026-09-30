puts "A2_PROBE start [clock format [clock seconds] -format %T]"
puts "A2 at start: argv exists=[info exists argv] argv={[expr {[info exists argv] ? $argv : "<unset>"}]} argc=[expr {[info exists argc] ? $argc : "<unset>"}] current_project={[current_project -quiet]} current_design={[current_design -quiet]}"
cd {/tmpdir/claude-74755/kit-interactive/home/builds/lfsr_a2}
puts "A2 step1: source build_rm.tcl with argv as Vivado left it (never set by the user); baked STOP_AFTER=link"
set rc [catch {source build_rm.tcl} e]
puts "A2 step1 rc=$rc err={$e} P(STOP_AFTER)=$P(STOP_AFTER) stage=$::stage"
puts "A2 step1 current_project={[current_project -quiet]} current_design={[current_design -quiet]} pblocks={[get_pblocks]}"
set rpc [get_cells u_rp_dut]
set leaf [get_cells -hierarchical -filter {NAME =~ u_rp_dut/* && IS_PRIMITIVE}]
puts "A2 leaf=[llength $leaf] refs=[lsort -unique [get_property REF_NAME $leaf]]"
puts "A2 hd_pr=[get_cells -quiet -hierarchical -filter {NAME =~ u_rp_dut/HD_PR_Connection*}]"

# ---- A4a: the floorplan, in the linked design this session holds ----
set rmcells [get_cells -hierarchical -filter {NAME =~ u_rp_dut/* && IS_PRIMITIVE && REF_NAME != GND && REF_NAME != VCC && NAME !~ *HD_PR_Connection*}]
puts "A4a rm cells to place: [llength $rmcells] refs=[lsort -unique [get_property REF_NAME $rmcells]]"
set rc [catch {create_pblock pblock_lfsr} e]; puts "A4a create_pblock rc=$rc {$e}"
set rc [catch {resize_pblock [get_pblocks pblock_lfsr] -add {SLICE_X80Y90:SLICE_X87Y104}} e]; puts "A4a resize_pblock rc=$rc {$e}"
set rc [catch {set_property PARENT pblock_rp_dut [get_pblocks pblock_lfsr]} e]; puts "A4a set PARENT rc=$rc {$e}"
set rc [catch {add_cells_to_pblock [get_pblocks pblock_lfsr] $rmcells} e]; puts "A4a add_cells_to_pblock rc=$rc {$e}"
set pb [get_pblocks pblock_lfsr]
puts "A4a pblock_lfsr PARENT=[get_property PARENT $pb] GRID_RANGES=[get_property GRID_RANGES $pb] cells=[llength [get_cells -of_objects $pb]] rp_parent=[get_property PARENT [get_pblocks pblock_rp_dut]]"
puts "A4a pblock_rp_dut cells=[get_cells -of_objects [get_pblocks pblock_rp_dut]]"
foreach {f a} [list fp_cell_excl.xdc {-cell u_rp_dut -exclude_timing} fp_cell.xdc {-cell u_rp_dut} fp_full_excl.xdc {-exclude_timing}] {
    set rc [catch {write_xdc {*}$a -force [file join /tmpdir/claude-74755/kit-interactive/runs/a4 $f]} e]
    puts "A4a write_xdc $a -> $f rc=$rc {$e} size=[expr {[file exists /tmpdir/claude-74755/kit-interactive/runs/a4/$f] ? [file size /tmpdir/claude-74755/kit-interactive/runs/a4/$f] : -1}]"
}
puts "A4a ---- fp_cell_excl.xdc ----"
set fh [open /tmpdir/claude-74755/kit-interactive/runs/a4/fp_cell_excl.xdc r]; puts [read $fh]; close $fh
puts "A4a ---- end ----"
# re-read test in the same design: delete the child, read the written file -cell, as build_rm.tcl does
set rc [catch {delete_pblocks [get_pblocks pblock_lfsr]} e]; puts "A4a delete_pblocks rc=$rc {$e} pblocks now={[get_pblocks]}"
set rc [catch {read_xdc -cell [get_cells u_rp_dut] /tmpdir/claude-74755/kit-interactive/runs/a4/fp_cell_excl.xdc} e]; puts "A4a reread fp_cell_excl.xdc -cell rc=$rc {$e}"
set pb [get_pblocks -quiet pblock_lfsr]
puts "A4a after reread: pblocks={[get_pblocks]} pblock_lfsr PARENT={[expr {$pb ne "" ? [get_property PARENT $pb] : "-"}]} GRID={[expr {$pb ne "" ? [get_property GRID_RANGES $pb] : "-"}]} cells=[expr {$pb ne "" ? [llength [get_cells -of_objects $pb]] : -1}]"

# ---- A2: re-source with that design still open ----
puts "A2 step2: source build_rm.tcl again, the linked design still open, argv {STOP_AFTER=synth}"
set argv {STOP_AFTER=synth}
set rc [catch {source build_rm.tcl} e]
puts "A2 step2 rc=$rc err={$e} stage=$::stage current_project={[current_project -quiet]} current_design={[current_design -quiet]}"
set fh [open out/lfsr_floor_build.json r]; puts "A2 step2 receipt: [read $fh]"; close $fh
puts "A2 step3: close_project, then source with argv {STOP_AFTER=preflight}"
catch {close_project} e; puts "A2 close_project {$e} current_project={[current_project -quiet]}"
set argv {STOP_AFTER=preflight}
set rc [catch {source build_rm.tcl} e]
puts "A2 step3 rc=$rc err={$e} stage=$::stage"
puts "A2 step4: unset argv, then source"
unset argv
set rc [catch {source build_rm.tcl} e]
puts "A2 step4 rc=$rc err={$e} stage=$::stage"
puts "A2_PROBE end [clock format [clock seconds] -format %T]"
exit
