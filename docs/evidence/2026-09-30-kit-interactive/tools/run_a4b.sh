#!/bin/bash
cd /tmpdir/claude-74755/kit-interactive/runs/a4
echo "A4B start $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
nice -n 10 /usr/bin/time -v -o /tmpdir/claude-74755/kit-interactive/runs/a4/a4b_time_v.txt /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode tcl -log /tmpdir/claude-74755/kit-interactive/runs/a4/a4b_vivado.log -journal /tmpdir/claude-74755/kit-interactive/runs/a4/a4b_vivado.jou < /tmpdir/claude-74755/kit-interactive/runs/a4/a4b_stdin.tcl > /tmpdir/claude-74755/kit-interactive/runs/a4/a4b_stdout.txt 2>&1
echo "A4B vivado rc=$? end $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
