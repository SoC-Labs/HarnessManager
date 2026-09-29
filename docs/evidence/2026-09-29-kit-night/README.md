# KIT-NIGHT: HM's DUT build flow, end to end, on the RC2 kit v2 (29 Sep 2026)

**Result: PASS, board-free.** The runbook's target A (`minimal`) went from the kit zip to an overlay that is
Programmable for static `0x44EE76D5`, on real Vivado 2026.1, with HM `506a5ef` (the P8 commit of
`CLEAN_ACCOUNT_SYNTHESIS_RUN_2026-09-30.md`). Build: **56 min 27 s** at load ~50 (a re-run on the fixed branch head: 28 min 36 s at load ~33, byte-identical pair). All 24 gates passed; `kit check`
passed; `kit pack --import` put it in a private store.

The run found 12 things (§8). Four small HM bugs are fixed on `team/kit-night` (§9). The rest are written up
for their owners. The one most likely to trip tomorrow's clean-account run is a runbook line (§8, F1).

- **Lane:** KIT-NIGHT. **Branch:** `team/kit-night` (from `main` `506a5ef`). Not pushed.
- **Machine:** srv03335 (RHEL 8.10, 16 cores, 251 GB RAM), shared. Load average 20 at the start and 48-59
  during place and route.
- **No board, no fpgahub, no lease.** The hub was used once: one `scp` of the kit zip.

## 1. Inputs

| What | Value |
|---|---|
| Kit | `mps3_rc2_0x44EE76D5_kit.zip`, 37,740,608 B, from the hub `/home/david/mints/0x44EE76D5/` |
| Kit sha256 | `95768b649dc2325ac3c8d2f14a5aa3da58d4f5554a0e56fda775a8defa7e906f` (**matches** the published value) |
| kit_id | `mps3/0x44EE76D5/vivado-2026.1`; 14 files, 38,287,216 B; CRC-32 of `static_routed_locked.dcp` = `0x44EE76D5` |
| Static | RC2 `0x44EE76D5`, USERCODE `0xFB1F8C76`, `clr_max` 1,048,576 B |
| HM | `506a5ef` for every step in §3-§6. `harness-manager 0.1.0`. The worktree venv (`make venv PYVERIFY=`, python3.11, pyverify 0.1.0 vendored wheel) |
| Vivado | `/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado`, `Vivado v2026.1 (lin64) Build 6511674` |
| Licence | `XILINXD_LICENSE_FILE=27070@xilinxlm1..3.soton.ac.uk` (from `/etc/profile.d`). Vivado: `INFO: [Common 17-3922] A valid Vivado Design Suite ENTERPRISE license has been detected. Your current license is active and will expire on Permanent.`, then `[Common 17-349] Got license for feature 'Synthesis' and/or device 'xcku115'` (and 'Implementation' ×5) |
| Design | The runbook's target A: HM's built-in `minimal`, as steps 5-9 of `CLEAN_ACCOUNT_SYNTHESIS_RUN_2026-09-30.md` (guide worktree `a2debe8`) run it. It has no `-tclargs RM_SOURCES`, because HM defaults it |

## 2. Isolation

Every command ran through `tools/kn.sh`, a copy of the runbook's `cr` wrapper. It uses
`env -i HOME=<scratch>/home USER LOGNAME TERM SHELL bash -lc`, so each command gets exactly the login
environment that a new account on srv03335 gets. Then it sets:
- `HARNESS_MANAGER_STATE_DIR=<scratch>/home/.config/harness-manager` (the same path the default gives under
  that HOME);
- `HARNESS_MANAGER_PTY_DIR`, `XDG_{CONFIG,DATA,CACHE}_HOME` and `TMPDIR`, all under
  `/tmpdir/claude-74755/kit-night/`;
- `PATH`, with the worktree venv first.

- Before the first HM command, `kit list` said `no kits cached`, and `config path` named only the scratch
  dirs (`logs/kitnight_commands.log`, 20:56:10).
