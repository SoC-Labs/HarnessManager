Build nanosoc (rm_id 0x01000001) for static 0x44EE76D5
kit mps3/0x44EE76D5/vivado-2026.1: Vivado 2026.1 exactly (another major.minor is refused),
part xcku115-flvb1760-1-c, partition u_rp_dut (47 ports / 148 bits).

1. Build (about 20 min for a small RM with 2 threads; 4-8 GB of RAM):
     /research/CAD/Xilinx/Vivado//2026.1/Vivado/bin/vivado -mode batch -source build_rm.tcl -log build_rm.log -journal build_rm.jou
   (/research/CAD/Xilinx/Vivado//2026.1/Vivado/bin/vivado is Vivado 2026.1 on the machine that wrote this; elsewhere use that release's vivado: a bare `vivado` runs whatever is first on PATH)
   Vivado exits 0 even when a gate fails: the verdict is the last HM_RM_BUILD_* line
   and out/nanosoc_build.json. Synth only: -tclargs STOP_AFTER=synth
2. Check the pair, with no board and no Vivado:
     harness-manager kit check out/nanosoc_build.json
3. Add it to Program (writes the overlay manifest from the receipt):
     harness-manager kit pack out/nanosoc_build.json --import

Every step, with its state: harness-manager kit guide --static-id 0x44EE76D5 --design <your design .json> --build-dir .
