# nanosoc.json: the nanoSoC RM design file for HM's kit

One generic design file for the single-core nanoSoC (Cortex-M0) RM on the MPS3 DFX partition.
It holds no machine paths: every path is relative to the folder the file sits in.

## What HM can and cannot do about roots

HM resolves a relative path in a design file against the design file's own folder. It has no
variable or environment expansion. So the Arm IP library, which lives outside any shipped tree,
is reached through a folder **next to the file**: a symlink (or a copy) named `arm_ip_library`.

## Layout (everything next to `nanosoc.json`)

```
nanosoc.json
nanosoc_m0_soc/     the gated nanoSoC source (nanosoc_arch_tech submodule checked out)
ahb_qspi/           the ahb_qspi IP checkout
platform/           the platform files the wrapper needs, same sub-paths as the platform repo:
  fpga/rp/nanosoc/rp_nanosoc_wrapper.sv   (the RM top)
  fpga/rp/nanosoc/uart_axis_shim.sv
  fpga/rp/nanosoc/hello_image.hex         (IMEM image; swap for your own)
  fpga/shell/ip/clcd/clcd_core.sv
  fpga/rp/nanosoc_exp/ahb_clcd.sv
  fpga/rp/nanosoc_exp/nanosoc_exp_socket.sv
arm_ip_library      -> your Arm Academic Access library (what ARM_IP_LIBRARY_PATH names)
```

## Before you build

1. `ln -s "$ARM_IP_LIBRARY_PATH" arm_ip_library` (next to `nanosoc.json`).
2. Generate the stage-0 boot ROM: `make -C nanosoc_m0_soc/pynq firmware`. The design reads
   `nanosoc_m0_soc/imp/fpga/firmware/stage0/{nanosoc_region_bootrom.v,bootrom.sv}`, which are build outputs.
3. `nanosoc_m0_soc/build_soc/rtl/` must be the generated SoC (it is tracked in the repo).

## Build

Needs Vivado 2026.1 on PATH and the MPS3 kit (`mps3_rc2_0x44EE76D5_kit.zip`). From this folder:

```
harness-manager kit import mps3_rc2_0x44EE76D5_kit.zip
harness-manager kit script --static-id 0x44EE76D5 --design nanosoc.json --out ~/builds/nanosoc --jobs 4
harness-manager kit build ~/builds/nanosoc          # prints the Vivado command; run it
harness-manager kit check ~/builds/nanosoc
harness-manager kit pack ~/builds/nanosoc --import
```

Budget about 50 minutes and 5 GB of RAM.

## What is in it

- `rm_id` `0x01008BC3`: HM's stable proposal for the name `nanosoc`. It differs from the fielded
  nanosoc's id (`0x01000001`), so your overlay never shadows the fielded one.
- `build.sources`: 246 files in compile order (packages first). `build.defines`: `RAM_PRELOAD`.
- `build.generics.IMEM_MEM_FPGA_IMG`: the IMEM image, `platform/fpga/rp/nanosoc/hello_image.hex`.
  The wrapper's own default is an absolute path on one machine, so the generic is required.
  To run other firmware, put its word-format hex there (or change the path).
