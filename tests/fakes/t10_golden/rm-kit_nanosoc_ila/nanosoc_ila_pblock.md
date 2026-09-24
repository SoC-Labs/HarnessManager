# nanosoc_ila: the partition's floorplan (static 0x72BB0A36)

The RM is placed and routed inside this pblock by the DFX link. It is fixed by the
static: an RM that does not fit, or needs a site type the pblock lacks, needs a new
static (a re-mint).

| fact | value | source |
|---|---|---|
| name | pblock_rp_dut | fpga/dfx/dfx_floorplan.xdc:28 @ e5436302 |
| rp instance | u_rp_dut | fpga/dfx/dfx_floorplan.xdc:19 @ e5436302 |
| clock regions | X2Y0 X3Y0 X2Y1 X3Y1 | fpga/dfx/dfx_floorplan.xdc:73 @ e5436302 |
| slr | SLR0 | fpga/dfx/prod_results_2026-07-06-realshell/dryrun_slr.txt:6 @ e5436302 |
| site types | SLICE DSP48E2 RAMB18 RAMB36 | fpga/dfx/dfx_floorplan.xdc:74 @ e5436302 |
| slice range | SLICE_X48Y0:SLICE_X95Y119 | src/linux_soc/hw/build.tcl:98 @ e5436302 |
| snapping mode | ON | fpga/dfx/dfx_floorplan.xdc:131 @ e5436302 |
| exclude placement contain routing | True | docs/planning/SERVICES_PARTITION.md:80 @ e5436302 |
| capacity | LUT 42,824, FF 85,648, CLB 5,353 | docs/planning/SERVICES_PARTITION.md:81 @ e5436302 |
| bram tiles | 144 | fpga/dfx/dfx_floorplan.xdc:70 @ e5436302 |
| reference use | rm_nanosoc: 7,903 LUT (18.5%), 16.5 BRAM tiles (11.5%) | fpga/dfx/dfx_floorplan.xdc:69 @ e5436302 |
| io sites | 0 | fpga/dfx/dfx_floorplan.xdc:58 @ e5436302 |
| no clock or bscan sites | no BUFG, BSCAN or IOB site: the RM may hold no clock buffer, BSCANE2 or pad | docs/planning/HANDOVER_RM_ILA_OVER_XVC.md:65 @ e5436302 |
| dut clk hd clk src | BUFGCE_X2Y24 | fpga/rp/nanosoc/nanosoc_ooc.xdc:72 @ e5436302 |
| icap | CONFIG_SITE_X0Y0 (clock region X5Y1, SLR0) | fpga/dfx/prod_results_2026-07-06-realshell/dryrun_slr.txt:8 @ e5436302 |

What this means for an RM:

- no IO sites: every board pin reaches the RM through the shell (see the connectivity sheet);
- no BUFG, MMCM or BSCAN site: generate no clock in the RM; an RM debug hub uses the
  shell's BSCAN legs (group dbgbscan) and runs on phy_rmii_ref_clk;
- the capacity above is the whole pblock; leave routing headroom.
