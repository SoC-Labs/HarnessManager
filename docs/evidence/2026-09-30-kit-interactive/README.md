# KIT-INTERACTIVE: the DUT build in your own Vivado, and nested pblocks (30 Sep 2026)

**Result: all four pass, board-free, on Vivado 2026.1 with the RC2 kit (static 0x44EE76D5).**

| | What | Verdict | Evidence |
|---|---|---|---|
| A1 | `vivado -mode tcl -source build_rm.tcl … -tclargs STOP_AFTER=link` stays at the prompt with the linked design open | **PASS** | `vivado/a1_tcl_mode.txt` |
| A2 | `cd DIR; set argv {…}; source build_rm.tcl` in a running `vivado -mode tcl` | **PASS** (one fix: `argv` unset) | `vivado/a2_sourced_session.txt` |
| A3 | `xvfb-run -a vivado -mode gui -source … -tclargs STOP_AFTER=link`: markers and `HM_RM_BUILD_STOPPED` in `build_rm.log`, clean exit | **PASS** | `vivado/a3_gui_build_rm_log.txt` |
| A4 | Floorplan loop: stop after link, a child pblock inside `pblock_rp_dut`, saved as `rm_xdc`, the full build | **PASS** | `vivado/a4_*`, `design/` |

Three findings matter more than the passes:

1. **Vivado accepts a nested pblock in the DFX partition.** `create_pblock`, `resize_pblock`
   inside `SLICE_X48Y0:SLICE_X95Y119`, `set_property PARENT pblock_rp_dut`, `add_cells_to_pblock`:
   no refusal, no HDPR DRC, `SNAPPING_MODE NESTED`. The full build with it passed all 25 gates (24 PASS + the `ltx` NOTE), `kit check` passed, and all 118 cells assigned to the child (all 89 RM registers among them) are placed inside `SLICE_X80Y90:SLICE_X87Y104`.
2. **`write_xdc -cell u_rp_dut -exclude_timing FILE` is not a usable `rm_xdc`.** It writes the
   partition's own pblock too. Read back with `read_xdc -cell u_rp_dut` (how the build reads
   `rm_xdc`), that becomes a second top-level pblock `u_rp_dut_pblock_rp_dut` over the same area,
   which takes the RM's cells and leaves `pblock_rp_dut` with none (HDPR-141 ×3). The working
   command is **`hm_save_floorplan FILE`**, a proc `build_rm.tcl` now defines (§6). A full build with the `write_xdc` file as `rm_xdc` **fails at `place_design`**: `ERROR: [DRC PLDE-1] Design Exceptions: ERROR: u_rp_dut/GND_HD_Inserted_Inst_dbg_bscan_tdo constrained such that no valid location exists on the device`. The lines the journal echoes (full `u_rp_dut/…` names) do not work either: read `-cell`, the child pblock gets no cells (`CRITICAL WARNING: [Vivado 12-1433] Expecting a non-empty list of cells to be added to the pblock`).
3. **Any `build.rm_xdc` crashed Vivado 2026.1** (segfault, exit 139) right after the link, 2 of 2:
   the template read it with a cell object taken before `read_checkpoint -cell`. No build had set
   `rm_xdc` before. Fixed (§7).
4. **(Added 1 Oct, N2.) The static<->RM boundary was not timed in any external-RM build** (Linux
   v2.0.0 known issue 11): the OOC `create_clock -name dut_clk` rode in the RM checkpoint and at the
   link overwrote the static's OSCCLK1 clock of the same name. Writing the RM checkpoint before the
   OOC XDC fixes it: no_clock 27,984 → 0 for minimal, every gate green, the pair byte-identical (§11).

### The command the UI should print (the lead's `write_xdc` question)

**Save a nested pblock drawn after `STOP_AFTER=link` with:**

```tcl
hm_save_floorplan ~/designs/my_rm_floorplan.xdc
```

then put `"rm_xdc": "my_rm_floorplan.xdc"` in the design's `build` object (a path relative to the
design file) and run `kit script` again. `hm_save_floorplan` is a proc `build_rm.tcl` defines, so
it exists in every session that ran the script (GUI, `-mode tcl`, or sourced). It writes each child
of `pblock_rp_dut` (ranges, `EXCLUDE_PLACEMENT`/`CONTAIN_ROUTING` if set, cells) with names relative
to `u_rp_dut`, the form `read_xdc -cell u_rp_dut` reads. Proven: A3 saved it, A4 built with it (25
gates, every assigned cell inside the child pblock).

**`write_xdc -cell u_rp_dut -exclude_timing <file>` does NOT give a usable `rm_xdc`.** Vivado accepts
the file on the next build's `read_xdc -cell u_rp_dut` (no error at the link, `drc_hdpr_link` PASS),
but it also recreates the partition's own pblock as `u_rp_dut_pblock_rp_dut` (PARENT ROOT), which
takes the RM's cells (HDPR-141 ×3) and then fails `place_design`:
`ERROR: [DRC PLDE-1] Design Exceptions: ERROR: u_rp_dut/GND_HD_Inserted_Inst_dbg_bscan_tdo constrained such that no valid location exists on the device`
(§6, `vivado/v3_write_xdc_build_log.txt`). **Copying the echoed Tcl lines does not work either:**
the journal writes full `u_rp_dut/…` cell names, which match nothing under `read_xdc -cell`; the
pblock is created empty with `CRITICAL WARNING: [Vivado 12-1433] Expecting a non-empty list of cells
to be added to the pblock` (§6, A4c). A user who writes the file by hand must use names relative to
`u_rp_dut` (or a `-filter {NAME =~ …}` query), as `hm_save_floorplan` does.

