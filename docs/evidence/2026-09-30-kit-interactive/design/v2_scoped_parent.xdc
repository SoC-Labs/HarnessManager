# v2: v1 plus an explicit PARENT
create_pblock pblock_lfsr
resize_pblock [get_pblocks pblock_lfsr] -add {SLICE_X80Y90:SLICE_X87Y104}
set_property PARENT pblock_rp_dut [get_pblocks pblock_lfsr]
add_cells_to_pblock [get_pblocks pblock_lfsr] [get_cells -hierarchical -filter {IS_PRIMITIVE && REF_NAME != GND && REF_NAME != VCC && NAME !~ *HD_PR_Connection* && NAME !~ *HD_Inserted*}]
