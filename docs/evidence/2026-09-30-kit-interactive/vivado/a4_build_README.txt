Build lfsr_floor (rm_id 0x0100B8C6) for static 0x44EE76D5
kit mps3/0x44EE76D5/vivado-2026.1: Vivado 2026.1 exactly (another major.minor is refused),
part xcku115-flvb1760-1-c, partition u_rp_dut (47 ports / 148 bits).

1. Build (about 30 min for a small RM on a quiet machine,
   up to an hour when the machine is loaded; nanosoc about 50 min; 4-8 GB of RAM):
     /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode batch -source build_rm.tcl -log build_rm.log -journal build_rm.jou
   (/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado is Vivado 2026.1 on the machine that wrote this; elsewhere use that release's vivado: a bare `vivado` runs whatever is first on PATH)
   Vivado exits 0 even when a gate fails. The verdict is out/lfsr_floor_build.json's state, or
   the last line of build_rm.log that STARTS with HM_RM_BUILD_ (the log also echoes
   this script, whose text holds HM_RM_BUILD_FAILED):
     grep -E '^HM_RM_BUILD_' build_rm.log | tail -1
   Synth only: -tclargs STOP_AFTER=synth
   In your own Vivado (it stays open after the script): -mode gui or -mode tcl, or in
   one already open: cd <this dir>; set argv {STOP_AFTER=link}; source build_rm.tcl
   (`harness-manager kit build . --gui --stop-after link` prints both). After link,
   floorplan inside the partition's pblock and save it with hm_save_floorplan FILE,
   then give FILE as the design's build.rm_xdc.
   Timing: the receipt's rm_wns/rm_whs are your RM's own paths. With no timed path
   inside the partition (minimal) they are empty, and rm_timing_note says so with the
   whole-design WNS/WHS from out/lfsr_floor_timing.rpt (`kit check` prints it).
2. Check the pair, with no board and no Vivado:
     harness-manager kit check out/lfsr_floor_build.json
3. Add it to Program (writes the overlay manifest from the receipt):
     harness-manager kit pack out/lfsr_floor_build.json --import

Every step, with its state: harness-manager kit guide --static-id 0x44EE76D5 --design /tmpdir/claude-74755/kit-interactive/design/lfsr_floor.json --build-dir .
