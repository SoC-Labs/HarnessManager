# The DUT build kit: where it lives and how it reaches Vivado

**Lane KIT-STORE, 2026-09-24.** Board-free design, with one Vivado spike. Sister lanes: **KIT-GUIDE** owns the user journey and the in-app instructions; **XVC** owns serving `.ltx` files and XVC debug to a running board. This page owns where the build inputs live, how they get onto the user's machine, and what the mint must publish.

david's question: *"How should we provide the XDCs and the DCP used for building the DUT bitstream from Harness Manager? Should the DCP be downloaded onto one of the SD cards so it can be offloaded and served from Harness Manager to a Vivado session? Can we include instructions from within Harness Manager to help with this?"*

## 1. The answer

**No, the DCP should not go on an SD card as its home.**

- The **MCC SD** is ruled out. The FPGA cannot read it, and it is reachable only as USB mass storage from the host on the Debug USB. Its writes are the known slow, corruption-prone path (§4.3).
- The **user microSD** is only usable under the Linux harness (mint 3 onwards), and only in `/persist`. The D13 store (32 MiB, 8 MiB slots) and the bare-metal harness have no room for it. Served from there, a kit moves at about 0.5 MB/s through one 100 MHz hart that is also running the harness.

**The recommendation:**

1. Publish a **per-static kit** as a signed channel asset next to the release that fields that static.
2. HM fetches it into its **content-addressed cache**, keyed by `static_id` + sha256.
3. HM writes a plain directory that Vivado opens locally.

The board's only job is to say which kit it needs: `static_id` **is** the CRC-32 of the locked static DCP's bytes, which the spike checked on four real DCPs (§11). So integrity costs one CRC. The hub mint directory stays the archive of record, and a second source for lab users. A copy served by the board (B) is an optional later fallback for boards that are offline by policy; nothing needs it today.

In-app instructions: yes. KIT-GUIDE writes them. This page gives them the data to show (§6).

## 2. What a DUT developer needs

Three layers. Only the first comes from the mint, and only the first is large.

| Layer | Made by | Changes when |
|---|---|---|
| **Static kit** (the locked static DCP and its identity) | the mint | every mint (new `static_id`) |
| **Design constraints** (OOC XDC, wrapper skeleton, connectivity, pblock facts) | HM `xdc rm-kit` (T10), from the pin model | the boundary or the design changes |
| **The user's RM** (their synth DCP, `_rm.xdc`, sources) | the user | every build |

### 2.1 The artefact table

Sizes are measured on real files (read-only; the compression was run on `/tmp` copies).
- **0x72BB0A36** (fielded, bare-metal, Vivado 2024.1): sizes from its `mint.json`. Its DCP exists only on the hub and in the mint worktree, which this lane may not touch.
- **0x3F1A560F**: the previous fielded static, measured locally.
- **0x61BC6789**: the Linux P-mint, Vivado 2026.1, and the stand-in for mint 3.

