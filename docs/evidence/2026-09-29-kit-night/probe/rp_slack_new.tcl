proc rp_slack { rp_inst } {
    set regs [get_cells -quiet -hierarchical -filter "NAME =~ ${rp_inst}/* && IS_SEQUENTIAL"]
    set regs [lsearch -all -inline -not -glob $regs "*/HD_PR_Connection_*"]
    if { [llength $regs] == 0 } { return [list "" "" 0] }
    set out {}
    foreach kind {-setup -hold} {
        set worst ""
        foreach dir {-to -from} {
            set p [get_timing_paths -quiet $kind -max_paths 1 -nworst 1 $dir $regs]
            if { $p ne "" } {
                set s [get_property SLACK $p]
                if { $s ne "" && ($worst eq "" || $s < $worst) } { set worst $s }
            }
        }
        lappend out $worst
    }
    lappend out [llength $regs]
    return $out
}