**HM's XDC checker** (`services/xdc/syntax.py` `check_xdc`) accepts the `hm_save_floorplan` file (a
unit test runs it on the proc's output), and also every other file here, the two that fail
included: it checks the Tcl subset, not `-cell` scoping. Running it on `rm_xdc` at preflight would
pass the nested-pblock file but would not catch the `write_xdc` one.

- **Lane:** KIT-INTERACTIVE. **Branch:** `team/kit-interactive` from `main` `1a127de`. Not pushed.
- **Machine:** srv03335 (RHEL 8.10, 16 cores, 251 GB), shared: 1-minute load 17-72 during the runs.
- **No board, hub, ssh, fpgahub or lease.** No Vivado run bound a port. One Vivado at a time, `nice -n 10`, 4 threads.
  The `make check` gate's virtual-board tests bind free loopback ports in 10000-19999 (§9).

## 1. Inputs

| What | Value |
|---|---|
| Kit | `mps3_rc2_0x44EE76D5_kit.zip` copied from `/tmpdir/claude-74755/kit-night/kit/`; sha256 `95768b64…7e906f` (matches KIT-NIGHT's); kit_id `mps3/0x44EE76D5/vivado-2026.1` |
| Vivado | `/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado`, v2026.1 build 6511674; ENTERPRISE licence from `/etc/profile.d` |
| HM | the worktree venv (`make venv PYVERIFY=`): `1a127de` for A1, A2, A4b and the "before" logs (their `build_rm.tcl` was written by it); the working tree that became `67f4261` for A3 and A4c; `67f4261` for the two crash runs; `89a0202` (the crash fix, §7) for the A4 build, the V3 negative and the new half of the old/new comparison |
| The RM | `design/rtl/rm_lfsr_floor.sv`: HM's wrapper skeleton for the design (every port of the 148-bit boundary) with a 32-bit Galois LFSR, a 24-bit counter and a registered GPIO stage on `dut_clk` (89 flops, 251 leaf cells after link). Design `design/lfsr_floor.json`: groups `clkrst status gpio`, rm_id `0x0100B8C6` (HM's proposal for the name, design id 0xB8C6 in the user range 0x8000-0xFFFF) |
| Floorplan | `design/lfsr_floor_floorplan.xdc`: one child pblock `pblock_lfsr` = `SLICE_X80Y90:SLICE_X87Y104` (8 × 15 slices) holding the RM's 118 LUT/CARRY/FF cells |

## 2. Isolation

Every HM command went through `tools/ki.sh` (KIT-NIGHT's `kn.sh` with this lane's paths):
`env -i HOME=/tmpdir/claude-74755/kit-interactive/home … bash -lc`, with
`HARNESS_MANAGER_STATE_DIR`, `HARNESS_MANAGER_PTY_DIR`, `XDG_*` and `TMPDIR` under
`/tmpdir/claude-74755/kit-interactive/`, the worktree venv first on PATH, and the scratch home's
`~/.bash_profile` sourcing Vivado 2026.1's `settings64.sh`. Before the first step `kit list` said
`no kits cached` and `config path` named only scratch dirs (`logs/s00_isolation.txt`). Vivado ran
through the same wrapper, so its `~/.Xilinx` is the scratch home's. One `kit guide` (s06, 17:26)
ran its `vivado -version` and 17 s launch probe while A2's Vivado ran; every other Vivado process
ran alone.

## 3. The Vivado runs

| Run | Mode, template | Start → end (BST) | Wall | Peak RSS | Load (1 min) | Result |
|---|---|---|---|---|---|---|
| A1 | `-mode tcl -source`, STOP_AFTER=link, probe over stdin; old template (1a127de) | 17:19:55 → 17:25:18 | 5 min 24 s | 3.10 GB | 58 → 47 | stopped after link, design open, exit 0 |
| A2 | `-mode tcl`, `source` ×4 over stdin; old template | 17:25:36 → 17:31:22 | 5 min 47 s | 4.25 GB | 44 → 52 | see §5 |
| probe | `-source s.tcl -script x.tcl`: tcl mode / GUI under xvfb | 17:32 / 17:33 | 27 s / 100 s | | | `-script` runs after `-source`, in the GUI too; its `exit` closes the GUI with exit 0 |
| A4b | `-mode tcl`, link + five `read_xdc` variants; old template | 17:35:41 → 17:40:32 | 4 min 51 s | 3.10 GB | 34 → 18 | §6 |
| A3 | `xvfb-run -a … -mode gui -source … -script a3_after.tcl -tclargs STOP_AFTER=link`; new template | 17:43:54 → 17:52:46 | 8 min 53 s | 6.29 GB (two statics) | 17 → 20 | stopped after link; floorplan saved; re-source with the design open; exit 0 |
| A4 crash 1 | batch, full build, `rm_xdc` = the floorplan; 67f4261 | 17:54:33 → 18:04:34 | 10 min 1 s | 3.09 GB | 20 → 62 | **segfault** after `clocks_after_link` (§7) |
| A4 crash 2 | batch, STOP_AFTER=link, `rm_xdc` = `write_xdc -cell`'s file; 67f4261 | 18:05:40 → 18:14:24 | 8 min 44 s | 3.10 GB | 58 → 42 | **segfault**, same place |
| **A4** | batch, full build, `rm_xdc` = the floorplan; 89a0202 (67f4261 + the fix) | 18:15:08 → 18:49:32 | **34 min 24 s** | 4.90 GB | 42 → 24 | `HM_RM_BUILD_COMPLETE`, 25 gates |
| placement | `-mode tcl`, `open_checkpoint` of the routed DCP, `tools/place_stdin.tcl` | 18:49:59 → 18:51:39 | 1 min 40 s | | 25 | §8 |
| batch old / new | batch, STOP_AFTER=link, `RM_XDC=` (none): the 1a127de template, then this lane's (89a0202) | 18:53:06 → 19:01:32 | 4 min 7 s / 4 min 20 s | 3.18 / 3.10 GB | 17 → 15 | the 19 `HM_` lines and the receipts are **identical** (`vivado/batch_markers_old_vs_new_template.txt`). Later, on request, `HM_STAGE` gained the epoch seconds (§9) |
| V3 negative | batch, full build, `rm_xdc` = `write_xdc -cell`'s file; 89a0202 | 19:01:49 → 19:07:27 | 5 min 38 s | 3.90 GB | 15 → 22 | **FAILED** at `place_design` (DRC PLDE-1), §6 |
| stage seconds | batch, STOP_AFTER=preflight, the template with `HM_STAGE <stage> <seconds>` | 22:06 | 20 s | | 4 | `HM_STAGE preflight 1790802386`, the rest unchanged (`logs/s22_vivado_stage_secs.txt`) |
| A4c | `-mode tcl`, link + the journal's full-name lines read `-cell` and without | 19:08:32 → 19:12:48 | 4 min 16 s | 3.18 GB | 13 → 14 | `-cell`: empty child pblock (Vivado 12-1433); without: 118 cells |

