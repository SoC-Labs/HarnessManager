#!/bin/bash
cd /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_a2
echo "A3 start $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
nice -n 10 /usr/bin/time -v -o /tmpdir/claude-74755/kit-interactive/runs/a3/time_v.txt xvfb-run -a /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode gui -source /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_a2/build_rm.tcl -log /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_a2/build_rm.log -journal /tmpdir/claude-74755/kit-interactive/home/builds/lfsr_a2/build_rm.jou -script /tmpdir/claude-74755/kit-interactive/runs/a3/a3_after.tcl -tclargs STOP_AFTER=link < /dev/null > /tmpdir/claude-74755/kit-interactive/runs/a3/stdout.txt 2>&1
echo "A3 vivado rc=$? end $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
