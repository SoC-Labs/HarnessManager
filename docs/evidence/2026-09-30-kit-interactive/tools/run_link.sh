#!/bin/bash
# usage: run_link.sh TAG BUILD_DIR   (STOP_AFTER=link, -mode tcl, then probe.tcl)
KI=/tmpdir/claude-74755/kit-interactive; T=$1; D=$2; O=$KI/runs/n2
cd $D
echo "N2 $T start $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
{ echo "set ::N2TAG $T"; cat $O/probe.tcl; } > $O/${T}_stdin.tcl
nice -n 10 /usr/bin/time -v -o $O/${T}_time_v.txt /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode tcl -source $D/build_rm.tcl -log $D/build_rm.log -journal $D/build_rm.jou -tclargs STOP_AFTER=link JOBS=4 < $O/${T}_stdin.tcl > $O/${T}_stdout.txt 2>&1
echo "N2 $T vivado rc=$? end $(date +%T) load=$(cut -d' ' -f1-3 /proc/loadavg)"
