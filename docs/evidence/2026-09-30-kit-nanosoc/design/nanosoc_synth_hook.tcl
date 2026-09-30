# nanosoc_synth_hook.tcl -- build.synth_hook of designs/nanosoc/nanosoc.json (KIT-NANOSOC).
# Sourced by HM's build_rm.tcl inside the OOC synth project, after RM_SOURCES are read and
# before synth_design.
#
# WHY IT EXISTS: HM's design document has no `generics`. rp_nanosoc_wrapper's
# IMEM_MEM_FPGA_IMG defaults to an ABSOLUTE path in one workstation's platform checkout
# (the wrapper's own FIXME); the platform's ooc_synth.tcl overrides it with
# `synth_design -generic IMEM_MEM_FPGA_IMG=...` when IMEM_IMG is set. The only place a
# user can do that under HM is here: the fileset's `generic` property, which synth_design
# reads in a non-project in-memory run. $readmemh needs an absolute path (Vivado's cwd is
# not this directory), so it is resolved from this file's own location.
set _img [file normalize [file join [file dirname [info script]] \
    src/platform/fpga/rp/nanosoc/hello_image.hex]]
if { ![file isfile $_img] } { error "nanosoc_synth_hook: no IMEM image at $_img" }
set_property generic "IMEM_MEM_FPGA_IMG=$_img" [current_fileset]
puts "INFO: nanosoc_synth_hook: IMEM_MEM_FPGA_IMG=$_img"
