# Build a DUT: from your RTL to a board you can debug

**Lane:** KIT-GUIDE (design only; nothing here is wired into Harness Manager yet).
**Sister lanes:** KIT-STORE owns where the static DCP lives and how it is fetched ("kit fetch"). XVC owns live debug and serving the `.ltx`. T10 owns the XDC kit.
**Status:** 2026-09-24. Spike (b) (the board-free partial validator) is done and tested. The Tcl template has been smoke-run in Vivado 2024.1 (see §9).

david asked: "how we should provide xdc's and the dcp used for building the DUT bitstream from harness manager? … Can we include instructions from within harness manager to help with this?"

## 0. Recommendation

- **Put a "Build" section before "Program" on every board.** It walks a lab user from "I have RTL" to "it runs on mps3-01 and I can debug it". It has six step cards, and Harness Manager (HM) works out each card's state.
- **Make the instructions runnable.** HM generates one Vivado Tcl script, `build_rm.tcl`, per (board pack, static, Vivado version, design). Every step in the guide is a stage in that script, and every check is a gate that prints `HM_GATE <name> PASS|FAIL`. The script writes a **build receipt** (`<rm>_build.json`) that HM reads back.
- **Bind the partial to the static with the receipt, not the bitstream.** A static is identified by the CRC-32 of its locked DCP (that CRC *is* the static_id). The script re-computes that CRC from the file it actually opens, runs `pr_verify` against it, and records both in the receipt.
- **What the partial can prove with no Vivado and no board.** HM validates the partial pair itself: right device, right SLR, right partition frames, partial and clearing not swapped, and no device-global writes. The spike showed this works, and that it cannot tell one static from another.
- **The DCP question.** The static DCP never needs to reach an SD card or be "served into" Vivado. Vivado opens only local files. So HM fetches the kit to the machine that runs Vivado (KIT-STORE's `kit fetch`), and proves the file is the board's static before any Vivado time is spent.

### david's three questions, answered briefly

| Question | Answer |
|---|---|
| How should HM provide the XDCs? | T10's RM kit already does: `xdc rm-kit` writes the OOC XDC, the connectivity sheet, the pblock facts and a wrapper skeleton. The Build guide makes it step 4, and the generated script reads the OOC XDC by path. |
| …and the DCP? Offload it from an SD card and serve it to a Vivado session? | Vivado opens only a local file, so something must copy the DCP to the machine that runs Vivado. *Where the kit is archived* (the hub, a release asset, or the board's own card as a copy of last resort) is KIT-STORE's call. This lane needs three things from it: a local path, the kit facts, and proof that the file is the board's static. HM gets that proof by re-computing the CRC-32, which *is* the static_id, before any Vivado time is spent. |
| Instructions inside HM? | Yes. A "Build" section with live step states, plus `kit guide` on the CLI. The instructions are a generated, runnable `build_rm.tcl` whose gates the guide reads back. |

## 1. The journey

### 1.1 Prerequisites (for the fielded static 0x72BB0A36)

| What | Value | Source / how HM knows |
|---|---|---|
| Vivado | **exactly 2024.1**. The Linux harness (mint 3) will need 2026.1. | A routed DCP opens only in the release that wrote it (2026.1 DCPs do not open in 2024.1). The kit records it; `static_stamp.json` and the `.bit` header say `Version=2024.1`. |
| Part | `xcku115-flvb1760-1-c` | `build_dfx.tcl:78`; the partial's `.bit` header |
| Licence | A licence that covers the KU115, which is probably Vivado ML **Enterprise** (the lab's floating server). DFX itself needs no separate licence in 2024.1. | Not detectable without running synth: the failure is `[Common 17-345] A valid license was not found`. The guide shows this as *unchecked*, and the script's `part_installed` gate only proves the device files exist. **Verify** that KU115 is outside the free ML Standard list. |
| OS | Linux or Windows (the script uses no `exec`, no python and no shell) | template design |
| RAM | ~3 GB to open the static and link it (KIT-STORE's measurement: `open_checkpoint` peak 2.78 GB, 57 s). Plan for 8 GB for a nanoSoC-sized RM. | KIT-STORE spike log; mint timings |
| Disk | The kit is ~10 MB (`static_routed_locked.dcp` 10,152,801 B). It needs no second, reference DCP (§3.3). One build writes 20–40 MB. The Vivado install with UltraScale is tens of GB. | `fielded/0x72BB0A36/mint.json` |
| Time | 10–20 min per small RM, and ~20 min for nanoSoC, at the mint's thread count on this box | `mint.json` routed-DCP timestamps (greybox 13:55, regdemo_a 14:10, regdemo_b 14:23, led 14:35) |

### 1.2 The steps

| # | Step | What the user does | What HM checks | The failure it catches |
|---|---|---|---|---|
| 1 | **Target** | Picks a board, or names a static_id with no board attached | the board's live `shell_id`; is there a kit for (static_id, Vivado)? | building for the static that is *minted* rather than the one *fielded* (`docs/FIELDED_SHELL.md`: "They are routinely not [equal]") |
| 2 | **Tools** | Installs Vivado 2024.1 | the CLI finds `vivado` on PATH, `$XILINX_VIVADO` or the standard install roots, and runs `vivado -version` | wrong release: the DCP will not open, after a download and a wait |
| 3 | **Kit** | `harness-manager kit fetch` (KIT-STORE) | zlib CRC-32 of `static_routed_locked.dcp` == static_id (`build_dfx.tcl:725`); the kit's sha256s | a wrong, stale or corrupt DCP: the whole class behind `fetch_fielded.sh` and the "re-minted by accident" warnings |
| 4 | **Wrapper and XDC** | `harness-manager xdc rm-kit --design my_rm.json` (T10), then fills in the skeleton | the wrapper's ports against the boundary (47 ports / 148 bits: missing port, width, direction); clock periods against partition-timing; `rm_id` unique in the catalogue | the drift that `pin_check` catches, which otherwise fails at link or `pr_verify` after a full synth (`_template/README.md` "Why the boundary is not negotiable") |
| 5 | **Build** | `harness-manager kit script`, then `vivado -mode batch -source build_rm.tcl` (or `kit build`, which runs it) | ~25 gates inside Vivado (§3.2). The receipt records each one. | a black-boxed (empty) RM; the wrong rm_id; a clockless OOC synth; RP pins moved; HDPR-16/18/50; negative slack in *your* paths; `pr_verify` incompatible; a clearing over 256 KiB |
| 6 | **Check** | `harness-manager kit check out/my_rm_build.json` | receipt ↔ files (crc32, len), receipt static_id ↔ the board, a partial-pair stream check (§4), the `.ltx` pairing | a file copied from the wrong build; swapped partial and clearing; a full image offered as a partial; a half-copied file |
| 7 | **Add** | `harness-manager kit pack … --import` ("Add to Program") | writes the overlay triple (manifest per `overlay-manifest.md`) and imports it into the content store, which refuses a bad CRC (`overlays.import_overlay`) | a hand-written manifest claiming a static it was not built against |
| 8 | **Program and debug** | Program (existing), then Consoles, Debug and ILA (XVC lane) | the existing deploy preflight: shell_id, crc/len, pair, clearing fits, transport, USERCODE; after the swap the shell reads rm_id back | everything that is only knowable on the board |

### 1.3 Where users fail today (the platform's own traps)

Each trap below cites where the platform documents it. The last column says which gate now catches it.

| Trap | Where it is documented | Caught by |
|---|---|---|
| **`make prod` to add one RM re-mints** the static, and every fielded overlay stops fitting | `docs/site/docs/guides/adding-an-rm.md` §6 "danger" | The script never writes a static: it CRC-checks the locked DCP before opening it (`static_id` gate), and it writes checkpoints only under `OUT_DIR` |
| **The minted static is not the fielded one** | `docs/FIELDED_SHELL.md` "The two facts are not the same fact" | step 1 targets the board's live `shell_id` |
| **A missing source synthesises as a black box**, and the build "succeeds" with an empty RM | `adding-an-rm.md` §3 (`synth_mode "inline"`), `build_dfx.tcl:145-164` | `no_black_boxes` gate |
| **rm_id in three places drifts** (wrapper localparam, rm_list, manifest) | `_template/README.md` "rm_id, and the three places it must agree" | `rm_id_match`: the id is read out of the netlist's constant drivers, and the manifest is generated from the receipt |
| **A clean timing report because nothing was constrained** | `adding-an-rm.md` §5 and "When it goes wrong" | `ooc_clocks` gate; `rm_timing` is scoped to the RM's own registers |
| **A static-side debug core punches hub ports into the RP** | `tools/debug_probes.tcl:147-165` | `rp_pins_link` and `rp_pins_opt` gates |
| **An ILA with no RM-side hub (HDPR-16), or a BUFG/BSCAN/IO site in the pblock (HDPR-18/50)** | `scripts/harness_gates/check_hdpr_reports.py` | `drc_hdpr_link` and `drc_routed` |
| **A stale `.ltx` shipped with a new partial**; a full-design `write_debug_probes` scatters files | `tools/debug_probes.tcl:67-77`, `:6-11` | the `.ltx` is written `-cell`, from the same open routed design, and its crc goes in the receipt |
| **A clearing bigger than the 256 KiB arena** is staged to QSPI, and the next swap-away fails closed | HM `harness_manager_mps3/deploy.py` (d); `firmware/platform/Makefile` | `clearing_fits`, in the script and again in `kit check` |
| **Loading a partial onto a different static implementation destroys the FPGA configuration** (twice on 2026-07-24), and `static_id` alone cannot see it | `fpga/dfx/gen_manifest.py:188-199` | receipt `static_id` (CRC of the opened DCP) + `pr_verify`; deploy preflight (f) USERCODE when JTAG is present |
| **Vivado exits 0 after a Tcl error** | `tools/debug_probes.tcl:17`; the Makefile greps markers | markers: `HM_RM_BUILD_COMPLETE` / `HM_RM_BUILD_FAILED gate=…`. The receipt's `state` is the verdict. |
| **Vivado's python breaks `exec python3`** | `build_dfx.tcl:442-457` | the script has no `exec` |
| **Generated SoC RTL drifts, or regeneration wipes edits** (nanoSoC's `nanosoc.sv`, patched at build time) | `fpga/rp/nanosoc/ooc_synth.tcl:38-47` | not gateable. The receipt should carry the sha256 of each source (CCR KG-6), and the guide's troubleshooting says "edit the generator's template, not its output". |
| **The docs lag the boundary** (the guide says 35 ports; it is 47) | `adding-an-rm.md:22,146` vs `boundary.yaml` | HM's boundary comes from the pin model, which T10's generator derives from `boundary.yaml` and holds to the fielded hashes |
| **A register-width assumption** (the BD instantiates width 32, not the RTL default) | memory: CSR decode regression | the `width` check on the wrapper; `NGPIO` is fixed at 16 by the boundary |

## 2. How a partial is bound to a static (the chain)

Every link in this chain is a check that a machine makes:

```
board ──ping──▶ shell_id 0x72BB0A36
                     ║ equals
kit  static_routed_locked.dcp ──zlib.crc32──▶ 0x72BB0A36     (kit fetch, and the script's static_id gate)
                     ║ the same file
build  open_checkpoint + read_checkpoint -cell + route        (the script)
       pr_verify <ref> <routed>  ──▶ "are compatible"         (pr_verify gate)
       receipt { static_id: CRC of the file it opened, partial_crc32, clearing_crc32, rm_id_netlist, gates }
                     ║
check  crc32(partial.bin) == receipt.partial_crc32 ; stream checks (§4)
pack   manifest.json { static_id, rm_id, crc32s, static_usercode } ── import_overlay (refuses a bad CRC)
                     ║
board  deploy preflight: shell_id == static_id ; crc/len ; pair ; clearing fits ; (USERCODE via JTAG)
       swap ──▶ the shell reads DFXCTL.RM_ID back == rm_id
```

The partial bitstream cannot carry this binding itself: every `-cell` write says `UserID=0XFFFFFFFF`, and the partition's frame box is identical on all three statics measured (§4). That is why the receipt exists. A hand-made manifest can claim any static_id. A receipt's static_id is a CRC that the build computed from the bytes it opened.

## 3. The generated Tcl (`docs/design/build_rm.tcl.template`)

### 3.1 Shape

- **A parameter block.** HM fills it from the kit and the design: part, RP instance, boundary bits, static_id, USERCODE, the DCP path, the pr_verify reference, the clearing limit, the RM's name/id/top/sources/hooks/OOC XDC/RM XDC, the output directory, jobs, and a stop-after stage.
- **Overrides.** Any parameter can be overridden with `-tclargs NAME=VALUE`, so `kit build` does not need to re-render.
- **Six stages:** preflight → synth → link → impl → verify → bitstream. `STOP_AFTER` ends the run early; this is how the guide's "Check my wrapper in Vivado" button runs synth only.
- **Every gate prints `HM_GATE`, and a failure writes the receipt before it errors.** A failed build still tells HM which gate failed.
- **It needs nothing but Vivado.** There is no `rm_list.tcl`, no Makefile, no python and no platform checkout.

### 3.2 Stages, gates and where each comes from

| Stage | Gate | Derived from (platform `feat/rm-ila-mint` @ e5436302) |
|---|---|---|
| preflight | `vivado_version`, `part_installed`, `static_dcp_present`, **`static_id`** (CRC-32 of the DCP), `rm_id_format`, `rm_id_nonzero` (0 is the greybox), `source_present`, `ooc_xdc_present` | `build_dfx.tcl:420-430` (`file_crc32`), `:725` (static_id scheme); `:74-76` (refuse a missing DCP) |
| synth | `no_black_boxes`, **`boundary_bits`** (OOC port bits == 148), **`rm_id_match`** (rm_id read from the netlist's GND/VCC drivers), `ooc_clocks` | `build_dfx.tcl:166-187` (OOC synth); `_template/ooc_synth.tcl:50-89` (read the OOC XDC after `synth_design`); `boundary.yaml totals.bits` via `debug_probes.tcl:130-145` |
| link | `rp_cell`, **`rp_pins_link`**, `clocks_after_link`, **`drc_hdpr_link`** | `build_dfx.tcl:224-244` (open the static, black-box a stub, `read_checkpoint -cell`); `:250-256`; `:279-283`; `:341-349` (`read_xdc -cell` for RM-internal timing); `:386` (`PERSIST NO`); `:390-395` + `check_hdpr_reports.py` |
| impl | `rp_pins_opt`, `drc_routed`, **`rm_timing`** (worst setup/hold over paths that start or end in the RP) | `build_dfx.tcl:397-408`; `debug_probes.tcl:147-165` |
| verify | **`pr_verify`** (parses "are compatible") | `build_dfx.tcl:551` (incremental add), `:739`; the report format in `prod_results_2026-07-06-realshell/pr_verify_rm_led.rpt` |
| bitstream | `ltx_written`, `artefact` ×4, **`clearing_fits`** | `build_dfx.tcl:555` (`write_bitstream -force -bin_file -cell`), `:559-566`; `debug_probes.tcl:63-120` (`write_debug_probes -cell`, delete `_clear.ltx`) |

**What a DUT build is.** It is the platform's *incremental add* (`build_dfx.tcl:518-603`, `make add-rm-%` at `Makefile:524-560`): link one RM into the already-locked static, route only the RM, `pr_verify` it, and write the pair. static_id is read, never re-minted. The template is that path, taken out of the mint machinery.

**Deliberately not carried over:**
- USR_ACCESS/USERID stamping. It is full-image only (`build_dfx.tcl:809-815`), and a partial must never write AXSS; §4 checks that.
- The floorplan and the shell's implementation-time XDC. Both are baked into the locked DCP (`build_dfx.tcl:372-379`, `:292-293`).
- The readiness filter and `rm_list.tcl`. The user's RM is the only RM.

### 3.3 What `pr_verify` compares against

The platform compares against `config_rm_greybox_routed.dcp` (`Makefile:546`). That is a second 11 MB DCP in every kit. The template takes `PR_VERIFY_REF` and, when it is empty, falls back to the locked static itself.

**KIT-STORE's spike (b2) answered the positive case.** `pr_verify static_routed_locked.dcp config_rm_led_routed.dcp` reports `[Vivado 12-3253] … are compatible`, just as the flow's own greybox-vs-led pair does; that pair took 99 s. So the kit can be a single DCP.

**KIT-STORE's negative settled the rest.** It compared the locked 0x3F1A560F static with a routed config of 0xA8C1C535:
- Vivado reports `[Constraints 18-891] HDPRVerify-08: … places instance u_shell/…/BUFG_DRCK … at site BUFGCE_X1Y118, yet … does not`, then `[Vivado 12-3515] … are not compatible`;
- `pr_verify` itself raises `[Common 17-39] 'pr_verify' failed due to earlier errors`.

So the locked static both accepts the right static and refuses a wrong one. **The kit needs one DCP** (D2). The template's `pr_verify` gate treats a Tcl error or "not compatible" as FAIL, and needs "are compatible" to pass. (This lane had queued the same negative on 0x72BB0A36 and cancelled it, so as not to load the DCP twice.)

### 3.4 The receipt (`<rm>_build.json`, schema `harness-manager-rm-build` v1)

The script writes it after a pass, a failed gate or `STOP_AFTER`. It holds:
- `state`: passed | failed | stopped;
- `stage`, `kit_id`, `vivado`, `part`, `rp_inst`;
- `rm_name`, `rm_top`, `rm_id`, `rm_id_netlist`;
- `static_id` (CRC of the opened DCP) and `static_usercode` (from the kit);
- `rm_wns` and `rm_whs`;
- `pr_verify_ref`;
- `partial_bin`, `partial_len`, `partial_crc32`, `clearing_bin`, `clearing_len`, `clearing_crc32`;
- `ltx` and `ltx_crc32`;
- `built`;
- `gates`: a list of `{gate, verdict, detail}`.

HM derives the overlay manifest from it. Nobody writes a manifest by hand.

## 4. Validation before deploy, without Vivado (spike (b))

`tools/spike_kit_guide/partial_check.py` reads a `.bit` or `.bin`. Its packet walker always skips a packet's payload by its count, so frame data is never read as headers (the failure mode `tools/bit_identity.py` warns about). A stream it cannot walk to DESYNC is reported as unparsed, never as clean.

**Measured facts.** These come from 7 RMs on 3 statics (0x3F1A560F, 0xA8C1C535, 0x72BB0A36), plus the fielded full image:

- **Every partial writes only SLR0**: IDCODE `0x0390D093`. The full image writes SLR0 and SLR1 (`0x03902093`), and passes SLR1's stream through register 0x1E.
- **Every partial's frames are block type 0 at rows 0–1, columns 94–200, plus block type 1 (BRAM content) at columns 6–11.** Every clearing is block type 0 only, rows 0–1, columns 101–193. Every stream ends at FAR `0x03BE0000` (block type 7, not a frame).
- **The two roles have different commands.** A partial issues `GRESTORE` + `START` and never `AGHIGH`. A clearing issues `AGHIGH` and never `START`.
- **SHUTDOWN, AGHIGH, GRESTORE and COR0 are normal here.** A validator that refused them would refuse every silicon-proven partial on this platform.
- **The `.bit` header** carries `PARTIAL=TRUE;COMPRESS=TRUE;Version=2024.1`, the part, the date, and `UserID=0XFFFFFFFF`.

| Check | Catches | Verdict on the samples |
|---|---|---|
| `.bit` header length, part, `PARTIAL=TRUE`, Vivado version | a full image offered as a partial, the wrong device | N1 and N6 refused |
| stream walks to DESYNC | a truncated or half-copied file | N3 refused |
| IDCODE is one known SLR of the part | another device; a multi-SLR image | N1 refused |
| no IPROG, AXSS, WBSTAR or SLR pass-through | a partial that would reboot or restamp the FPGA | N1 refused |
| role (partial vs clearing) from the commands | **swapped files**, which the deploy's name-based pair check cannot see | N2 refused |
| frame box inside the kit's reference partial; command/register vocabulary a subset of it | another partition or another device | N1 refused |
| `.bin` payload == `.bit` payload | a `.bin` from another build | N4 refused |
| clearing ≤ the arena; clearing frames inside the partial's | QSPI staging; a clearing from another RM's partition | N5 and N2 refused |
| **static binding** | nothing: *unchecked*. A partial of 0xA8C1C535 passes against a 0x72BB0A36 reference (N7). | honest limit |

Runtime is 0.4 s for a 2.3 MB partial (Python 3.11, stdlib only).

## 5. The in-app guide

### 5.1 Where it lives

- **A new board section, "Build".** It sits between XDC and Program in `SECTIONS` (`web/static/js/app.js:28`). The journey reads left to right: XDC → Build → Program → Consoles/Debug.
- **The RM-kit step embeds T10's kit picker, and XDC stays as it is.** XDC remains the full-board export and the RM-kit reference.
- **It also works with no board.** It can target a named static_id, and CLI `kit guide --static-id 0x72BB0A36` does the same, because building needs a static, not a board.

### 5.2 Step cards and their states

States are `done` · `next` · `blocked` · `failed` · `unchecked`, driven only by what HM can detect:

| Card | done when | blocked / failed shows |
|---|---|---|
| 1 Target | the board reported `shell_id` and a kit exists for it | "no kit for 0x… (Vivado …)": ask the lab, or pick a static that has one |
| 2 Tools | `vivado -version` = the kit's version (CLI/local daemon only) | the version found and the one needed. The licence is always shown *unchecked*, with the error text to watch for. |
| 3 Kit | the kit is fetched and its CRC == static_id | "the file is not this static": re-fetch (KIT-STORE) |
| 4 Wrapper & XDC | the design passes every XDC-kit check with its `wrapper` | the first failing check, with its hint (T10's findings) |
| 5 Build | a receipt with `state: passed` exists in the chosen out dir | `failed`: the gate, its detail and the troubleshooting card below. `stopped`: "finish the build" |
| 6 Check & add | `kit check` passes and the overlay is in the store (it shows in Program) | the failing check. `unchecked` rows stay listed. |

After step 6 comes Program → Consoles/Debug.

### 5.3 Mock-up (web)

```
┌ mps3-01 · static 0x72BB0A36 · harness 1.0.0 ───────────────────────────────┐
│ Overview  Program  Consoles  Debug  …  XDC  [Build]  Activity               │
├─────────────────────────────────────────────────────────────────────────────┤
│ Build a DUT for this board                                  kit: mps3/0x72BB0A36/vivado-2024.1 │
│                                                                             │
│ ✓ 1 Target    static 0x72BB0A36 (fielded 2026-09-24), partition u_rp_dut    │
│ ✓ 2 Tools     Vivado 2024.1 at /apps/Xilinx/Vivado/2024.1  · licence: unchecked (?) │
│ ✓ 3 Kit       static_routed_locked.dcp  CRC 0x72BB0A36 = board ✓           │
│ ✓ 4 Wrapper   my_rm.json · 47/47 ports · rm_id 0x010080F0 (unused)  [XDC ▸] │
│ ▶ 5 Build     ┌───────────────────────────────────────────────────────────┐ │
│               │ vivado -mode batch -source build_rm.tcl -log build_rm.log │ │
│               └───────────────────────────────────────────── [Copy] ──────┘ │
│               [Download build_rm.tcl + kit files]   [Run here]  (Vivado found locally) │
│               Out dir: ~/work/my_rm/out   [Watch]                           │
│               preflight ✓  synth ✓  link ✓  impl ●  verify ○  bitstream ○    │
│ ○ 6 Check & add   waits for the receipt                                     │
│                                                                             │
│ When it goes wrong ▾   (one row per gate; the failed one opens itself)      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 5.4 Troubleshooting, one card per gate

Each card says what the gate means, the likely cause, and the fix. Each cites a source.

| Gate | Most likely cause | Fix |
|---|---|---|
| `vivado_version` | another Vivado on PATH | run the version the kit names. A 2026.1 kit needs 2026.1: Vivado 2024.1 refuses it with `[Runs 36-378] The checkpoint … was created with 'Vivado v2026.1 (64-bit)', and cannot be opened in this version` (KIT-STORE spike). |
| `static_id` | the wrong or a corrupt DCP | `kit fetch` again. Never rebuild the static (`adding-an-rm.md` §6). |
| `no_black_boxes` | a source file missing from `RM_SOURCES`, or a filelist | add it. The listed cells name the missing modules. |
| `boundary_bits` | the wrapper's port list was edited | start again from the kit's skeleton. The XDC step names the port. |
| `rm_id_match` / `rm_id_constant` | the localparam differs from the design's `rm_id`, or `rm_id` is driven by logic | drive `rm_id` from one constant. HM writes the manifest from the netlist value. |
| `ooc_clocks` | the OOC XDC was not read, or it names no clock | use the kit's `<name>_ooc.xdc` |
| `rp_pins_link` / `rp_pins_opt` | the boundary changed, or a static-side debug core exists | the wrong kit for this static; tell the lab (`debug_probes.tcl:147-165`) |
| `drc_hdpr_link` / `drc_routed` | HDPR-16: an ILA with no RM debug hub. HDPR-18/50: a BUFG, MMCM, BSCAN or pad in the RM. | ILAs need the `dbgbscan` group and an RM-side mode-1 debug_bridge (`fpga/rp/common/rp_dbg_hub.sv`). Generate no clocks in the RM (`_template/README.md` rules 1–2). |
| `rm_timing` | real negative slack in your RM, or an RM-internal async crossing or generated clock left unconstrained | add `RM_XDC` (`read_xdc -cell`, `build_dfx.tcl:311-349`; `fpga/rp/eth_ss/eth_ss_rm.xdc` is the worked example). `ALLOW_TIMING_FAIL=1` is for experiments only. |
| `pr_verify` | the static in the build is not the reference | fetch the kit again, and do not mix kits |
| `clearing_fits` | the RM is too large for the harness's clearing arena | shrink the RM or ask for a harness with a larger `clr_max` |
| (synth) licence error `[Common 17-345]` | no licence for KU115 | point `XILINXD_LICENSE_FILE` at the lab server |

### 5.5 The CLI path

```
harness-manager kit guide   [TARGET | --static-id ID] [--vivado VER]   # the steps, their state, the next command
harness-manager kit fetch   …                                          # KIT-STORE
harness-manager kit script  --design my_rm.json --out build/my_rm [TARGET | --static-id ID]
                                                                       # build_rm.tcl + OOC XDC + skeleton + README
harness-manager kit build   build/my_rm [--stop-after synth] [--jobs 2] # runs Vivado locally; streams HM_STAGE/HM_GATE
harness-manager kit check   build/my_rm/out/my_rm_build.json [TARGET]  # receipt + files + stream checks (+ live shell_id)
harness-manager kit check   PARTIAL.bin --clearing CLEAR.bin [--static-id ID]   # no receipt: stream checks only
harness-manager kit pack    build/my_rm/out/my_rm_build.json [--import] [--out DIR]  # the overlay triple, into Program
```

`kit guide mps3-01` (sketch):

```
Build a DUT for mps3-01 (static 0x72BB0A36, kit mps3/0x72BB0A36/vivado-2024.1)
  done   1 target    the board runs 0x72BB0A36 (fielded 2026-09-24)
  done   2 tools     Vivado 2024.1 at /apps/Xilinx/Vivado/2024.1/bin/vivado   licence: unchecked
  done   3 kit       ~/.cache/harness-manager/kits/mps3/0x72BB0A36/2024.1  CRC-32 0x72BB0A36 = board
  done   4 wrapper   my_rm.json: 47/47 ports, clocks match, rm_id 0x010080F0 unused
  FAILED 5 build     gate rm_timing: your RM's paths: setup WNS -0.412 ns (receipt build/my_rm/out/my_rm_build.json)
                     fix: an RM-internal async crossing needs RM_XDC (read_xdc -cell); see `kit guide --why rm_timing`
  -      6 check     waits for a passed build
next: fix the timing, then: harness-manager kit build build/my_rm
```

- Exit codes follow `core.errors`: 0 passed; 15 REFUSED for a failed check, with every check in `--json`; 12 UNAVAILABLE when no Vivado or kit is found.
- `kit guide` prints the steps as TSV/JSON like every other verb.

## 6. Product surface

### 6.1 API (additive, `daemon/kit_api.py`, one `register(ctx)` like the other extension routers)

| Method and path | Returns |
|---|---|
| `GET /boards/{bid}/guide` · `GET /guide?pack=&static_id=&vivado=` | `{kit, vivado: {found, version, path} or null, steps: [{id, title, state, detail, reason?, actions: [{kind: copy\|download\|run\|link, …}]}]}` |
| `POST /guide/script` `{pack?, static_id?, vivado?, design, paths?}` | `{files: {build_rm.tcl, <name>_ooc.xdc, <name>_wrapper_skeleton.sv, README.txt}}`, or `format: "zip"` |
| `POST /guide/build` `{dir, stop_after?, jobs?}` | 202 job `kit_build`. Only when the daemon is loopback and found Vivado locally; never through a hub. Events: `job.progress {phase: stage, done, total: 6}` and `kit.gate {gate, verdict, detail}`. |
| `POST /overlays/check` (body: zip of receipt + pair [+ ltx]) | `{passed, checks: [...], facts}`. It never touches a board. |
| `POST /overlays/import` (same body) | `{overlay: OverlayRef}` after the same checks; 409 REFUSED with `error.data.checks` |

A file-path body is refused, as the XDC routes refuse one, because the daemon's filesystem is not the caller's. The one exception is the loopback `guide/build` job.

### 6.2 Core vs the MPS3 pack

| Core (board-agnostic) | MPS3 pack |
|---|---|
| guide engine (steps, states, actions), receipt schema and checker, template renderer | `BuildProfile` for a static: part, `rp_inst`, boundary bits/ports, `clr_max`, Vivado version, kit id |
| Xilinx packet walker (UltraScale family: IDCODE per SLR, FAR fields) | the reference frame box and vocabulary from the kit's greybox pair; the known IDCODEs |
| Vivado discovery, `kit build` runner, marker parser | troubleshooting entries that name shell facts (dbgbscan, `clr_max`, the decoupler's safe values) |
| `/guide`, `/overlays/check`, `/overlays/import`, the Build section | the overlay format (`pyverify.overlay`), deploy (T2), the pin model (T10) |

A board pack opts in with one hook, `pack.dut_build_profile(static_id) -> BuildProfile | None`. A pack without it gets a Build section that says why.

### 6.3 CCRs (other owners' files; not edited by this lane)

| # | Owner | Change |
|---|---|---|
| KG-1 | lead (`core.pack`) | `BuildProfile` and the optional `dut_build_profile` hook |
| KG-2 | T10 (`cmd_xdc` wiring, in flight) | `xdc rm-kit` gains `--wrapper FILE`, so the check can run on a user's own wrapper from the CLI. The service already supports `wrapper` (verified on the spike RM and on a negative case). |
| KG-3 | T2 (`overlays.py`) | `import_overlay` stores the `.ltx` and the receipt next to the pair. Today the store drops the `.ltx` (`_StoredOverlay.ltx_path` returns None), so a user's ILA build loses its probes on import (XVC lane). |
| KG-4 | KIT-STORE | the kit contract the guide needs: `static_routed_locked.dcp` (+ optional reference routed DCP), `static_id`, `static_usercode`, Vivado version, part, `rp_inst`, the greybox partial + clearing `.bin` (1.3 MB, for §4), a `kit.json` with sha256s |
| KG-5 | lead (`web/app.js` SECTIONS) | the Build section between XDC and Program |
| KG-6 | this lane (template v2) | the receipt records the sha256 of each RM source and the synth-time warning counts (e.g. `[Synth 8-3848]` net has no driver, which is how the nanoSoC exp_* bug showed: `fpga/rp/nanosoc/ooc_synth.tcl:28-35`) |

## 7. Decisions for david

**D1. Where Vivado runs.**
1. *(recommended)* On the user's machine. HM generates the script, and `kit build` / "Run here" drive a local Vivado. The hub never runs Vivado for users.
2. HM on the hub or a build server runs Vivado as a job for anyone. Users need no install, but it adds licence and load contention on an already overloaded box, plus a queue.
3. CI (GitHub runners). There is no Vivado or licence there. Not viable.

**D2. The `pr_verify` reference.**
1. *(recommended; KIT-STORE's spikes show it accepts the right static and refuses the wrong one)* The locked static itself: the kit is one DCP.
2. Ship `config_rm_greybox_routed.dcp` as well (+11 MB), as the platform does.
3. Skip `pr_verify` for users and trust the link. Not recommended: it is the only Vivado-side proof that the static in the build is the fielded one.

**D3. User rm_id allocation.**
1. *(recommended)* HM proposes a design_id from a reserved user range (e.g. `0x8000–0xFFFF`), warns on a clash with the catalogue, and never needs `rm_list.tcl`.
2. Users register in the platform's `rm_list.tcl` (a PR per user design).
3. No allocation, first come first served. A clash confuses names only, because the shell verifies the full rm_id after the swap.

**D4. Where the guide lives.**
1. *(recommended)* A new "Build" board section, plus `kit guide` on the CLI.
2. A tab inside XDC.
3. The CLI only, with docs.

## 8. Lane plan (after david's decisions)

| Lane | Scope | Owns | Est. |
|---|---|---|---|
| KG-A Engine | `services/kitguide/`: `BuildProfile` use, step states, template renderer (the template moves into package data), receipt parser, Vivado discovery | new files | 5 h |
| KG-B Validator | `partial_check` → `services/kitguide/bitstream.py` with the golden fixtures (a trimmed greybox pair, negatives N1–N7), plus `kit check`/`kit pack` on `import_overlay` | new files + CCR KG-3 | 4 h |
| KG-C CLI + API | `cli/cmd_kit.py` (guide, script, build, check, pack), `daemon/kit_api.py` (guide, script, build job, overlays check/import) | new files; the lead wires them | 5 h |
| KG-D Web | `sections/build.js`: step cards, copy/download/run, live gates from job events, troubleshooting cards; browser test | new file + CCR KG-5 | 6 h |
| KG-E Silicon | one real user-style RM (the spike RM) built with the generated script, packed, programmed on mps3-01, rm_id read back; needs a board window | evidence | 2 h + board slot |

Total: ~22 h of agent time, runnable as four parallel lanes (A first, then B–D against its interfaces), plus one board slot.

## 9. Spike results

See the hand-back and `tools/spike_kit_guide/`. (Filled in below when the Vivado smoke-run completes.)
