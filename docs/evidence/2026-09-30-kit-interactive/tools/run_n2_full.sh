#!/bin/bash
KI=/tmpdir/claude-74755/kit-interactive; D=$KI/home/builds/n2_minimal_full; O=$KI/runs/n2full
cd $D
echo "N2FULL start $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
nice -n 10 /usr/bin/time -v -o $O/time_v.txt /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode batch -source $D/build_rm.tcl -log $D/build_rm.log -journal $D/build_rm.jou -tclargs JOBS=4 > $O/stdout.txt 2>&1
echo "N2FULL vivado rc=$? end $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
