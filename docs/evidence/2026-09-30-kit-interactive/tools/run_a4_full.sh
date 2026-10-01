#!/bin/bash
cd /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_floor
echo "A4FULL start $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
nice -n 10 /usr/bin/time -v -o /tmpdir/claude-74755/kit-interactive/runs/a4full/time_v.txt /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode batch -source /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_floor/build_rm.tcl -log /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_floor/build_rm.log -journal /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_floor/build_rm.jou -tclargs JOBS=4 > /tmpdir/claude-74755/kit-interactive/runs/a4full/stdout.txt 2>&1
echo "A4FULL vivado rc=$? end $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
