yosys -import
set stdcell_lib "/src/tools/yosys/examples/cmos/cmos_cells.lib"
set stdcell_v "/src/tools/yosys/examples/cmos/cmos_cells.v"

#AUTO_GEN_SYNTH

read_verilog -sv {*}$comp_args {*}$rtl_files
hierarchy -check -top $top
read_verilog -lib $stdcell_v
synth -top $top {*}$synth_args
dfflibmap -liberty $stdcell_lib
abc -liberty $stdcell_lib
opt_clean
write_verilog {*}$netlist_args ${top}_netlist.v 
log "REPORT_BEGIN"
stat -top $top -liberty $stdcell_lib
log "REPORT_END"