## 4. A1: Tcl mode stays open

`vivado -mode tcl -source DIR/build_rm.tcl -log DIR/build_rm.log -journal DIR/build_rm.jou -tclargs STOP_AFTER=link JOBS=4`,
with `tools/a1_stdin.tcl` on stdin. Vivado sources the script, which ends with
`HM_RM_BUILD_STOPPED after=link` (14 gates, all PASS) and returns to the prompt; then it reads
stdin:

```
A1 current_project=project_static_routed_locked
A1 current_design=checkpoint_static_routed_locked
A1 pblocks=pblock_rp_dut
A1 rp_cell=u_rp_dut IS_BLACKBOX=0 HD.RECONFIGURABLE=1 REF_NAME=rm_lfsr_floor
A1 pblock_rp_dut GRID_RANGES=RAMB36_X6Y0:RAMB36_X11Y23 RAMB18_X6Y0:RAMB18_X11Y47 SLICE_X48Y0:SLICE_X95Y119 DSP48E2_X9Y0:DSP48E2_X17Y47
A1 pblock_rp_dut PARENT=ROOT SNAPPING_MODE=ON
A1 rm_leaf_cells=251 seq=89
A1 P(STOP_AFTER)=link stage=link
```

`exit` then closes it (exit 0). The receipt is `state: stopped, stage: link`
(`vivado/a1_receipt_stopped_link.json`).

## 5. A2: sourced into a running Vivado

`vivado -mode tcl` with no `-tclargs`, then `tools/a2_stdin.tcl`:

| Step | What | What happened |
|---|---|---|
| start | Vivado's own `argv` | `argv exists=1 argv={} argc=0`: Vivado always sets it, `{}` without `-tclargs` |
| 1 | `cd DIR; source build_rm.tcl` with `argv` never set by the user | builds with the script's own values (this dir: `kit script --stop-after link`): stopped after link, the design open |
| 2 | `set argv {STOP_AFTER=synth}; source build_rm.tcl` **with the linked design still open** | runs: `create_project -in_memory` opens a second project beside it, synth, `close_project` closes only that one; the linked design is still current afterwards |
| 3 | `close_project; set argv {STOP_AFTER=preflight}; source …` | runs |
| 4 | `unset argv; source build_rm.tcl` | **fails before any marker**: `can't read "argv": no such variable` |

