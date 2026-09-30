#!/bin/bash
cd /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_floor
echo "A1 start $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
nice -n 10 /usr/bin/time -v -o /tmpdir/claude-74755/kit-interactive/runs/a1/time_v.txt /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode tcl -source /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_floor/build_rm.tcl -log /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_floor/build_rm.log -journal /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_floor/build_rm.jou -tclargs STOP_AFTER=link JOBS=4 < /tmpdir/claude-74755/kit-interactive/runs/a1/a1_stdin.tcl > /tmpdir/claude-74755/kit-interactive/runs/a1/stdout.txt 2>&1
echo "A1 vivado rc=$? end $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
