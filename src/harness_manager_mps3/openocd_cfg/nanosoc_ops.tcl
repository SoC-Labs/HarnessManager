# nanosoc_ops.tcl — reproducible SWD op recipes for the nanoSoC DUT.
#
# Named OpenOCD procs for the common debug operations, keyed to the nanoSoC
# memory map, so the recipes aren't re-derived by hand each session. Layers
# on the two committed cfgs (swd_remote_bitbang.cfg -> bare SW-DP transport,
# swd_cortex_m.cfg -> the cortex_m target) — source all three, target first:
#
#   openocd -f host/openocd/swd_remote_bitbang.cfg \
#           -f host/openocd/swd_cortex_m.cfg \
#           -f host/openocd/nanosoc_ops.tcl \
#           -c init -c "nanosoc_load_app /abs/app.bin" -c nanosoc_reset_run \
#           -c shutdown
#
# These are the same op batches pyverify.swd.SwdDebugger emits
# (host/pyverify/pyverify/swd.py) — the memory-map constants below MUST stay
# in lockstep with that module. The Python API is the primary interface (it
# also does the <hub-host> ssh hub-relay + output parsing); this file is
# the raw-OpenOCD equivalent for a hand-run session on the hub.
#
# Memory map (docs/SWD_BRINGUP_PLAN.md §Appendix; docs/nanosoc_m0_soc/
# RTL_ANATOMY.md "Base addresses", from nanosoc_memmap.h):
set NANOSOC_IMEM_BASE   0x10000000  ;# 64KB; the app lives here, remaps to 0x0 post-boot
set NANOSOC_IMEM_SIZE   0x00010000
set NANOSOC_IMEM_REMAP  0x00000000  ;# post-boot alias (running vectors / PC live here)
set NANOSOC_DMEM_BASE   0x18000000  ;# 64KB (reset SP 0x1800FC00 = top of DMEM)
set NANOSOC_SCS_CPUID   0xE000ED00  ;# Cortex-M SCS CPUID (0x410CC200 = M0)

# nanosoc_cpuid — read the SCS CPUID (proves DP + AP/AHB-AP->core path).
proc nanosoc_cpuid {} {
    global NANOSOC_SCS_CPUID
    mdw $NANOSOC_SCS_CPUID
}

# nanosoc_halt_pc — halt and print PC (reg needs a halted core). On the
# proven DUT this reads a remapped-IMEM address (e.g. 0x378 = app running).
proc nanosoc_halt_pc {} {
    halt
    reg pc
}

# nanosoc_dump {addr {nwords 4}} — dump nwords 32-bit words from addr.
proc nanosoc_dump {addr {nwords 4}} {
    mdw $addr $nwords
}

# nanosoc_dump_imem {{nwords 8}} — dump the head of physical IMEM (the vector
# table / entry that a load_image just wrote).
proc nanosoc_dump_imem {{nwords 8}} {
    global NANOSOC_IMEM_BASE
    mdw $NANOSOC_IMEM_BASE $nwords
}

# nanosoc_load_app {path {addr 0x10000000}} — load a DUT firmware image into
# IMEM over SWD (halt first; you cannot reliably load a running core). Pass a
# .bin (needs addr, default IMEM base) or leave addr as the ELF's own by using
# load_image directly. LANDMINE: always follow with nanosoc_reset_run before
# running — a fresh image started without a reset can wedge in Default_Handler
# (old SysTick/NVIC still armed; ARMv6-M can't clear an ACTIVE exception —
# SWD_BRINGUP_PLAN.md Rung 6 / lab note swd-load-systick-wedge).
proc nanosoc_load_app {path {addr 0x10000000}} {
    halt
    load_image $path $addr
    echo "nanosoc_load_app: loaded $path @ $addr — run nanosoc_reset_run before executing"
}

# nanosoc_reset_run — reset the core and run (clears SysTick/NVIC after a load).
proc nanosoc_reset_run {} {
    reset run
}

# nanosoc_reset_halt — reset and halt. CAVEAT (SWD_BRINGUP_PLAN §2a):
# vector-catch reset-halt is unreliable here (the SW-DP shares the core reset
# domain). If it flaps, use nanosoc_reset_run then `halt`.
proc nanosoc_reset_halt {} {
    reset halt
}
