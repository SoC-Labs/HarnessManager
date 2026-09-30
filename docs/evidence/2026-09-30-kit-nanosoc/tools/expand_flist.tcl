# Expand nanosoc_m0_soc/pynq/filelist.tcl (READ-ONLY) into an ordered source list,
# with no Vivado: read_verilog / set_property / current_fileset are stubbed.
set ::env(SOCLABS_NANOSOC_SOC_DIR) /home/dam1n19/SoCLabs/nanosoc_m0_soc
set ::env(SOCLABS_NANOSOC_ARCH_TECH_DIR) /home/dam1n19/SoCLabs/nanosoc_m0_soc/nanosoc_arch_tech
set ::env(SOCLABS_NANOSOC_GEN_DIR) /home/dam1n19/SoCLabs/nanosoc_m0_soc/nanosoc_arch_tech/nanosoc_gen
set ::env(ARM_IP_LIBRARY_PATH) /research/AAA/ip_library
set ::env(FPGA_BOOTROM_DIR) /home/dam1n19/SoCLabs/nanosoc_m0_soc/imp/fpga/firmware/stage0
set ::env(SOCLABS_AHB_QSPI_DIR) /home/dam1n19/SoCLabs/ahb_qspi
set ::env(SOCLABS_CORESIGHT_SOC400_TECH_DIR) /home/dam1n19/SoCLabs/nanosoc_m0_soc/nanosoc_arch_tech/rtl/coresight_soc400_tech
proc current_fileset {} { return fs }
proc set_property {k v o} { puts "PROP $k [join $v { }]" }
proc read_verilog {args} {
  set sv 0
  if {[lindex $args 0] eq "-sv"} { set sv 1; set args [lrange $args 1 end] }
  foreach f [lindex $args 0] { puts "SRC [expr {$sv ? "sv" : "v"}] $f" }
}
source /home/dam1n19/SoCLabs/nanosoc_m0_soc/pynq/filelist.tcl
