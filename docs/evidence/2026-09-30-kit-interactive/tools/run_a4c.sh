#!/bin/bash
cd /tmpdir/claude-74755/kit-interactive/runs/a4
echo "A4C start $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
nice -n 10 /usr/bin/time -v -o /tmpdir/claude-74755/kit-interactive/runs/a4/a4c_time_v.txt /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode tcl -log /tmpdir/claude-74755/kit-interactive/runs/a4/a4c_vivado.log -journal /tmpdir/claude-74755/kit-interactive/runs/a4/a4c_vivado.jou < /tmpdir/claude-74755/kit-interactive/runs/a4/a4c_stdin.tcl > /tmpdir/claude-74755/kit-interactive/runs/a4/a4c_stdout.txt 2>&1
echo "A4C vivado rc=$? end $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
