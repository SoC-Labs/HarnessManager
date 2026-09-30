puts "A1_PROBE begin [clock format [clock seconds] -format %T]"
puts "A1 current_project=[current_project]"
puts "A1 current_design=[current_design]"
puts "A1 pblocks=[get_pblocks]"
set rpc [get_cells u_rp_dut]
puts "A1 rp_cell=$rpc IS_BLACKBOX=[get_property IS_BLACKBOX $rpc] HD.RECONFIGURABLE=[get_property HD.RECONFIGURABLE $rpc] REF_NAME=[get_property REF_NAME $rpc]"
puts "A1 rp_pblock=[get_pblocks -of_objects $rpc]"
puts "A1 pblock_rp_dut GRID_RANGES=[get_property GRID_RANGES [get_pblocks pblock_rp_dut]]"
puts "A1 pblock_rp_dut PARENT=[get_property PARENT [get_pblocks pblock_rp_dut]] SNAPPING_MODE=[get_property SNAPPING_MODE [get_pblocks pblock_rp_dut]]"
set leaf [get_cells -hierarchical -filter {NAME =~ u_rp_dut/* && IS_PRIMITIVE}]
puts "A1 rm_leaf_cells=[llength $leaf] seq=[llength [filter $leaf IS_SEQUENTIAL]]"
puts "A1 ref_names=[lsort -unique [get_property REF_NAME $leaf]]"
puts "A1 P(STOP_AFTER)=$P(STOP_AFTER) stage=$::stage"
puts "A1 argv=$argv"
puts "A1_PROBE end"
exit