Step 4 is the one fix: `build_rm.tcl` now reads `argv` only if it exists, so with none it builds
with its own values (a test runs it in Python's Tcl; A3 ran it in Vivado). A design already open
needs no fix and gets no refusal: Vivado 2026.1 opens the build's projects beside it, in
`-mode tcl` (step 2) and in the GUI (A3: `project_static_routed_locked` and `…_2`). Each linked
static holds about 3 GB (A3's peak RSS 6.29 GB with two, A1's 3.10 GB with one), so the docs
say to `close_project` the old one first.

A2 also did the floorplan interactively (A4a in `vivado/a2_sourced_session.txt`):
`create_pblock pblock_lfsr`, `resize_pblock … -add {SLICE_X80Y90:SLICE_X87Y104}`,
`set_property PARENT pblock_rp_dut …`, `add_cells_to_pblock … ` (196 cells): every command
rc 0, `PARENT=pblock_rp_dut`. And it wrote the three `write_xdc` variants of §6.

## 6. A3: the GUI; and the floorplan saved

`xvfb-run -a vivado -mode gui -source DIR/build_rm.tcl -log DIR/build_rm.log -journal DIR/build_rm.jou -script tools/a3_after.tcl -tclargs STOP_AFTER=link`.
`-script` runs after `-source` (the probe above) and plays the user at the console; its last
line, `exit`, closes the GUI (exit 0). The command a user types is the same without `-script`.
In `build_rm.log`:

```
HM_STAGE preflight … HM_STAGE synth … HM_STAGE link
HM_RECEIPT …/lfsr_a2/out/lfsr_floor_build.json
HM_RM_BUILD_STOPPED after=link
A3 after-script start 17:48:54 mode=gui
A3 projects={project_static_routed_locked} … pblocks={pblock_rp_dut}
A3 pblock_lfsr PARENT=pblock_rp_dut cells=118
hm_save_floorplan: 1 pblock(s) inside pblock_rp_dut -> …/design/lfsr_floor_floorplan.xdc (give it as RM_XDC)
A3 read_xdc -cell rc=0 …                      (the saved file read back as the build reads it)
A3 pblock pblock_rp_dut PARENT=ROOT … cells=133
A3 pblock u_rp_dut_pblock_lfsr PARENT=pblock_rp_dut GRID=SLICE_X80Y90:SLICE_X87Y104 SNAP=NESTED cells=118
A3 lfsr_q_reg[0] in u_rp_dut_pblock_lfsr
A3 hdpr violations=0
A3 round-trip save rc=0 …                     (identical to the first file: the name keeps no prefix)
A3 re-source: design open, argv unset
HM_STAGE preflight … HM_RM_BUILD_STOPPED after=link   (215 s)
A3 projects={project_static_routed_locked project_static_routed_locked_2} current_project={project_static_routed_locked_2}
```

### The `rm_xdc` forms, read the way the build reads them (A4b)

After one link, each file was read with `read_xdc -cell u_rp_dut` (V4: without `-cell`), the
pblocks printed, and the result deleted (`vivado/a4b_scoped_read.txt`). **`read_xdc -cell`
prefixes every pblock the file creates with `u_rp_dut_`, and makes it a child of the cell's
pblock.**

| File | What it is | Result |
|---|---|---|
| V1 `design/v1_scoped_min.xdc` | `create_pblock`, `resize_pblock`, `add_cells_to_pblock` with a filter; no PARENT | `u_rp_dut_pblock_lfsr`, PARENT `pblock_rp_dut`, NESTED, 118 cells, 0 HDPR |
| V2 `design/v2_scoped_parent.xdc` | V1 + `set_property PARENT pblock_rp_dut` | the same |
| V3 `design/write_xdc_cell_exclude_timing.xdc` | `write_xdc -cell u_rp_dut -exclude_timing` | **`u_rp_dut_pblock_rp_dut`, PARENT ROOT**, the partition's full ranges, EXCLUDE_PLACEMENT + CONTAIN_ROUTING, 55 cells; `u_rp_dut_pblock_lfsr` its child (196 cells); **`pblock_rp_dut` 0 cells**; `INFO [Constraints 18-12550]`; **HDPR-141 ×3** |
| V4 `design/v4_global_journal.xdc` (no `-cell`) / V4c (with) | full names matched by a filter (`NAME =~ u_rp_dut/*`), explicit PARENT | the child is right (`pblock_lfsr` / `u_rp_dut_pblock_lfsr` under `pblock_rp_dut`, 118 cells); their 78 HDPR-1 errors are V3's leftover: its cleanup had left `u_rp_dut` in no pblock (`pblock_rp_dut cells=0`) |
| `hm_save_floorplan` (A3) | the child pblocks only, names relative to `u_rp_dut` | as V1, 0 HDPR; a second save gives the same file |

`write_xdc -cell u_rp_dut -exclude_timing` writes (`design/write_xdc_cell_exclude_timing.xdc`):
`create_pblock pblock_rp_dut` with the link's GND/VCC/`HD_PR_Connection_Inserted_*` cells, the
partition's ranges, `CONTAIN_ROUTING 1`, `EXCLUDE_PLACEMENT 1`, `SNAPPING_MODE ON`,
`set_property HD.RECONFIGURABLE true [current_design]`, then the child. `-exclude_timing`
drops only the four OOC clock lines (`design/write_xdc_cell.xdc` has them).

**The next build with that file fails.** Same RM, `rm_xdc` = the `write_xdc -cell u_rp_dut
-exclude_timing` file, batch, full build, the crash fixed (`vivado/v3_write_xdc_build_log.txt`,
19:01:49 → 19:07:27, 5 min 38 s). The link passes (`drc_hdpr_link` PASS: HDPR-141 is a warning,
not a blocker), with Vivado now treating the scoped copy as the reconfigurable pblock and the
real one as static (`vivado/v3_write_xdc_drc_hdpr_link.rpt`):

```
HDPR-141#1 Warning  Reconfigurable Pblocks must not overlap static pblocks
The derived ranges of HD.RECONFIGURABLE Pblock 'u_rp_dut_pblock_rp_dut' overlaps with static Pblock 'pblock_rp_dut'. This would reduce the available resources of static pblock 'pblock_rp_dut'. …
```

then `place_design` refuses:

```
ERROR: [DRC PLDE-1] Design Exceptions: ERROR: u_rp_dut/GND_HD_Inserted_Inst_dbg_bscan_tdo constrained such that no valid location exists on the device
ERROR: [Vivado_Tcl 4-23] Error(s) found during DRC. Placer not run.
HM_GATE tcl_error FAIL stage=impl: ERROR: [Common 17-39] 'place_design' failed due to earlier errors.
HM_RM_BUILD_FAILED gate=tcl_error
```

(receipt `vivado/v3_write_xdc_receipt_failed.json`).

**Nor do the lines the journal echoes** (A4c, `vivado/a4c_journal_lines.txt`): a pblock drawn
and assigned by hand is journalled with full names (`add_cells_to_pblock pblock_1 [get_cells
[list {u_rp_dut/lfsr_q_reg[0]} …]]`, `design/j_journal_fullnames.xdc`). Read the way the build
reads `rm_xdc` (`-cell u_rp_dut`), the names do not resolve inside the cell: the child pblock is
created (`u_rp_dut_pblock_1`) and stays empty, with only a CRITICAL WARNING:

```
CRITICAL WARNING: [Vivado 12-1433] Expecting a non-empty list of cells to be added to the pblock.  Please verify the correctness of the <cells> argument. [.../j_journal_fullnames.xdc:6]
J1 pblock u_rp_dut_pblock_1 PARENT=pblock_rp_dut GRID=SLICE_X80Y90:SLICE_X87Y104 cells=0
```

Read without `-cell` the same file works (118 cells, 0 HDPR), but the build reads `rm_xdc` with
`-cell`. So the command the UI should print is **`hm_save_floorplan FILE`**, and the design's
`build.rm_xdc: FILE`.

HM's XDC checker (`services/xdc/syntax.py`, `check_xdc`) accepts all of these files, the
`write_xdc` ones included: it checks the Tcl subset, not what `-cell` scoping does. So running
it on `rm_xdc` at preflight would accept the nested-pblock file, and would not catch V3.

## 7. The crash: `rm_xdc` segfaulted Vivado 2026.1

The first A4 build stopped dead after `HM_GATE clocks_after_link PASS` (`crash/crash_runs.txt`):

```
Abnormal program termination (11)
Please check '…/lfsr_floor/hs_err_pid3860606.log' for details
segfault in …/unwrapped/lnx64.o/vivado -exec vivado -mode batch -source …/build_rm.tcl …
```

`crash/hs_err_run1_full_build.log`: `HDLHStrings::findName` ← `HANUCvNameMap::getName` ←
`HANURenameMgr::getHierarchicalNameAppend` ← `HASCUtils::getXDCName` ← `Tcl_GetString` ←
`task_options_base::parse_options`: Vivado turning a Tcl object into a cell name while it parses
a command's options. The next command in the script is
`read_xdc -cell $rp_cell $P(RM_XDC)`, and `$rp_cell` was taken (`get_cells -quiet $rp`)
**before** `read_checkpoint -cell $rp_cell $synth_dcp` replaced the cell's netlist. A second
build dir with another `rm_xdc` (the V3 file), STOP_AFTER=link, crashed in the same place
(`crash/hs_err_run2_repro_link.log`). The sessions above had read the same kind of file with
`read_xdc -cell [get_cells u_rp_dut]` without a crash. And Vivado warns of exactly this in every
build's log, just before the link (A1's; KIT-NIGHT's scratch logs; KIT-NANOSOC's `vivado/build_rm.log`):

