# pnr_runner's measurement script. Run by `openroad -exit` from the ORFS flow
# directory with the design's ORFS variables in the environment (vars.sh) and
# the runner's own, all PNR_*:
#   PNR_ODB   the database to measure (3_place.odb or 6_final.odb)
#   PNR_SDC   its constraints (3_place.sdc or 6_final.sdc)
#   PNR_MODE  fast (placement parasitics) or full (extracted parasitics)
#   PNR_NETS  names of nets to measure one by one (may be empty)
#   PNR_OUT   where to write sta.txt (key=value) and paths.txt
#
# Everything is reported in the library's own units (ns on nangate45, ps on
# asap7); the runner converts.

source $::env(SCRIPTS_DIR)/load.tcl
load_design $::env(PNR_ODB) $::env(PNR_SDC)

if { $::env(PNR_MODE) eq "full" && [file exists $::env(RESULTS_DIR)/6_final.spef] } {
  read_spef $::env(RESULTS_DIR)/6_final.spef
  set parasitics spef
} elseif { $::env(PNR_MODE) eq "full" } {
  estimate_parasitics -global_routing
  set parasitics global_routing
} else {
  estimate_parasitics -placement
  set parasitics placement
}

set out [open $::env(PNR_OUT)/sta.txt w]
puts $out "parasitics=$parasitics"
puts $out "wns=[sta::worst_slack_cmd max]"
puts $out "tns=[sta::total_negative_slack_cmd max]"
puts $out "hold_wns=[sta::worst_slack_cmd min]"

# the clock's minimum period: the worst register-to-register path, so the
# period the design would close at, independent of where the constraint sits
foreach clk [all_clocks] {
  set name [get_name $clk]
  if { [catch { set pmin [sta::find_clk_min_period $clk 1] } ] } {
    continue
  }
  if { $pmin > 0 } {
    puts $out "period_min:$name=$pmin"
  }
}

# cells: everything placed, the flops among them, and the area
set block [ord::get_db_block]
set dbu [$block getDbUnitsPerMicron]
set insts 0
set fill 0
foreach inst [$block getInsts] {
  set master [$inst getMaster]
  if { [$master isFiller] || [$master getType] eq "CORE_SPACER" } {
    incr fill
    continue
  }
  incr insts
}
puts $out "cells=$insts"
puts $out "fill_cells=$fill"
puts $out "flops=[llength [all_registers -cells]]"
puts $out "area=[sta::format_area [rsz::design_area] 4]"
puts $out "utilization=[format %.4f [rsz::utilization]]"

# wire: the half-perimeter of every signal net's pins, the one length a
# placement can give; routed lengths come from the detailed router's metrics
proc net_hpwl { net dbu } {
  set xmin 1e18; set ymin 1e18; set xmax -1e18; set ymax -1e18
  set pins 0
  foreach it [$net getITerms] {
    set r [$it getBBox]
    set xmin [expr {min($xmin, [$r xMin])}]; set xmax [expr {max($xmax, [$r xMax])}]
    set ymin [expr {min($ymin, [$r yMin])}]; set ymax [expr {max($ymax, [$r yMax])}]
    incr pins
  }
  foreach bt [$net getBTerms] {
    set r [$bt getBBox]
    set xmin [expr {min($xmin, [$r xMin])}]; set xmax [expr {max($xmax, [$r xMax])}]
    set ymin [expr {min($ymin, [$r yMin])}]; set ymax [expr {max($ymax, [$r yMax])}]
    incr pins
  }
  if { $pins < 2 } { return [list 0 $pins] }
  return [list [expr {(($xmax - $xmin) + ($ymax - $ymin)) / double($dbu)}] $pins]
}

set total 0.0
set longest 0.0
set longest_net ""
foreach net [$block getNets] {
  if { [$net getSigType] ne "SIGNAL" } { continue }
  lassign [net_hpwl $net $dbu] l pins
  set total [expr {$total + $l}]
  if { $l > $longest } { set longest $l; set longest_net [$net getName] }
}
puts $out "hpwl_total=[format %.2f $total]"
puts $out "hpwl_longest=[format %.2f $longest]"
puts $out "hpwl_longest_net=$longest_net"

# the database keeps a flat name's escapes (u_rd.q\[3\] for the netlist's
# \u_rd.q[3]), so the lookup is by the name with the backslashes dropped
array set by_name {}
if { [llength $::env(PNR_NETS)] } {
  foreach net [$block getNets] {
    set by_name([string map {\\ {}} [$net getName]]) $net
  }
}
foreach name $::env(PNR_NETS) {
  set key [string map {\\ {}} $name]
  set net NULL
  if { [info exists by_name($key)] } { set net $by_name($key) }
  if { $net eq "NULL" } {
    puts $out "net:$name=missing"
    continue
  }
  lassign [net_hpwl $net $dbu] l pins
  set routed ""
  if { $::env(PNR_MODE) eq "full" } {
    # the router's own length of the net, written to a file since the
    # command logs it rather than returning it
    set tmp $::env(PNR_OUT)/wl.tmp
    if { ![catch { report_wire_length -net [list $name] -detailed_route -file $tmp }] } {
      set fh [open $tmp r]
      set txt [read $fh]
      close $fh
      # one line per net: "drt: <net> <length um> <pins>"
      regexp {drt:\s+\S+\s+([\d.]+)} $txt -> routed
      file delete $tmp
    }
  }
  puts $out "net:$name=[format %.2f $l],$pins,$routed"
}
close $out

# the worst path, with every cell on it, for the runner to count: once over
# everything (ports included, under the runner's IO budget), once between
# registers, which is the depth the module carries wherever it is placed
report_checks -path_delay max -format full_clock_expanded -group_path_count 1 \
  > $::env(PNR_OUT)/paths.txt
set fh [open $::env(PNR_OUT)/paths.txt a]
puts $fh "=== register to register ==="
close $fh
report_checks -path_delay max -format full_clock_expanded -group_path_count 1 \
  -from [all_registers -clock_pins] -to [all_registers -data_pins] \
  >> $::env(PNR_OUT)/paths.txt
