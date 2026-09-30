# KIT-NANOSOC: the platform's real DUT (nanosoc) through HM's kit flow, on RC2 kit v2 (30 Sep 2026)

**Result: PASS, board-free. The pair is byte-identical to the fielded nanosoc.** The platform's
`rm_nanosoc` (rm_id `0x01000001`) went from its RTL to an overlay that is Programmable for static
`0x44EE76D5`, through `kit script` → `kit build` → Vivado 2026.1 → `kit check` → `kit pack --import`,
with HM `ab311b4` (main). Build: **55 min 26 s** at load ~30, peak RSS **4.81 GiB**. All **270 gates**
passed (269 PASS + 1 NOTE). `nanosoc_partial.bin` and `nanosoc_partial_clear.bin` have the **same sha256**
as the fielded `overlay_mbv/nanosoc/{nanosoc.bin, nanosoc_clear.bin}` (§7). Timing and pblock resources
match the fielded report to the last digit (§6).

What it took, and where HM fell short, is §10 (the recipe) and §11 (the gaps, ranked). Four HM
changes are on `team/kit-nanosoc` (§12): the worst gap (the built-in `nanosoc` built an **empty RM under
nanosoc's name and rm_id**, proven in Vivado) is fixed, `build.generics` exists, and `kit pack
--import` now says when Program lists another overlay instead, and the guide sees a running build. A second full build on the branch head,
with `build.generics` and no synth hook, is §13.

- **Lane:** KIT-NANOSOC. **Branch:** `team/kit-nanosoc` (from `main` `ab311b4`). Not pushed, not merged.
- **Machine:** srv03335 (RHEL 8.10, 16 cores, 251 GB RAM), shared. Load average 36 at the start, 19-34
  during the build.
- **No board, no fpgahub, no lease, no hub access** (the kit zip came from KIT-NIGHT's scratch copy,
  sha256 checked). No port bound.

## 1. Inputs

| What | Value |
|---|---|
| Kit | `mps3_rc2_0x44EE76D5_kit.zip`, copied from `/tmpdir/claude-74755/kit-night/kit/`; sha256 `95768b649dc2325ac3c8d2f14a5aa3da58d4f5554a0e56fda775a8defa7e906f` (**matches**); kit_id `mps3/0x44EE76D5/vivado-2026.1`, 38,287,216 B |
| Static | RC2 `0x44EE76D5`, USERCODE `0xFB1F8C76`, `clr_max` 1,048,576 B, partition `u_rp_dut` 47 ports / 148 bits |
| HM | `ab311b4` (main) for §3 steps 0-7: it wrote the build's `build_rm.tcl` (00:16:11). The later steps ran on the branch as it grew (the venv is an editable install): `a29b51e` from 00:31 (the guide during the build, `kit check`, `kit pack --import`), `8fff173` from 01:18:51 (s12d, s13, s14). The branch does not change `kit check`. The worktree venv: `make venv PYVERIFY=`, python3.11, the vendored pyverify 0.1.0 wheel |
| Vivado | `/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado`, v2026.1 build 6511674, ENTERPRISE licence from `/etc/profile.d` |
| The DUT | the platform's `rm_nanosoc` exactly as the fielded mint built it (§4) |
| Fielded pair (comparison) | `mps3-nanosoc-platform-lx/fpga/dfx/build_mint3_rc2_linux/overlay_mbv/nanosoc/`: `nanosoc.bin` 2,298,736 B, `nanosoc_clear.bin` 169,000 B, built 2026-09-25 by the mint at platform `fb1f8c7` |

## 2. Isolation

- Every HM command ran through `tools/kns.sh`, KIT-NIGHT's `kn.sh` with this lane's paths:
  `env -i HOME=/tmpdir/claude-74755/kit-nanosoc/home ... bash -lc`, with `HARNESS_MANAGER_STATE_DIR`,
  `HARNESS_MANAGER_PTY_DIR`, `XDG_{CONFIG,DATA,CACHE}_HOME` and `TMPDIR` all under
  `/tmpdir/claude-74755/kit-nanosoc/`, and the worktree venv first on PATH.
- The scratch home's `~/.bash_profile` sources Vivado 2026.1's `settings64.sh` (KIT-NIGHT F1's fix: a
  login shell never reads `~/.bashrc`). So every step saw 2026.1 on PATH and the guide's Tools step was
  `done`.
- **Before the first real step** (`logs/s00_isolation.txt`, 00:11:58): `kit list` said `no kits cached`,
  and `config path` named only the scratch dirs.
- The unit tests ran with the suite's per-test state dir (conftest). Only port-free test files ran.
- david's `~/.config/harness-manager` changed during the night, but not through this lane: the main
  checkout's daemon (pid 3357624, started 00:46:25) and its `tools.hil run --plan linux-nocard` on board 2
  (started 00:49:04) wrote `daemon.log`, `leases`, `checks`, `identity`, `ssh`, `tunnel`. Every HM process
  of this lane ran from `hm-kit-nanosoc/.venv` with the private state dir.
- The platform repo, its worktrees, the `nanosoc_m0_soc` and `ahb_qspi` checkouts and
  `/research/AAA/ip_library` were only read. What the build needed from the first three was copied into
  the scratch design dir (§4).

## 3. Commands, in order, with exit codes

Every HM command is `tools/kns.sh '<command>'`; `~` is the scratch home. The per-step outputs are in
`logs/`; `logs/kitnanosoc_commands.log` is the transcript's command and rc lines.

| Step | Command | rc | Took | Output |
|---|---|---|---|---|
| 0 | `harness-manager kit list; harness-manager config path` (empty state) | 0 | 4 s | `logs/s00_isolation.txt`: `no kits cached`, scratch paths only |
| 1 | `sha256sum mps3_rc2_0x44EE76D5_kit.zip; harness-manager kit import mps3_rc2_0x44EE76D5_kit.zip` | 0 | 4 s | `logs/s01_kit_import.txt`: sha256 matches; `files` ok (14), `static_id` ok |
| 2 | `harness-manager kit info --static-id 0x44EE76D5` | 0 | 5 s | `logs/s02_kit_info.txt`: vivado ok, `vivado_path` ok, boundary 47/148 ok |
| 3 | `harness-manager kit script --static-id 0x44EE76D5 --design nanosoc` (the **built-in**) | 0 | 5 s | `logs/s03_builtin_nanosoc_dry.txt`: two warnings only, and RM_SOURCES = the skeleton (**G1**) |
| 4 | `harness-manager kit script --static-id 0x44EE76D5 --design designs/nanosoc/nanosoc.json` (preview) | 0 | 5 s | `logs/s04_script_dry.txt`: one warning, `rm_id_range` (the platform's own id: expected) |
| probe | a toy Vivado run (not through HM): `set_property generic "IMG=<abs>" [current_fileset]` in an in-memory project, then `synth_design` | 0 | about 2 min | `probe/generic/`: `Parameter IMG bound to: …/img.hex`, `$readmem data file … is read successfully`: the fileset property reaches a non-project `synth_design`, so a synth hook can set a generic (G2) |
| 5 | `harness-manager kit script --static-id 0x44EE76D5 --design designs/nanosoc/nanosoc.json --out ~/builds/nanosoc --jobs 4` | 0 | 4 s | `logs/s05_kit_script.txt`: 22 files |
| 6 | `harness-manager kit build ~/builds/nanosoc --jobs 4` (and `--json`) | 0 | 8 s | `logs/s06_kit_build.txt`: the 2026.1 path (with `//`: KIT-NIGHT F7, fixed on `team/kit-night` only), `"ran": false` |
| 7 | `nice -n 10 /usr/bin/time -v <the printed command>` (`logs/s07_vivado_command.txt`) | 0 | **55 min 26 s** | `HM_RM_BUILD_COMPLETE rm=nanosoc rm_id=0x01000001 static_id=0x44EE76D5`; `vivado/build_rm.log`, `logs/s07_time_v.txt` |
| 7 (during) | `harness-manager kit guide ... --design designs/nanosoc/nanosoc.json --build-dir ~/builds/nanosoc` | 0 | 31 s | `logs/s08_kit_guide_during_build.txt`: 1-4 done, **5 Build NEXT "no receipt yet"** while Vivado was in link (**G7**) |
| 3 (fix) | the same, on `a29b51e` | 0 | 4 s | `logs/s03b_builtin_nanosoc_dry_fixed.txt`: the `sources` warning names the 13 undriven outputs, says the built-in is constraints-only, and RM_SOURCES is empty |
| probe | the built-in `nanosoc`, `--stop-after link`: as the branch head writes it, then with ab311b4's `RM_SOURCES=xdc/nanosoc_wrapper_skeleton.sv` | 0 | 5 min 55 s | `logs/s09c_builtin_nanosoc_probe.txt`: head stops at `sources_given`; ab311b4's skeleton **passes all 14 gates through link with 0 cells** (**G1**) |
| 8 | `harness-manager kit check ~/builds/nanosoc` | 0 | 3 s | `logs/s10a_kit_check.txt` (§8) |
| 8 | `harness-manager kit check ~/builds/nanosoc/out/nanosoc_build.json --static-id 0x44EE76D5 --json` | 0 | 2 s | `logs/s10b_kit_check.json.txt` |
| 9 | `harness-manager kit pack ~/builds/nanosoc --import` | 0 | 3 s | `logs/s11a_kit_pack_import.txt`: `overlay nanosoc (0x01000001) for static 0x44EE76D5` / `imported into the store (06139f71f1a8): it shows in Program` |
| 9 | `python tools/overlays_boardfree.py 0x44EE76D5 1048576` | 0 | 2 s | `logs/s12a_boardfree_programmable.txt`: **PROGRAMMABLE** (§9) |
| 9 (negative) | `python tools/overlays_boardfree.py 0x72BB0A36 262144` | 1 | 1 s | `logs/s12b_boardfree_negative.txt`: REFUSED, `shell_id` and pyverify `static_id mismatch` |
| 9 | `python tools/shadow_check.py <a copy of the fielded nanosoc triple>` | 0 | 1 s | `logs/s12c_shadow_check.txt`: with the fielded triple as an overlay dir, the import is **shadowed** (**G5**) |
| 9 | the handover copy in a **fresh** state dir: `sha256sum -c SHA256SUMS`, `kit list` (empty), `kit check … --static-id 0x44EE76D5`, `kit pack … --import`, the board-free listing | 0 | 4 s | `logs/s13_handover_fresh_state.txt`: all OK, Programmable |
| 9 (fix) | on `8fff173`, a fresh state dir with the fielded triple in `HARNESS_MANAGER_MPS3_OVERLAY_DIRS`: `kit pack … --import` | 0 | 2 s | `logs/s12d_pack_import_shadowed_fixed.txt`: "…but Program lists …/nanosoc/manifest.json instead … byte-identical … the same bits" |
| after | `harness-manager kit guide … --build-dir ~/builds/nanosoc` | 0 | 30 s | `logs/s14_kit_guide_after.txt`: **all six steps done**, "nanosoc passed 270 gates" |
| after (fix) | on `2c105cf`: `kit guide … --build-dir ~/builds/g7demo` (the first 3000 lines of this build's log: stage link), then `touch -d "3 hours ago"` on it and the guide again | 0 | 31 s | `logs/s15_guide_running_build_fixed.txt`: "a build is running here: stage link …" with no command; then "… no verdict: that run died" with the command |

## 4. The design .json

`design/nanosoc.json` is the design this build used. `design/nanosoc_generics.json` is the same design
on the branch head, with `build.generics` instead of the synth hook (§13).

| Key | Value | Where it came from |
|---|---|---|
| `kind`, `name`, `rm_id` | `rm`, `nanosoc`, `0x01000001` | the platform's `fpga/dfx/rm_list.tcl`: design_id 0x0001, v1.0.0 |
| `use`, `clocks` | clkrst, jtag, uart, status, gpio, qspi `{timed}`; `jtag_tck` | the built-in `nanosoc` design, unchanged |
| `wrapper` | `src/platform/fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` | the port list is checked against the boundary: 47 ports / 148 bits, no finding |
| `build.top` | `rp_nanosoc_wrapper` | `rm_list.tcl` |
| `build.sources` | **246 files, in order**: 241 from `nanosoc_m0_soc/pynq/filelist.tcl` (the file the platform's `ooc_synth.tcl` sources), then `uart_axis_shim.sv`, `rp_nanosoc_wrapper.sv`, `clcd_core.sv`, `ahb_clcd.sv`, `nanosoc_exp_socket.sv` (ooc_synth.tcl's order). 214 `.v`, 32 `.sv`; the 6 SystemVerilog packages come before their users | `tools/expand_flist.tcl` sourced the filelist in a plain `tclsh` with `read_verilog`/`set_property` stubbed (`design/flist_expanded.txt`); `tools/gen_design.py` wrote the JSON |
| `build.include_dirs` | 9: the SoC's `build_soc/rtl` + 8 Arm IP dirs | the filelist's `set_property include_dirs` |
| `build.defines` | `RAM_PRELOAD` | the filelist's `set_property verilog_define` |
| `build.synth_hook` | `nanosoc_synth_hook.tcl`: `set_property generic "IMEM_MEM_FPGA_IMG=<abs path of hello_image.hex>" [current_fileset]` | **the workaround for G2**: HM had no generics. The wrapper's default is one workstation's platform checkout (the wrapper's own FIXME) |

Sources: the SoC and `ahb_qspi` files are a **snapshot** in the scratch design dir (`design/src_snapshot.sha256`,
129 files; `design/src_provenance.txt`): `nanosoc_m0_soc` @ `86f0a5b` dirty (the mint recorded the same
commit, dirty), `ahb_qspi` @ `0ab9f42`, and the platform files from the `-lx` worktree, each
byte-identical to `git show fb1f8c7:<path>` (the mint's repo commit). The Arm IP is read **in place** from
`/research/AAA/ip_library` by absolute path, never copied.

The platform's `ooc_synth.tcl` also flips `exp_*` port directions when the SoC's `nanosoc.sv` needs it.
This SoC tree does not (`exp_hsel` is an output), and the fielded synth log says the same ("no flip
needed"), so the design needs no patched copy.

Checks that the netlist is the fielded one's, before implementation:
- `$readmem data file '…/hello_image.hex' is read successfully` (the hook's path), md5 `e0cf6827…` = the
  image the fielded synth read (`/home/dam1n19/SoCLabs/mps3-nanosoc-platform/fpga/rp/nanosoc/hello_image.hex`);
- the synthesised module set (100 modules) and **Report Cell Usage are identical** to the fielded
  `rm_nanosoc_synth/synth.log` (the mint's run dir, `/tmpdir/claude-74755/linux-lanes/flow/mint3rc2-fb1f8c7/fpga/dfx/build_mint3_rc2/`; CARRY8 68, DSP48E2 3, LUT1-6 164/1195/1181/1733/2478/5972, RAMB18E2 9,
  RAMB36E2 16, FDCE 4471, FDPE 1142, FDRE 592, FDSE 3).

## 5. The build

| | |
|---|---|
| Command | `/research/CAD/Xilinx/Vivado//2026.1/Vivado/bin/vivado -mode batch -source ~/builds/nanosoc/build_rm.tcl -log …/build_rm.log -journal …/build_rm.jou -tclargs JOBS=4`, under `nice -n 10 /usr/bin/time -v` |
| Wall | 00:16:38 → 01:12:04 BST: **55 min 26 s**; 120 % CPU; exit 0 |
| Peak RSS | **5,046,148 kB (4.81 GiB)**; Vivado's own peak 5,605 MB (`write_bitstream`) |
| Per step (elapsed) | synth 13:12 · open static 0:56 · read_checkpoint -cell 2:35 · opt 1:20 · place 11:22 · phys_opt 0:33 · route 14:02 · write_checkpoint 0:25 · pr_verify 3:44 · open routed 1:09 · write_bitstream 2:22 |
| Gates | **270: 269 PASS + 1 NOTE** (`ltx`: no debug core). 247 of them are `source_present` (G10). Then `ooc_xdc_present`; synth 4 (`no_black_boxes`, `boundary_bits` 148/148, `rm_id_match` 0x01000001, `ooc_clocks` dut_clk dbg_bscan_tck dbg_bscan_drck jtag_tck); link 4 (148 partition pins, 23 clocks, no HDPR-16/18/50); impl 3 (`rp_pins_opt`, `drc_routed`, `rm_timing`); `pr_verify`; bitstream 5 + `clearing_fits` |
| pr_verify | compatible with the kit's `static_routed_locked.dcp`: 102 partition pins, 43,911 static tiles, 97,460 static cells, 1,244,073 static routed pips compared on both |
| CRITICAL WARNINGs | 18. 12 at link, from HM's `nanosoc_ooc.xdc` (lines 35-52: `set_false_path` on ports with no timed start/end point, re-applied by `read_checkpoint -cell`); 6 static `[Timing 38-285]` (the debug_bridge TCK). Same classes as the platform mint's own `prod/build.log` (89 × 18-512/513 and 49 × 38-285 over 13 configs; 26 of them from `nanosoc_ooc.xdc`). KIT-NIGHT F3 |

## 6. Timing and resources, against the fielded build

From `out/nanosoc_timing.rpt` and `out/nanosoc_util.rpt` (pblock `pblock_rp_dut`), against the mint's
`prod/timing_rm_nanosoc.rpt` and `prod/util_rm_nanosoc.rpt`:

| | This build | Fielded (mint, 25 Sep) |
|---|---|---|
| **dut_clk** (intra-clock) WNS / TNS / endpoints | **3.563 ns** / 0.000 / 12,892 | 3.563 ns / 0.000 / 12,892 |
| dut_clk WHS / THS | 0.030 ns / 0.000 | 0.030 ns / 0.000 |
| async_default dut_clk→dut_clk WNS / WHS | 7.523 / 0.117 ns | 7.523 / 0.117 ns |
| Whole design WNS / TNS / WHS / WPWS | 0.207 / 0.000 / 0.030 / 0.124 ns (87,315 setup endpoints) | the same, the same count |
| The receipt's `rm_timing` gate | "your RM's paths (6365 registers): setup WNS 3.563 ns, hold WHS 0.030 ns" | n/a |
| Pblock CLB LUTs (logic / memory) | 11,051 (10,939 / 112) of 42,824 (25.81 %) | 11,051 (10,939 / 112) |
| Pblock CLB registers | 6,210 | 6,210 |
| CLBs · CARRY8 · Block RAM tiles · DSPs | 1,989 · 68 · 20.5 · 3 | 1,989 · 68 · 20.5 · 3 |

The whole-design numbers are the static's worst paths (the same 0.207 ns as KIT-NIGHT's `minimal`).

## 7. The pair, against the fielded pair

| File | Bytes | CRC-32 | sha256 | Fielded |
|---|---|---|---|---|
| `nanosoc_partial.bin` | 2,298,736 | `0x930EB9EA` | `3b697246426c9d556e30f5348064f08c5fd519dfb2947d07dde719a250b75f9d` | `nanosoc.bin`: **the same sha256** (`cmp`: identical) |
| `nanosoc_partial_clear.bin` | 169,000 | `0x77CCEF0F` | `983b53d663476dcbba2e1fa1b03d70e5951b4225443fa4889e18430a39e7a7a0` | `nanosoc_clear.bin`: **the same sha256** (`cmp`: identical) |
| `nanosoc_partial.bit` | 2,298,972 | | `380a5d9534f1928b0f21d93a2e976b7551d76c354141730908faf93dfad103e0` | (header carries the build date) |
| `nanosoc_partial_clear.bit` | 169,247 | | `1ce7f1b3c39bef46c4158e997bedc4fd736419811b3411104fb6c420cb958e2a` | |
| `nanosoc_build.json` (receipt) | 49,812 | | `3b9b51bace67182c3bd8eab898f44906c689ec6e8a36dca7fff7ef9a2552ced3` | |

- **Equivalent in role, and more: the same bits.** Same rm_id `0x01000001`, same boundary (148 bits),
  same static binding (`static_id` 0x44EE76D5 = the CRC-32 of the kit's DCP; pr_verify compatible), same
  clearing size, and byte-identical partial and clearing. Vivado 2026.1 is deterministic here: the same
  netlist placed and routed into the same locked static gives the same frames, although HM's OOC XDC
  differs from the platform's (it names RC2's `BUFGCE_X2Y47`, the platform's names `0x72BB0A36`'s
  `X2Y24`, and it keeps `qspi` timed): both are OOC-only.
- The overlay manifest differs from the fielded one only by `"built": "2026-09-30"` and
  `"build_receipt": "nanosoc_build.json"` (`vivado/overlay_manifest.json`).
- None of `.bit`, `.bin`, `.dcp` is committed. They are in `/tmpdir/claude-74755/kit-nanosoc/home/builds/nanosoc/out/`
  and the handover copy (§9).

## 8. `kit check`

`kit check ~/builds/nanosoc` (rc 0, `logs/s10a_kit_check.txt`): `the build nanosoc: passed (unchecked
is not a pass: see the list)`, **23 ok and 1 unchecked**:
- `build` ("the build passed 270 gates"), `rm_id` 0x01000001, `static_id` "built against static 0x44EE76D5";
- `partial` 2,298,736 B CRC `0x930EB9EA`, `clearing` 169,000 B CRC `0x77CCEF0F`, both as built;
- the partial's stream: header length, part, `PARTIAL=TRUE`, 2 sections to DESYNC (505,161 FDRI words,
  3,564 frames), IDCODE `0x0390D093`=SLR0, no IPROG/AXSS/WBSTAR, role `partial`, frame box
  `{0: rows 0-1, cols 94-200; 1: rows 0-1, cols 6-11}` inside the partition's, vocabulary;
- `bit_bin_pair`;
- the clearing's stream (3 sections, 38,622 FDRI words, 223 frames, role `clearing`, box `{0: rows 0-1, cols 101-193}`);
- `clearing_fits` (169,000 of 1,048,576 B), `clearing_pairs_partial`;
- `static_binding` *unchecked* (by design: a partial carries no static identity).

With `--static-id 0x44EE76D5 --json` the result is the same (`logs/s10b_kit_check.json.txt`).

## 9. Pack, import, Programmable, and the handover for board 2

- `kit pack ~/builds/nanosoc --import` (rc 0) wrote `~/builds/nanosoc/overlay/nanosoc/{manifest.json,
  nanosoc.bin, nanosoc_clear.bin, nanosoc_build.json}` and imported it into the private store as
  `06139f71f1a8`.
- Board-free "Programmable" (`tools/overlays_boardfree.py`, KIT-NIGHT's tool plus the partial's sha256):
  against `0x44EE76D5`, `shell_id`, `crc and length`, `clearing pairs partial`, `clearing fits` ok,
  `static_usercode` *unchecked* (JTAG), pyverify `Overlay.validate` ok: **PROGRAMMABLE**. Against
  `0x72BB0A36`: **REFUSED** (rc 1).

**The overlay triple for board 2** is on srv03335 at
`/tmpdir/claude-74755/kit-nanosoc/handover/nanosoc_0x01000001/`: `overlay/nanosoc/` (the triple + the
receipt), `out/` (receipt, both `.bin`, both `.bit`) and `SHA256SUMS` (`vivado/handover_SHA256SUMS`). In a
fresh state dir with no kit, `sha256sum -c`, `kit check --static-id 0x44EE76D5` and `kit pack --import`
pass, and it is Programmable (`logs/s13_…`).

- **It is the fielded nanosoc, bit for bit.** Loading it on board 2 loads the same frames as the fielded
  `nanosoc`; a board test proves the flow's plumbing (manifest, store, push), not a different design.
- **Deploy it with `--overlay-dir`.** The catalogue keeps the first overlay of a (name, rm_id, static):
  overlay dirs before the store (G5, `logs/s12c_…`). If the board's HM already lists the fielded
  `nanosoc`, an import is shadowed. So:
  `harness-manager program 192.168.11.101 nanosoc --overlay-dir /tmpdir/claude-74755/kit-nanosoc/handover/nanosoc_0x01000001/overlay`
  (board 2's address as KIT-NIGHT and tonight's HIL run name it; with the lease, and not during another lane's board run).
- Or import it: `harness-manager kit pack /tmpdir/claude-74755/kit-nanosoc/handover/nanosoc_0x01000001/out/nanosoc_build.json --import`
  (on `team/kit-nanosoc`, that command says when Program lists another `nanosoc` instead).

## 10. What a user must do to build their own DUT this way (the recipe that worked)

1. Put Vivado 2026.1 in the login profile: append `source /research/CAD/Xilinx/Vivado/2026.1/Vivado/settings64.sh` to `~/.bash_profile` (not `~/.bashrc`).
2. `harness-manager kit import mps3_rc2_0x44EE76D5_kit.zip`.
3. Start the wrapper from the kit's skeleton (`harness-manager xdc rm-kit --design <your design .json> --out x/` writes `x/<name>_wrapper_skeleton.sv`): all 47 ports, `rm_id` a constant. nanosoc's wrapper is the platform's `rp_nanosoc_wrapper.sv`.
4. List every source **in compile order** (packages first). A Tcl filelist has to be expanded by hand: `tools/expand_flist.tcl` (a stubbed `tclsh`) is how this lane did it, in about 15 minutes (G3).
5. Write the design `.json`: `use`/`clocks` (copy a built-in's), `wrapper`, `rm_id`, `build: {top, sources, include_dirs, defines}`, and on `team/kit-nanosoc` `generics` for a `$readmemh` image (on main: a `synth_hook` with `set_property generic … [current_fileset]`, `design/nanosoc_synth_hook.tcl`).
6. `harness-manager kit script --static-id 0x44EE76D5 --design my.json --out ~/builds/my --jobs 4`, then `harness-manager kit build ~/builds/my` and run the printed command. **Budget 1 hour** for a nanosoc-sized RM on a loaded srv03335 (4 threads, 5 GB RAM).
7. `harness-manager kit check ~/builds/my`, then `harness-manager kit pack ~/builds/my --import`. If the design keeps a fielded RM's name and rm_id, program it with `--overlay-dir` (G5).

## 11. The gaps, ranked by how badly they block a user building their own DUT

| # | Gap | What happened | Workaround a user can do | State |
|---|---|---|---|---|
| **G1** | `--design nanosoc` (the built-in) builds an **empty RM** under nanosoc's name and rm_id | The built-in names 7 used groups and no RTL, so ab311b4 set RM_SOURCES to the skeleton, which drives `rm_id = 0x01000001` and leaves 13 outputs as `// assign … = ...;`. The warning said it "ties every other output off". Run to link: **every gate passes, 0 cells** (`logs/s09c_…`). It would pack as `nanosoc` with the rm_id the shell's post-swap check accepts | Never use a built-in `nanosoc*` design to build: write a `.json` with `build.sources` | **fixed** `e5dc902`: the skeleton is RM_SOURCES only when it is a whole RM (`minimal`); otherwise RM_SOURCES is empty, the build stops at `sources_given`, and the warning names the undriven outputs and says the built-in is constraints-only |
| **G2** | No **generics**: a `$readmemh` image path (any SoC with baked firmware) could not be set | nanosoc's `IMEM_MEM_FPGA_IMG` default is `/home/dam1n19/SoCLabs/mps3-nanosoc-platform/…/hello_image.hex`; the platform passes `-generic` | `build.synth_hook` with `set_property generic "NAME=<abs path>" [current_fileset]` (probed on 2026.1: `probe/generic/` + this build's readmem line) | **fixed** `a29b51e`: `build.generics` `{NAME: value \| {"path": FILE}}`, one `-generic` each; a path is written absolute and a missing file fails `generic_file_present` at preflight (Vivado only warns, [Synth 8-4445], and builds a blank ROM). Proven end to end in §13 |
| **G3** | No **filelist** import | nanosoc's recipe is a Tcl filelist with globs, env vars and fileset properties. HM wants every file listed: 246 here | Expand it (`tools/expand_flist.tcl` + `tools/gen_design.py`), or a `synth_hook` that sets `::env(...)` and sources the filelist (then `source_present` checks nothing) | open. Suggest `build.filelist`: a `.f` with `+incdir+`/`+define+`, expanded by HM with each file gated |
| **G4** | The `build` keys were **undocumented** for users | Only `script.py`'s docstring and API.md listed them; XDC_EXPORT's design reference had none | read `script.py` | **fixed** `a29b51e` (docs): USER_GUIDE §7 table + example; XDC_EXPORT points at it |
| **G5** | An import with a fielded RM's name, rm_id and static is **shadowed** in Program | The catalogue keeps the first (overlay dirs, then the store) and only logs the rest; `kit pack --import` said "it shows in Program" (`logs/s12c_…`) | `program TARGET NAME --overlay-dir <the triple's parent>` | **message fixed** `8fff173`: the import names the overlay Program lists instead, says if its bits are identical, and gives the `--overlay-dir` (`logs/s12d_…`); the web result too. Resolving by recency is a design decision, not made here |
| G6 | A non-HDL file in `build.sources` failed synthesis with a parse error | The template reads every non-`.v`/`.vhd` file as SystemVerilog | know which key takes it | **fixed** `a29b51e`: `.hex/.mem/.mif/.coe/.xci/.xcix/.xdc/.tcl/.dcp/.edf` refused at `kit script`, naming the key |
| G7 | The guide cannot see a **running** build | At 00:32, with Vivado in link, step 5 said `NEXT … no receipt yet` and offered the Vivado command again: a second Vivado in the same dir would clobber `out/`; with a last run's receipt there, it showed that one | look at `build_rm.log`'s last `HM_STAGE` | **fixed** `2c105cf`: a log with a stage, no verdict, written in the last 30 min → "a build is running here: stage link …", no command, the old receipt ignored; an older one → "that run died" with the command (`logs/s15_…`, on the real log) |
| G8 | The platform's nanosoc sources are not packaged for a user | The RTL spans 3 checkouts (`nanosoc_m0_soc` at a dirty regenerated commit, `ahb_qspi`, the platform) plus the licensed Arm IP at `/research/AAA/ip_library` | this lane's snapshot + manifest (`design/src_*`) | open (platform/SoC Labs). A user outside the lab cannot build nanosoc at all without Arm IP access; their own DUT is not affected |
| G9 | A build dir is **not relocatable** | Design-relative paths are written absolute into `build_rm.tcl`, though its header says the dir can be moved | keep the design where it was | open, low |
| G10 | **Receipt bloat** | 247 of 270 gates are `source_present`; the guide says "passed 270 gates"; the receipt is 49.8 KB | none needed | open, low. Suggest one `sources_present` gate ("246 files") that names the first missing file |
| G11 | 18 **CRITICAL WARNINGs** | §5: 12 from the OOC XDC at link (as in the platform's own mint), 6 static | expect them (KIT-NIGHT F3) | open (T10 + kit owner) |
| G12 | KIT-NIGHT's four fixes are not on main | This lane saw F7's `//` path and F2's "about 20 min" README line again; nanosoc took 55 min | — | merge `team/kit-night` |

Not exercised: Xilinx IP (`.xci`) and netlists (`.edf`) in an RM. nanosoc has none. The hook is the
documented route (`read_ip`); nothing here tested it.

Worked as documented: SystemVerilog packages (by order), include dirs, defines, the wrapper port check,
the rm_id checks, and every gate.

## 12. HM changes on `team/kit-nanosoc` (not pushed, not merged)

| Commit | Change | Tests (each with its negative twin; the new ones fail on ab311b4) |
|---|---|---|
| `e5dc902` | G1: the skeleton is RM_SOURCES only when it is a whole RM; `rm_kit` records `facts.skeleton_undriven` (one predicate shared with the skeleton renderer) | `tests/unit/test_kit_rc2.py`: built-in nanosoc → empty RM_SOURCES + the warning / `minimal` and a fully-tied design still build as their skeleton, one untied output → no built-in sentence; the fact lists used-group outputs only / never `rm_id` or an unused group's |
| `a29b51e` | G2 `build.generics` (+ `RM_GENERICS`/`RM_GENERIC_FILES` in `build_rm.tcl`, gate `generic_file_present`, GATE_HELP); G6 HDL-only `build.sources`; G4 docs (USER_GUIDE §7, XDC_EXPORT, DUT_BUILD_GUIDE gate table, CHANGELOG) | `tests/unit/test_kit_nanosoc.py` (18): render / none; 5 malformed forms refused / absolute path in an inline design taken; 6 non-HDL suffixes refused / every HDL suffix taken; the script **run in Python's Tcl** with Vivado stubbed: one `-generic` each + the gate / none; a missing file fails before `synth_design` |
| `8fff173` | G5: `import_overlay` returns `shadowed_by`, `shadow_same_bits`; `kit pack --import` and the web **Add to Program** result say so; API.md, CHANGELOG | 4 more in `test_kit_nanosoc.py`: shadowed by a dir with other bits / not shadowed (none, or another name); same bits reported; the CLI line and `--overlay-dir` / an unshadowed import still "shows in Program" |
| `2c105cf` | G7: `build.running_build()`; the guide's Build step says a build is running (no command, the old receipt ignored) or that the last run died; USER_GUIDE, CHANGELOG | 4 more in `test_kit_nanosoc.py`: stage + freshness / any verdict ends the run, echoed script lines do not count; running → no second Vivado / no log → the command; a running rebuild hides the last receipt; a 3 h old log is a run that died |

- `ruff check` passes on `src` and the touched tests.
- Port-free suite: every `tests/unit/test_kit_*.py`, `test_otac_kits.py`, `test_cli_help_coverage.py`,
  `test_t5_help.py`, `test_t10_{checks,golden,model,syntax,vivado}.py`, and `tests/integration/test_kit_cli.py`
  minus its 2 virtual-board tests, plus `tests/web/test_kit_ui_build_static.py`, `test_t14_static.py`,
  `tests/unit/test_t2_overlays.py`, `test_t2_overlay_extras.py`: **441 passed, 5 skipped** on `2c105cf`.
- The full `make test` was **not** run: the virtual board binds 10000-19999, which this lane may not bind.
  The browser tests (Playwright) were not run; the web change is 4 lines, checked by running its render
  function in node against the three cases.
- The T10 goldens are unchanged (facts are not in them).

## 13. Run 2: the branch head, with `build.generics` and no synth hook

The branch head was built again, all the way through, to prove `build.generics` in Vivado (and that
the template change still gives a passing build). HM `a29b51e`, the same kit and private state, the design
`design/nanosoc_generics.json` (the same 246 sources, no `synth_hook`, `build.generics:
{"IMEM_MEM_FPGA_IMG": {"path": "src/platform/fpga/rp/nanosoc/hello_image.hex"}}`), a new
`~/builds/nanosoc_head` (`logs/r2/`).

| | |
|---|---|
| `kit script … --design designs/nanosoc/nanosoc_generics.json --out ~/builds/nanosoc_head --jobs 4` / `kit build … --jobs 4` | rc 0 / rc 0 (`logs/r2/s05_…`, `s06_…`). The parameter block differs from run 1's only in `RM_GENERICS` / `RM_GENERIC_FILES` (set) and `RM_SYNTH_HOOK` (empty); `RM_SOURCES` is the same line. The OOC XDC differs only in its title comment |
| Vivado | 01:12:51 → 02:01:23: **48 min 32 s** at load 10-46 (128 % CPU); peak RSS 5,060,540 kB (4.83 GiB); exit 0 (`logs/r2/s07_time_v.txt`). synth 12:28 · place 10:25 · route 10:22 · pr_verify 3:02 · write_bitstream 2:18 |
| The generic | `HM_GATE generic_file_present PASS …/hello_image.hex` at preflight; `Parameter IMEM_MEM_FPGA_IMG bound to: …/hello_image.hex`; `$readmem data file '…/hello_image.hex' is read successfully` (`logs/r2/markers.txt`) |
| Verdict | `HM_RM_BUILD_COMPLETE`, **270 gates** (269 PASS + NOTE ltx); `rm_timing` 6365 registers, setup WNS 3.563 ns, hold WHS 0.030 ns; 18 CRITICAL WARNINGs, as run 1 |
| The pair | **byte-identical to run 1 and to the fielded pair**: `3b697246…5f9d` / `983b53d6…a7a0` (`logs/r2/sha256_pair.txt`) |
| `kit check ~/builds/nanosoc_head --static-id 0x44EE76D5` / `kit pack … --out …` | rc 0 / rc 0 (not imported: run 1's identical pair is already in the store) |

So `build.generics` does what the synth hook did, with no Tcl, and the same bits come out.

## 14. Files here

- `design/`: `nanosoc.json` (this build), `nanosoc_generics.json` (run 2), `nanosoc_synth_hook.tcl` (the
  G2 workaround on main), `flist_expanded.txt` (the filelist, expanded), `src_snapshot.sha256` and
  `src_provenance.txt` (the 129 copied source files and where they came from).
- `vivado/` (not `build/`: the repo's `.gitignore` drops every `build/` directory, which is why
  KIT-NIGHT's `build/` files are not in its commit `2db8f33`): what HM generated (`build_rm.tcl`, `README.txt`, the OOC XDC, skeleton, connectivity and
  pblock sheets), the Vivado log and journal, the receipt, the reports (timing: its first 200 lines; the
  full one is in the scratch dir), the overlay manifest, the handover SHA256SUMS.
- `logs/`: every command's output by step (`s00`…`s14`), `s07_time_v.txt`, `kitnanosoc_commands.log`
  (the transcript's command and rc lines), `r2/` (§13).
- `probe/generic/`: the toy run that showed a synth hook can set a generic (G2's workaround).
- `tools/`: `kns.sh` (the isolation wrapper), `expand_flist.tcl`, `gen_design.py`,
  `overlays_boardfree.py` (KIT-NIGHT's, plus sha256 and store records), `shadow_check.py`.