```
WARNING: [Vivado 12-12435] Any Tcl variable pointing to design objects become invalid when read_checkpoint is executed subsequently after open_checkpoint. The content of those variables need to be rebuilt after read_checkpoint. Otherwise this could result in unwanted behavior due to referencing objects that do not exist anymore.
```

**Fix:** `read_xdc -cell [get_cells $rp] $P(RM_XDC)` (`build_rm.tcl.template`, the link stage),
pinned by `test_rm_xdc_is_read_with_the_cell_asked_for_again`. The A4 build below ran with it
and passed the same point. No earlier build had set `rm_xdc` (KIT-NIGHT, KIT-NANOSOC: `RM_XDC {}`),
so the crash never showed.

## 8. A4: the full build with the nested pblock

Command: `kit build ~/builds/lfsr_floor --jobs 4` printed it (`logs/s10_kit_build_full.txt`):
`vivado -mode batch -source …/build_rm.tcl -log …/build_rm.log -journal …/build_rm.jou -tclargs JOBS=4`,
run under `nice -n 10`. The design names `build.rm_xdc: lfsr_floor_floorplan.xdc`, the file
`hm_save_floorplan` wrote in A3, so `RM_XDC` in the generated script is that file.

| | |
|---|---|
| Wall | 18:15:08 → 18:49:32: **34 min 24 s**, 121 % CPU, peak RSS 4.90 GB, load 42 → 24 |
| Per step (elapsed) | synth 1:04 · open_checkpoint 1:16 · read_checkpoint -cell 2:23 · opt 1:10 · place 9:02 · phys_opt 0:33 · route 10:26 · pr_verify 2:24 · open_checkpoint 0:46 · write_bitstream 1:33 |
| `rm_xdc` at link | `Parsing XDC File […/lfsr_floor_floorplan.xdc] for cell 'u_rp_dut'` / `Finished Parsing …`: no warning between |
| Gates | **25: 24 PASS + NOTE `ltx`** (no debug core). Link: `rp_cell`, `rp_pins_link` 148/148, `clocks_after_link` 22, **`drc_hdpr_link` PASS** (no HDPR-16/18/50, no Error; `vivado/a4_lfsr_floor_drc_hdpr_link.rpt`). Impl: `rp_pins_opt`, **`drc_routed` PASS**, **`rm_timing` PASS** (89 registers: setup WNS 18.478 ns, hold WHS 0.056 ns). **`pr_verify` PASS** (`[Vivado 12-3253] … are compatible`: 102 partition pins, 43,911 static tiles, 1,244,073 static routed pips; `vivado/a4_lfsr_floor_pr_verify.rpt`). Bitstream: 4 artefacts, **`clearing_fits` PASS** (71,832 B of 1,048,576 B) |
| Whole design | WNS 0.207 ns, TNS 0, WHS 0.030 ns (the static's, as KIT-NIGHT's; `vivado/a4_lfsr_floor_timing_head.rpt`) |
| The pair | `lfsr_floor_partial.bin` 1,283,428 B, CRC-32 `0x0B6972E6`, sha256 `544ed0bd…cbd9`; `lfsr_floor_partial_clear.bin` 71,832 B, `0x83CDC5F1`, `b74156f5…186a` |
| CRITICAL WARNINGs | 20: 14 from HM's OOC XDC (KIT-NIGHT F3) and 6 from the static (`Timing 38-285`, debug_bridge `tck_i_reg`); none from the floorplan |

**Where the cells went** (`vivado/a4_placement_report.txt`, from the routed DCP):