- david's `~/.config/harness-manager` changed during the night, but not through this lane. A daemon from
  the **main checkout** (`/home/dam1n19/SoCLabs/harness-manager/.venv`, pid 2120817, started 22:11) and its
  lease on `mps3_02_pl` wrote there. Every HM process of this lane ran from `hm-kit-night/.venv` with the
  private state dir.
- The unit tests ran with the suite's own per-test state dir (conftest). Only port-free test files ran,
  because the virtual board binds 10000-19999 (§9).

## 3. Commands, in order, with exit codes

Every command is `tools/kn.sh '<command>'`. `~` is the scratch home. The full transcript is
`logs/kitnight_commands.log`.

| Step | Command | rc | Took | Output |
|---|---|---|---|---|
| 3 | `harness-manager kit list` (empty cache, before anything) | 0 | 2 s | `no kits cached` |
| 3 | `harness-manager kit import mps3_rc2_0x44EE76D5_kit.zip` | 0 | 2 s | `logs/s03a_kit_import.txt`: `files` ok (14), `static_id` ok |
| 3 | `harness-manager kit list; harness-manager kit info --static-id 0x44EE76D5` | 0 | 7 s | `logs/s03b_kit_list_info.txt`: vivado ok (2026.1 by install root), `vivado_path` **warning** (PATH has 2024.1), part ok, boundary 47/148 ok |
| 3 | `harness-manager kit verify mps3_rc2_0x44EE76D5_kit.zip` | **15** | 1 s | `logs/s03c_kit_verify_zip.txt`: `cannot read …zip/kit.json: Not a directory` (F6, fixed) |
| 3 | `harness-manager kit fetch --static-id 0x44EE76D5 --out ~/kitdir && harness-manager kit verify ~/kitdir` | 0 | 2 s | `logs/s03d_kit_fetch_verify_dir.txt`: all ok |
| 4 | `source …/2026.1/Vivado/settings64.sh; which vivado; harness-manager config test tools vivado` | 0 | 5 s | `logs/s04_config_test_vivado.txt`: PASS; the path is printed with `Vivado//2026.1` (F7, fixed) |
| 4 | `harness-manager config test tools vivado` (without sourcing) | 0 | 6 s | `logs/s04b_…`: PASS, with the note that PATH's `vivado` is 2024.1 |
| 5 | `source …settings64.sh; harness-manager kit guide --static-id 0x44EE76D5 --design minimal` | 0 | 35 s | `logs/s05_kit_guide.txt`: 1-4 done, **5 NEXT**, 6 waits |
| 6 | `source …settings64.sh; harness-manager kit script --static-id 0x44EE76D5 --design minimal --out ~/builds/minimal` | 0 | 4 s | `logs/s06_kit_script.txt`: 22 files; the two expected warnings (`rm_id_proposed` 0x0100F28A, `sources` = the skeleton) |
| 6b | `grep -n -e "assign dut_lockup" -e "assign irq_out" ~/builds/minimal/xdc/minimal_wrapper_skeleton.sv` | 0 | 1 s | both tie-offs present (lines 73-74) |
| 7 | `harness-manager kit build ~/builds/minimal --jobs 4` (and `--json`) | 0 | 6 s | `logs/s07a_kit_build.{txt,json}`: the full 2026.1 path, `"ran": false` |
| 7 | `nice -n 10 /usr/bin/time -v <the printed command>` (`logs/s07b_vivado_command.txt`) | 0 | **56 min 27 s** | `HM_RM_BUILD_COMPLETE rm=minimal rm_id=0x0100F28A static_id=0x44EE76D5`; `build/build_rm.log` |
| 7 (during) | `harness-manager kit guide … --design minimal [--build-dir ~/builds/minimal]`, without sourcing settings64 | 0 | 36-39 s | `logs/s07d_…`: **2 Tools NEXT, 5 Build "waits for 2 tools"**, `next: export PATH=…` (F1) |
| 8 (negative) | `harness-manager kit check ~/builds/minimal` / `kit pack … --import`, before the receipt existed | 3 / 3 | 4 s | `logs/s07e_…`: "no build receipt … run the build first" |
| 8 | `harness-manager kit check ~/builds/minimal` | 0 | 2 s | `logs/s08a_kit_check.txt` (§6) |
| 8 | `harness-manager kit check ~/builds/minimal --static-id 0x44EE76D5` (the kit README's form; and `--json`) | 0 | 3 s | `logs/s08b_…`, `logs/s08c_kit_check.json` |
| 9 | `harness-manager kit pack ~/builds/minimal --import` | 0 | 2 s | `overlay minimal (0x0100F28A) for static 0x44EE76D5 -> …/overlay/minimal` / `imported into the store (3f84f88675bf): it shows in Program` |
| 9 | `harness-manager overlays` | **2** | 1 s | needs TARGET: there is no board-free listing (F9) |
| 9 | `python tools/overlays_boardfree.py 0x44EE76D5 1048576` | 0 | 2 s | `logs/s09c_…`: **PROGRAMMABLE** (§7) |
| 9 (negative) | `python tools/overlays_boardfree.py 0x72BB0A36 262144` | 1 | 2 s | `logs/s09d_…`: REFUSED, `shell_id` and pyverify `static_id mismatch` |
| 9 | the handover copy into a **fresh** state dir: `kit list` (empty), `kit check …/minimal_build.json --static-id 0x44EE76D5`, `kit pack … --import`, the board-free listing | 0 | 4 s | `logs/s09f_…`: passes and imports with no kit cached (the frame box is then *unchecked*) |
| after | `source …settings64.sh; harness-manager kit guide … --build-dir ~/builds/minimal` (and `--json`) | 0 | 57 s / 23 s | `logs/s09g_…`, `logs/s09h_…`: **all six steps done**; the launch probe took 17.4 s |

## 4. The build

| | |
|---|---|
| Command | `/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode batch -source ~/builds/minimal/build_rm.tcl -log ~/builds/minimal/build_rm.log -journal ~/builds/minimal/build_rm.jou -tclargs JOBS=4`, under `nice -n 10` |
| Wall | 20:58:23 → 21:54:51 BST: **56 min 27 s** (`logs/s07_time_v.txt`); 77 % CPU; Vivado exit 0 |
| Peak RSS | **4,895,236 kB (4.67 GiB)**; Vivado's own peak: 5,473 MB (`write_bitstream`) |
| Per step (elapsed) | synth 1:06 · open_checkpoint 1:21 · opt 2:20 · place 10:31 · phys_opt 0:31 · **route 28:01** · pr_verify 3:37 · open_checkpoint 1:12 · write_bitstream 2:02 |
| Gates | **24: 23 PASS + 1 NOTE** (`ltx`: no debug core, so no .ltx). preflight 6 · synth 4 (`boundary_bits` 148/148, `rm_id_match` 0x0100F28A, `ooc_clocks` dut_clk dbg_bscan_tck dbg_bscan_drck) · link 4 (148 partition pins, 22 clocks propagated, no HDPR-16/18/50) · impl 3 · verify 1 · bitstream 6 |
| pr_verify | `[Vivado 12-3253] PR_VERIFY: check points …/kit/static/static_routed_locked.dcp and …/out/minimal_routed.dcp are compatible` (both: 102 partition pins, 43,911 static tiles, 1,244,073 static routed pips compared) |
| CRITICAL WARNINGs | 21. 15 come from HM's own `minimal_ooc.xdc` and 6 from the static (F3) |

## 5. Timing

- **The RP (the `rm_timing` gate):** no timed path. `minimal` drives only constants, so there is no RM path
  to time. The receipt's `rm_wns` and `rm_whs` are empty. On `506a5ef` the gate said `your RM's paths
  (3 registers): setup WNS none (no timed path), hold WHS none (no timed path)`. Those 3 "registers" are
  Vivado's `HD_PR_Connection_S_IN_FDCE_*` loads (F5, fixed).
- **The whole routed design (static + RP)** (`build/minimal_timing_summary_head.rpt`):

  | WNS | TNS | failing / total endpoints | WHS | THS | failing / total | WPWS |
  |---|---|---|---|---|---|---|
  | **0.207 ns** | **0.000 ns** | 0 / 68,599 | **0.030 ns** | 0.000 ns | 0 / 67,238 | 0.124 ns |

  "All user specified timing constraints are met."

## 6. The pair, and `kit check`

| File (in `~/builds/minimal/out/`) | Bytes | CRC-32 | sha256 |
|---|---|---|---|
| `minimal_partial.bin` | 1,244,136 | `0x5BAB2009` | `86e27c2e9bc064d297473db43c92b5681bc21c026e7475dadf08f7cb134e4f81` |
| `minimal_partial_clear.bin` | 67,748 | `0x0B0F0205` | `810683ab4addbd96ec77602199b6261da6ed0fd3a4df401a8c938e7440bf5ce4` |
| `minimal_partial.bit` | 1,244,372 | | `eb2324025aa30b6573ad752eb3f183aeceae754b42cfd640460c9b12d92fa209` |
| `minimal_partial_clear.bit` | 67,995 | | `69a9b3b1f7f79fced55b0c407a77c94aa767da77f2e5d102380d0ecefe49729e` |
| `minimal_build.json` (receipt) | 3,887 | | `35ee59155a4ff1475694d3f9456f699238bcc5e5a16f805d4180d52fa2963e85` |
| `minimal_routed.dcp` | 41,277,027 | | `3f9080dfe4c4163cec4242a48b5856a1f1b546d452e64a1fd20a2067494f783e` |
| `minimal_synth.dcp` | 16,472 | | `90362824b387f69dd8500f9e2eef2f18461d068a8f67d6dc0cbb769b957492db` |

- The clearing is 67,748 B, against the harness's 1,048,576 B.
- None of these files is committed: `.bit`, `.bin` and `.dcp` never go in git. They are in
  `/tmpdir/claude-74755/kit-night/home/builds/minimal/`.

`kit check ~/builds/minimal` (rc 0, `logs/s08a_kit_check.txt`) gave the runbook's expected first line:
`the build minimal: passed (unchecked is not a pass: see the list)`. It then listed **23 ok and 1 unchecked**:
- `build` (24 gates), `rm_id` 0x0100F28A, `static_id` "built against static 0x44EE76D5";
- `partial` and `clearing` (length and CRC as built);
- the partial's stream checks: header length, part, `PARTIAL=TRUE`, stream to DESYNC (6,239 frames),
  IDCODE `0x0390D093`=SLR0, no IPROG/AXSS/WBSTAR, role `partial`, frame box inside the kit's
  (`{0: rows 0-1, cols 94-200; 1: rows 0-1, cols 6-11}`), vocabulary;
- `bit_bin_pair`;
- the clearing's stream checks (286 frames, role `clearing`, box `{0: rows 0-1, cols 101-193}`);
- `clearing_fits`, `clearing_pairs_partial`;
- `static_binding` *unchecked* (by design: a partial carries no static identity).

With `--static-id 0x44EE76D5` the result is the same.

## 7. Pack, import, and "Programmable"

- `kit pack ~/builds/minimal --import` (rc 0) wrote the overlay triple
  `~/builds/minimal/overlay/minimal/{manifest.json, minimal.bin, minimal_clear.bin, minimal_build.json}`
  (manifest: `build/overlay_manifest.json`; static 0x44EE76D5, rm_id 0x0100F28A, usercode 0xFB1F8C76,
  vivado 2026.1). It imported the triple into the private store as `3f84f88675bf`.
- `harness-manager overlays` needs a TARGET (a live shell), so there is no board-free "is it Programmable"
  (F9).
- `tools/overlays_boardfree.py` runs the MPS3 deploy preflight's own item functions on the catalogue from
  the state dir's store, with a **simulated** live read: shell_id as given, impl linux, `clr_max` from the
  kit. There is no socket and no board.
  - Against `0x44EE76D5`: `shell_id` ok, `crc and length` ok, `clearing pairs partial` ok,
    `clearing fits` 67,748 of 1,048,576 B ok, `static_usercode` *unchecked* (needs JTAG), pyverify
    `Overlay.validate(expected_static_id=0x44EE76D5)` ok. The verdict is **PROGRAMMABLE (board-free items)**.
  - The negative twin, against `0x72BB0A36`: **REFUSED**, `overlay built for shell 0x44ee76d5, the board
    runs 0x72bb0a36`.

**The handover copy for board 2** is on srv03335 at
`/tmpdir/claude-74755/kit-night/handover/minimal_0x0100F28A/`: `out/` (receipt, both `.bin`, both
`.bit`), `overlay/minimal/` (the triple), and `SHA256SUMS` (copied here as `build/handover_SHA256SUMS`).
- It was checked in a **fresh** state dir with no kit: `kit check` passed, `kit pack --import` imported
  it, and the board-free listing says Programmable (`logs/s09f_…`).
- To add it to david's HM: `harness-manager kit pack /tmpdir/claude-74755/kit-night/handover/minimal_0x0100F28A/out/minimal_build.json --import`.
  With the RC2 kit cached, the frame box is checked too.
- Or skip the import: `harness-manager program 192.168.11.101 minimal --overlay-dir /tmpdir/claude-74755/kit-night/handover/minimal_0x0100F28A/overlay`.

## 8. Findings, most likely to trip the clean-account run first

The clean run uses HM `506a5ef`, so it will see F1-F12 as written here, fixed or not.

| # | Step | What HM / the doc says | What really happened | Suggested fix | Owner / state |
|---|---|---|---|---|---|
| **F1** | runbook 4-5 | Step 4: "Do this in the same shell as step 7, **or put it in `~/.bashrc`**". Step 5: the next step should say "next" | `cr` runs `env -i … bash -lc`, and a login shell on srv03335 **never reads `~/.bashrc`**. A marker line in the clean home's `~/.bashrc` did not print. So in step 5's fresh shell, PATH's `vivado` is 2024.1. `kit guide` then shows `NEXT 2 Tools` (`vivado` on PATH is 2024.1) and `5 Build … waits for 2 tools`, with `next: export PATH=/research/…/2026.1/Vivado/bin:$PATH`. That export does not persist between `cr` calls either. `config test tools vivado` says PASS, and `kit build` prints the full 2026.1 path, so the build itself is unaffected | Runbook step 4: "append `source /research/CAD/Xilinx/Vivado/2026.1/Vivado/settings64.sh` to the clean home's **`~/.bash_profile`**", which step 0 already says for "same shell" things. Or prefix steps 4-7 with that `source`. Also say that step 2 Tools shows NEXT if PATH is 2024.1 | guide lead (runbook); not changed here |
| **F2** | runbook 7; HM README; kit README | "about 35 minutes" (runbook); "about 20 min for a small RM with 2 threads" (HM's build README and the web Build card) | **56 min 27 s** wall at load 48-59, with 4 threads and nice 10 (route 28 min, place 10.5 min). The re-run (§10) took **28 min 36 s** at load ~33. Peak RSS 4.7 GiB | Runbook: "35-60 min on a loaded srv03335; start it by 10:00 for the 14:00 slot" | HM text **fixed** (`e43b749`); runbook: guide lead |
| **F3** | 7 (build log) | nothing | **21 CRITICAL WARNINGs** in `build_rm.log`. 15 come from HM's generated `minimal_ooc.xdc`: 14 × `set_false_path: list of objects … contains no valid startpoints/endpoints` for every tied-off group (and the resets), lines 32-52, and 1 × `Port dut_clk is not connected to any net, cannot set HD.CLK_SRC` (line 23: minimal never loads dut_clk). 6 come from the static: `Generated clock u_shell/…/debug_bridge_0/…/tck_i_reg/Q … does not have a valid master clock`. The gates all pass, but an agent reading the log may record these as failures | XDC service (T10): its own header says "-quiet on every port query so a tied-off port that synthesis removed is not an error", but the warning comes from `set_false_path` / `set_property` themselves. Put `-quiet` on those commands for tied-off groups, or omit false paths for groups the design ties off. Runbook/README: say to expect them. The 6 static ones belong to the platform/kit. **Checked tonight** (`probe/xdc_quiet/`): the same build dir, run with `STOP_AFTER=link` and a copy of the XDC with `-quiet` on `set_false_path` and `set_property HD.CLK_SRC`, gave **0** CRITICAL WARNINGs, where the original gives 15 through the same stages. All 14 gates up to the link still passed, including `ooc_clocks` | T10 + guide lead; not changed here. The change touches T10's goldens and `xdc_semantics` (`test_t10_golden.py` compares against the platform's hand-written XDCs) |
| **F4** | kit README (in the zip and in `builds/*/kit/`) | "KNOWN GAP (HM main 36b12df) … step 4 is refused"; step 5 "add `-tclargs RM_SOURCES=xdc/minimal_wrapper_skeleton.sv`"; "Check this copy without HM: `sha256sum -c SHA256SUMS`" | On `506a5ef` step 4 works, and `RM_SOURCES` defaults to the skeleton, so the extra `-tclargs` is harmless but contradicts the runbook ("only if `sources_given` fails"). `SHA256SUMS` is in the zip but not in `kit.json`'s files, so the kit HM exports (`kit fetch --out`, `builds/*/kit/`) has no `SHA256SUMS`, yet it carries that README | Kit v3 README: drop the gap and the RM_SOURCES line; list `SHA256SUMS` in `kit.json` or say "in the zip" | Linux lead |
| F5 | 7 (`rm_timing`) | `your RM's paths (3 registers): setup WNS none (no timed path)…` for a design whose outputs are all constants (synth: 0 registers). The template's own "your RM has no registers" message never showed | The routed DCP shows 3 `FDCE`s, `u_rp_dut/HD_PR_Connection_S_IN_FDCE_{dut_clk,dbg_bscan_tck,phy_rmii_ref_clk}`: flops Vivado inserts on unloaded partition clock inputs (`probe/regs_result.txt`) | `rp_slack` excludes `*/HD_PR_Connection_*` | **fixed** `96f80fa`: checked in Vivado on the routed DCP (`probe/slack*_result.txt`) and in a second full build (§10) |
| F6 | 3 | help: `kit verify DIR` | `kit verify <the kit zip>` exits **15**: `cannot read …zip/kit.json: Not a directory — is this a kit directory?` | Take the zip (import already does) | **fixed** `dddf85d` |
| F7 | 4-7 | `Vivado 2026.1 at /research/CAD/Xilinx/Vivado//2026.1/Vivado/bin/vivado` in `config test`, `kit guide`, `kit script`'s `next:` line and the build README | `settings64.sh` puts `…/Vivado//2026.1/Vivado/bin` on PATH, and HM printed `which`'s result as it came. The command works, but it looks like a typo in the line users copy | normpath | **fixed** `d8a81df` |
| F8 | 6 (build README) | last line: `kit guide … --design <your design .json> --build-dir .` for the built-in `minimal` | a placeholder where the name is known | Name the design (built-in name or file path) | **fixed** `e43b749` |
| F9 | 9 | runbook: "it shows in Program" | `harness-manager overlays` needs a TARGET (exit 2): no board-free way to see that the overlay is Programmable for a static | `overlays --static-id ID` (board-free: files, pair, shell_id, clearing fits; usercode unchecked), as `tools/overlays_boardfree.py` does | HM lead: after 6 Oct (a new verb) |
| F10 | 5, after 9 | the runbook gives no time for `kit guide` | `kit guide` takes **23-57 s per call** at load 50: `vivado -version` runs, then the launch probe (17 s). The probe cache is per process. It is well under the 60 s launch timeout, which in any case gives *unchecked*, not a failure | Runbook: "about a minute" | guide lead |
| F11 | 7 (receipt) | the brief asked for the RP's WNS/TNS | minimal has no RM path, so the receipt's `rm_wns`/`rm_whs` are empty (correct). Only the whole design has numbers (§5) | none. The guide text could say so | info |
| F12 | 7 (pr_verify) | the gates count 148 partition pins | `pr_verify` says "partition pins compared = 102", equal on both DCPs | none (a different count, and compatible) | info |

## 9. HM fixes on `team/kit-night` (not pushed)

| Commit | Fix | Tests (in `tests/unit/test_kit_night.py`, each with its negative twin; each fails on the old code) |
|---|---|---|
| `dddf85d` | `kit verify` takes a kit zip: the same checks as import, nothing cached, the extraction removed. A non-zip is exit 2; a zip with no kit.json gives KitFormatError. Help, helptext and USER_GUIDE updated | good zip / tampered zip; not a zip / no kit.json / plain dir; import still takes the zip; the CLI rc 0 / 15 / 2 |
| `d8a81df` | The PATH `vivado` is normalised (no `//`) | doubled spelling → clean path and command / clean spelling kept / no PATH vivado stays None |
| `e43b749` | The build README names the design (`--design minimal`) and gives the measured time; so does the web Build card | built-in → name / file → path / inline → placeholder |
| `96f80fa` | `build_rm.tcl`'s `rp_slack` excludes Vivado's `HD_PR_Connection_*` flops | the 3 inserted flops → 0 registers, nothing timed / one real flop beside them → 1, only it timed (Python's Tcl, Vivado stubbed); plus the Vivado 2026.1 check on the real routed DCP |

- Gate: `make lint` passes (ruff and shellcheck).
- Port-free tests pass: all `tests/unit/test_kit_*.py`, `test_otac_kits.py`, `test_cli_help_coverage.py`,
  `test_t5_help.py`, and `tests/integration/test_kit_cli.py` minus its 2 virtual-board tests. That is 264
  passed and 2 skipped, plus the new file.
- The full `make test` was **not** run. The virtual board binds 10000-19999, and this lane may not bind
  those ports.

## 10. The branch head, built again end to end

The branch head was built again, all the way through, to show that the four fixes (one of them in
`build_rm.tcl`) still give a passing build. Head `96f80fa`, the same kit and private state, a new
`~/builds/minimal_head` (`logs/r2/`).

| | |
|---|---|
| `kit script` / `kit build --jobs 4` | rc 0. The printed path is now `/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado` (no `//`), and the README says `--design minimal`. The generated `build_rm.tcl` differs from run 1 only in the `rp_slack` change |
| Vivado | 22:11:11 → 22:39:49: **28 min 36 s** at load ~33 (126 % CPU); route 9:39, place 6:36; peak RSS 4.70 GiB; exit 0 |
| Verdict | `HM_RM_BUILD_COMPLETE`, **24 gates** (23 PASS + NOTE ltx). `rm_timing` now says **`your RM has no registers (every output a constant, as minimal's): no path of its own to time`** |
| Timing | the same as run 1: WNS 0.207 / TNS 0.000 / WHS 0.030 / THS 0.000; the route checksum is the same (`2518d3992`) |
| The pair | **byte-identical to run 1**: `minimal_partial.bin` `86e27c2e…4e81`, `minimal_partial_clear.bin` `810683ab…5ce4`. The `.bit` files differ only because their header has the build date |
| `kit check ~/builds/minimal_head --static-id 0x44EE76D5` / `kit pack … --out …` | rc 0 / rc 0 (not imported: run 1's identical pair is already in the store) |

So the same machine gave 28 min at load ~33 and 56 min at load ~50-59: the build time is mostly the
box's load.

## 11. Files here

- `logs/`: every command's output (`s03`…`s09`, by runbook step), the fix re-checks (`fix1`, `fix2`),
  `/usr/bin/time -v`, and the full transcript `kitnight_commands.log`.
- `build/`: what HM generated (`build_rm.tcl`, `README.txt`, the skeleton, the OOC XDC), the Vivado log and
  journal, the receipt, the reports (the timing report is its first 200 lines; the full 240 KB one is in the
  scratch dir), the overlay manifest, and the handover SHA256SUMS.
- `logs/r2/`: the re-run on the branch head (§10): its script/build output, markers, receipt, sha256s and time.
- `probe/`: the Tcl run on the routed DCP for F5, and its results. `probe/xdc_quiet/`: the `-quiet` OOC XDC
  experiment for F3.
- `tools/`: `kn.sh` (the isolation wrapper) and `overlays_boardfree.py`.
