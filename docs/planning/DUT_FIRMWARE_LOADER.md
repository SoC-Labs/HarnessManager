# DUT firmware loader: scope and plan (lane FWLOAD-SCOPE)

- **Asked by:** david, Thu 1 Oct 22:30. "Do we have a firmware loader for the SoC firmware? Can we
  build this in to the GUI? How should this work? What is the best way of doing this? Can we do
  this straight from harness manager by uploading a bin file?"
- **Status:** plan only. No code. Every claim cites `path:line`. Anything not checked in a file
  is marked **UNVERIFIED**.
- **Branch:** `team/fwload-scope` (base `a6dd60c`).

## 0. The answer

1. **Do we have a firmware loader?** Yes, three of them, and none is in Harness Manager (HM):
   - the M0 QSPI flash loader, which is silicon-proven but run from scripts on the hub;
   - OpenOCD `load_image` / gdb `load` through `debug up`, which works but is slow on Linux today;
   - an RM rebuild with the firmware baked into IMEM (HM's Build tab), which takes 48-55 min.
2. **Can HM load a .bin/.elf/.hex upload?** Yes.
   - **RAM (run now):** HM can ship this on the OpenOCD path that already exists (P0). On a Linux
     board today a 4 KiB image takes about 4 min.
   - **Flash (keeps across resets):** P1, through the M0 loader. It needs david's call on writing
     the DUT flash (D2).
   - **Both get fast** (seconds) once the board does the JTAG itself with the v2.1 UIO adapter (P2).
3. **The best way:** one **Load firmware** action on the Workbench.
   - HM checks the image against the loaded design's declared memory and sends it once.
   - The board drives JTAG locally and holds the JTAG lock so no swap can start.
   - Every write is verified by a CRC computed on the DUT and compared with known data.
4. **Phases:**
   - **P0:** HM-only RAM load + run. About 34 h of HM work and 2 h of platform work.
   - **P1:** flash through the M0 loader. About 16 h HM + 12 h platform. Gated on D2.
   - **P2:** board-side `mps3-debug load` with UIO. About 18 h for the Linux lead + 8 h HM. Lands in v2.1.
5. **For david:** five decisions in §11. **For the Linux lead:** seven questions in §12.

## 1. Sources and conventions

| Prefix | Where | Ref |
|---|---|---|
| `HM:` | this repo, `/home/dam1n19/SoCLabs/hm-fwload-scope` | `a6dd60c` |
| `HM@fp7:` | HM branch `team/fix-pack-7` (DEBUG-DOWN-FIRST, not on main yet) | `92f0181` |
| `P:` | platform repo `/home/dam1n19/SoCLabs/mps3-nanosoc-platform` | `origin/master` `e3e2afd` (the v2.0.0 merge) |
| `P@<branch>:` | a platform branch | named per cite |
| `SOC:` | `/home/dam1n19/SoCLabs/nanosoc_m0_soc`, the SoC tree the RM build reads | working tree (read-only) |
| `LL:` | `/tmpdir/claude-74755/linux-lanes` (the Linux lead's lanes) | files as of 1 Oct |
| `MEM:` | `/home/dam1n19/.claude/projects/-home-dam1n19-SoCLabs-mps3-nanosoc-platform/memory` (the leads' session notes) | **secondary source**: used only where no repo file holds the fact |

The rates below are from silicon unless marked *model*. "B/s" is payload bytes per second, end to end.

## 2. HAZARDS: read these first

### 2.1 The DUT flash (SST26VF064B)

| # | Hazard | Source | What the loader must do |
|---|---|---|---|
| H1 | **Writing the DUT's SST26 is a david-approval action today.** HM's HIL rules say never write it; the CI allow-list has no flash verb; the on-board GDB proposal says the harness itself never writes it. | `HM:docs/HIL_LINUX.md:101-102`; `HM:docs/planning/ci-2026-09-30/matrix.md:146-150`; `P:docs/planning/linux_lanes/GDB_SERVER_PROPOSAL.md:36-37` | Hide the flash target until D2 is decided. After that: consent, lease and design gates (§8.2). |
| H2 | **`nanosoc_multicore`'s CPU1 boot ROM writes one `0x00` byte at flash `0x20000+i` on EVERY boot.** `0x20000` is where `nanosoc_upy`'s COLD segment starts. This is the likely writer of the 13 zeroed bytes found on 24 Sep. | `LL:nanosoc_boot/evidence.txt:25-26`; `P@docs/known-issues-14-15:docs/planning/RELEASE_NOTES_v2.0.0.md:191-196`; `P:docs/evidence/2026-09-w3/rf_flash_fix_20260924.txt:21-30` | Refuse every load on `nanosoc_multicore`. Keep a per-board flash ledger, and warn before a deploy of `nanosoc_multicore` on a board whose flash holds a table. Offer a read-only CRC check afterwards. |
| H3 | **Plain `nanosoc` flash-boots any `BOOT` table.** Its ROM is QSPI-enabled. MicroPython's ~40 KB HOT segment wraps the 16 KiB IMEM, and the result is a HardFault lockup (pc `0xfffffffe`). The v2.1 fix rebuilds `rm_nanosoc` with `QSPI_FLASH_PRESENT=0`. | `P@docs/known-issues-14-15:…/RELEASE_NOTES_v2.0.0.md:184-190`; `LL:nanosoc_boot/evidence.txt:3-14`; `HM:docs/evidence/2026-10-01-eocd-b2/SUMMARY.md:16,29` | A RAM image on `nanosoc` is **overwritten by the ROM at the next DUT reset** when flash holds a table. So "run" must start from the debugger (§4.3), never through a reset, unless the design declares no flash boot. |
| H4 | **A flash write interrupted mid-sector** leaves a half image. The ROM then copies, fails the CRC and falls back into the overwritten IMEM. | `SOC:firmware/bootloader/stage0/stage0_bootloader.c:487-502,525-533`; release-notes KI 14 (above) | Erase the boot-table sector **first** and write it **last**. An interrupted write then leaves no valid table, and the ROM takes its no-table path. Keep a per-chunk CRC so a resumed job redoes only bad chunks (§9). |
| H5 | **Silent-success traps in the loader.** Every QSPI register write needs a read-back, and BUSY must be seen to *assert* before you wait for it to clear. Otherwise every operation reports OK and does nothing: a CRC once returned the CRC of 256 zero bytes. | `P:firmware/qspi_loader/qspi_loader.c:130-157`; `P:host/pyverify/pyverify/qspi_loader.py:22-30` | Never trust a status code. Verify against **known data** (§9.1). |
| H6 | **Never read the XiP aperture (`0x7000_0000`) before the controller is set up.** In July an unconfigured read hung the bus and took the whole shell off the network. | `P:scripts/qspi_loader_bringup.py:30-31`; `MEM:qspi-xip-integration.md:35-36` | Read flash only through the loader's `CMD_CRC`, never with a memory read of `0x7…`. |
| H7 | **The controller reaches 4 MiB of the 8 MiB part** (`ADDR[21:0]`). | `P:host/pyverify/pyverify/qspi_flash.py:30-40`; `P:firmware/qspi_loader/qspi_loader.c:120` | Refuse a flash image that ends past `0x40_0000`. |

### 2.2 The core and JTAG

| # | Hazard | Source | What the loader must do |
|---|---|---|---|
| H8 | **The loader needs the core halted, ideally reset-halted.** ARMv6-M cannot clear an ACTIVE exception, so a new image entered without a reset can wedge in `Default_Handler`. Reset-halt is "not reliable on this DUT" by the July notes (`dap_npotrst` is the wrapper reset). On 30 Sep `ql_reset_halt` worked: DEMCR VC_CORERESET plus AIRCR SYSRESETREQ inside `catch`. A HardFault-halted core double-faults in the loader without it. | `P:host/pyverify/pyverify/qspi_loader.py:61-65`; `HM:src/harness_manager_mps3/openocd_cfg/nanosoc_ops.tcl:53-76`; `P:fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:546`; `MEM:m0-flash-loader-proven.md:42`; `LL:field/mps3-b2-flash:104` (the proc itself is only on the hub, `/home/david/w1_flash/ql_reset.tcl`, **not in any repo**) | Use reset-halt first, and fall back to halt + quiesce SysTick/NVIC. Refuse "run" when the core is still in a handler (IPSR ≠ 0) after both. Move `ql_reset_halt` into the repo (P0-P2). |
| H9 | **The board serves one JTAG client at a time.** A second connection to 6921 is accepted and closed. An on-board session holds 6921 (and, with UIO, the `jtag-bb` flock). | `P:firmware/jtag_server/jtag_server.c:192-210`; `P@feat/jtag-uio-adapter:fpga/shell/ip/jtag_bb/sw/openocd/README.md:70-97` | One loader per board. Refuse with `HeldError` (4), naming the holder: a host OpenOCD, another tool's session, or a user's gdb. |
| H10 | **Swaps must stop debug first.** A swap rebuilds the DAP. DEBUG-DOWN-FIRST asks the board's OpenOCD down before every deploy. | `HM@fp7:src/harness_manager/services/deploy.py:200-226`; `HM:src/harness_manager/services/debug.py:1198-1212` | A load is a board **job** (the board gate refuses a swap meanwhile: `HM:src/harness_manager/daemon/jobs.py:11-21`). A load must never start while a deploy runs. |
| H11 | **The loader clobbers IMEM** and needs the 128 KiB IMEM of `nanosoc_upy`: its stack top is `0x1002_0000`. | `P:scripts/qspi_loader_bringup.py:33-34`; `P:firmware/qspi_loader/qspi_loader_jtag.tcl:48` | Run flash jobs only on `nanosoc_upy` until a 16 KiB loader variant exists. After a flash job, IMEM holds the loader, not the old program. |

### 2.3 The board and the harness

| # | Hazard | Source | What the loader must do |
|---|---|---|---|
| H12 | **After a warm harness restart the RP stays in reset until the first swap** (`stats` `rm_ok: false`). | `HM:docs/HIL_LINUX.md:103-104` | Refuse with "program the design once first". |
| H13 | **The console prints once at reset into a 16-byte FIFO.** Attach the console **before** the DUT starts, or the banner is lost. | `P@docs/known-issues-14-15:…/RELEASE_NOTES_v2.0.0.md:188`; `LL:nanosoc_boot/evidence.txt:15-16` | Open `uart0` before "run". This is automatic. |
| H14 | **IMEM size constants disagree across the tree:** 64 KB (`nanosoc_ops.tcl`), 128 KB (`swd.py`, the loader), 16 KiB (the `nanosoc` RTL). | `HM:src/harness_manager_mps3/openocd_cfg/nanosoc_ops.tcl:22-25`; `P:host/pyverify/pyverify/swd.py:128-142`; `P:fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:106-107` | Never use one global size. Use the per-design table (§4.1). |

## 3. What exists today

### 3.1 Inventory

| Path | What it does | Speed (measured) | Proof on silicon | Limits |
|---|---|---|---|---|
| **M0 QSPI flash loader** (`P:firmware/qspi_loader/`, `P:host/pyverify/pyverify/qspi_loader.py`, `P:firmware/qspi_loader/qspi_loader_jtag.tcl`, hub tool `LL:field/mps3-b2-flash`) | Loads a 760 B stub into IMEM. Bulk-writes 48 KiB chunks. The M0 erases, programs and CRCs the SST26 locally. | Bare metal, host OpenOCD: 160 KB in **3 min 29 s** (~0.8 KB/s). Linux v7n, hub OpenOCD direct to 6921: ~25 B/s, **107 min** for 160 KB. | **Proven:** SWD Jul 19-21, JTAG on bare metal Sep 24, Linux board 2 Sep 30 (CRC `0x0DBE004A`, then the banner and `print(1+1)`) | Needs `nanosoc_upy` (128 KiB IMEM), a free JTAG, an unclaimed board (or the board's own OpenOCD), and `ql_reset_halt`. Hub scripts only; HM has none of it. |
| Register-at-a-time QSPI (`P:host/pyverify/pyverify/qspi_flash.py`, `P:scripts/qspi_write_smoke.py`) | Drives the controller one register per debug op. | **~1.5 B/s**: 256 B in 167 s; 160 KB would take ~29 h | Proven correct Jul 18 | Diagnostics only. Superseded by the loader. |
| **OpenOCD load to RAM**: `debug up`, then gdb `load`/`restore` or Tcl `load_image` | Writes IMEM/DMEM through the SWJ-DP's AHB-AP. | Linux board path (on-board OpenOCD, rc image): **~19 B/s** (4 × 1 KiB in 213 s). Host OpenOCD over the claim SSH (rc jsf image): ~47 B/s. Bare metal host: ~0.8 KB/s class. | **Partly proven:** a 1 KiB write + read-back round trip on board 2 (E-OCD OCD4). A whole image loaded and run: **UNPROVEN** | Volatile: lost at a swap or power-off, and at a DUT reset on a flash-booting design (H3). Needs halt/reset-halt (H8). |
| **RM rebuild with baked IMEM** (HM Build tab, `build.generics` `IMEM_MEM_FPGA_IMG`) | Bakes a `$readmemh` word-hex into the partial. | **48-55 min** per `nanosoc` build, plus ~64 s to program | **Proven** with `hello_image.hex` (KIT-NANOSOC) | Slow; needs Vivado and the kit. On a flash-booting design the flash table still wins (H3). |
| **MicroPython REPL** (`nanosoc_upy`) | Type or paste Python at the console. | Paced 20 ms/char = **~50 B/s** | **Proven** (`print(1+1)` → `2`) | Script-level only. No filesystem, so nothing persists. |

Sources for the table:
- loader: `P:firmware/qspi_loader/qspi_loader.c:1-28`, `P:host/pyverify/pyverify/qspi_loader.py:3-30`, `P:docs/evidence/2026-09-w3/rf_flash_fix_20260924.txt:1-4,33-42`, `MEM:m0-flash-loader-proven.md:40`, `LL:field/mps3-b2-flash:97-111`;
- register path: `P:host/pyverify/pyverify/qspi_loader.py:4-6`;
- OpenOCD path: `HM:docs/evidence/2026-10-01-eocd-b2/SUMMARY.md:17,36-41`, `HM:docs/evidence/2026-10-01-eocd-b2/ocd4_ram.txt:4-14`, `MEM:openocd-on-harness-mvp.md:58,64`;
- rebuild: `HM:docs/evidence/2026-09-30-kit-nanosoc/README.md:6,227,275-276`, `HM:src/harness_manager/services/kit/script.py:154-158`, `HM:docs/evidence/2026-10-01-eocd-b2/SUMMARY.md:13`;
- REPL: `HM:src/harness_manager_mps3/constants.py:36-40`, `P:firmware/micropython/port/mpconfigport.h:13-15`, `P:docs/evidence/2026-09-w3/rf_flash_fix_20260924.txt:37-41`.

Notes:
- The hub tool's header expected "Direct: ~4 min" (`LL:field/mps3-b2-flash:14-16`). The run took 107 min (`MEM:m0-flash-loader-proven.md:40`): the Linux `jtag_server` per-byte path is the wall, not the network.
- The bare-metal JTAG script was marked "NOT RUN ON SILICON" when written (`P:firmware/qspi_loader/qspi_loader_jtag.tcl:23-26`). That is now stale: its first silicon run was 24 Sep (`P:docs/evidence/2026-09-w3/rf_flash_fix_20260924.txt:3-4`).

### 3.2 Other ways into DUT memory: there are none today

The only memory path into the DUT is **the SWJ-DP over `jtag_bb`** (JTAG to the AHB-AP).

| Candidate | State | Source |
|---|---|---|
| Shell↔DUT AXI/AHB (`mmio_*`) | "no shell↔DUT AXI in v0"; `mmio_*` is deferred | `P:docs/contracts/partition-pins.md:13-14,237-238` |
| `exp_*` expansion port | A DUT **master** at `0x6000_0000` that feeds the CLCD socket and SoCScope. Not a way in. | `P:fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:563-583` |
| ADP / FT1248 (SoCDebug) | Looped back inside the RM: it only self-drains so the ROM banner does not stall | `P:fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:364-374` |
| Console UART2 | Real, but the nanoSoC UART has no RX FIFO, so input is paced at 20 ms/char (~50 B/s). It also needs resident firmware to receive. | `P:fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:385-390`; `HM:src/harness_manager_mps3/constants.py:36-40` |
| hostio4 | Planned, not in the shell. It needs a ~15-pin boundary widen, which re-keys the static (a mint). | `MEM:hostio-support-planned.md:21-22` |
| A shell-side SST26 writer | Gone. The DUT owns the whole SST26 since the overlay store moved to the microSD. | `P:docs/contracts/OPEN_ISSUES.md:103-108` |
| `updatemem` on a partial | Used only for the shell's MicroBlaze firmware in the static. Never tried on an RM partial. **UNVERIFIED** | `P:docs/BUILD_AND_MINT.md:282`; `P:docs/TROUBLESHOOTING.md:56` |

### 3.3 Time per image, by path

| Path | Rate | 4 KiB (`hello`, 4128 B) | 16 KiB (a full `nanosoc` IMEM) | 160 KB flash image |
|---|---|---|---|---|
| Linux, board OpenOCD over `remote_bitbang` (today) | ~19-21 B/s | ~3.5 min | ~14 min | ~107 min (measured, at ~25 B/s) |
| Linux, host OpenOCD over the claim SSH (rc jsf) | ~47 B/s | ~1.5 min | ~6 min | ~57 min |
| Bare metal (`0x72BB0A36`), host OpenOCD | ~0.8 KB/s | ~5 s | ~20 s | 3 min 29 s (measured) |
| `remote_bitbang` deferred-reads patch (v2.2.0-ws.1), *model* | QEMU 6.5-11×; 4 KiB script 222 s → 10-35 s | 10-35 s | ~1-2 min | ~7-25 min |
| **v2.1 UIO adapter** (`mps3_jtagbb`), *model* | **~20 KB/s** | **<1 s** | **~1 s** | ~8 s upload + the M0's erase/program time (**UNVERIFIED**; below 3.5 min, since the bare-metal total includes it) |

Sources:
- the ws.1 patch: `MEM:openocd-on-harness-mvp.md:79,87`;
- the UIO model: `P@feat/jtag-uio-adapter:fpga/shell/ip/jtag_bb/sw/openocd/README.md:186-200`.

Two caveats:
- **Writes vs reads.** The measured rates are dumps (reads) or mixed traffic. Write-only throughput was never measured on its own (**UNVERIFIED**). The 25 B/s flash write is consistent with ~20 B/s.
- **Verify.** Verification by read-back doubles every row. An on-target CRC (§9.1) costs about nothing.

## 4. The DUT: memory map and boot flow

### 4.1 Per-design memory (design number = `rm_id & 0xFFFF`)

| Design | IMEM (phys `0x1000_0000`, alias `0x0` after REMAP) | DMEM (`0x1800_0000`) | Flash (SST26 via the SoC's `qspi_ctrl` `0x7400_0000`, XiP `0x7000_0000`) | DAP cfg | Firmware load? |
|---|---|---|---|---|---|
| `0x0000` greybox, `0x0002` eth_ss, `0x001E` led, other no-DAP designs | — | — | — | none (`dap=no`) | **No**: "load a design with a CPU" |
| `0x0001` nanosoc | **16 KiB** (`IMEM_RAM_ADDR_W=14`) | **16 KiB** | Controller present and the ROM flash-boots (H3) until v2.1 | `nanosoc_mps3_jtag.cfg` | RAM: yes. Flash: **no** (no 128 KiB loader; v2.1 drops its flash boot) |
| `0x0005` nanosoc_upy | **128 KiB** (`17`) | **64 KiB** (`16`) | Boot table v1 at `0x0`, HOT at `0x1000`, COLD at `0x20000` | same | RAM: yes. **Flash: yes** (the loader's home) |
| `0x0008` nanosoc_iice, `0x000A` nanosoc_ila | 16 KiB (they nest `rp_nanosoc_wrapper` with its defaults) | 16 KiB | as nanosoc | `nanosoc_iice_chain.cfg` / `nanosoc_mps3_jtag.cfg` | RAM: yes. Flash: no |
| `0x0003` nanosoc_multicore | CPU1 16 KiB by its comment; the arithmetic in the same line says 64 KiB (**UNVERIFIED**) | CPU1 4 KiB | CPU1 ROM writes flash (H2) | 2-AP cfg, cpu0 fails (`PARTNO 0x0`) | **No** (H2, two cores, cfg unproven) |

Sources:
- nanosoc: `P:fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:106-107`;
- nanosoc_upy: `P:fpga/rp/nanosoc_upy/rp_nanosoc_upy_wrapper.sv:67-76`;
- the iice wrapper: `P:fpga/rp/nanosoc_iice/rp_nanosoc_iice_core.sv:165-170`;
- multicore: `P:fpga/rp/nanosoc_multicore/rp_nanosoc_multicore_wrapper.sv:66-68`, `HM:docs/evidence/2026-10-01-eocd-b2/SUMMARY.md:21`;
- the address map: `SOC:build_soc/firmware/nanosoc_memmap.h:14-29`;
- the DAP table: `P@feat/jtag-uio-harnessd-lock:src/linux_harness/sw/br2_external/package/mps3-debug/designs.conf:16-31`.

Gap: HM's own `KNOWN_DESIGNS`/`DAP_DESIGN_CONFIGS` lack `0x000A` (`HM:src/harness_manager_mps3/constants.py:103-120`).

### 4.2 Boot flow (stage-0 ROM, 2 KB case-ROM at `0x0`)

1. `main()` checks `BOOT_CFG.QSPI_PRESENT`. If set, `qspi_boot()` runs:
   - it reads the table at flash `0x0`;
   - a bad magic or table prints `Q:BADTBL` and returns.
   - (`SOC:firmware/bootloader/stage0/stage0_bootloader.c:508-526,439-468`)
2. On a good table it CPU-copies HOT (`app_offset`, `app_size`) to IMEM `0x1000_0000`, then CRC32-checks it (`:484-492`).
3. On success it calls `FlashLoader()`: REMAP=1 (IMEM appears at `0x0`), print `I=/V=/M=/W=`, take SP/PC from IMEM (`:258-302,496-502`).
4. If QSPI is absent or the boot failed, it calls `FlashLoader()` anyway on whatever IMEM holds (`:532-533`). That is how a baked or preloaded image runs, and why H3/H4 are dangerous after an IMEM overwrite.
5. The COLD segment runs in place from XiP `0x7002_0000` (`P:firmware/micropython/flash_pack.py:7-17`; `P:firmware/micropython/port/nanosoc_xip.ld:47-49`).

The recorded image pair (`P:fpga/rp/nanosoc_upy/flash_image.json:6-18,55-59`):
- `flash_image.bin`: 163,840 B, CRC `0x0DBE004A`;
- HOT: 40,020 B, CRC `0xF7E850CB`;
- the expected stage-0 line on 6930: `W=18010000,000083B5`.

### 4.3 What a "bin file" needs

| Need | Rule |
|---|---|
| **Format** | `.elf` (preferred: it carries the addresses, entry and sections), Intel `.hex` (carries addresses), raw `.bin` (needs `--at`; default IMEM base). Refuse anything else. |
| **Load address** | Apps are **linked at `0x0`** (the IMEM alias: `P:firmware/micropython/port/nanosoc.ld:19-20`). Before REMAP, `0x0` is the ROM, so a gdb `load` to `0x0` on a core halted in the ROM goes nowhere. Whether it is a silent no-op or an error is **UNVERIFIED**. **Rule:** translate `0x0..IMEM_size` to `0x1000_0000+off` (the physical IMEM is always mapped: `P:host/pyverify/pyverify/swd.py:57-63`). Pass DMEM/XiP addresses through and check them against the regions. |
| **Entry** | Take it from the vector table, not ELF `e_entry`: word 0 = initial MSP (inside DMEM, 8-aligned), word 1 = Reset_Handler (odd, inside the IMEM alias). Cortex-M0 has no VTOR, so vectors come through REMAP (`MEM:multicore-dev-tree-and-xip-model.md:23`). |
| **Size** | ≤ the design's IMEM (16 KiB nanosoc, 128 KiB nanosoc_upy). A HOT segment in flash must also be ≤ IMEM: the v2.1 ROM rejects larger, today's ROM corrupts (H3). |
| **RAM vs flash** | **RAM** = IMEM (+ DMEM init). Runs now; gone at a swap or power-off, and at a DUT reset on a flash-booting design. **Flash** = a v1 boot table + HOT (+ COLD). Survives resets and power; the ROM copies it at every reset of a QSPI-enabled design. |
| **Flash packing** | HM packs a plain app (ELF/BIN ≤ IMEM) into table@`0x0` + HOT@`0x1000`, the same bytes as `flash_pack.py`: magic `BOOT`, v1, one entry, `binascii.crc32` (`P:firmware/micropython/flash_pack.py:43-74`). An ELF with `.text.xip*` sections at `0x7002_xxxx` adds COLD@`0x20000`. A pre-packed `flash_image.bin` (table magic at offset 0) is written as is, after its table CRC checks out. |

## 5. The Linux harness's plans that matter here

| Item | State | What it gives a loader | Source |
|---|---|---|---|
| **On-board OpenOCD + `mps3-debug/1`** | Shipping in v2.0.0 (david, 1 Oct 16:31: "base all the decisions around this") | A gdb port per core over the claim SSH. HM's `debug up` already uses it. | `HM:src/harness_manager/services/debug_onboard.py:1-46`; `MEM:openocd-on-harness-mvp.md:72` |
| **v2.1 UIO adapter `mps3_jtagbb`** | Done board-free (`feat/jtag-uio-adapter` `96d2c44`: unit 55, e2e, QEMU 16). Silicon check Fri 2 Oct 12:00-12:30 on board 2. | OpenOCD drives JTAGBB in-process: one store per TCK change, no socket, no harnessd in the bit path. *Model:* ~20 KB/s, 4 KiB in 0.2-1 s at nice 0. | `P@feat/jtag-uio-adapter:fpga/shell/ip/jtag_bb/sw/openocd/README.md:1-12,172-211,256-300` |
| **Exclusivity + swap interlock** | Same branch | `flock` on `/dev/uio<jtag-bb>`, a held silent 6921 connection, and DFXCTL.STATUS read before every rising TCK. A swap parks and latches the adapter. | `…/README.md:66-142` |
| **harnessd lock (P1/P2/P3)** | `feat/jtag-uio-harnessd-lock` `7920c08`, accepted. Board proof Tue 6 / Wed 7. | P1: harnessd holds the same flock across a swap, so **a swap is refused while a JTAG session (or a load) holds it**. P3: `swap_seq` in identify. The launcher advertises `"harnessd-lock"`. | `P@feat/jtag-uio-harnessd-lock:src/linux_harness/sw/br2_external/package/mps3-debug/src/mps3_debug.c:42-66`; `MEM:openocd-on-harness-mvp.md:110-112`; `HM@fp7:src/harness_manager/services/debug_onboard.py:132` |
| **v0.19 gdb verbs** (`debug_open/close/status`, `gdb_server` feature) | **Parked** (WIP `38b93e2` on `wip/gdb-stage1-verbs`: no tests, no FakeShell, no contract text) | A harnessd-supervised OpenOCD. Overtaken by `mps3-debug` as the MVP path. | `P@wip/gdb-stage1-verbs` commit `38b93e2` message |
| **`remote_bitbang` deferred reads + nice 0** | `feat/rbb-deferred-reads` `4d53e54`, for v2.2.0-ws.1 | *Model* 6-20× on today's path; no harnessd change | `MEM:openocd-on-harness-mvp.md:79,87` |
| **6910 push framing** | Shipping: kind 0/1 (clearing/partial), kind 2 (slot image) | A proven, windowed, CRC-checked bulk channel with a refuse-before-write rule. A firmware kind 3 would reuse it. | `P:docs/contracts/net-protocol.md:2064-2072,1416-1470` |

### 5.1 What a harness-native "dut load" looks like

There are two front doors to the same board-side worker. Recommendation: **C1 first**.

**C1: `mps3-debug load` over the claim SSH.** No harnessd or net-protocol change.

```
# HM streams the image on stdin (the claim SSH it already holds); the board keeps it in /run (tmpfs)
mps3-debug load --target ram|flash --addr 0x10000000 --len 4128 --crc32 0x1234ABCD \
                --start debugger|rom|none [--rm NAME] --json  < image.bin
-> {"schema":"mps3-debug/1","op":"load","state":"running","job":"j7", ...}     (returns at once)
mps3-debug load --status --json
-> {"schema":"mps3-debug/1","op":"load","state":"running|done|failed",
    "target":"flash","addr":"0x0","len":163840,
    "phase":"enter|erase|program|verify|table|start","done":98304,"total":163840,
    "crc32":"0x0DBE004A","verified":"crc|readback|none","started":"rom|debugger|none",
    "t_ms":{"upload":…,"erase":…,"program":…,"verify":…},
    "error":null | {"code":"busy|no_dap|no_cfg|crc_mismatch|no_flash|refused","message":…,"hint":…}}
exit codes: 0 ok, 4 busy, 6 failed, 13 no_dap, 14 no_cfg, 15 refused (as mps3-debug/1)
```

- **The worker** takes the `jtag-bb` flock for the whole job. With P1 that alone makes harnessd refuse a swap.
- **The engine:**
  - a one-shot OpenOCD (UIO adapter, `gdb_port disabled`) when no session is up;
  - the live session's local Tcl port when one is up, so the user's gdb survives a reload (Q2);
  - flash jobs run the image's copies of `qspi_loader.bin` and `qspi_loader_jtag.tcl` (Q3).
- **The job is detached** (like `up`'s monitor), so a dropped SSH does not kill a flash write mid-sector.
- **The capability is advertised** as `"load"` in `version.capabilities` (additive in `mps3-debug/1`: `…/mps3_debug.c:42-49`).

**C2: a harnessd verb (net-protocol v0.20, additive; feature `dut_load`).** Only if non-SSH or bare-metal clients need it.

```
-> {"op":"dut","act":"status"}  <- {"ok":true,"rm":"0x01000005","targets":[{"name":"imem","kind":"ram",
     "base":"0x10000000","alias":"0x0","size":131072},…,{"name":"flash","kind":"flash","size":4194304,
     "sector":4096,"loader":"ql/1"}],"job":null,"jtag":{"held_by":null}}
-> {"op":"dut","act":"load","target":"flash","addr":"0x0","len":163840,"crc32":"0x0DBE004A","start":"rom"}
   <- {"ok":true,"push":6910}                       then the image on 6910 with header kind 3:
   magic "MPS3" | ver 1 | kind 3 | rm_slot = target (1 ram, 2 flash) | static_id | rm_id (= the loaded one) | len_words | crc32
-> {"op":"dut","act":"status"} (poll)  <- {"job":{"act":"load","state":"programming","done":…,"total":…}}
```

- It follows the `slot` pattern: check before any write, a CRC on the transport, a job polled through `status` (`P:docs/contracts/net-protocol.md:1416-1470`).
- harnessd would supervise the same worker as C1.
- It needs a pyverify client and a FakeShell model first: HM never re-implements net-protocol (`HM:pyproject.toml:15-16`).

## 6. Options, ranked

The rank is for "upload a file in HM and run it". The build order is P0 → P1 → P2 (§10).

| Rank | Option | Speed today → after v2.1 | Robustness | Who builds what | Proof status |
|---|---|---|---|---|---|
| **1 (target)** | **C. Board-side load** (C1 `mps3-debug load`; C2 harnessd `dut` + 6910 kind 3) with the UIO adapter | n/a today → RAM 16 KiB ~1 s, flash 160 KB ~10 s + M0 time (*model*) | **Best:** the board holds the flock (P1 refuses swaps), the image crosses the network once, a CRC at both ends, the job survives an HM disconnect | Linux lead: worker, designs.conf memory, loader files in the image. HM: board engine + negotiation. pyverify: the C2 client only. | **None** (design). It depends on the UIO silicon check (Fri 2 Oct) and the v2.1 integration. |
| **2 (ship first)** | **A. RAM load + run through the existing OpenOCD** (HM speaks the GDB remote protocol to the gdb port `debug up` already forwards: on-board or host) | 4 KiB ~3.5 min, 16 KiB ~14 min (board path); 1.5/6 min (host path); seconds on bare metal → <1 s after v2.1 | Good, once verify and reset-halt are in. Volatile by nature (H3). | HM only (RSP client, service, GUI, CLI). Platform: `ql_reset_halt` into the repo. | Memory writes proven (OCD4, 1 KiB round trip). A full load + run through HM: **UNPROVEN**. |
| **3 (persist)** | **B. Flash through the M0 loader**, driven by HM over the same gdb port (P1), later by the board (C) | 160 KB ~107 min (Linux), 3.5 min (bare metal) → ~10 s + M0 time | Good with the table-last ordering, per-chunk CRC and known-data checks (§9). Gated by H1. | Platform: loader v1.1 (CRC over RAM, JEDEC, optional 16 KiB build), pyverify `DebugPort` seam. HM: flash packer, flash engine, ledger, consent. | Loader **proven** 3× (Jul, Sep 24, Sep 30). Through HM: **UNPROVEN**. |
| 4 | **D. RM rebuild with baked IMEM** (Build tab, `build.generics`) | 48-55 min + ~64 s program, both today and later | Highest: reproducible, survives everything, ships with the design | Done (KIT-NANOSOC). Optional: a "bake this firmware" shortcut from the loader's file. | **Proven** |
| 5 | **E. MicroPython REPL** on `nanosoc_upy` | ~50 B/s (paced), both today and later | Volatile; Python only | Done (console). Optional: an HM "paste script" with pacing. | **Proven** |
| — | Not viable | — | — | register-at-a-time QSPI (~1.5 B/s), UART bootloader (no RX FIFO), ADP (looped), hostio4 / `mmio_*` (need a mint), `updatemem` on partials (untried) | §3.2 |

### Baseline that needs no new code

A user can already do option A by hand: run `harness-manager debug up B`, then `arm-none-eabi-gdb app.elf`, then `target extended-remote :<port>` and `restore app.bin binary 0x10000000`, then set `$sp`/`$pc`.
- Use `restore` to `0x1000_0000`, not `load` to `0x0`: §4.3.
- P0 should print this recipe in the Debug card's help. It is the fallback while P0 is built.

## 7. Recommended architecture

### 7.1 HM core seam (board-agnostic)

| Piece | Where | Shape |
|---|---|---|
| Capability | `core/capabilities.py` (append-only names: `HM:src/harness_manager/core/capabilities.py:20-47`) | `FIRMWARE_LOAD = "firmware_load"`. The pack's `CapabilitySpec` routes (`:50-73`) say what is missing: "needs a design with a CPU", "needs the board's OpenOCD or OpenOCD on this PC", "needs a harness with `load`". |
| Adapter | `core/pack.py`: a new optional `BoardSession.firmware: FirmwareAdapter \| None` beside `debug`/`deploy` (`HM:src/harness_manager/core/pack.py:639-652`) | `targets(identity) -> [MemTarget(name, kind ram\|flash, base, alias, size, sector, persist, boot)]` · `preflight(image, target, opts) -> [PreflightItem]` (the shared `preflight_refusal` rule: `:76-94`) · `load(image, target, opts, progress) -> FirmwareResult` · `crc(target, offset, length) -> int` (read-only) · `start(how)` |
| Image model | `harness_manager/services/firmware_image.py` (pure, board-free) | Parses ELF32 (EM_ARM, LE, PT_LOAD by `p_paddr`), Intel HEX and BIN into `segments[(addr, bytes)]`, plus `vectors`, `sha256` and `crc32`. No new dependency: a ~150-line ELF/HEX reader. |
| Service | `harness_manager/services/firmware.py` | Gates (lease holder, claim, board job, design, consent). Stores the image in the `ContentStore` (`kind="firmware"`, `HM:src/harness_manager/core/services.py:37-51`). Runs the job (`job.progress` phases), attaches the console before start, keeps a **flash ledger** per board, and publishes `firmware.state`. |
| Jobs + gate | `daemon/jobs.py` | A load is a board job: one per board, swaps refused meanwhile (`HM:src/harness_manager/daemon/jobs.py:1-27`). A flash write also joins the reset guard's "never reset mid-write" list (`HM:src/harness_manager/services/reset_guard.py:1-30`). |
| Events (contract) | `docs/CONTRACTS.md` event table | `firmware.state {state: idle\|loading\|verifying\|started\|failed, target, image{name,sha256,size}, crc32, verified, started, phase, detail}`. Progress rides `job.progress` like deploy. |

### 7.2 MPS3 pack (`harness_manager_mps3/firmware.py`)

**Design facts.**
- The source of truth is the overlay manifest (D5): a new additive `dut` block.
- The fallback is a pack table keyed by design number, with exactly §4.1.
- The manifest already reserves an unused `fw` key (`P:docs/contracts/overlay-manifest.md:80,210`; parsed and unused at `P:host/pyverify/pyverify/overlay.py:169`). That is where a design's default firmware would live.

**Engine RSP (P0/P1).** HM connects to the gdb port that the debug service holds or opens: the `gdb0` claim forward on a Linux board, or the host OpenOCD's port. It speaks the GDB remote protocol:
- `X` / `M` writes, `m` reads;
- `qCRC` for the on-target CRC (§9.1);
- `qRcmd` for `monitor` commands (the reset-halt recipe, `mww` REMAP, `resume`), `P` for SP/PC, `D` to detach.

The same engine works on the board path, the host path and bare metal. No file is staged on the board, and no OpenOCD Tcl port is needed (it is not forwarded: `HM:src/harness_manager_mps3/claim.py:176-183`). If HM did not start the debug session, HM leaves it as it found it. If a user's gdb is attached, HM refuses with 4 and asks them to detach (Q6).

**Engine Board (P2).** Calls `mps3-debug load` over the claim SSH (C1) when `version.capabilities` has `"load"`, else the RSP engine. Later C2 through pyverify.

**RAM start sequence ("start from the debugger", the default).** It works whatever the flash holds:
1. Reset-halt: DEMCR `VC_CORERESET`, then AIRCR `SYSRESETREQ` in `catch`.
   - Fallback: halt + SysTick off + NVIC ICER/ICPR (`P:host/pyverify/pyverify/qspi_loader.py:114-119`).
   - Then check IPSR = 0, else refuse "run".
2. Write the segments to the physical addresses (§4.3).
3. Verify with a CRC.
4. Set `SYSCON.REMAP = 1`. The address is in the SoC memmap: **UNVERIFIED**. CMSDK's default is `0x4001_F000`; confirm (Q5).
5. Set MSP = vec[0], PC = vec[1], Thumb bit.
6. Open the console (H13), then `resume` and detach.

**"Start through the boot ROM"** (DUT reset: `HM:src/harness_manager_mps3/pack.py:146-147`) is offered only when the design declares no flash boot, or HM's ledger says flash holds no table (H3/H4).

**Flash sequence (P1, `nanosoc_upy` only).**
1. Reset-halt (step 1 above).
2. `ql_enter` with the vendored loader (§9.1 self-tests).
3. Erase the table sector `0x0`; blank-check it.
4. For each 48 KiB chunk: erase its sectors, blank-check, `X`-write the chunk to `0x1001_0040`, `CMD_PROGRAM`, then a CRC of that range against the chunk's CRC.
5. Program the table sector **last**.
6. Whole-image CRC against the file's CRC.
7. Write the ledger.
8. Start through the ROM with the console open. Check that stage-0 prints `W=<the image's vec[0]>,<vec[1]>` (`P:fpga/rp/nanosoc_upy/flash_image.json:55-59`).

The mailbox layout, opcodes and timeouts are taken unchanged from `P:firmware/qspi_loader/qspi_loader_jtag.tcl:43-153`.

### 7.3 The harness contract it needs

| Need | P0 | P1 | P2 |
|---|---|---|---|
| A gdb port per core, no other gdb client | have (`mps3-debug/1`) | have | — |
| `ql_reset_halt` in the repo (`host/openocd/nanosoc_ops.tcl`) and on the image | HM sends it as `monitor` lines meanwhile | same | in the image |
| SYSCON REMAP address + "a SYSRESETREQ keeps the DAP alive" | **Q5** | Q5 | Q5 |
| `mps3-debug load` (C1) with the flock held for the whole job, `--status` polling, `"load"` capability | — | — | **Q1-Q3** |
| `designs.conf` (or the manifest) memory fields: `imem=BASE:SIZE alias=0x0 dmem=BASE:SIZE flash=sst26:4M loader=ql128` | — | — | Q4 |
| Optional C2: 6900 `dut` + 6910 kind 3 + feature `dut_load` | — | — | Q1 |

### 7.4 The design declaration (overlay manifest v0.6, additive; platform-owned)

```json
"dut": {
  "cpu": "cortex-m0", "cores": 1,
  "imem": {"base": "0x10000000", "alias": "0x0", "size": 131072},
  "dmem": {"base": "0x18000000", "size": 65536},
  "boot": "rom-flash-then-imem",          // or "rom-imem" (v2.1 nanosoc: QSPI_FLASH_PRESENT=0)
  "flash": {"part": "SST26VF064B", "reach": 4194304, "sector": 4096, "table": "boot_table/v1",
            "loader": "qspi_loader/1.1-imem128k"}      // absent = no flash target
}
```

`gen_manifest.py` would take these facts from `rm_list.tcl`, the same way `ip_class` is sourced (`P:docs/contracts/overlay-manifest.md:93-101`).

## 8. GUI and CLI

### 8.1 The Workbench "Firmware" strip

**Where.** A strip directly under the Program strip on the Workbench (`HM:src/harness_manager/web/static/js/sections/workbench.js:108-113`; the Program strip's pattern: `HM:src/harness_manager/web/static/js/sections/program.js:1-24`), with test id `part-firmware`.
- It shows only while the loaded design declares a CPU.
- Otherwise it shows one muted line: "greybox has no CPU: program nanosoc or nanosoc_upy to load firmware".

**Layout, left to right:**

1. **Drop zone.** "Drop an .elf, .hex or .bin" plus a Browse button. The file goes to `POST /firmware/inspect`, which parses it board-free and keeps it in the store.
   - The reply fills a **summary**: format; segments; entry and SP; a size bar against the target region ("4,128 B of 16 KiB IMEM"); the CRC32; and warnings ("linked at 0x0, loads at 0x1000_0000").
2. **Target chips.**
   - **RAM: run now** (the default).
   - **Flash: keep across resets.** Shown only on designs with a `flash` target, after D2. It opens the consent box (§8.2).
3. **Options.** Verify (on). Start after load (on): *from the debugger* (default) | *through the boot ROM* (only when it is safe, H3). Raw .bin only: a `Load at` field.
4. **Arm → Load.** The same interlock as Arm → Program (`HM:src/harness_manager/web/static/js/sections/program.js:34,459-468`). It needs the lease (R3, `:22-23`).
5. **Progress bar.** The phases are check → halt → load → verify → start; for flash, + enter loader → erase → program → table. It shows the rate and time left, as the deploy bar does, and states the expected time before you press Load ("about 4 min on this board's debug path").
6. **Outcome box.** For example: "Running `hello.elf` (4,128 B) from IMEM · CRC `0x1A2B3C4D` verified on the DUT · uart0 attached." On failure it names the phase, the reason, and the state the DUT is left in ("halted; IMEM holds a partial image; nothing ran").

**Also:**
- **The console underneath auto-attaches `uart0`** before start (H13).
- **The Debug rail** shows "OpenOCD in use by Load firmware" during a load.
- **The board's Overview** shows the **flash ledger**: what HM last wrote, its CRC, when, and by whom. It also shows "flash holds a boot table: nanosoc will boot it (KI 14)" and "nanosoc_multicore damages it (KI 15)".

### 8.2 Safety prompts and refusals

| Situation | Answer | Exit code |
|---|---|---|
| No lease on a hub board, or not this HM's claim | Refused, naming the holder (the existing lease gate) | 4 / 15 |
| The design has no CPU or no declared memory | "load a design with a CPU (nanosoc, nanosoc_upy)" | 13 |
| `nanosoc_multicore` | "firmware load is not supported on nanosoc_multicore: two cores, and its ROM writes the flash (KI 15)" | 15 |
| The image does not fit, overlaps itself, is the wrong ELF class/arch, or a segment falls outside every region | Refused, listing each segment | 14 |
| A bad vector table (SP outside DMEM or unaligned, Reset_Handler even or outside IMEM) | Refused to *start*. "Load without starting" stays possible. | 14 |
| Flash target | **Consent box:** what it writes (sectors, size), that it persists across resets and swaps, which designs will boot it, the current ledger entry it replaces, and the expected time. A checkbox plus typing the board's label. CLI: `--to flash --yes-flash LABEL`. | 15 without consent |
| A deploy, card or slot job, another load, or a swap is running | `HeldError`, naming the job (board gate) | 4 |
| JTAG held: another tool's on-board session, a host OpenOCD on 6921, or a user's gdb attached | `HeldError` with who and how to free it | 4 |
| The RP is in reset after a warm harness restart (`rm_ok: false`) | "program the design once first" (H12) | 15 |
| A CRC mismatch after a write | Failed. RAM: the image is not started. Flash: the table is not written, so the ROM ignores the flash (H4). The outcome says what is on the DUT now. | 6 |

### 8.3 The CLI twin

```
harness-manager firmware inspect FILE [--design nanosoc_upy] [--json]       # board-free: parse + checks
harness-manager firmware load TARGET FILE [--to ram|flash] [--at ADDR]
        [--start debugger|rom|none] [--no-verify] [--yes-flash LABEL] [--json]
harness-manager firmware start TARGET [--start debugger|rom]                 # re-start what is in IMEM
harness-manager firmware crc TARGET --flash OFFSET LENGTH                    # read-only (P1, the loader)
harness-manager firmware status TARGET                                       # a running job, the ledger
```

The exit codes are HM's existing table (`HM:src/harness_manager/core/errors.py:17-30`).

## 9. Robustness

### 9.1 CRC verify against known data

Two rules: never trust a status code, and never trust a CRC you have not seen fail.

**RAM.**
- Use `qCRC` (OpenOCD computes the CRC on the DUT) with a temporary work area in DMEM that does not overlap the image.
- The nanosoc cfg sets no work area today (`target create` with `-defer-examine` only: `P:host/openocd/nanosoc_mps3_jtag.cfg:95-96`). So HM sets one with `monitor nanosoc.cpu0 configure -work-area-phys … -work-area-size 0x400`.
- Whether OpenOCD's on-target checksum code runs on a Cortex-M0 is **UNVERIFIED**. If not, use the loader v1.1's `CMD_CRC_MEM`, or a full read-back (2× time).
- **Known-data proof per session:** CRC the first 256 B just written; compare with the host CRC **and** with `crc32(b"\0"*256)` / `crc32(b"\xff"*256)`. A match with either all-zero or all-FF is a failure, unless the image really is that.

**Flash.**
- Loader v1.1 adds `CMD_JEDEC`: the read must be `BF 26 43`. That proves the SPI read path returns real bytes, not the July all-zero RDATA.
- It also adds `CMD_CRC_MEM`: a CRC of the loader's own 760 B in IMEM (known bytes) proves the mailbox and CRC engine before any flash op.
- After each erase: blank-check (CRC = `crc32(b"\xff"*n)`).
- After each chunk: CRC of that range = the chunk's CRC.
- At the end: the whole-image CRC. For the shipped image that is `0x0DBE004A`.

### 9.2 Resume and retry

- Chunks are 48 KiB, the loader buffer (`P:firmware/qspi_loader/qspi_loader.c:33-35`). The job record keeps the plan and each chunk's state.
- **A retry** re-erases and reprograms that chunk once. A second mismatch fails the job, with the table still unwritten.
- **A resume** after any interruption (HM crash, network, Ctrl-C) re-enters the loader, CRCs every chunk, redoes only the bad ones, then writes the table.
- Timeouts are sized from the measured rate, never fixed. A `timeout 1200` would have killed the 107-min write (`MEM:m0-flash-loader-proven.md:45`).

### 9.3 Interruption recovery

- The table is erased first and written last (H4). An interrupted flash job therefore always leaves "no table": the ROM prints `Q:BADTBL` and runs IMEM, and the board is never left booting a half image.
- The ledger records "incomplete write at <time>". `firmware status` and the Overview say so and offer **Resume**.
- An interrupted RAM load is never started. The outcome says IMEM holds a partial image.

### 9.4 Image checks (board-free, at inspect time)

- ELF class, endianness and machine. Segment overlap. Every byte inside one declared region.
- Size against IMEM. A HOT segment ≤ IMEM.
- Vector-table sanity (§8.2). For flash images: the table magic and the table CRC against the HOT bytes.
- The file's sha256 is stored with the job, so "what is on the board" is always a known file.

### 9.5 Lease and design gating

- Only the lease holder may load. A Linux board must be claimed by this HM (the same route as `debug up`).
- Only designs whose declaration (manifest `dut` block, else the pack table) names the target may load.
- A load is a board job, so it holds the board gate: no swap, deploy, reset, power cycle or card job meanwhile.
- With P2 on a `harnessd-lock` board, harnessd itself refuses swaps for the whole load.

## 10. Phased plan

Hours are agent working hours. Board time is extra and needs a lease from david.

### P0: RAM load + run through the existing OpenOCD (HM only)

| # | Work | Owner | Hours |
|---|---|---|---|
| P0.1 | `firmware_image.py`: ELF/HEX/BIN parser, address translation, checks; `firmware inspect` | HM lane | 5 |
| P0.2 | GDB remote-protocol client (`X/m/P/qCRC/qRcmd/D`, packet size, retries), plus a fake gdb server with a Cortex-M0 memory model for tests | HM lane | 7 |
| P0.3 | `FirmwareService` + capability + `BoardSession.firmware` + the MPS3 adapter (design table, RSP engine, start sequence, console-first) + `firmware.state` event + CONTRACTS row | HM lane | 9 |
| P0.4 | CLI `firmware inspect\|load\|start\|status` | HM lane | 3 |
| P0.5 | GUI Firmware strip (drop, summary, RAM target, Arm → Load, progress, outcome, refusals) + browser tests | HM lane | 8 |
| P0.6 | Docs: USER_GUIDE section, the Debug card's manual recipe, a HIL_LINUX step "F-RAM" | HM lane | 2 |
| P0.7 | `ql_reset_halt` committed in `host/openocd/nanosoc_ops.tcl` from the hub copy, plus the REMAP address (Q5) | platform / Linux lead | 2 |
| | **Total** | | **~34 HM + 2 platform** |

**Board-free tests:**
- parser goldens (`hello` ELF/BIN/HEX, the upy HOT);
- each refusal row of §8.2;
- the fake gdb server: the load, a CRC mismatch, a held port, a user gdb attached, reset-halt failing → the halt+quiesce fallback;
- the job gate refuses a deploy during a load.

**Silicon proof** (board 2, `nanosoc_upy` or `nanosoc`, about 45 min at today's ~20 B/s; RAM only, so no H1 issue):
1. Load `hello_image.bin` (4128 B), verify, start from the debugger: the banner on uart0.
2. A deliberately corrupted image: the CRC is caught, nothing starts.
3. A deploy during the load is refused (4).
4. Ctrl-C mid-load: the outcome says "partial, not started".
5. Repeat step 1 through the host path (`debug.on_board=false`) and record both rates.

**Timing:** start on this team branch now. Merge after the HM v0.1.0 tag (Tue 6 Oct). Ship in the next HM release.

### P1: flash through the M0 loader (HM drives it). Gated on D2.

| # | Work | Owner | Hours |
|---|---|---|---|
| P1.1 | Loader v1.1: `CMD_JEDEC`, `CMD_CRC_MEM`, a per-command ops counter check. Keep the read-backs and BUSY-assert (H5). Re-run the qspi bench in sim before any silicon. | platform | 6 |
| P1.2 | pyverify: `M0FlashLoader` over a transport-neutral `DebugPort` (halt, read32, write_block, set_reg, resume) instead of `SwdDebugger`; ship `qspi_loader.bin` as package data with its sha256 | platform | 4 |
| P1.3 | Optional: a 16 KiB-IMEM loader build (mailbox and buffer moved) for `nanosoc` | platform | 2 |
| P1.4 | HM flash engine: the boot-table packer (`flash_pack.py`-identical, golden-tested against `flash_image.bin` md5 `45017bb6…`), table-first-erase / table-last-write, per-chunk CRC, resume, ledger, reset guard | HM lane | 10 |
| P1.5 | HM GUI/CLI: Flash target, consent box, `firmware crc`, ledger on the Overview, KI 14/15 warnings on deploy | HM lane | 6 |
| | **Total** | | **~16 HM + 12 platform** |

**Silicon proof** (attended, with david's explicit OK; board 2):
1. Read-only first: JEDEC + `CMD_CRC_MEM` + `firmware crc --flash 0x0 163840` = `0x0DBE004A` (board 2's recorded image).
2. A **4 KiB write at the scratch offset `0x100000`**, outside the boot map. About 3 min at 25 B/s. Then erase it back.
3. The full 160 KB image only on bare metal (3.5 min) or after v2.1. Today's Linux path would take ~107 min.

### P2: board-side load with the UIO adapter (v2.1)

| # | Work | Owner | Hours |
|---|---|---|---|
| P2.1 | `mps3-debug load` (C1): stdin → /run, a detached job, the flock for the whole job, one-shot UIO OpenOCD or the live session's Tcl, `--status`, `"load"` capability, the loader + Tcl + `ql_reset_halt` in the image, QEMU tests | Linux lead | 16 |
| P2.2 | `designs.conf` memory fields, or read the manifest `dut` block (Q4) | Linux lead | 2 |
| P2.3 | HM Board engine: negotiation (`"load"` → board engine, else RSP), SSH stdin streaming, status polling → `job.progress`, the same refusals | HM lane | 8 |
| P2.4 | Optional C2: harnessd `dut` verb + 6910 kind 3 + pyverify client + FakeShell | Linux lead (+ pyverify) | 20-26 |
| | **Total (C1 path)** | | **~18 Linux + 8 HM** |

**Silicon proof** (board 2, a v2.1 image with `harnessd-lock`, about 30 min):
1. RAM 16 KiB in under 5 s.
2. Flash 160 KB with CRC `0x0DBE004A` and the banner after a ROM start.
3. A swap during a load is refused by harnessd.
4. Kill HM mid-flash: the board job finishes, and `--status` shows it.

**Dependencies:** the UIO silicon check (Fri 2 Oct 12:00-12:30), the v2.1 integration review (Tue 6), and the lock proof (Tue 6 / Wed 7) (`MEM:openocd-on-harness-mvp.md:97,112`). The P2 date is the Linux lead's call (Q7).

## 11. Decisions for david (recommendation first)

1. **D1. Build "Load firmware" into HM in three phases: RAM now (P0), flash next (P1), board-side with v2.1 (P2).** *Recommend: yes.* P0 works on today's OpenOCD path, on every engine. P2 makes it fast without changing the UI.
2. **D2. DUT flash writes from HM.** *Recommend:*
   - allow them as an explicit user action on the leased board;
   - only on designs that declare flash (`nanosoc_upy`);
   - with the typed-label consent and the table-last ordering;
   - **after** the P1 read-only + scratch proofs.
   Until then, flash stays hidden. HIL and CI keep "never write the SST26".
3. **D3. Amend the harness rule** "the harness itself never writes the DUT SST26" (`P:docs/planning/linux_lanes/GDB_SERVER_PROPOSAL.md:36-37`) to "only on an explicit, leased user request (`load --target flash`), never on its own". *Recommend: yes, when P2 lands.* Without it, C cannot do flash.
4. **D4. Ship P0 although it is slow on Linux today** (~3.5 min per 4 KiB on the board path). *Recommend: yes.* Show the expected time before Load, and don't hold the HM v0.1.0 tag for it. It gets fast with v2.1 and is fast on bare metal now.
5. **D5. One source for each design's memory map: the overlay manifest's new `dut` block** (platform-owned, from `rm_list.tcl`), with HM's table as the fallback for old overlays. *Recommend: yes.* Today four places disagree (H14).

## 12. Questions for the Linux lead (exact)

1. **Q1.** Will you take a board-side `mps3-debug load` (§5.1 C1: stdin → /run, a detached job, the `jtag-bb` flock held for the whole job, `--status --json`, `"load"` in `version.capabilities`) for v2.1? Or do you want the harnessd `dut` verb + 6910 kind 3 (C2) instead of, or as well as, C1?
2. **Q2.** With the P1 lock, will a load hold the same flock so harnessd refuses a swap for the whole job? When a `mps3-debug up` session is live, should `load` run through that session's local Tcl port (the user's gdb stays attached), or answer `busy` (4)?
3. **Q3.** Can the image ship `qspi_loader.bin` (760 B, md5 `19e9db99…`), `qspi_loader_jtag.tcl` and `ql_reset_halt` under `/usr/share/mps3/openocd`? And will you move `ql_reset_halt` from the hub's `/home/david/w1_flash/ql_reset.tcl` into `host/openocd/nanosoc_ops.tcl`?
4. **Q4.** Memory facts per design: should `designs.conf` get `imem=/dmem=/flash=/loader=` fields, or should `mps3-debug` read the overlay manifest's proposed `dut` block, so there is one source (D5)?
5. **Q5.** On `nanosoc`/`nanosoc_upy`:
   - does an AIRCR `SYSRESETREQ` reset `SYSCON.REMAP` while the DAP stays up (`dap_npotrst` is the wrapper reset: `P:fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:266,546`)?
   - what is the REMAP register's address?
   - did `ql_reset_halt` halt at the ROM's reset vector on 30 Sep, or did it only stop the double fault?
6. **Q6.** In Friday's 12:00 board-2 UIO slot, can you add a timed 4 KiB `load_image` write to `0x1000_0000` and a `verify_image` with a DMEM work area? That gives the first write-only rate and tells us whether OpenOCD's on-target checksum runs on the Cortex-M0. Also: does the launcher keep `gdb_max_connections 1`, and can `mps3-debug status` report `gdb_clients` so HM can refuse before it connects?
7. **Q7.** Dates: the v2.1 image with the UIO adapter + lock, `rm_nanosoc` with `QSPI_FLASH_PRESENT=0` (KI 14), and the multicore ROM fix (KI 15). P1 and P2 proofs are scheduled against these.

## 13. Open items and UNVERIFIED

- **The write rate on every path** (only reads and mixed traffic were measured): Q6.
- **The M0's erase+program time for 160 KB** without the upload. It is below 3 min 29 s, but the exact time is unknown. The loader programs 4 bytes per page-program command (`P:firmware/qspi_loader/qspi_loader.c:231-263`), although `SPI_CMD.N_RW_BYTES` is 4 bits wide (`P:host/pyverify/pyverify/qspi_flash.py:30-40`). Whether 16-byte programs work is **UNVERIFIED**; it could be a later loader speed-up.
- **`qCRC` on-target checksum on Cortex-M0, and OpenOCD gdb-server behaviour under `qRcmd`** (resume, then `m` reads while running): both are standard OpenOCD 0.12 features, but this build has not been exercised from HM (P0.2 tests against a fake first).
- **What a write to `0x0` does while REMAP=0** (silent or a bus error): §4.3. HM avoids it by translating addresses.
- **`nanosoc_multicore` CPU1 IMEM size** (16 vs 64 KiB): §4.1. Moot while multicore is refused.
- **The loader not entering on a failed-boot empty XiP RM** (`MEM:flash-boot-on-fpga-proven.md:52-55`): no repo record. P1's read-only proof covers it.