```
PLACE pblocks=pblock_rp_dut u_rp_dut_pblock_lfsr
PLACE pblock pblock_rp_dut PARENT=ROOT GRID=DSP48E2_X9Y0:DSP48E2_X17Y47 SLICE_X48Y0:SLICE_X95Y119 RAMB36_X6Y0:RAMB36_X11Y23 RAMB18_X6Y0:RAMB18_X11Y47 SNAPPING_MODE=ON cells=168
PLACE pblock u_rp_dut_pblock_lfsr PARENT=pblock_rp_dut GRID=SLICE_X80Y90:SLICE_X87Y104 SNAPPING_MODE=NESTED cells=118
PLACE assigned=118 inside SLICE_X80Y90:SLICE_X87Y104=118 outside=0
PLACE slices used by the child's cells: SLICE_X80Y104 … SLICE_X85Y104 (19 slices, X80-X85, Y99-Y104)
PLACE RM registers=89 inside the child's range=89
```

Every cell and its site: `vivado/a4_place_lfsr_floor_child_cells.txt`; every RM register:
`vivado/a4_place_lfsr_floor_register_sites.txt`; the child pblock's utilization:
`vivado/a4_place_lfsr_floor_util_child_pblock.rpt`. `pblock_rp_dut` itself holds the 168 cells not
assigned to the child (the filter in A3 left out GND/VCC and the `HD_PR_Connection_*` /
`*HD_Inserted*` cells Vivado adds at the link; they were not listed one by one).

**`kit check ~/builds/lfsr_floor`**: rc 0, `the build lfsr_floor: passed (unchecked is not a pass:
see the list)`: 25 ok (build, rm_id, static_id, timing, partial, clearing, the partial's and the
clearing's stream checks, frame box inside the kit's, bit_bin_pair, clearing_fits,
clearing_pairs_partial) and `static_binding` unchecked, as always (`logs/s16_kit_check_a4.txt`;
with `--static-id 0x44EE76D5 --json`: `logs/s16b_kit_check_a4.json`). `kit guide`: steps 1-5 done,
6 next (`logs/s17_…`). `kit pack … --import`: imported into the scratch store (`logs/s18_…`).

## 9. HM before and after (board-free)

| Command | Before (`1a127de`) | After |
|---|---|---|
| `kit check DIR` on a stopped receipt | rc **15**: `the build lfsr_floor failed 1 check (build: the build stopped after link (STOP_AFTER): finish it)` (`logs/s06b_…`) | rc **0**: `the build lfsr_floor: stopped after link (STOP_AFTER=link), not a failure: the 14 gates up to there passed; there is no pair to check or pack until the build runs to the end` / `next: harness-manager kit build DIR --stop-after bitstream` (`logs/s07_…`); `--static-id` of another static still exits 14 |
| `kit guide` on a stopped receipt | `NEXT 5 Build stopped after link (STOP_AFTER): finish the build`, `next:` the batch command with no STOP_AFTER, which stops again when the script was written with `--stop-after link` (`logs/s06_…`) | `NEXT 5 Build lfsr_floor stopped after link (STOP_AFTER=link), not a failure: …`, `next:` the command with `-tclargs STOP_AFTER=bitstream` (`logs/s14_…`) |
| `kit build DIR [--stop-after link] [--gui]` | the batch command only (`logs/s04_…`) | the batch (or, `--gui`, the GUI) command; `cd {DIR}; set argv {…}; source build_rm.tcl`; with link, the floorplan words (`logs/s10_…`, `logs/s15_…`); `--json`: `mode`, `commands`, `source_tcl`, `stays_open` (`logs/s10b_…`) |

**The HM changes** (branch `team/kit-interactive`: `67f4261`, `89a0202`, `cdcafa9`, and the docs
commit), each with a test and its negative twin; every new test fails on `1a127de` except the one
that pins batch's markers, which must pass on both:

