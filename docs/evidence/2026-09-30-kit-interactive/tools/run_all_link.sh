#!/bin/bash
R=/tmpdir/claude-74755/kit-interactive/runs/n2/run_link.sh; B=/tmpdir/claude-74755/kit-interactive/home/builds
for t in n2_base n2_b n2_a n2_lfsr_base n2_lfsr_b; do $R $t $B/$t; done
