# host/openocd — debug front-end configs (SWD 6920 / XVC 2542)

The shell exposes **two independent debug transports** over the one
Ethernet port (docs/contracts/net-protocol.md v0.2 port map;
ARCHITECTURE_SPEC.md §10). They are consumed by **different tools** — only
one of them is OpenOCD's:

| Port | Service | Consumed by | Config here |
|---|---|---|---|
| 6920 | **SWD** — OpenOCD `remote_bitbang` byte protocol → DUT (nanoSoC CM0) debug | **OpenOCD** on the host | `swd_remote_bitbang.cfg` |
| 2542 | **XVC** — Xilinx Virtual Cable → Debug Bridge → shell/RM **ILAs** | **Vivado hw_server** (`open_hw_target -xvc_url`), *not* OpenOCD | none needed — see "XVC path" below |

Both wrap the same argv/Tcl that `pyverify.debug`
(`host/pyverify/pyverify/debug.py`) builds programmatically
(`OpenOcdRemoteBitbangConfig` / `XvcTarget`); if you change one, change the
other.

## SWD path (OpenOCD, TCP 6920)

`swd_remote_bitbang.cfg` sets up OpenOCD's `remote_bitbang` adapter
(net-protocol.md v0.2: `O/o/c` SWDIO drive/release/sample, `d/e/f/g`
{CLK,DIO} combos, `r/s/t/u` trst/srst — **srst maps onto `dbg_resetn`** —
`B/b` LED, `Q` quit) plus a minimal DAP declaration, enough to read DPIDR.
Requires OpenOCD ≥ the Jan-2021 remote_bitbang-SWD merge (any 0.12.0+
release).

DPIDR smoke test (the "is the whole SWD chain alive?" one-liner):

```sh
openocd -c "set SHELL_HOST 192.168.10.101" \
        -f host/openocd/swd_remote_bitbang.cfg \
        -c init -c shutdown
# success: a line like  Info : SWD DPIDR 0x0bb11477
# (0x0bb11477 = Cortex-M0 SW-DP; confirm the real value at bring-up)
```

Equivalent pure-`-c` invocation — this is byte-for-byte what
`pyverify.debug.OpenOcdRemoteBitbangConfig("192.168.10.101").command()`
generates (it sources `target/nanosoc_mps3.cfg`, the full target config
that doesn't exist yet — A2/A3 territory; the .cfg here declares a bare
DAP instead so the smoke test works today):

```sh
openocd \
  -c 'adapter driver remote_bitbang' \
  -c 'remote_bitbang host 192.168.10.101' \
  -c 'remote_bitbang port 6920' \
  -c 'transport select swd' \
  -c 'adapter speed 1000' \
  -c 'source [find target/nanosoc_mps3.cfg]' \
  -c init -c '<op>' -c shutdown
```

From Python: `pyverify.debug.launch_openocd(OpenOcdRemoteBitbangConfig("192.168.10.101"))`.

### High-level SWD API + op recipes (halt / read / load_image)

`swd_remote_bitbang.cfg` alone is enough for the DPIDR smoke; the
`cortex_m` target for **halt / read PC / read+write memory / load_image /
reset** comes from `swd_cortex_m.cfg` (source it *after* the bare cfg —
order matters). Two ways to drive the named operations, both keyed to the
same nanoSoC memory map (**IMEM `0x1000_0000`** = where the app lives,
remaps to `0x0` post-boot; DMEM `0x1800_0000`):

**1. From Python — `pyverify.swd.SwdDebugger`** (the primary interface: it
adds the `<hub-host>` **ssh hub-relay** and parses OpenOCD's text into
Python values). See `host/pyverify/pyverify/swd.py`.

```python
from pyverify.swd import SwdDebugger
dbg = SwdDebugger()                    # OpenOCD on <hub-host>, .101:6920
hex(dbg.dpidr())                       # '0xbb11477'  (Cortex-M0 SW-DP)
hex(dbg.cpuid())                       # '0x410cc200' (M0 identity)
hex(dbg.read_pc())                     # halt + reg pc
dbg.load_image("firmware/app.bin")     # -> IMEM 0x10000000  (the payoff)
dbg.reset_run()                        # clears SysTick/NVIC — REQUIRED after a load
SwdDebugger(dry_run=True).load_image("app.bin")   # preview the argv, no board
```