| Change | Where | Tests (`tests/unit/test_kit_interactive.py`, `tests/integration/test_kit_interactive_cli.py`) |
|---|---|---|
| `kit build --gui`; the Tcl line for an open Vivado; `--json` `mode`/`commands`/`source_tcl`/`stays_open` | `render.py:131-196` (`MODES`, `tclargs`, `vivado_command(mode=)`, `SOURCE_WHEN`, `stays_open`, `source_tcl`); `cmd_kit.py:142` (flag), `:410-466` (`_build`); `helptext.py:361`; the build README (`script.py:372`) | the gui/tcl commands are the batch one in another mode (twin: a bad mode); tclargs (twin: a bad stage); the line always sets argv (twin: `set argv {}`; a brace refused); the floorplan words name the static's partition; CLI: the line and the floorplan words (twin: none without link), `--gui` first (twin: batch first), JSON has every way (twin: batch, link), a script written to stop at link (twin: `--stop-after bitstream`) |
| `argv` may be unset | `build_rm.tcl.template:114` | a session with no argv builds with the script's own values (twin: an argv still wins) |
| `hm_save_floorplan FILE` | `build_rm.tcl.template:285-329` | writes the children scoped for `read_xdc -cell`, never the partition's pblock, and `check_xdc` takes it (twin: no child is an error that says how); a round trip keeps the name (twin: an unset property is not written) |
| `rm_xdc` read with the cell asked for again | `build_rm.tcl.template:509-512` | the new spelling in the link stage, after `read_checkpoint -cell` (twin: the crashing form is gone) |
| batch unchanged but for the stage seconds | (the three above) | the preflight markers, pinned; in Vivado, §3's old/new comparison |
| `HM_STAGE <stage> <epoch seconds>` (UI2-API-BUILD's CCR) | `build_rm.tcl.template:161-164` | the pin now requires the seconds (it fails on the template without them); every reader takes the stage from the first word (twin: a log without seconds); a running build reads its stage either way (twin: a verdict ends it). HM's readers: `render.parse_markers`, `build.running_build` (`rest.split()[0]`), the guide through it; the web Build section reads the receipt's `stage`, not the log |
| a stopped receipt is not a failure | `build.py:101-121` (`build_dir_of`, `stopped_words`, `finish_hint`); `cmd_kit.py:481-495` (`_check`); `guide.py:421-429` | the words (a NOTE is no pass) and `kit pack` still refusing it; `kit check` rc 0 with `state: stopped` (twins: pack refuses, a failed receipt still 15); another static still 14 (twin: the right one 0); the guide offers `STOP_AFTER=bitstream` (twin: a failed receipt is FAILED, no finish command) |

**The gate.** `nice -n 10 make venv PYVERIFY=`, `.venv/bin/pip install -q --find-links vendor
-e '.[dev,webtest]'`, then the targeted tests (the new files plus every port-free kit test: 338
passed, 2 skipped), `make lint` (pass), and `nice -n 10 make check PYVERIFY=` after the Vivado work:
19:13 → 20:24 on `cdcafa9` (+ the docstring-only `2cdecfd` edit landing during it), **CHECK PASS**,
6218 passed, 20 skipped, 1 deselected, 70 min 31 s at load 14-28 (`logs/make_check_1_summary.txt`).
It includes the virtual-board tests, which bind free loopback ports in 10000-19999. The lead asked
for one more run at low load, to `/tmpdir/claude-74755/kit-interactive-gate.log`: its result is the next paragraph.

Second run, to `/tmpdir/claude-74755/kit-interactive-gate.log` (`logs/make_check_2_summary.txt`), on
`fd6b9bd` (the HM_STAGE seconds): the first attempt was killed at 17 % by the agent harness's 30-min
limit on background commands (not by a test), so it ran again detached, 22:38 → 00:25 (106 min), at
load 30-104 while three other lanes ran their own `make check`. **5 failed, 6215 passed, 20
skipped.** Each failure is a timing assertion, and each passed 3/3 run alone the next day
(`logs/gate2_triage.txt`). None touches the kit code:

| Test | Its assertion under load |
|---|---|
| `test_lm5_display_cli.py::test_ctrl_bracket_ends_the_view_as_in_console[\x1d]` | 1 frame rendered before Ctrl-], 2 expected |
| `test_locate_api.py::test_no_background_contact_ever_sends_a_locate` | "the beat ran": no hello in the window |
| `test_q2_daemon_debris.py::test_negative_twin_without_the_fix_sighup_leaves_daemon_json` | `subprocess.TimeoutExpired` starting the daemon |
| `test_quiet_poll.py::test_hm_never_holds_the_single_client_port_longer_than_one_request` | a hold of 20.6 ms against a 20 ms bound |
| `test_mcc_fix_hub.py::test_the_hub_reader_compiles_and_runs_under_python_36` | the last oscillator read came back None |

Third run, on the tip after N2 (`5b79aa0`), to `/tmpdir/claude-74755/kit-interactive-gate3.log`
(`logs/make_check_3_summary.txt`): 13:45 → 14:51 (65 min 47 s), load 12-34. **3 failed, 6218
passed, 20 skipped.** `test_q2_daemon_debris` (the same `TimeoutExpired` as run 2) and two cases of
`test_debug_6921_drain.py` (`OpenOCD could not connect to the board's JTAG server`). Run alone
(`logs/gate3_triage.txt`): q2 3/3 pass; the reset-retried case 3/3; the held-at-once case 2/3 at
once, then the whole file 6/6 (14 tests each). That file belongs to DEBUG-6921 (`cd9c859`) and this
lane does not touch it or `services/debug.py`: a flaky test of another lane, reported, not fixed.

So the gate is green but for load flakes: run 1 (`cdcafa9`) CHECK PASS, run 2 (`fd6b9bd`) 5 load
flakes that pass 3/3 alone, run 3 (`5b79aa0`) 3 flakes, two of them in another lane's test.

## 11. N2: the OOC clocks no longer reach the link (Linux v2.0.0 known issue 11)

**The bug** (the Linux lead's TRIAL_BUILD T1, HM's N1): the synth stage read the OOC XDC and THEN
wrote the RM checkpoint, so the checkpoint carried `create_clock -name dut_clk [get_ports dut_clk]`.
At the link it came back and, because the static's primary clock on OSCCLK1 is also named
`dut_clk` (`fpga/shell/constraints/mps3_harness.xdc:44`), it overwrote it:

```
WARNING: [Constraints 18-619] A clock with name 'dut_clk' already exists, overwriting the previous clock with the same name. [.../minimal_ooc.xdc:17]
```

OSCCLK1 lost its clock, and with it clk_wiz_shell and clk_wiz_dut: check_timing counted 27,984
register/latch pins with no clock, and the summary still said "All user specified timing
constraints are met".

