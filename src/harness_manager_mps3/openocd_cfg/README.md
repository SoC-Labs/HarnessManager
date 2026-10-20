# host/openocd — debug front-end configs (JTAG 6921 / XVC 2542)

The shell exposes **two independent debug transports** over the one Ethernet
port (`docs/contracts/net-protocol.md` port map). They are consumed by
**different tools**; only one of them is OpenOCD's:

| Port | Service | Consumed by | Config here |
|---|---|---|---|
| 6921 | **JTAG** — the shell's `jtag_server`, OpenOCD `remote_bitbang` (JTAG half) → `jtag_bb` (`0x44A7_0000`) → DUT SoC-400 SWJ-DP in JTAG mode | **OpenOCD** (host, or the board's own via `mps3-debug`) | `nanosoc_mps3_jtag.cfg` + `nanosoc_ops.tcl` |
| 2542 | **XVC** — Xilinx Virtual Cable → Debug Bridge → shell/RM **ILAs** | **Vivado hw_server** (`open_hw_target -xvc_url`), *not* OpenOCD | none needed — see "XVC path" below |

TCP 6920 is reserved: it was the retired SWD `swd_server`; never reuse it.

| File | What |
|---|---|
| `nanosoc_mps3_jtag.cfg` | nanoSoC DUT target: transport (`TRANSPORT_MODE` rbb / xvc / uio) + TAP `0x6ba00477` + `cortex_m` |
| `nanosoc_mps3_multicore_jtag.cfg` | `rm_nanosoc_multicore`: one SWJ-DP (JTAG mode), two AHB-APs |
| `nanosoc_qs_mps3_jtag.cfg` | Quickstart nanoSoC (design `0x000B`, rm_nanosoc_qs v3): Cortex-M0 JTAG-DP, IDCODE `0x0ba01477` |
| `nanosoc_iice_chain.cfg` | the IICE RM: two TAPs on the one set of `jtag_*` partition pins |
| `nanosoc_ops.tcl` | named op recipes (halt, read PC, dump, load, reset) |

## JTAG path (OpenOCD, TCP 6921)

The DUT's SoC-400 SWJ-DP is driven in **JTAG mode** (TAP IDCODE
`0x6ba00477`). Overrides go **before** `-f` (the cfg sets defaults only
`if ![info exists …]`, so a later `-c "set …"` is silently ignored):

```sh
openocd -c "set TRANSPORT_MODE rbb" -c "set RBB_HOST <board-ip>" \
        -f host/openocd/nanosoc_mps3_jtag.cfg \
        -f host/openocd/nanosoc_ops.tcl \
        -c init -c nanosoc_halt_pc -c resume -c shutdown
```

**On the board instead:** the Linux harness image ships OpenOCD as the
`mps3-debug` package (`harness-manager debug up`, or `mps3-debug up` over
SSH); its `designs.conf` maps each design ID to the right cfg (`0x000B` →
`nanosoc_qs_mps3_jtag.cfg`). Then only Arm GDB is needed on the PC.

### Op recipes (`nanosoc_ops.tcl`)

Source it *after* the target cfg. Keyed to the nanoSoC memory map
(**IMEM `0x1000_0000`** = where the app lives, remaps to `0x0` post-boot;
DMEM `0x1800_0000`):

| Operation | `-c` batch (after `-f …nanosoc_mps3_jtag.cfg -f …nanosoc_ops.tcl`) |
|---|---|
| Halt + read PC | `-c init -c nanosoc_halt_pc -c shutdown` |
| CPUID identity | `-c init -c nanosoc_cpuid -c shutdown` |
| Dump IMEM head | `-c init -c "nanosoc_dump_imem 8" -c shutdown` |
| **Load a DUT image** | `-c init -c "nanosoc_load_app /abs/app.bin" -c nanosoc_reset_run -c shutdown` |
| Reset + run | `-c init -c nanosoc_reset_run -c shutdown` |
| Reset + halt | `-c init -c nanosoc_reset_halt -c shutdown` |

**LANDMINE:** always `nanosoc_reset_run` (or `reset run`) *after* a load —
a fresh image started without a reset can wedge in `Default_Handler`
(old SysTick/NVIC armed; ARMv6-M can't clear an ACTIVE exception).
The bit-banged link is a *correctness* channel, not a throughput one.

The DUT flash writer (maintainer-only) is `firmware/qspi_loader/` driven by
`qspi_loader_jtag.tcl` over the same 6921 link.

## XVC path (Vivado, TCP 2542) — *not* an OpenOCD config

XVC on 2542 is served by the shell's `xvc_server` in front of the Debug
Bridge IP and carries **ILA/VIO traffic for Vivado's hardware manager**.
Attach with Vivado (or `vivado_lab`):

```tcl
open_hw_manager
connect_hw_server
open_hw_target -xvc_url 192.168.10.101:2542   ;# pyverify XvcTarget.open_hw_target_tcl()
```

After every DFX swap the hw_target is stale and the new RM's probes file
must be reloaded; this is `pyverify.debug.XvcTarget.refresh_ila_tcl(ltx)`
(assumes the hw_manager/hw_server from the snippet above are open):

```tcl
open_hw_target -xvc_url 192.168.10.101:2542   ;# never a bare open_hw_target: that opens the FIRST target
set_property PROBES.FILE overlay/nanosoc/nanosoc.ltx [current_hw_device]
refresh_hw_device [current_hw_device]
```

Batch form, matching `pyverify.debug.launch_vivado_xvc_tcl` (script on
stdin, no journal/log litter):

```sh
echo 'open_hw_manager
connect_hw_server
open_hw_target -xvc_url 192.168.10.101:2542' \
  | vivado -mode batch -nojournal -nolog -source /dev/stdin
```

The per-RM `.ltx` path comes from the overlay manifest
(`pyverify.overlay`, `docs/contracts/overlay-manifest.md`), not from
anything in this directory.
