yosys -import

#AUTO_GEN_SYNTH

read_verilog -sv {*}$comp_args {*}$rtl_files
synth_xilinx -top $top {*}$synth_args
write_verilog {*}$netlist_args ${top}_netlist.v 