**The change** (`d574894`, `build_rm.tcl.template` synth stage): `write_checkpoint -force
$synth_dcp` straight after `synth_design`, then `read_xdc $P(RM_OOC_XDC)` for the OOC reports and
gates only. The candidate the lead asked to prove first (the nanoSoC Quickstart team's); candidate
a, `read_xdc -mode out_of_context`, gave the same numbers at the link and is not used.

**Before/after at the link** (Vivado 2026.1, RC2 kit, `-mode tcl`, STOP_AFTER=link, then
`tools/n2_probe.tcl`: `report_timing_summary -check_timing_verbose`, `check_timing`,
`get_timing_paths -through` the RP pins; `n2/`). Batch 00:19:59 → 00:42:47, each run 4-5 min, load 10-30:

| | minimal before | minimal after (b) | minimal (a) | lfsr_floor before | lfsr_floor after (b) |
|---|---|---|---|---|---|
| check_timing no_clock | **27,984** (27,446 OSCCLK1 + 538 debug_bridge tck_i_reg/Q) | **0** | 0 | 27,984 | **0** |
| unconstrained_internal_endpoints | 87,346 | **423** (the constant-clock class) | 423 | 87,379 | 423 |
| clocks after link (`clocks_after_link` gate) | 22: `dut_clk` on `u_rp_dut/dut_clk`, `dbg_bscan_tck`/`drck` on RP pins; no clk_wiz clocks | 23: `dut_clk` on **OSCCLK1**, `clk_out1_shell_bd_clk_wiz_dut_0`, `clk_out1/2_shell_bd_clk_wiz_shell_0` | 23 | 22 | 23 |
| `[Constraints 18-619]` at link | 1 | **0** | 0 | 1 | 0 |
| CRITICAL WARNINGs in build_rm.log | 19 (7 × 18-512, 7 × 18-513 from the OOC XDC re-read at link, 4 × Timing 38-285, 1 × Common 17-69) | **1** (Common 17-69: minimal's unloaded dut_clk, at OOC) | 1 | 18 | **0** |
| boundary paths through the RP pins (10 worst) | 0 (minimal has none) | 0 | 0 | 10, **no launch clock, no slack** (unconstrained) | 10, **timed**: `dut_clkrst_0/.../dut_sync_q_reg[2]/C -> u_rp_dut/gpio_i_q_reg[0]/R`, clk_out1_shell_bd_clk_wiz_dut_0 → itself, **slack 18.670 ns** |
| gates | all PASS | all PASS | all PASS | all PASS | all PASS |

The `HM_` lines are the same in number and order; the one detail that changes is
`clocks_after_link`'s count, 22 → 23 (the static's clocks survive), besides the intended HM_STAGE
seconds. A test pins the new order (`test_the_rm_checkpoint_is_written_before_the_ooc_xdc_is_read`,
fails on `fd6b9bd`).

**The full build with the change** (`minimal`, batch, all stages): **PASS** (`n2/n2_minimal_full_build_log.txt`, receipt
`n2/n2_minimal_full_build.json`, report head `n2/n2_minimal_full_timing_head.rpt`). Batch, 13:11:12 → 13:41:36 on
Thu 1 Oct, **30 min 24 s**, peak RSS 4.91 GB, load 23 → 17 (synth 0:49, open_checkpoint 0:54,
read_checkpoint -cell 1:46, opt 0:59, place 7:24, phys_opt 0:27, route 10:46, pr_verify 2:23,
write_bitstream 1:41).

| | minimal before (old template: timing from the guide lead's clean run, CRITICAL WARNINGs from KIT-NIGHT's run) | minimal after (`d574894`) |
|---|---|---|
| check_timing no_clock | 27,984 | **0** |
| unconstrained_internal_endpoints | 87,115 | **423** |
| Design Timing Summary: WNS / TNS / WHS / THS / WPWS | 0.207 / 0.000 / 0.030 / 0.000 / 0.124 | 0.207 / 0.000 / 0.030 / 0.000 / 0.124 |
| setup endpoints timed (TNS Total Endpoints) | 68,599 | **157,500** |
| hold endpoints timed (THS Total Endpoints) | 67,238 | **156,711** |
| pulse-width endpoints | 28,168 | 56,160 |
| CRITICAL WARNINGs | 21 (KIT-NIGHT F3: 15 from the OOC XDC at link, 6 Timing 38-285) | **1** (Common 17-69, minimal's unloaded `dut_clk` at OOC) |
| gates | 24: 23 PASS + NOTE ltx | **24: 23 PASS + NOTE ltx**, the same sequence as KIT-NIGHT's r2 build; only `clocks_after_link` says 23 (was 22) |
| `kit check --static-id 0x44EE76D5` | passed | **passed** (`logs/s26_kit_check_n2_full.txt`) |
| the pair | `minimal_partial.bin` `86e27c2e…4e81`, clear `810683ab…5ce4` (KIT-NIGHT) | **byte-identical**: the fix changes what is timed, not what is built |

So the timed endpoints more than double, the boundary is analysed, and the slack does not move:
the static was signed off fully timed at the mint, and minimal adds no path of its own.

Not covered: a checkpoint the user brings (`RM_SYNTH_DCP`) still carries whatever constraints were in
it when it was written; the template's comment and the docs say to write it before any
`create_clock`.

## 10. Files here

- `README.md` (this).
- `design/`: the RM (`rtl/rm_lfsr_floor.sv`), `lfsr_floor.json` (the design, with `build.rm_xdc`),
  `lfsr_floor_wxdc.json` (the V3 variant), `lfsr_floor_floorplan.xdc` (what `hm_save_floorplan`
  wrote in A3), the two `write_xdc -cell` outputs, the V1/V2/V4 files of §6 and A4c's
  `j_journal_fullnames.xdc`.
- `vivado/`: trimmed logs of A1, A2, A3, A4b, A4c; A4's receipt, reports (`a4_lfsr_floor_*`), trimmed `build_rm.log`, generated `build_rm.tcl` and README, and the placement report and lists (`a4_placement_report.txt`, `a4_place_*`); the V3 negative's log, receipt and HDPR report (`v3_write_xdc_*`); the old-vs-new batch markers (`batch_markers_old_vs_new_template.txt`); A1's and A3's stopped receipts.
- `crash/`: the two `hs_err_pid*.log` and the two runs' markers.
- `n2/`: §11: the five link probes (before, after, candidate a; minimal and lfsr_floor), their
  check_timing sections and boundary reports, and the full N2 build of minimal.
- `logs/`: every HM command's output (`s00`…), by step.
- `tools/`: `ki.sh` (the isolation wrapper), each run's `run_*.sh`, and the Tcl each run fed
  Vivado (`a1_stdin.tcl`, `a2_stdin.tcl`, `a3_after.tcl`, `a4b_stdin.tcl`, `a4c_stdin.tcl`,
  `place_stdin.tcl`).

No `.dcp`, `.bit` or `.bin` is committed. They are in `/tmpdir/claude-74755/kit-interactive/home/builds/`.
