# nanosoc: the partition's floorplan (static 0xA5856D30)

The RM is placed and routed inside this pblock by the DFX link. It is fixed by the
static: an RM that does not fit, or needs a site type the pblock lacks, needs a new
static (a re-mint).

| fact | value | source |
|---|---|---|
| name | pblock_rp_dut | fpga/dfx/dfx_floorplan.xdc:28 @ 89a8dcdf |
| rp instance | u_rp_dut | fpga/dfx/dfx_floorplan.xdc:19 @ 89a8dcdf |
| clock regions | X0Y0 X1Y0 X2Y0 X3Y0 X0Y1 X1Y1 X2Y1 X3Y1 X0Y2 X1Y2 X2Y2 X3Y2 X0Y3 X1Y3 X2Y3 X3Y3 X0Y4 X1Y4 X2Y4 X3Y4 | fpga/dfx/dfx_floorplan.xdc:86 @ 89a8dcdf |
| slr | SLR0 | fpga/dfx/prod_results_2026-07-06-realshell/dryrun_slr.txt:6 @ 89a8dcdf |
| site types | SLICE DSP48E2 RAMB18 RAMB36 | fpga/dfx/dfx_floorplan.xdc:88 @ 89a8dcdf |
| slice range | SLICE_X0Y0:SLICE_X95Y299 | fpga/dfx/dfx_floorplan.xdc:88 @ 89a8dcdf |
| snapping mode | ON | fpga/dfx/dfx_floorplan.xdc:178 @ 89a8dcdf |
| exclude placement contain routing | True | docs/planning/SERVICES_PARTITION.md:80 @ 89a8dcdf |
| capacity | LUT 213,456, FF 426,912, DSP 2,160 | docs/evidence/2026-10-mint4a-shapeA/util_rm_greybox.rpt:81 @ 89a8dcdf |
| bram tiles | 720 | docs/evidence/2026-10-mint4a-shapeA/util_rm_greybox.rpt:152 @ 89a8dcdf |
| reference use | rm_nanosoc: 11,319 LUT (5.30%), 6,353 FF (1.49%), 21.5 BRAM tiles (2.99%), 3 DSP (0.14%), routed, from the mint build's prod/util_rm_nanosoc.rpt (not in git) | fielded/0xA5856D30/mint.json:270 @ 89a8dcdf |
| io sites | 0 | fpga/dfx/dfx_floorplan.xdc:58 @ 89a8dcdf |
| no clock or bscan sites | no BUFG, BSCAN or IOB site: the RM may hold no clock buffer, BSCANE2 or pad | docs/planning/HANDOVER_RM_ILA_OVER_XVC.md:65 @ 89a8dcdf |
| dut clk hd clk src | BUFGCE_X2Y24 | fpga/rp/nanosoc/nanosoc_ooc.xdc:71 @ 89a8dcdf |
| icap | CONFIG_SITE_X0Y0 (clock region X5Y1, SLR0) | fpga/dfx/prod_results_2026-07-06-realshell/dryrun_slr.txt:8 @ 89a8dcdf |
| ramb36 range | RAMB36_X0Y0:RAMB36_X11Y59 | fpga/dfx/dfx_floorplan.xdc:88 @ 89a8dcdf |
| ramb18 range | RAMB18_X0Y0:RAMB18_X11Y119 | fpga/dfx/dfx_floorplan.xdc:88 @ 89a8dcdf |
| dsp48e2 range | DSP48E2_X0Y0:DSP48E2_X17Y119 | fpga/dfx/dfx_floorplan.xdc:88 @ 89a8dcdf |

What this means for an RM:

- no IO sites: every board pin reaches the RM through the shell (see the connectivity sheet);
- no BUFG, MMCM or BSCAN site: generate no clock in the RM; an RM debug hub uses the
  shell's BSCAN legs (group dbgbscan) and runs on phy_rmii_ref_clk;
- the capacity above is the whole pblock; leave routing headroom.
