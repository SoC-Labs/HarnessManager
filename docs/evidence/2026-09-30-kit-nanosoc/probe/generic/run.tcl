create_project -in_memory -part xcku115-flvb1760-1-c
read_verilog -sv toy.sv
set_property generic "IMG=[file normalize img.hex]" [current_fileset]
puts "GENERIC_PROP=[get_property generic [current_fileset]]"
synth_design -mode out_of_context -top toy -part xcku115-flvb1760-1-c
puts "TOY_DONE"
