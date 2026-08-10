#!/usr/bin/env python3
import argparse
import os

_SYNTH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "synth")
from proj_utils import *
from log_utils import *

wa_root = None

def generate_synth_command(args):
    print_message("info", f"Generating yosys command for block: {args.block}")
    
    jinja2_variables = {
        "TOOL": "yosys",
        "WA_ROOT": wa_root,
        "TOP_BLOCK": args.block,
        **os.environ
    }
    attributes_to_parse = ["include_dirs", "defines", "files", "comp_args"]
    attributes = parse_attributes(args.block, "design", attributes_to_parse, jinja2_variables)


    file_list = attributes['files']
    compargs_list = attributes['comp_args']
    synthargs_list = []
    netlistargs_list = []
    
    
    for incdir in attributes['include_dirs']:
        compargs_list.append(f"-I{incdir}")
    for define in attributes['defines']:
        compargs_list.append(f"-D{define}")


    if args.noattr_netlist:
        netlistargs_list.append("-noattr")
    if args.flatten:
        synthargs_list.append("-flatten")
    if args.spartan6:
        synthargs_list.append("-family xc6s") # Spartan 6

    tcl_lines = [
            f"set top {args.top}",
            f"set comp_args {{{' '.join(compargs_list)}}}",
            f"set rtl_files {{{' '.join(file_list)}}}",
            f"set synth_args {{{' '.join(synthargs_list)}}}",
            f"set netlist_args {{{' '.join(netlistargs_list)}}}"]

    params_line = []

    if args.rundir:
        synthdir = args.rundir
    else:
        synthdir = f'{wa_root}/synth/{args.block}/{args.top}'
    if args.clean:
        shutil.rmtree(synthdir, ignore_errors=True)
    os.makedirs(synthdir, exist_ok=True)

    
    if args.verilator:
        verilator_cmd = f"verilator --lint-only --sv --timing --Wall {' '.join(compargs_list)} {' '.join(file_list)} --top-module {args.top}"
        r = run_command(verilator_cmd, synthdir)
        print(verilator_cmd)
        if r:
           print_message('error',f"Linter Failed {r}")
        exit(1)

    if args.user_tcl:
        replace_lines_in_pattern_file(f'{args.user_tcl}', f'{synthdir}/yosys_synth.tcl', "AUTO_GEN_SYNTH", tcl_lines)    
    elif args.xilinx:
        replace_lines_in_pattern_file(os.path.join(_SYNTH_DIR, 'yosys_synth_xilinx.tcl'), f'{synthdir}/yosys_synth.tcl', "AUTO_GEN_SYNTH", tcl_lines)
    else:
        replace_lines_in_pattern_file(os.path.join(_SYNTH_DIR, 'yosys_synth.tcl'), f'{synthdir}/yosys_synth.tcl', "AUTO_GEN_SYNTH", tcl_lines)

    yosys_cmd = f"yosys -t yosys_synth.tcl --logfile synth.log -q"
    r = run_command(yosys_cmd, synthdir)
    if r:
       print_message('error',f"Synthesis Failed {r}")

    print_message("info","Synthesis results:")
    if args.xilinx:
        run_command("egrep 'Number of cells|Estimated number of LCs' synth.log", synthdir)
    else:
        run_command("awk '/REPORT_BEGIN/{flag=1; count=3; next} /REPORT_END/{flag=0} flag{if (count-- <=0) print}' synth.log", synthdir)
    print_message("info", f"LOG    : {synthdir}/synth.log")
    print_message("info", f"NETLIST: {synthdir}/{args.top}_netlist.v")
    if args.list_modules:
        run_command("grep 'Generating RTLIL representation for module' synth.log", synthdir)



def main():
    global wa_root
    wa_root = get_git_root()
    if not wa_root:
        print_message("error","You are not inside valid workarea.")
        exit(1)
    
    parser = argparse.ArgumentParser(description="Yosys Synthesis Wrapper")
    parser.add_argument("-b",'--block', type=str, help="name of design or verif block", required=True)
    parser.add_argument('--top', type=str, help="Set top module name (default: tb)")
    parser.add_argument("--clean", action="store_true", help="clean run directory")
    parser.add_argument("--pedant", action="store_true", help="Stop run when there are warnings")
    parser.add_argument("--rundir", type=str, help="Set user-defined rundir")
    parser.add_argument("--user_tcl", action="store", help="Use ser defined tcl synth script")
    parser.add_argument("--files", action="append", nargs="*", help="Command arguments to pass to compilation process")
    parser.add_argument("--verilator", action="store_true", help="use verilator as a linter")
    parser.add_argument("--xilinx", action="store_true", help="Synthesise for Xilinx")
    parser.add_argument("--spartan6", action="store_true", help="Synthesise for Xilinx Spartan 6 family")
    parser.add_argument("--flatten", action="store_true", help="flatten synthesis")
    parser.add_argument("--noattr_netlist", action="store_true", help="remove attributes from netlist")
    parser.add_argument("--list-modules", action="store_true", help="List all compiled modules")

    args = parser.parse_args()
    
    if args.top is None:
        args.top = args.block
    if args.spartan6:
        args.xilinx = True

    generate_synth_command(args)

if __name__ == "__main__":
    main()
