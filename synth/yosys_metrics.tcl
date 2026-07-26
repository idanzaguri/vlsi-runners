yosys -import
set stdcell_lib "/src/tools/yosys/examples/cmos/cmos_cells.lib"
set stdcell_v "/src/tools/yosys/examples/cmos/cmos_cells.v"

#AUTO_GEN_SYNTH

read_verilog -sv {*}$comp_args {*}$rtl_files
hierarchy -check -top $top
read_verilog -lib $stdcell_v
synth -top $top {*}$synth_args

# Metrics are parsed out of the log between these markers (see metric_runner.py).
# Values are relative proxies for comparing RTL designs, not sign-off numbers.
#
# timing -> critical-path logic depth (levels between registers) from `ltp`.
# Run it HERE, on the generic-gate netlist, where flops are $_DFF_* primitives
# ltp recognizes. After liberty mapping (dfflibmap/abc below) ltp no longer sees
# the mapped DFFs as sequential, walks the register feedback as combinational,
# and reports false loops + a garbage depth.
log "METRIC_TIMING_BEGIN"
ltp -noff
log "METRIC_TIMING_END"

# area -> chip area + cell count from `stat -liberty`, on the mapped netlist.
dfflibmap -liberty $stdcell_lib
abc -liberty $stdcell_lib
opt_clean
log "METRIC_AREA_BEGIN"
stat -top $top -liberty $stdcell_lib
log "METRIC_AREA_END"
