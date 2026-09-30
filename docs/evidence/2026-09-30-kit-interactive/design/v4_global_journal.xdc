# v4: what the session's journal echoes: full names, read WITHOUT -cell
create_pblock pblock_lfsr
resize_pblock [get_pblocks pblock_lfsr] -add {SLICE_X80Y90:SLICE_X87Y104}
set_property PARENT pblock_rp_dut [get_pblocks pblock_lfsr]
add_cells_to_pblock [get_pblocks pblock_lfsr] [get_cells -hierarchical -filter {NAME =~ u_rp_dut/* && IS_PRIMITIVE && REF_NAME != GND && REF_NAME != VCC && NAME !~ *HD_PR_Connection* && NAME !~ *HD_Inserted*}]