| File | 0x72BB0A36 | 0x3F1A560F (measured) | 0x61BC6789 Linux (measured) | zstd -19 | Vivado lock | Redistributable? |
|---|---|---|---|---|---|---|
| `static_routed_locked.dcp`: routing-locked, RP black-boxed; **CRC-32 = static_id** | 10,152,801 B | 10,027,747 B | 37,912,459 B | −1.5 % / −1.7 % | exact: 2024.1 (checkpoint v22, build 5076996) / 2026.1 (v26, build 6511674) | No Arm IP. It holds AMD secured IP (encrypted in the DCP) and SoC Labs shell IP from a private repo: **david decides (D2)** |
| ~~`config_rm_greybox_routed.dcp`~~: the `pr_verify` reference the flow uses today. **Not needed in the kit:** the spike shows the locked static is an equivalent `pr_verify` reference (§11) | (10,995,256 B) | (10,812,075 B) | (39,733,476 B) | −3.0 % / −3.2 % | exact, as above | as above |
| `static_stamp.json`: `usercode` for the overlay manifest's `static_usercode` | 507 B | 507 B | 507 B | — | — | yes |
| `mint.json`: provenance (`static_canon`, part, Vivado) | 18,314 B | 17,110 B | 14,464 B | — | — | yes (holds build paths) |
| Static `.ltx` + sidecar (the static's own debug cores) | none | none | 851 B + 1,047 B (MIG debug hub + DDR4) | — | exact | yes; **XVC lane** serves it |
| RM `.ltx` + sidecar (per overlay) | 2 RMs, per overlay | — | per overlay | — | exact | travels with the overlay, not the kit (XVC lane) |
| `boundary.yaml`, `rp_dut_stub.sv`, partition-timing clocks | 11.8 KB + 4.6 KB + (JSON extract) | same | same | — | none | yes |
| `build_rm.tcl`, standalone (**does not exist yet**, F2) | ~20 KB | | | — | exact | yes |
| `gen_manifest.py` (stdlib only) | 18 KB | | | — | none | yes |
| HM-generated: `<rm>_ooc.xdc`, `_wrapper_skeleton.sv`, `_connectivity.{md,csv}`, `_pblock.md` | tens of KB | | | — | none | yes |
| **Kit total** (without the reference routed DCP) | **≈ 10.2 MB** | ≈ 10.1 MB | **≈ 38 MB** | ≈ −2 % | | |

These are **not in the kit**:
- `shell_harness.xsa` (1.1 MB): the firmware BSP needs it, an RM build does not;
- `.mmi` and the flashable `.bit`: the release bundle carries them;
- `shell_static_synth.dcp`: only the next mint needs it;
- `dfx_floorplan.xdc` and `mps3_harness_timing.xdc`: both are **inside** the locked DCP. The flow only reads them for the reference config, `build_dfx.tcl:373` and `:293`.

**The kit does not compress.** A DCP is already a zip, and its netlist members are encrypted (`XlxV18EB` headers). zstd -19, xz -9 and gzip -9 all save 1.5–3 %. Ship the files raw; the transfer planning below uses raw sizes.

### 2.2 The Vivado version lock

- `dcp.xml` inside each checkpoint names the release:
  - 0x3F1A560F: `Vivado v2024.1`, `Checkpoint Version="22"`;
  - 0x61BC6789: `Vivado v2026.1`, `Checkpoint Version="26"`.
- Vivado 2024.1 **refuses** the 2026.1 DCP: `[Runs 36-378] … cannot be opened in this version` (spike S7, §11).
- AMD's DFX rule is that every configuration is implemented in the static's release (UG909). So a kit pins **one exact Vivado release**, and HM must say which one before the user spends an hour in OOC synthesis.
  - The fielded 0x72BB0A36 needs 2024.1.
  - Mint 3 needs 2026.1.
- The part is `xcku115-flvb1760-1-c`. An implementation licence for KU115 is needed. It is **not** in the free Standard edition's device list as far as this lane knows; KIT-GUIDE should verify that before telling users.

### 2.3 What is inside the static, for the licensing question

- **Bare-metal static:** the IP list is from `fpga/shell/bd/shell_bd.tcl` on `feat/rm-ila-mint`, and the spike's cell list agrees with it.
  - AMD IP: `microblaze`, `mdm`, `axi_emc`, `axi_hwicap`, `axi_quad_spi`, `axi_intc`, `axi_timer`, `axi_timebase_wdt`, `axi_uartlite`, `axi_interconnect`, `axi_clock_converter`, `clk_wiz` ×2, `debug_bridge`, `dfx_decoupler`, `dfx_axi_shutdown_manager`, `blk_mem_gen`, `lmb_*`, `proc_sys_reset`.
  - SoC Labs IP (`soclabs.org:user:*`): `dfx_ctl`, `uart_bridge`, `jtag_bb`, `telem`, `clcd`, `clcd_kvm`, `board_gpio`, `dut_clkrst`, `dut_egress`, `eth_mac_test_subsystem`, `usr_access_rd`.
- **Linux static** (`feat/linux-harness`): the same, plus MicroBlaze-V (`cpu_mbv.tcl`), the DDR4 MIG (`ddr4_mig.tcl`) and `usd_spi`.
- **No Arm IP in either static.** The Arm cores (nanoSoC, multicore, upy, eth_ss) live in RMs, and david's 2026-09-23 strategy already keeps those in a private overlay repo.
- **AMD secured IP is present.** In the spike, `write_edif` of the opened static wrote the netlist plus **1,051 separate `.edn` files, each Xilinx-encrypted** (`axi_jtag`, `bsip`, `dfx_decoupler`, `blk_mem_gen`, …). Anyone with Vivado can open the DCP and link against it, and those cells stay encrypted.
  - Distributing a locked static DCP to Vivado users is how AMD's own DFX platforms work: Vitis/Alveo platforms ship theirs inside the platform `.xsa`.
  - G6 of the handover already asks someone to confirm AMD's terms. Add "static DCP" to that question.
- **What a DCP reveals:**
  - the **SoC Labs shell netlist**, readable in Vivado (it comes from a private repo);
  - **absolute build paths**: the XDC members of the DCP are plain text and carry `cfile:/home/dam1n19/SoCLabs/...`.
  - No hostnames, keys or addresses: a grep of all four XDC members for `mapstone|passw|token|secret|192.168` found 0 hits.
  - These two points are the whole licensing decision (D2).

## 3. The one fact that simplifies everything

`build_dfx.tcl:725` defines `static_id = CRC-32 of the raw bytes of static_routed_locked.dcp`. The spike checked four real files, and each matches its own `static_id.txt`:

```
0x3F1A560F  10,027,747 B  crc32=0x3F1A560F
0x61BC6789  37,912,459 B  crc32=0x61BC6789
0x2B082E1B  33,167,177 B  crc32=0x2B082E1B
0xA8C1C535   9,579,088 B  crc32=0xA8C1C535
```

The board already reports `static_id` (`BoardIdentity.shell_id`, from `ping`/`version`), so:

- **"Which kit does this board need?"** is `identity().shell_id`. Nothing new is needed on the board.
- **"Is this the right DCP?"** is one CRC-32 on the host. It is exact for accidental mismatch; sha256 in the signed channel covers tampering.
- **Integrity does not depend on where the file came from.** Every source (channel, hub, board, a colleague's USB stick) is checked the same way.

`usercode` (`static_stamp.json`) binds overlays to the implementation run, and the board reports it (`BoardIdentity.usercode`). The kit carries it, so a user-built overlay manifest gets the right `static_usercode` and passes the deploy preflight.

## 4. Where the kit should live: the options

### 4.1 The matrix

Transfer times are for the 0x72BB0A36 kit (10 MB) / a mint-3 kit (38 MB); §11 is why the kit is that small.

| | A. Hub mint dir over SSH | B. Board: user µSD `/persist`, served by the Linux harness | C. MCC SD | D. Signed channel asset (GitHub Release) | E. HM content-addressed cache |
|---|---|---|---|---|---|
| **Standalone, Ethernet-only** | no: needs an SSH account on mapstone-dev. A2 (fpgahub's store over REST) needs fpgahub changes, §4.4 | yes, but **Linux harness only** (mint 3+), and only with a card | **no**: the FPGA cannot read it; USB mass storage from the Debug-USB host only | **yes**: the user already reaches GitHub for HM and the SD bundle | local layer: offline after the first fetch |
| **Integrity** (matches the loaded static) | CRC-32 = static_id, plus `MANIFEST.md5` | CRC-32 against the live static_id. It is a third copy to keep in sync with the MCC SD base and the µSD image | CRC-32 | minisign-signed sha256 + size, then CRC-32 | re-hashed on reuse (`ContentStore.verify`) |
| **Speed** | seconds on campus | **≈ 20 s / ≈ 70 s** (≤ 570 KB/s network, ≤ ~1 MB/s µSD; see 4.2) | reads only on the hub; **writes ≈ minutes per 12 MB**, so +10–40 MB per mint means 2–8+ min of the board in USB-MSC mode | ≈ 1 s / ≈ 3 s at 100 Mbit/s | instant |
| **Wear / D13 fit** | n/a | `/persist` fits (GBs). **p4 `0xDA` does not** (32 MiB; slots 8 MiB). The MBR's 4 entries are all taken (p1/p2 stage0, p3 `/persist`, p4 D13), so there is no raw kit partition. Bare metal has no filesystem at all. Wear: one ~40 MB write per mint, negligible | the config card; 8.3 names for the MCC; the sd-install timeout trap | n/a | host disk |
| **Security / licensing** | SSH accounts | the board serves the DCP to anyone with its SSH key (or LAN, if it is served as a verb) | whoever holds the USB | per-asset: `public` or `github-token`, already in the channel schema | local |
| **Version churn** | per-static subdirs since `987cf26`, but one mint once overwrote the fielded DCP there (fielded/0x72BB0A36 README) | holds only the current static's kit; every mint must re-provision `/persist` | every mint rewrites the config card | one asset per release; old statics stay fetchable as long as their release exists | LRU, never evicting a registered board's static |
| **HM effort** | small (HM already SSHes to the hub) | medium-high (serve path, provisioning, µSD speed, card-less boards) | small but unsafe | small: `Downloader` (resume, sha256, token-scoped) and `ChannelClient` exist | small: `ContentStore` exists |
| **Verdict** | **lab mirror + archive of record** | **optional later fallback** (D3) | **no** | **primary source** | **always** (the cache Vivado reads from) |

### 4.2 The user microSD throughput estimate (B)

Derived from the RTL and drivers on `feat/usd-overlay-store`; nothing has been measured on the board.

- **Controller** (`fpga/shell/ip/usd_spi/README.md`):
  - programmed I/O, one 32-bit shift per `DATA` write, no DMA, no IRQ;
  - `SCK = aclk / (2·(DIV+1))`: 12.5 MHz at `DIV=3`, 25 MHz at `DIV=1` (the maximum for SD timing).
- **Linux** (`spi-usd.c`, `gen_dts.py:555`): `spi-max-frequency = <12500000>`.
  - Each 4 bytes cost 2.56 µs on the wire plus about 0.6 µs of AXI-Lite (a DATA write, ≥1 STATUS poll and a DATA read, at ~0.2 µs each; the D13 README's unmeasured figure).
  - That is ≈ 3.2 µs per word, so a **ceiling of ≈ 1.25 MB/s**, or ≈ 2.1 MB/s at 25 MHz.
  - `mmc_spi` framing, token waits and software CRC on one 100 MHz hart pull it below that. Plan on **0.7–1.0 MB/s**.
- **Bare metal:** the D13 driver's budget is one block per superloop pass, so **≈ 430 KB/s** (`firmware/usd/README.md`).
- **Network out of the board:** the only measured bulk rate is ≈ 570 KB/s (the bare-metal OTW reconfig, `2f8813d`). Linux `smsc911x` plus dropbear crypto on the same hart is unmeasured; assume no better.
- **The result:** B is bounded at ≈ 0.5 MB/s, and every second of it takes the hart that runs the console relays, ICAP and the swap FSM.

### 4.3 Why not the MCC SD (C), in one paragraph

The MCC card is not a data store the FPGA can reach. Only the host whose USB is on the Debug port sees it, and in standalone mode that host is gone after the first install. Writes through the MCC's USB-MSC take minutes per 12 MB. A client that times out and retries mid-write corrupts the card and darkens the board (memory `sd-install-timeout-trap`; HM `harness_manager_mps3/sd.py` encodes it). Adding 10–40 MB per mint to that path buys nothing that D does not do better.

### 4.4 A2: the hub over REST, for token-only users

fpgahub already has a content-addressed store (`src/fpgahub/bitstream_store.py`): sha256, a 128 MiB upload limit, 5 GiB in total, LRU eviction of unpinned entries.

Three things stop it holding kits today:
- `_KIND_SUFFIX` accepts only `.bit` and `.dtbo`;
- upload is admin-only;
- `GET /api/v1/bitstreams/{id}/download` is **admin-only**.

A `kind: "kit"` (`.zip`), download for the `read` or `write` role, and `pinned` kits would give token users a hub source. That is an fpgahub request, not a blocker: D covers them.

## 5. The recommended architecture

```
 mint (platform FLOW)                                   user's machine
 ─────────────────────                                  ─────────────────────────────────────────────
 make -C fpga/dfx kit  ──► mps3-kit-<sid>.zip + kit.json
        │                         │
        ├─► MINT_HUB/<sid>/kit/   │  (A: archive of record, lab mirror)
        │                         ▼
        └─► release bundle ──► channel.json (minisign) ──►  HM KitService.fetch(static_id)
            component kind "rm-kit",                          │  Downloader: resume (Range), sha256, size,
            access public | github-token (D2)                 │  token only to GitHub hosts
                                                              ▼
                                           ContentStore  kind="rm_kit" {static_id, usercode, vivado}
                                                              │  CRC-32(dcp) == static_id == board.shell_id
                                                              ▼
                                   harness-manager kit fetch --board mps3-01 --out ./kit
                                   ./kit/  static/*.dcp  xdc/*.xdc  tcl/build_rm.tcl  kit.json  README
                                                              │
                                                  vivado -mode batch -source kit/tcl/build_rm.tcl
                                                              │   (Vivado 2024.1 exactly, for 0x72BB0A36)
                                                              ▼
                                   overlay/<rm>/{manifest.json, .bin, _clear.bin, .ltx}
                                                              │
                                   harness-manager program mps3-01 <rm> --overlays overlay/   (existing deploy)
```

**Sources, tried in order.** The user can pin one with `--source`.
1. The **cache**.
2. The **channel** (D), when the static's release lists a kit.
3. The **hub** (A), over the SSH that hub mode already uses: `MINT_HUB/<sid>/kit/`, or the loose per-static files until F1 lands.
4. `file:` or a directory, for a colleague's copy, a `fielded/<sid>/` dir, or a mint `prod/` dir.
5. The **board** (B), only if the pack offers it and david takes D3.

Whatever the source, the same checks run.

## 6. The flow into Vivado

### 6.1 Fetch

1. **Detect.**
   - With a board: `session.identity()` gives `shell_id`, `usercode`, `harness_impl` and `proto`. This works over direct Ethernet and over the hub tunnel alike, because it is the same `ping`.
   - Offline: `--static-id 0x72BB0A36`.
   - If the board reports no `usercode` (older firmware), the kit still resolves by `static_id`, and the usercode check is reported UNCHECKED (the deploy service's rule: a check that could not be made does not block).
2. **Resolve.**
   - Look up `rm_kit` records in the ContentStore for that `static_id`.
   - Otherwise find a channel component with `kind: rm-kit` and `static_id` equal to it, in any release, including superseded ones.
   - Otherwise try the hub, then the file sources.
3. **Download.** Use `services/update/download.py` unchanged:
   - it writes `partial/<sha>.part`, resumes with `Range: bytes=N-` and restarts on a 200;
   - it refuses more bytes than declared;
   - it sends the GitHub token only to GitHub hosts, unredirected;
   - it finishes into `blobs/<sha256>`.
4. **Verify.** The MPS3 pack's `KitAdapter.check()` returns `PreflightItem`s, blocking or unchecked by the deploy service's rule:
   - `kit.sha256`, from the channel;
   - every file's sha256 against `kit.json`;
   - `crc32(static_routed_locked.dcp) == kit.static_id == board.shell_id` (**identity item**);
   - `stamp.usercode == board.usercode` (identity item; UNCHECKED when the board does not report it);
   - `kit.vivado` is present in `$PATH` / `$XILINX_VIVADO` / the configured path, and `vivado -version` matches (a warning, not a block; the Tcl enforces it again).
5. **Export** to `--out DIR`, as a hardlink or a copy of the cached files, plus the design-specific XDC set from T10's `rm_kit(model, design)`.
   - The model for a static other than the one baked into `mps3_board_pins.json` is built from the kit's `boundary/` (F3).
   - Until F3 lands, `xdc rm-kit` serves only the model's own static. Asked for another, `kit fetch` says so and ships the DCP without XDCs.
6. **Tell the user the next command:** the exact `vivado` invocation, and the `program` command for afterwards. KIT-GUIDE owns the wording.
   - For a user already inside a Vivado Tcl session: `kit fetch … --print-tcl` prints `set HM_KIT_DIR …; set HM_STATIC_ID …; set HM_VIVADO …`, so `source [exec harness-manager kit fetch mps3-01 --out kit --print-tcl]` works.
   - This is the whole of "serving the kit to a Vivado session". Vivado opens checkpoints only from a local path, so HM's job ends at a verified local directory.

**Cache policy.** Keep every kit whose `static_id` is on a board in `boards.toml` or seen in the last 90 days, and LRU the rest over a configurable cap (default 1 GiB). A kit is 10–40 MB.

### 6.2 The reverse path (built partial → board)

This is already designed and built; this lane adds only the glue in the kit's Tcl.

- `build_rm.tcl` (F2) ends by calling the kit's `gen_manifest.py`, with `static_id` and `static_usercode` taken from the kit.
- That writes the platform's overlay triple, `overlay/<rm>/{manifest.json, <rm>.bin, <rm>_clear.bin[, .ltx]}`, per `docs/contracts/overlay-manifest.md`.
- From there it is the existing path:
  - `harness-manager program TARGET <rm> --overlays overlay/`;
  - `DeployService`: preflight (the static_id and usercode identity items), push, confirm `rm_id`;
  - or `import_overlay()` into the ContentStore, so it appears in `overlays`.
- An RM with ILAs hands its `.ltx` to the XVC lane's serving path; the kit does not.

## 7. The surface

### 7.1 Core vs pack

**Core** (`harness_manager/services/kit.py`, board-agnostic):
- `KitService(engine)`:
  - `required(session) -> KitKey`;
  - `sources()`;
  - `fetch(key, source=None, progress) -> CachedKit`;
  - `export(key, out_dir, design=None)`;
  - `verify(dir)`;
  - `list()`;
  - `import_(path)`.
- `KitKey = {board_type, static_id, usercode, impl, vivado}`.
- Storage: the ContentStore, with the kit zip as one blob, `kind="rm_kit"`. Extraction uses `update.bundle.safe_extract`.
- The channel: one new `kind` value, `rm-kit`, on a new target `host-kit` that the update planner never deploys. It cannot be `host-store`: the planner fetches every `host-store` component on each harness update (`planner.py:267`), which would make every user download 10–40 MB they may never use.

**Pack** (`harness_manager_mps3/kit.py`), implementing a new `KitAdapter` protocol in `core/pack.py`:
- `kit_key(identity)`;
- `check(kit_dir, identity) -> [PreflightItem]` (CRC-32 = static_id, usercode, boundary bits, Vivado);
- `render(kit_dir, out_dir, design)`, which calls T10;
- `vivado_hint()`;
- optionally `board_source(session)`, for B.

A pack with no `KitAdapter` shows "no build kit for this board type" (capability `build_kit`).

### 7.2 CLI (new `cli/cmd_kit.py`; one registration line in `main.py`, coordinated with its owner)

```
harness-manager kit info   TARGET | --static-id ID        # needed static, Vivado release, cached?, sources, size
harness-manager kit fetch  TARGET | --static-id ID  --out DIR  [--design my_rm.json] [--source cache|channel|hub|board|PATH]
harness-manager kit verify DIR [--board TARGET]
harness-manager kit list                                  # cached kits, size, last used, which boards need them
harness-manager kit import DIR|ZIP                         # lab: a fielded/<sid>/ dir or a mint prod/ dir -> cache
```

`kit import fielded/0x72BB0A36` works **today**, before any FLOW change. After `fetch_fielded.sh`, that directory holds `static_routed_locked.dcp`, `static_stamp.json`, `mint.json` and `static_id.txt`, and the import keeps only those. It is the bridge for the fielded static until F1/F2 land.

**The gap until F2:** there is no repo-free build script. A lab user with the platform repo runs `make -C fpga/dfx add-rm-<name>` against the kit's DCP (`DFX_REUSE_LOCKED`). An outside user needs F2. The spike (§11) shows F2 is small: the link needs nothing but the DCP.

The exit codes are HM's existing ones:
- 14 (INCOMPATIBLE): the CRC or usercode does not match the board;
- 15 (REFUSED): corrupt, or an unknown schema;
- 3 (ABSENT): no source has the kit.

### 7.3 Daemon API (new `daemon/kit_api.py`; the same conventions as `docs/API.md`)

| Route | Does |
|---|---|
| `GET /boards/{bid}/kit` | the `KitKey`, cached or not, available sources, size, Vivado release, the local Vivado found |
| `POST /kits/fetch` `{board_id? , static_id?, source?}` | 202 job `kit_fetch`; `kit.progress {phase, bytes, total}` events |
| `GET /kits` | cached kits |
| `GET /kits/{static_id}/zip?design=` | streams the exported kit as a zip, for a browser that is not on the daemon's host |
| `POST /kits/{static_id}/export` `{out_dir, design?}` | writes the directory on the daemon's host |

### 7.4 UI

A **Build kit** card on the board page. KIT-GUIDE owns the words, the steps and where it sits in the journey. The card shows:
- `static_id`, `impl`, the Vivado release (with the found or not-found local Vivado);
- the kit size and whether it is cached;
- **Fetch**, **Download zip** and **Copy Vivado command**.

It reuses the XDC section's design picker, so one click gives the DCP and the XDCs together.

## 8. `kit.json`, v1 (proposal for the FLOW lane)

```json
{
  "schema": "hm-rm-kit", "schema_version": 1,
  "board_type": "mps3", "part": "xcku115-flvb1760-1-c",
  "static_id": "0x72BB0A36", "static_usercode": "0xC8551081", "harness_impl": "bare-metal",
  "vivado": {"release": "2024.1", "build": 5076996, "checkpoint_version": 22},
  "rp": {"inst": "u_rp_dut", "pblock": "pblock_rp_dut", "ports": 47, "bits": 148},
  "pr_verify_ref": "static/static_routed_locked.dcp",
  "files": [
    {"path": "static/static_routed_locked.dcp", "role": "locked_static", "size": 10152801,
     "sha256": "…", "crc32": "0x72BB0A36"},
    {"path": "static/static_stamp.json", "role": "stamp", "size": 507, "sha256": "…"},
    {"path": "boundary/boundary.yaml", "role": "boundary", "sha256": "…"},
    {"path": "tcl/build_rm.tcl", "role": "build", "sha256": "…"},
    {"path": "tools/gen_manifest.py", "role": "manifest", "sha256": "…"}
  ],
  "ip_class": "open", "source": {"repo_sha": "c855108…", "dirty": true},
  "generated_by": "fpga/dfx/tools/pack_kit.py"
}
```

`pr_verify_ref` names the locked static itself: the spike showed that `pr_verify` against it compares exactly the same static as `pr_verify` against the routed greybox (§11). The field stays so that a static whose lock turns out insufficient can name a routed reference.

## 9. Requests to the platform FLOW lane

The lane asks; the FLOW lane edits the platform.

| # | Request | Why |
|---|---|---|
| F1 | `make -C fpga/dfx kit` (in `mint`, after stage 7) writes `prod/kit/mps3-kit-<sid>.zip` + `kit.json` (§8), and stage 8 copies it to `MINT_HUB/<sid>/kit/` | one artefact per static; HM never assembles a kit from loose files |
| F2 | A **standalone `build_rm.tcl`** in the kit: the `build_config first_rm=0` path + `pr_verify` **against the locked static** + `write_bitstream -cell -bin_file` + `write_debug_probes -cell` + `gen_manifest.py`, reading **only** kit files, the user's synth DCP and an optional `_rm.xdc`. It keeps the gates: the clocks guard, RP pin count == `boundary.bits`, HDPR prelink/routed, **CRC-32(DCP) == static_id**, `version -short` == `kit.vivado.release`, `-jobs`/`maxThreads` as parameters | today's `add-rm-*` needs the whole repo, `rm_list.tcl` and `tools.env`; the spike shows the link itself needs only the DCP |
| F3 | Put `boundary.yaml`, `rp_dut_stub.sv` and the partition clocks (as JSON) in the kit | HM can then render XDCs for **any** static, not just the one baked into `mps3_board_pins.json` (mint 3 changes the boundary) |
| F4 | Record `vivado.build` and `checkpoint_version` (read from the DCP's `dcp.xml`) in `mint.json`'s `static_canon` | HM names the exact release without opening the zip |
| F5 | Add the kit to the release bundle (G1) as a component: `kind: rm-kit`, `ip_class: open`, `access` per D2 | the channel is the primary source |
| F6 | Make `MINT_HUB/<sid>/` write-once for `static_routed_locked.dcp` (refuse if the bytes differ) | the 0x3F1A560F overwrite incident; the hub is the archive of record |
| F7 | For release mints, build in a neutral path, or accept that `cfile:/home/…` paths ship inside the DCP | the DCP's XDC members are plain text (§2.3); input to D2 |
| F8 | Linux: `config_rm_greybox_static.ltx` + sidecar go in the kit | the static's MIG debug hub; the XVC lane consumes it |
| F9 | `add-rm-*`: let `DFX_REF_ROUTED` default to the locked static (`DFX_REUSE_LOCKED`) | spike S6: `pr_verify` against the locked static compares exactly the same static. Then a fielded static needs **one** preserved DCP, not two |

## 10. Decisions for david

**D1. Where the kit lives (primary).**
- (a) **Recommended:** a signed channel asset, cached by HM; the hub mint dir stays the archive and a lab mirror.
- (b) The hub mint dir only, over SSH. This excludes standalone users.
- (c) On the board, in the user µSD `/persist`. Linux only, ≈ 0.5 MB/s, and a third copy to keep in sync.
- (d) The MCC SD. Unreachable over Ethernet, and the slow, corruptible write path.

**D2. Who may download the static DCP.**
- (a) **Recommended:** token-gated (`access: github-token`) in the dist channel, like the `arm-aaa` overlays, until the shell netlist is cleared for publication.
- (b) Public. The DCP holds no Arm IP; AMD secured IP stays encrypted, but the SoC Labs shell netlist and build paths become readable.
- (c) Hub SSH accounts only.

**D3. A board-served copy (B).**
- (a) **Recommended:** not now. Revisit after mint 3 with the measured µSD and SSH throughput at B2.
- (b) Build it for mint 3: `/persist` + a harnessd serve path + provisioning.
- (c) Never.

**D4. How strict the Vivado release check is.**
- (a) **Recommended:** HM warns, and `build_rm.tcl` refuses a different `major.minor`; the build number is only a warning.
- (b) Both only warn.

## 11. The spike

**The choice: (b), the link against a copied locked static.** It retires the biggest risk for the least time.

- The whole design assumes a user can build an RM from the kit alone, with no platform repo.
- If the locked DCP depended on repo files (`dfx_floorplan.xdc`, `mps3_harness_timing.xdc`, `boundary.yaml`, `rm_list.tcl`) or on absolute paths, then the kit would be the repo, and "where the DCP lives" would be the wrong question.
- Spike (a), sizes, came almost free from `mint.json` and local files, and was folded in. It found that the kit does not compress.
- Spike (c), D13 fit, is answered by the layout contract (§4.1), with no measurement needed.

**Setup.**
- Inputs: `/tmp/kit-spike/` holds read-only copies of 0x3F1A560F's `static_routed_locked.dcp` and the RM synth DCPs from the same mint. 0x72BB0A36's DCP is only on the hub and in the mint worktree, both off-limits. 0x3F1A560F is the previous fielded static: same flow, same Vivado, boundary 136 pins.
- Vivado 2024.1 (`/apps/Xilinx/Vivado/2024.1`), `general.maxThreads 2`, `nice -n 10`.
- No opt, place or route.

**Results.** The logs were in `/tmp/kit-spike/`, deleted after this run; the numbers are copied here.

| # | Test | Result |
|---|---|---|
| S1 | `static_id == CRC-32(static_routed_locked.dcp)` on 4 real DCPs (0x3F1A560F, 0x61BC6789, 0x2B082E1B, 0xA8C1C535) | **4/4 match** |
| S2 | `open_checkpoint` of the `/tmp` copy | 58–61 s, peak RSS 2.5–2.6 GB. Part `xcku115-flvb1760-1-c`; `u_rp_dut` `IS_BLACKBOX=1`, `HD.RECONFIGURABLE=1`; `pblock_rp_dut` present with its grid; 7 clocks; 136 RP pins; 84,752 of 101,268 nets route-fixed; 21,904 cells LOC-fixed. **The floorplan, the DFX markup and the shell timing are all inside the DCP.** |
| S3 | `read_checkpoint -cell u_rp_dut rm_nanosoc_synth.dcp` (a real DUT: Cortex-M0 nanoSoC) | 77 s. `IS_BLACKBOX=0`, 136 RP pins, 19,602 leaf cells in the RP; the clock reaches `u_rp_dut/dut_clk`; route-fixed nets 86,187; **HDPR prelink DRC: 0 violations** |
| S4 | `update_design -black_box`, then re-link `rm_led_synth.dcp` in the same session | 76 s. 205 leaf cells, 136 pins, a clock on `dut_clk`; **HDPR prelink: 0 violations** |
| S5 | `pr_verify` control: routed greybox vs routed led (what the flow does today) | **compatible**, 99 s: 104 partition pins, 21,747 static cells, 502,036 routed nodes, 471,263 PIPs compared |
| S6 | `pr_verify` **locked static** vs routed led | **compatible**, 66 s, with **exactly the same counts** as S5. The locked static is a sufficient reference, so the kit drops `config_rm_greybox_routed.dcp` and halves: 10 MB bare-metal, 38 MB Linux |
| S7 | The 2026.1 Linux DCP opened in 2024.1 | **refused**: `ERROR: [Runs 36-378] … was created with 'Vivado v2026.1 (64-bit)', and cannot be opened in this version.` The version lock is real, and HM must say which release before the user starts |
| S8 | `write_edif` of the opened static (the secured-IP probe) | a 23 MB plain netlist **plus 1,051 separately Xilinx-encrypted `.edn` files** (AMD secured IP). No Arm cells: the IP cell list matches `shell_bd.tcl` (§2.3) |
| S9 | S2 + S4 again with **the platform repo and the `-lx` worktree hidden** (`bwrap` private mount namespace, `tmpfs` over both; nothing outside the process changed) | **links**: `IS_BLACKBOX=0`, 7 clocks, `report_timing_summary` runs; no ERROR and no CRITICAL WARNING. The `cfile:/home/…` paths inside the DCP are labels, never read. (A first attempt under `strace -f` segfaulted Vivado at start-up, a ptrace artefact, so it proved nothing) |
| S10 | Negative control: `pr_verify` 0x3F1A560F's locked static vs **0xA8C1C535's** routed led | **not compatible**: `HDPRVerify-08 … places instance …mdm_1…BUFG_DRCK at site BUFGCE_X1Y118, yet [the other] does not`. So the S6 check can fail: a kit for the wrong static is caught at `pr_verify`, not on the board |

**What this retires.**
- A user can link an RM against a kit's DCP with **no platform repo** (S9), in about 4 minutes and 2.6 GB on a loaded machine with 2 threads. The only other inputs are their synth DCP and the few kit files that F2 lists.
- F2 is therefore a short Tcl, not a port of the flow.
- Integrity is one CRC.
- The kit is half the size it would otherwise be.
- Still unproven here, because routing was out of scope: a full place, route and `write_bitstream -cell` from the kit. The platform's `make add-rm-*` does exactly that on every mint (26 HDPR reports clean on 0x72BB0A36), so the risk is low. F2's own test should run it once.

## 12. Lane plan (build, after david's decisions)

| # | Work | Hours | Needs |
|---|---|---|---|
| K1 | `kit.json` v1 schema + parser + a small fixture kit (a fake DCP with a chosen CRC) | 3 | — |
| K2 | Core `KitService`: ContentStore records, sources (cache/channel/hub-ssh/file), export, verify, cache policy + tests | 6 | K1 |
| K3 | MPS3 `KitAdapter`: identity → key, the CRC-32/usercode/Vivado checks, `kit import` of `fielded/<sid>/` and `prod/` + tests | 3 | K1 |
| K4 | Channel: `kind: rm-kit`, target `host-kit`, planner ignores it + tests | 2 | K1 |
| K5 | CLI `kit info/fetch/verify/list/import` + daemon routes + `kit.progress` + tests | 5 | K2, K3 |
| K6 | Per-static XDC model from the kit's `boundary/` (a T10 follow-up, with its owner) | 4 | F3 |
| K7 | Web UI card (KIT-GUIDE's text) + browser test | 3 | K5 |
| K8 | Hub source over SSH: `MINT_HUB/<sid>/kit/`, then the per-static loose files | 2 | K2 |
| K9 | One board check in an existing window: `kit info mps3-01` names 0x72BB0A36, and `kit verify` passes (≈ 15 min of board time) | 1 | K5 |
| | **Total** | **≈ 29 h (≈ 4 days)** | FLOW F1/F2 ≈ 1–1.5 days on their side |

Order: K1 → K2/K3/K4 in parallel → K5 → K7. K6 waits for F3. K8 can go any time after K2.
