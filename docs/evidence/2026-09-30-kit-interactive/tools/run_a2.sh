#!/bin/bash
cd /tmpdir/claude-74755/kit-interactive/runs/a2
echo "A2 start $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
nice -n 10 /usr/bin/time -v -o /tmpdir/claude-74755/kit-interactive/runs/a2/time_v.txt /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode tcl -log /tmpdir/claude-74755/kit-interactive/runs/a2/vivado_a2.log -journal /tmpdir/claude-74755/kit-interactive/runs/a2/vivado_a2.jou < /tmpdir/claude-74755/kit-interactive/runs/a2/a2_stdin.tcl > /tmpdir/claude-74755/kit-interactive/runs/a2/stdout.txt 2>&1
echo "A2 vivado rc=$? end $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
