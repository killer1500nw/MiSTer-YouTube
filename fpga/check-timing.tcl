# Run using quartus_sh -t check-timing.tcl, from the project directory.
# Checks the reported timing summaries, not completeness of I/O constraints.
proc check_summary {summary logtext} {
    foreach kind {Setup Hold Recovery Removal {Minimum Pulse Width}} {
        if {[string first "Type  : $kind " $summary] < 0} {
            error "Missing timing category: $kind"
        }
    }
    set lines [regexp -all -inline -line {^Slack[ \t]*:[^\r\n]*} $summary]
    if {[llength $lines] == 0} {error "No slack records found"}
    set worst 1e99
    foreach line $lines {
        if {![regexp {^Slack[ \t]*:[ \t]*(-?[0-9]+(?:\.[0-9]+)?)[ \t]*$} $line -> slack]} {
            error "Unrecognised slack record: $line"
        }
        if {$slack < $worst} {set worst $slack}
        if {$slack < 0} {error "Negative reported slack: $slack ns"}
    }
    if {[string first "Timing requirements not met" $logtext] >= 0} {
        error "Quartus reported timing requirements not met"
    }
    if {[string first "Full Compilation was successful" $logtext] < 0} {
        error "Full-compilation success marker is missing"
    }
    return "PASS: [llength $lines] non-negative slack records; worst $worst ns. Physical operation and I/O constraints still require review."
}
proc read_text {path} {
    set f [open $path r]
    set result [read $f]
    close $f
    return $result
}
if {![info exists ::timing_check_test]} {
    if {[catch {
        puts [check_summary [read_text output_files/YouTubeCRT.sta.summary] [read_text build.log]]
    } message]} {
        puts stderr "TIMING CHECK FAILED: $message"
        exit 2
    }
    exit 0
}