**2. Raw OpenOCD — `nanosoc_ops.tcl`** (named procs for a hand-run session
on the hub; the raw-OpenOCD twin of the Python API). Source all three cfgs:

| Operation | `-c` batch (after `-f …swd_remote_bitbang.cfg -f …swd_cortex_m.cfg -f …nanosoc_ops.tcl`) |
|---|---|
| DPIDR (bare) | `-c init -c shutdown` (drop the cortex_m + ops cfgs) |
| Halt + read PC | `-c init -c nanosoc_halt_pc -c shutdown` |
| CPUID identity | `-c init -c nanosoc_cpuid -c shutdown` |
| Dump IMEM head | `-c init -c "nanosoc_dump_imem 8" -c shutdown` |
| **Load a DUT image** | `-c init -c "nanosoc_load_app /abs/app.bin" -c nanosoc_reset_run -c shutdown` |
| Reset + run | `-c init -c nanosoc_reset_run -c shutdown` |

**Exact operator command to load new DUT firmware over SWD** (on the hub;
`SwdDebugger().load_image(...)` emits the ssh-wrapped equivalent):

```sh
ssh <hub-host> 'cd ~/SoCLabs/mps3-nanosoc-platform && \
  openocd -f host/openocd/swd_remote_bitbang.cfg \
          -f host/openocd/swd_cortex_m.cfg \
          -f host/openocd/nanosoc_ops.tcl \
          -c init -c "nanosoc_load_app /abs/path/app.bin" \
          -c nanosoc_reset_run -c shutdown'
```

**LANDMINE:** always `nanosoc_reset_run` (or `reset run`) *after* a load —
a fresh image started without a reset can wedge in `Default_Handler`
(old SysTick/NVIC armed; ARMv6-M can't clear an ACTIVE exception —
`docs/SWD_BRINGUP_PLAN.md` Rung 6). SWD is a *correctness* channel, not a
throughput one (§5): seconds per DPIDR, minutes to load a few KB.

## XVC path (Vivado, TCP 2542) — *not* an OpenOCD config

XVC on 2542 is served by the shell's `xvc_server` (A3) in front of the
Debug Bridge IP and carries **ILA/VIO traffic for Vivado's hardware
manager**. OpenOCD plays no role here — pointing OpenOCD at 2542 is a
category error (OpenOCD's own XVC support is for using an XVC *server* as
a JTAG cable to a DAP, which is the 6920/SWD job on this platform).
Attach with Vivado (or `vivado_lab`) instead:

```tcl
open_hw_manager
connect_hw_server
open_hw_target -xvc_url 192.168.10.101:2542   ;# pyverify XvcTarget.open_hw_target_tcl()
```

After every DFX swap the hw_target is stale and the new RM's probes file
must be reloaded (spec §6.3/§10 "XVC can't detect overlay replacement");
this is exactly `pyverify.debug.XvcTarget.refresh_ila_tcl(ltx)` (which
assumes the hw_manager/hw_server from the snippet above are already open):

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
(`pyverify.overlay`, docs/contracts/overlay-manifest.md), not from
anything in this directory.

## Validation status (2026-07-06, W-HOST-MISC)

No `openocd` binary exists on this dev machine (checked PATH,
/usr/local, /opt, environment modules), so `swd_remote_bitbang.cfg` has
**not** had an `openocd -f ... -c init` config-parse dry run here; it is
written against the OpenOCD 0.12 command set (`adapter driver
remote_bitbang`, `remote_bitbang host/port`, `transport select swd`,
`reset_config srst_only`, `swd newdap`, `dap create`) and mirrors
`pyverify.debug`'s tested argv builder. First host with OpenOCD
installed: run the DPIDR smoke command above against a live shell —
expect it to fail at **connect** ("Connection refused" to
192.168.10.101:6920) when no shell is up, never at config parse.
