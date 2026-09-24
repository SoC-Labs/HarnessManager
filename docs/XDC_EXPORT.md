# XDC export

Harness Manager writes constraint files for your own design from the board pack's pin model. There are two kits:

- **the RM kit**, for a reconfigurable module (RM) that loads into the harness shell's partition;
- **the full-board export**, for a whole-FPGA design that replaces the harness.

No board is needed. The kits come from the pin model, not from a live board.

## Quick start

```
harness-manager xdc info                                   # the model, the fielded shell, the designs
harness-manager xdc rm-kit --design nanosoc --out build/xdc
harness-manager xdc board  --design my_design.json --out build/xdc
```

Without `--out`, the verb shows the checks and the files it would write. `--json` prints everything, including the file contents. In the web UI, the **XDC** section of a board does the same: pick a kit and a design (or paste one), preview the files next to the checks, and download a zip.

## The pin model

The MPS3 model is `src/harness_manager_mps3/pins/mps3_board_pins.json`. It is **derived**: harness Lane C's `board_pins.yaml` does not exist yet. `tools/gen_mps3_pins.py` generates the model; never edit it by hand. It reads:

| Source | What it gives |
|---|---|
| `fpga/monolithic/nanosoc_mps3.xdc` + `nanosoc_mps3_top.sv` | every board net: the package pin, the IO standard, the direction (Arm's MPS3 pinmap, ported verbatim) |
| `fpga/shell/constraints/mps3_harness.xdc` + `optional/mps3_harness_touch.xdc` | which nets the fielded static owns (the mint flags say `SHELL_TOUCH=1`, `SHELL_REALPHY=0`) |
| `docs/contracts/partition-pins.md`, `fpga/shell/boundary.yaml`, `fpga/shell/rp_dut_stub.sv` | the partition boundary, read three ways; all three must agree |
| `fielded/0x72BB0A36/mint.json` | the hash of every file the fielded static was built from; each shell source must match it |
| `docs/contracts/partition-timing.md` | the clocks the shell drives across the boundary |
| `fpga/dfx/dfx_floorplan.xdc` and the cited reports | the pblock facts |
| `fpga/mps3_sd/templates/nanosoc.txt` | the oscillator frequencies the MCC programs |
| the Xilinx IBIS package file `xcku115_flvb1760.pkg` (ships with Vivado; no licence, no Vivado run) | the bank of every pin, and whether it is clock-capable (`_GC_`) |

**How the sources are read.** The generator reads the platform repo only through `git show <ref>:<path>`, so the branch the working tree is on never matters. The default ref is `feat/rm-ila-mint`, which holds the fielded static `0x72BB0A36`'s boundary: 47 ports, 148 bits, 20 decoupler interfaces.

**Provenance.** Every fact carries `src: "<source key>:<line>"`. The `sources` table gives each key's path, commit, sha256 and last change. `fielded_static_input: true` marks a file whose hash equals the fielded static's build input.

**How sure each net is.** Every net carries `verified`:

| Level | Meaning |
|---|---|
| `hw-proven` | the fielded shell places it, and the board runs it |
| `measured` | measured on the bench, in a shell variant that is not fielded |
| `pinmap` | Arm's pinmap; this platform does not exercise it |
| `inferred` | a bench report or a historical survey, not a pinmap |

**Bank voltages.** The generator infers VCCO from the IO standards the board's own pinmap uses in each bank. A bank with no board net has no VCCO in the model, and any use of it fails the bank check with that reason.

**Regenerating.**

```
.venv/bin/python tools/gen_mps3_pins.py            # write the model
.venv/bin/python tools/gen_mps3_pins.py --check    # exit 1 if the committed model is stale
```

Options: `--platform DIR` (default `../mps3-nanosoc-platform` or `$HM_PLATFORM_DIR`), `--ref BRANCH` (or `$HM_PLATFORM_REF`), `--pkg FILE`.

The generator refuses to write a wrong model: it exits 2 if the three boundary renders disagree, a shell file differs from the fielded build input, a shell pin is not in the pinmap, a bank needs two voltages, or a cited sentence has left its file.

## Designs

A design is a JSON document. The built-in designs are in `src/harness_manager_mps3/pins/designs/`:

| Name | Kit | What it is |
|---|---|---|
| `minimal` | rm-kit | clock, resets and `rm_id`; everything else tied off (`fpga/rp/_template`) |
| `nanosoc` | rm-kit | single-core nanoSoC, as `fpga/rp/nanosoc` constrains it |
| `nanosoc_ila` | rm-kit | nanoSoC with an RM debug hub and ILAs over XVC |
| `blinky` | board | the 50 MHz clock, a button, 8 LEDs, 8 switches |
| `shield_gpio` | board | 16 GPIO on the SH0 shield header, a clock on SH1 |
| `harness_shell` | board | the fielded shell's own 78 pads, rebuilt from the model |

### An RM design

```json
{
 "kind": "rm",
 "name": "my_rm",
 "rm_id": "0x0100_00F0",
 "use": {"clkrst": {}, "uart": {}, "status": {}, "gpio": {}, "eth": {"timed": true}},
 "clocks": ["jtag_tck"],
 "wrapper": "rtl/my_rm_wrapper.sv"
}
```

- **`use`**: the boundary groups the RM drives or reads: `clkrst`, `jtag`, `dbgbscan`, `eth`, `uart`, `status`, `gpio`, `qspi`. A group that is not listed is tied off.
  - `{"timed": true}`, or a list of signals, keeps that group's data out of the false paths, for an RM that times it synchronously (a MAC's RMII data, a QSPI controller). With `eth` timed, `phy_rmii_ref_clk` is declared.
