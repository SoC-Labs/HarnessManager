#!/bin/bash
cd /tmpdir/claude-74755/kit-interactive/runs/place
echo "PLACE start $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
nice -n 10 /usr/bin/time -v -o /tmpdir/claude-74755/kit-interactive/runs/place/time_v.txt /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode tcl -log /tmpdir/claude-74755/kit-interactive/runs/place/place.log -journal /tmpdir/claude-74755/kit-interactive/runs/place/place.jou < /tmpdir/claude-74755/kit-interactive/runs/place/place_stdin.tcl > /tmpdir/claude-74755/kit-interactive/runs/place/stdout.txt 2>&1
echo "PLACE vivado rc=$? end $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