- **`clocks`**: extra boundary clocks, such as `jtag_tck` (OOC-only) or `phy_rmii_ref_clk` (an RM debug hub's clock). The form `{"port", "period_ns"}` is checked against the shell's clock contract.
- **`wrapper`**: an ANSI (System)Verilog header, relative to the design file. Alternatively, `"ports": [{"name", "dir", "width"}]`. The port list is checked against the boundary, like the platform's `pin_check`.
- **`pins`**: package-pin requests. The partition has no IO sites, so each one fails with the reason.
- **`static_id`**: the static the RM is for. The default is the model's fielded shell.

### A board design

```json
{
 "kind": "board",
 "name": "my_top",
 "ports": [
  {"port": "clk", "net": "OSCCLK[1]", "dir": "in", "clock_mhz": 50},
  {"port": "led_n[7:0]", "net": "USER_nLED[7:0]", "dir": "out"},
  {"port": "gpio[3:0]", "net": "SH0_IO[3:0]", "dir": "inout"},
  {"port": "raw", "pin": "AW16", "iostandard": "LVCMOS33", "dir": "in"}
 ]
}
```

- A port names a board **net**, or a raw **pin**.
- `dir` defaults to the net's direction.
- `iostandard` defaults to the net's.
- `props` adds pad properties, for example `{"PULLUP": "true"}`.
- `clock_mhz` declares a clock.

## The kits

**The RM kit** writes the following files:

| File | What it is |
|---|---|
| `<name>_ooc.xdc` | out-of-context timing for your OOC synthesis, by boundary group (see the list below) |
| `<name>_connectivity.md`, `.csv` | every boundary signal: RM direction, used or tied off, timing treatment, the decoupler's clamp during a swap, what it reaches in the static (block, host service), and the board net and pin where it lands |
| `<name>_pblock.md` | the partition's floorplan, with sources: clock regions, SLR, site types, capacity, no IO/BUFG/BSCAN sites, and the BUFGCE that drives `dut_clk` |
| `<name>_wrapper_skeleton.sv` | every partition port, with the unused groups tied to their safe-idle value |

The OOC XDC holds:

- the shell's clocks, with the partition-timing periods;
- `HD.CLK_SRC`;
- one asynchronous clock-group set;
- a `-quiet` false path for every boundary crossing the design does not time itself.

For the nanoSoC designs, the OOC XDC matches the platform's own `nanosoc_ooc.xdc` and `nanosoc_ila_ooc.xdc`: the same clocks, periods and false-path port sets. A test holds it to that.

**The full-board export** writes three files:

- `<name>_pins.xdc`: `PACKAGE_PIN`, one line per port bit, with the net, bank and verification level;
- `<name>_io.xdc`: IO standards and pad properties, grouped by bank with its VCCO, plus the configuration voltage;
- `<name>_timing.xdc`: `create_clock` for every clock port. The model has no board trace delays, so IO delays are yours to add.

Both kits also write `manifest.json`: the design, every check, the facts, and the model's provenance.

## Checks

Every check is an **error** with a reason and, usually, a hint. **Any error refuses the export.** The CLI then exits 15 (REFUSED), and `--json` lists every check in `error.data.checks`; the daemon answers 409 with the same data. A preview still shows the files next to the failures. Notes travel with the files.

| Code | Fires when |
|---|---|
| `pin_conflict` | two ports on one pin; a reserved net (FPGA UART lane 0 is the MCC's console); a pin request in an RM (the partition has no IO sites) |
| `bank_voltage` | an IO standard whose VCCO is not the bank's; a bank whose VCCO the model does not know; an unknown standard; no standard at all |
| `direction` | driving a net the board drives (switches, oscillators); reading a net nothing drives (LEDs); an RM port whose direction is not the boundary's |
| `clock_capable` | a board clock on a non-GC pin; an RM clock on a boundary signal the shell does not drive as a clock |
| `clock_period` | a board clock that disagrees with the oscillator the MCC programs; an RM clock that disagrees with the static |
| `missing_pin` | a net or pin the model does not have; a boundary port the RM lacks; a port the boundary does not have (that would be a re-mint) |
| `width` | a bus onto a different width |
| `static_id` | an RM for a static the model does not describe, or, on a board route, a board that runs a different static |
| `syntax` | a generated XDC failed the built-in syntax check (a generator bug) |
| notes | `timed_group`, `tied_off`, `caution` (for example, SH\*_IO16/17 connect through 4K7 to IO14/15) |

## The daemon API (additions, CCR T10-2)

| Method and path | Returns |
|---|---|
| `GET /xdc?pack=mps3` | the catalogue: the model summary, the kits, the designs and the check codes |
| `POST /xdc/export` `{pack?, kit, design?, static_id?, preview?, format?}` | the kit (see the rules below) |
| `GET /boards/{bid}/xdc` | the catalogue for the board's pack, plus `board: {static_id, model_static_id, matches, reason}` |
| `POST /boards/{bid}/xdc/export` `{kit, design?, preview?, format?}` | the same, checked against the static the board runs |

Rules for the export routes:

- `design` is a built-in name or an inline design object. File paths are refused, because the daemon's filesystem is not the caller's.
- `preview: true` answers 200 with `{kind, design, ok, files, checks, facts}`, even when a check fails.
- Otherwise a failed check is 409 REFUSED with `error.data.checks`.
- `format: "zip"` returns `application/zip`: the files plus `manifest.json`.

The kits never touch the board. A board route reads only the static the board runs: the identity it reported when it was probed, or else one identity read under the board gate.

## What is not modelled yet

- **DDR4** (banks 49–51) and **FMC**: the pinmap has no FMC pins.
- **Connector pin numbers** (Pmod J28/J34): bench reports from the historical `docs/CONNECTOR_SURVEY.md`, marked `inferred`. Harness Lane C2 transcribes the TRM tables.
- **Board trace delays**: there are none in the model, so there are no IO delay constraints.
- **HP/HR bank type**: given only where a 3.3 V standard proves HR.
- **QSPI XiP I/O timing**: the signoff is owed on the shell side (`partition-pins.md`).
