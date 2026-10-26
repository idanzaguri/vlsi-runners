#!/usr/bin/env python3
import argparse
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from proj_utils import *
from log_utils import *


# ----------------------------------------------------------------------
# Log-scan patterns: shared between simulators where possible (UVM ones
# look the same on every UVM-1.2 simulator) and simulator-specific for
# the rest.
# ----------------------------------------------------------------------
UVM_SIM_PASSED = [r"--- UVM Report Summary ---"]
UVM_SIM_WARN   = [r"UVM_WARNING.* @"]
UVM_SIM_FAIL   = [r"UVM_ERROR.* @", r"UVM_FATAL.*@"]

# Modelsim/Questa
ms_comp_passed = [r"^End time:"]
ms_comp_warn   = [r"^\*\* Warning:"]
ms_comp_fail   = [r"^\*\* Error:"]
ms_comp_waive  = []

ms_sim_passed  = UVM_SIM_PASSED
ms_sim_warn    = UVM_SIM_WARN  + [r"^# \*\* Warning:"]
ms_sim_fail    = UVM_SIM_FAIL  + [r"^# \*\* Error:"]
ms_sim_waive   = [r"Warning: \(vopt-10587\)", r"Warning: \(vopt-13408\) Code coverage", r"Warning: \(vopt-10908\) Some"]

# VCS
vcs_comp_passed = [r"Chronologic VCS"]   # presence of the VCS banner is enough
vcs_comp_warn   = [r"^Warning-\["]
vcs_comp_fail   = [r"^Error-\[", r"^\s*Error:"]
vcs_comp_waive  = []

vcs_sim_passed  = UVM_SIM_PASSED
vcs_sim_warn    = UVM_SIM_WARN  + [r"^Warning-\["]
vcs_sim_fail    = UVM_SIM_FAIL  + [r"^Error-\[", r"\$fatal"]
vcs_sim_waive   = []

# Verilator. The compile log holds Verilator's messages, then make's and
# the C++ compiler's; the report line comes last, after a good build. The
# warnings inside the UVM library itself are waived per build (its path is
# only known then).
vlt_comp_passed = [r"^- V e r i l a t i o n   R e p o r t:"]
vlt_comp_warn   = [r"^%Warning"]
vlt_comp_fail   = [r"^%Error", r"^make: \*\*\*", r": (fatal )?error: "]
vlt_comp_waive  = []

vlt_sim_passed  = UVM_SIM_PASSED
vlt_sim_warn    = UVM_SIM_WARN  + [r"^%Warning"]
vlt_sim_fail    = UVM_SIM_FAIL  + [r"^%Error"]
vlt_sim_waive   = []

wa_root = None


def write_filelist(filelist_path, data):
    """Writes include directories, defines, and file paths to a filelist."""
    with open(filelist_path, "w") as f:
        for entry in data:
            f.write(entry + "\n")


def _resolve_simdir(args):
    if args.rundir:
        return args.rundir
    simdir = f'{wa_root}/sim/{args.block}/{args.test}_{args.seed}'
    if args.rundir_suffix:
        simdir += f'_{args.rundir_suffix}'
    return simdir


def _gather_attrs_and_filelist(args, simdir):
    """Run jinja substitution, parse attributes, write the filelist (shared
    between simulators -- +define+/+incdir+ syntax is the same for all
    three). Returns the simulation arguments, the design's files, and the
    Verilator configuration files and coverage types (config.yaml
    ``verilator_config``, ``verilator_coverage``)."""
    jinja2_variables = {
        "TOOL": {"modelsim": "vsim", "vcs": "vcs", "verilator": "verilator"}[args.simulator],
        "WA_ROOT": wa_root,
        "TOP_BLOCK": args.block,
        **os.environ,
    }
    attributes_to_parse = ["include_dirs", "defines", "files", "comp_args", "sim_args",
                           "verilator_config", "verilator_coverage"]
    attributes, blocks = parse_attributes(args.block, "verif", attributes_to_parse,
                                          jinja2_variables, with_blocks=True)
    # the RTL under test: every file of every `design` block in the tree.
    # Code coverage is collected on these and on nothing else (the bench,
    # the VIPs and UVM are not what coverage closure is about).
    design_files = frozenset(os.path.realpath(f)
                             for b in blocks if b.get("_block_type") == "design"
                             for f in (b.get("files") or []))

    data = []
    if args.comp_args:
        data.extend([w for sub in args.comp_args for item in sub for w in item.split()])
    data.extend(attributes["comp_args"])
    data.extend([f"+define+{d}" for d in attributes["defines"]])
    data.extend([f"+incdir+{d}" for d in attributes["include_dirs"]])
    data.extend(attributes["files"])
    if args.files:
        data.extend([w for sub in args.files for item in sub for w in item.split()])
    write_filelist(f"{simdir}/comp_filelist.f", data)

    sim_args = []
    if args.sim_args:
        sim_args.extend([w for sub in args.sim_args for item in sub for w in item.split()])
    sim_args.extend(attributes["sim_args"])
    return " ".join(sim_args), design_files, attributes["verilator_config"], attributes["verilator_coverage"]


# ----------------------------------------------------------------------
# Modelsim / Questa path
# ----------------------------------------------------------------------
def _snapshot_marker(simdir, lib):
    """Where a library records the optimized design unit built into it.

    Questa keeps optimized units inside the library, and `vdir` is the only
    way to list them by name, so the builder leaves a one-line marker beside
    the library instead: a runner can then tell a snapshot library from one
    built before snapshots existed without spawning a tool."""
    path = lib if os.path.isabs(lib) else os.path.join(simdir, lib)
    return f"{path}.vopt"


def _snapshot_name(simdir, lib):
    """The snapshot in this library, or None."""
    try:
        with open(_snapshot_marker(simdir, lib)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _mark_snapshot(simdir, lib, name):
    with open(_snapshot_marker(simdir, lib), "w") as f:
        f.write(f"{name}\n")


def _clear_snapshot(simdir, lib):
    """A recompile invalidates whatever was optimized before it."""
    try:
        os.remove(_snapshot_marker(simdir, lib))
    except OSError:
        pass


# Code coverage types: branch, condition, expression, FSM, statement and
# extended toggle. Collected by the Starter Edition too (checked on Questa
# FSE 2025.2: every type below, assertions, `coverage save`, `vcover merge`
# and `vcover report` all work; only covergroups and randomize() are refused,
# by the missing `svverification` license feature).
COVER_TYPES = "bcefsx"


def _design_units(simdir, lib, design_files):
    """The compiled modules, interfaces and entities whose source is one
    of ``design_files``.

    The library is the only place that knows which unit came from which
    file: `vdir -l` lists every unit with a `Source file:` line."""
    lib_path = lib if os.path.isabs(lib) else os.path.join(simdir, lib)
    r = subprocess.run(["vdir", "-l", "-lib", lib_path], cwd=simdir,
                       capture_output=True, text=True)
    if r.returncode:
        return []
    units, name = [], None
    for line in r.stdout.splitlines():
        if line and not line[0].isspace():
            # a unit header: "MODULE foo", "INTERFACE bar", "ENTITY baz",
            # "Verilog PACKAGE p", "OPTIMIZED DESIGN tb_opt"
            words = line.split()
            name = words[-1] if words[0] in ("MODULE", "INTERFACE", "ENTITY") else None
        elif name and line.strip().startswith("Source file:"):
            if os.path.realpath(line.split(":", 1)[1].strip()) in design_files:
                units.append(name)
            name = None
    return units


def _coverage_opts(simdir, lib, design_files):
    """The optimizer switches that instrument the design under test, and
    only it.

    A plain ``-cover`` instruments every unit in the library: the UVM bench,
    the VIP interfaces, everything. The report is then dominated by VIP
    toggles nobody will ever close (measured on one bench: eight agent
    interfaces, 25 000 toggle bins at under 10%, before a single line of
    the design). So the units whose source sits in a `design` block are
    selected by name, and the bench is left alone, which also keeps it at
    full speed. A tree without a design block (a VIP's own bench) covers
    everything, as before."""
    units = _design_units(simdir, lib, design_files) if design_files else []
    if design_files and not units:
        print_message("warning", "no compiled unit matched a design file, "
                                 "so the whole bench is covered")
    if not units:
        return f"-cover {COVER_TYPES}"
    return " ".join(f"+cover={COVER_TYPES}+{u}" for u in units)


def _print_coverage(simdir):
    """The totals table the simulation wrote, and where the database is."""
    try:
        with open(f"{simdir}/coverage.txt") as f:
            table = [line.rstrip() for line in f if line.strip()]
    except OSError:
        print_message("warning", "the simulation saved no coverage summary")
        return
    print("\n".join(table))
    print(f"COV:  {simdir}/coverage.ucdb")


def run_modelsim(args, simdir, sim_args, design_files=frozenset()):
    try:
        with open(f'{simdir}/sim.do', 'w') as f:
            if args.dump_mem:
                f.write(f"log -r {args.dut}/* -nofilter {{Memory}};\n")
            elif args.dump:
                f.write(f"log -r {args.dut}/*;\n")
            f.write("run -all;\n")
            if args.codecov:
                # the totals for the console, then the database for merging;
                # -instance keeps the saved tree under --dut
                f.write("coverage report -summary -output coverage.txt;\n")
                f.write(f"coverage save -testname {args.test}_{args.seed} -instance {args.dut} coverage.ucdb;\n")
            f.write("quit;\n")
    except Exception as e:
        print_message("error", f"An error occurred: {e}")
        exit(1)

    # The work library: this test's own by default, or a SHARED one built
    # once for every test of the same design (--lib). Questa's equivalent of
    # a VCS simv is an OPTIMIZED DESIGN UNIT inside that library: vopt names
    # one, and vsim then runs it without optimizing again. Without it every
    # test of a design re-optimizes it, which the run logs show as "Design is
    # being optimized..." every single time (measured on one design: 4.8 s
    # per run, 1.8 s against a snapshot).
    lib = args.lib or "sv_tb_work"
    snapshot = f"{args.top}_opt"
    waves = args.dump or args.dump_mem
    vlib_cmd = f"vlib -type flat {lib}"
    vlog_cmd = f"vlog -lint=full -work {lib} +acc=nprt -l comp.log -mfcu -f comp_filelist.f"
    vopt_cmd = f"vopt -work {lib} {args.top} -o {snapshot} +acc -l vopt.log"
    # coverage is an OPTIMIZER choice, not a compiler one: `vopt +cover` on
    # units compiled without -cover instruments them just the same, and the
    # units are picked after vlog, when the library can list them

    do_comp = not args.run_only
    do_sim = not args.compile_only

    if do_comp:
        r = run_command(vlib_cmd, simdir)
        if r:
            print_message('error', f"VLIB Failed {r}")
            exit(1)
        r = run_command(vlog_cmd, simdir)
        status = parse_log(f"{simdir}/comp.log", ms_comp_passed, ms_comp_warn, ms_comp_fail, ms_comp_waive, False, True)
        if r or status in ("FAILED", "UNKNOWN") or (status == "PASSED_WARN" and args.pedant):
            print_message('error', "Compilation Failed")
            print_message('error', f"LOG:  {simdir}/comp.log")
            exit(1)
        _clear_snapshot(simdir, lib)
        # a wave run wants the debug database, which is baked at optimization
        # time, so it keeps the old path and lets vsim optimize with -debugdb
        if not waves:
            if args.codecov:
                vopt_cmd += " " + _coverage_opts(simdir, lib, design_files)
            r = run_command(vopt_cmd, simdir)
            if r:
                print_message('error', f"Optimization Failed {r}")
                print_message('error', f"LOG:  {simdir}/vopt.log")
                exit(1)
            _mark_snapshot(simdir, lib, snapshot)

    # run the snapshot when there is one; a library built before this change,
    # or a wave run, falls back to optimizing inside vsim exactly as before
    use_snapshot = (not waves) and _snapshot_name(simdir, lib) == snapshot
    target = snapshot if use_snapshot else args.top
    vsim_cmd = (f"vsim -c -onfinish stop -work {lib} -l run.log -do sim.do "
                f"-sv_seed {args.seed} {target} {sim_args}")
    if args.uvm_test:
        vsim_cmd += f" +UVM_TESTNAME={args.uvm_test} +UVM_MAX_QUIT_COUNT={args.uvm_max_quit} +UVM_VERBOSITY={args.verbosity}"
    if waves:
        voptargs = "+acc"
        if args.codecov:
            voptargs += " " + _coverage_opts(simdir, lib, design_files)
        vsim_cmd += f' -wlf waves.wlf -voptargs="{voptargs}" -debugdb'
    elif not use_snapshot:
        # FIXME: a simulator optimization bug introduces race conditions (e.g. ovip_axi_4lite_test
        # fails). Disabling optimization with +acc works around it until the root cause is fixed.
        vsim_cmd += " -voptargs=+acc"   # the snapshot carries the same +acc
    if args.codecov:
        vsim_cmd += " -coverage"

    if do_sim:
        if args.codecov:
            # like the log below: a database left by an earlier run must
            # not answer for this one
            for stale in ("coverage.txt", "coverage.ucdb"):
                try:
                    os.remove(f"{simdir}/{stale}")
                except OSError:
                    pass
        expect = getattr(args, "expect_fail", None)
        if expect:
            # an expect_fail test reads the log to decide, so a log left by an
            # EARLIER run would answer for a simulation that never happened:
            # vsim missing, a refused license, any launch failure. Remove it
            # first, so "no log" can never look like "the guard fired".
            try:
                os.remove(f"{simdir}/run.log")
            except OSError:
                pass
        r = run_command(vsim_cmd, simdir)
        if r and not expect:
            print_message('error', f"Simulation Failed {r}")
            exit(1)
        status = parse_log(f"{simdir}/run.log", ms_sim_passed, ms_sim_warn, ms_sim_fail, ms_sim_waive, True, True)
        print("---")
        print(f"LOG:  {simdir}/run.log")
        if args.dump or args.dump_mem:
            print(f"DUMP: {simdir}/waves.wlf")
        if args.codecov:
            _print_coverage(simdir)
        _exit_on_verdict(args, simdir, r, status)


def _exit_on_verdict(args, simdir, r, status):
    """Exit with test_runner's code for a finished simulation: its exit
    code ``r`` and the status of its run.log. 0 passed, 2 passed with
    warnings (1 under --pedant), 1 failed; a test that declares
    ``expect_fail`` inverts it. Returns only for a plain pass."""
    expect = getattr(args, "expect_fail", None)
    if expect:
        # an EXPECTED failure (open item 50): this test exists to make a
        # guard fire, so the run failing is the pass condition. Both
        # halves are required, or a guard that stopped firing would read
        # as green: the run must have failed, and the log must carry the
        # guard's own words.
        try:
            log_text = open(f"{simdir}/run.log", errors="ignore").read()
        except OSError:
            print_message('error',
                          "the simulation left no log, so there is nothing "
                          "to check the expected failure against")
            exit(1)
        fired = expect in log_text
        failed = bool(r) or status in ("FAILED", "UNKNOWN")
        if fired and failed:
            print_message('info', f"expected failure seen: {expect}")
            exit(0)
        if not fired:
            print_message('error',
                          f"the guard did not fire: nothing in the log "
                          f"matched {expect!r}")
        else:
            print_message('error',
                          "the guard's text is in the log but the run "
                          "passed; the runner no longer treats it as a "
                          "failure")
        exit(1)
    if r or status in ("FAILED", "UNKNOWN"):
        exit(1)
    if status == "PASSED_WARN":
        exit(1 if args.pedant else 2)


# ----------------------------------------------------------------------
# VCS path (best-effort -- the user does not have VCS available locally, so
# this has not been runtime-validated. Keep it close to the documented VCS
# command shape so the eventual smoke test is small.)
# ----------------------------------------------------------------------
def run_vcs(args, simdir, sim_args):
    # The Modelsim filelist already uses +define+/+incdir+ which VCS accepts
    # natively; the file paths in the list are plain SV sources which vcs
    # consumes via -f.

    comp_cmd_parts = [
        "vcs",
        "-full64",
        "-sverilog",
        "-ntb_opts uvm-1.2",
        "-timescale=1ns/1ps",
        "-l comp.log",
        f"-top {args.top}",
        "-f comp_filelist.f",
        "-o simv",
    ]
    if args.dump or args.dump_mem:
        comp_cmd_parts.append("-debug_access+all")
    else:
        # -debug_access+pp gives enough visibility for UVM hierarchical access
        # without forcing the larger debug DB. Drop if it bloats compile time.
        comp_cmd_parts.append("-debug_access+pp")
    if args.codecov:
        comp_cmd_parts.append("-cm line+cond+fsm+tgl+branch")
    comp_cmd = " ".join(comp_cmd_parts)

    sim_cmd_parts = [
        "./simv",
        "-l run.log",
        f"+ntb_random_seed={args.seed}",
    ]
    if args.uvm_test:
        sim_cmd_parts += [
            f"+UVM_TESTNAME={args.uvm_test}",
            f"+UVM_MAX_QUIT_COUNT={args.uvm_max_quit}",
            f"+UVM_VERBOSITY={args.verbosity}",
        ]
    if args.dump or args.dump_mem:
        dump_do = f"{simdir}/dump.do"
        with open(dump_do, "w") as f:
            f.write("set fid [dump -file waves.vpd -type VPD]\n")
            # -depth 0 captures the full hierarchy; -aggregates adds memories/arrays.
            agg = " -aggregates" if args.dump_mem else ""
            f.write(f"dump -add {args.dut} -depth 0 -fid $fid{agg}\n")
            f.write("run\n")
            f.write("quit\n")
        sim_cmd_parts += ["-ucli", "-do dump.do"]
    if args.codecov:
        sim_cmd_parts.append("-cm line+cond+fsm+tgl+branch")
        sim_cmd_parts.append(f"-cm_name {args.test}_{args.seed}")
    if sim_args:
        sim_cmd_parts.append(sim_args)
    sim_cmd = " ".join(sim_cmd_parts)

    do_comp = not args.run_only
    do_sim  = not args.compile_only

    if do_comp:
        r = run_command(comp_cmd, simdir)
        status = parse_log(f"{simdir}/comp.log", vcs_comp_passed, vcs_comp_warn, vcs_comp_fail, vcs_comp_waive, False, True)
        if r or status in ("FAILED", "UNKNOWN") or (status == "PASSED_WARN" and args.pedant):
            print_message('error', "Compilation Failed")
            print_message('error', f"LOG:  {simdir}/comp.log")
            exit(1)

    if do_sim:
        r = run_command(sim_cmd, simdir)
        if r:
            print_message('error', f"Simulation Failed {r}")
            exit(1)
        status = parse_log(f"{simdir}/run.log", vcs_sim_passed, vcs_sim_warn, vcs_sim_fail, vcs_sim_waive, True, True)
        print("---")
        print(f"LOG:  {simdir}/run.log")
        if args.dump or args.dump_mem:
            print(f"DUMP: {simdir}/waves.vpd")
        if r or status in ("FAILED", "UNKNOWN"):
            exit(1)
        if status == "PASSED_WARN":
            exit(1 if args.pedant else 2)


# ----------------------------------------------------------------------
# Verilator path
#
# Verilator compiles the design and the bench into one C++ program, so a
# build takes minutes (most of it the UVM library) and a test is one run
# of that program: one build per design, then one process per test, as
# with a Questa snapshot. Verilator ships no UVM. The UVM 1.2 source comes
# from $UVM_HOME, and its DPI code from $UVM_HOME/src/dpi with the
# Verilator HDL backend of verilator/uvm12_dpi/ over it (uvm_hdl_force and
# the rest, through VPI). Verilator is 2-state: a test that checks X or Z
# cannot pass.
# ----------------------------------------------------------------------
_RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))


def _uvm_home():
    """The UVM 1.2 tree (the directory that holds src/): $UVM_HOME, or
    else the one inside the Questa install when vsim is on PATH, or None."""
    home = os.environ.get("UVM_HOME")
    if home:
        return os.path.normpath(home)
    vsim = shutil.which("vsim")
    if vsim:
        guess = os.path.normpath(os.path.join(os.path.dirname(vsim), "..", "verilog_src", "uvm-1.2"))
        if os.path.isfile(os.path.join(guess, "src", "uvm_pkg.sv")):
            return guess
    return None


def _uvm_dpi_source(mdir, uvm_home):
    """$UVM_HOME/src/dpi copied into the build directory, with the Verilator
    HDL backend put over the copy when that UVM has none. Returns the
    copy's uvm_dpi.cc, the one file to compile (it includes the others)."""
    dst = os.path.join(mdir, "uvm_dpi")
    shutil.rmtree(dst, ignore_errors=True)
    # copyfile, not copy2: an install's read-only modes would block the overlay
    shutil.copytree(os.path.join(uvm_home, "src", "dpi"), dst, copy_function=shutil.copyfile)
    with open(os.path.join(dst, "uvm_hdl.c"), errors="ignore") as f:
        has_backend = "verilator" in f.read().lower()
    if not has_backend:
        for name in ("uvm_hdl.c", "uvm_hdl_verilator.c"):
            shutil.copyfile(os.path.join(_RUNNER_DIR, "verilator", "uvm12_dpi", name),
                            os.path.join(dst, name))
    return os.path.join(dst, "uvm_dpi.cc")


def _run_logged(cmd, cwd, log_path, header=()):
    """Run ``cmd`` (a list) in ``cwd``, its stdout and stderr both to the
    console and to ``log_path``, which starts with the ``header`` lines.
    Verilator writes no log file of its own. Returns the exit code."""
    with open(log_path, "w") as log:
        for line in header:
            log.write(line + "\n")
            print(line)
        log.flush()
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, errors="replace")
        except OSError as e:
            print_message("error", f"{cmd[0]}: {e}")
            return 1
        try:
            for line in proc.stdout:
                log.write(line)
                sys.stdout.write(line)
                sys.stdout.flush()
            return proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGINT)
            return proc.wait()


# What a build can do, written beside it: waves (their format and the
# scope they cover) and code coverage. A run asks for what its build has.
_BUILD_RECORD = "vlsi_runners_build.json"


def _read_build_record(mdir):
    try:
        with open(os.path.join(mdir, _BUILD_RECORD)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _waves_source(mdir, top, scope, fmt):
    """A module bound into the top that starts the wave dump of ``scope``
    when the run has +vlsi_runners_waves. Verilator dumps only what
    $dumpvars asks for, and a bench need not call it. Returns its path."""
    path = os.path.join(mdir, "vlsi_runners_waves.sv")
    with open(path, "w") as f:
        f.write("// Written by test_runner --dump: the run's +vlsi_runners_waves starts the dump.\n"
                "module vlsi_runners_waves;\n"
                "  initial if ($test$plusargs(\"vlsi_runners_waves\")) begin\n"
                f"    $dumpfile(\"waves.{fmt}\");\n"
                f"    $dumpvars(0, {scope});\n"
                "  end\n"
                "endmodule\n"
                f"bind {top} vlsi_runners_waves u_vlsi_runners_waves ();\n")
    return path


def _source_files(vbin, pre_args, simdir):
    """Every source file Verilator reads for a build, `include`d ones and
    those of nested -f lists too, from the `line directives of its
    preprocessor output (-E, 3 s on an OVIP bench). Returns {name as
    Verilator knows it: real path}, or None when the preprocessor failed."""
    proc = subprocess.Popen([vbin, "-E", *pre_args], cwd=simdir, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            text=True, errors="replace")
    files = {}
    for line in proc.stdout:
        if line.startswith("`line "):
            m = re.match(r'`line \d+ "([^"]*)"', line)
            if m and m.group(1) not in files:
                files[m.group(1)] = os.path.realpath(os.path.join(simdir, m.group(1)))
    return files if proc.wait() == 0 else None


def _verilator_include(vbin):
    """Verilator's own include directory, which holds verilated_std.sv (its
    std package, compiled into every build without being named)."""
    try:
        root = subprocess.run([vbin, "--getenv", "VERILATOR_ROOT"], capture_output=True,
                              text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return os.path.join(root, "include") if root else None


def _not_covered(real, design_files, skip_dirs):
    """True for a file whose coverage does not count: one under
    ``skip_dirs`` (UVM, Verilator's own) always, and with a design block in
    the tree, everything that is not the design's (as with Questa: the
    bench, the VIPs and UVM are not what coverage closure is about). A VIP's
    own bench, without a design block, covers the rest."""
    if any(d and real.startswith(os.path.realpath(d) + os.sep) for d in skip_dirs):
        return True
    return bool(design_files) and real not in design_files


def _coverage_vlt(mdir, files, design_files, skip_dirs):
    """A Verilator configuration file that turns coverage off in every file
    that does not count, which also keeps the run fast. Each file is named:
    a coverage_on after `coverage_off -file "*"` turns nothing back on
    (checked on 5.052 and 5.053). Returns its path."""
    path = os.path.join(mdir, "vlsi_runners_coverage.vlt")
    with open(path, "w") as f:
        f.write("`verilator_config\n"
                "// Written by test_runner --codecov: coverage counts only in the design's files.\n")
        for d in skip_dirs[1:]:
            if d:
                f.write(f'coverage_off -file "{d}/*"\n')
        for name, real in sorted(files.items()):
            if _not_covered(real, design_files, skip_dirs):
                f.write(f'coverage_off -file "{name}"\n')
    return path


def _coverage_keys(line):
    """The keys of one point of a Verilator coverage.dat, ``C '<keys>' <count>``:
    \\x01 starts each key, \\x02 ends its name. "f" is the file, "t" the type."""
    return dict(x.partition("\x02")[::2] for x in line[3:line.rindex("' ")].split("\x01") if x)


def _coverage_records(dat):
    """(keys, count) of each point in a Verilator coverage.dat."""
    with open(dat, errors="replace") as f:
        for line in f:
            if line.startswith("C '"):
                yield _coverage_keys(line), int(line.rstrip("\n").rpartition("' ")[2])


def _keep_counted_coverage(dat, simdir, design_files, skip_dirs):
    """Drop from coverage.dat the points of files that do not count: FSM
    coverage ignores coverage_off (checked on 5.052 and 5.053)."""
    with open(dat, errors="replace") as f:
        lines = f.readlines()
    with open(dat, "w") as f:
        for line in lines:
            if line.startswith("C '"):
                real = os.path.realpath(os.path.join(simdir, _coverage_keys(line).get("f", "")))
                if _not_covered(real, design_files, skip_dirs):
                    continue
            f.write(line)


# Verilator's coverage types, in the order of a summary table
_VLT_COVER_TYPES = ["line", "branch", "expr", "toggle", "fsm_state", "fsm_arc"]


def coverage_table(dat):
    """The summary table of a Verilator coverage.dat: covered points and
    points per coverage type, and in total."""
    counts = {}
    for keys, count in _coverage_records(dat):
        t = keys.get("t", "?")
        covered, points = counts.get(t, (0, 0))
        counts[t] = (covered + (count > 0), points + 1)
    rank = lambda t: (_VLT_COVER_TYPES.index(t) if t in _VLT_COVER_TYPES else len(_VLT_COVER_TYPES), t)
    rows = [f"{'Coverage type':<14} {'Covered':>9} {'Points':>9} {'Percent':>8}"]
    for t in sorted(counts, key=rank):
        covered, points = counts[t]
        rows.append(f"{t:<14} {covered:>9} {points:>9} {100.0 * covered / points:>7.1f}%")
    covered = sum(c for c, _ in counts.values())
    points = sum(n for _, n in counts.values())
    rows.append(f"{'total':<14} {covered:>9} {points:>9} "
                f"{(100.0 * covered / points if points else 0.0):>7.1f}%")
    return rows


# The coverage types a project may name in config.yaml `verilator_coverage`,
# and Verilator's switch for each. `line` records branches too. Without the
# key a build takes them all (--coverage).
_VLT_COVER_SWITCHES = {"line": "--coverage-line", "toggle": "--coverage-toggle",
                       "expr": "--coverage-expr", "fsm": "--coverage-fsm", "user": "--coverage-user"}

_COCOTB_RUN = os.path.join(_RUNNER_DIR, "verilator", "cocotb_run.py")


def _coverage_switches(types):
    if not types:
        return ["--coverage"]
    bad = [t for t in types if t not in _VLT_COVER_SWITCHES]
    if bad:
        print_message("error", f"config.yaml verilator_coverage: {', '.join(bad)} is not one of "
                               f"{', '.join(_VLT_COVER_SWITCHES)}")
        exit(1)
    return [_VLT_COVER_SWITCHES[t] for t in types]


def _build_kind(args):
    """The kind of a build, the folder of a shared one: default, or what it
    adds (coverage, waves, waves with memories)."""
    parts = (["coverage"] if args.codecov else []) + \
            (["waves_mem"] if args.dump_mem else ["waves"] if args.dump else [])
    return "_".join(parts) or "default"


def run_verilator(args, simdir, sim_args, vlt_files=(), design_files=frozenset(), cov_types=()):
    """Build and run one test on Verilator: a UVM bench (a C++ program of
    the bench and UVM, Verilator's --binary), or a cocotb bench
    (--cocotb_module: cocotb's own runner builds it and runs a Python test
    module against it). Without --lib, the tests of one design share one
    build per kind, sim/<block>/_build/<comp>/<kind>: a build takes minutes,
    and Verilator rebuilds only what changed."""
    # from PATH: Verilator's own wrapper reads $VERILATOR_BIN (the name of
    # its verilator_bin), so a runner variable of that name made it call
    # itself; a project picks another build by putting it first on PATH
    vbin = shutil.which("verilator")
    cocotb = bool(args.cocotb_module)
    uvm_home = None if cocotb else _uvm_home()
    if args.lib:
        mdir = args.lib if os.path.isabs(args.lib) else os.path.join(simdir, args.lib)
    else:
        mdir = os.path.join(wa_root, "sim", args.block, "_build", args.comp or "default", _build_kind(args))
    # cocotb's runner names the program after the top (-o <top>)
    binary = os.path.join(mdir, args.top if cocotb else f"V{args.top}")
    if not args.run_only:
        _verilator_build(args, simdir, mdir, binary, vbin, uvm_home, list(vlt_files), design_files,
                         cov_types, cocotb)
    if not args.compile_only:
        run = _verilator_run_cocotb if cocotb else _verilator_run_uvm
        run(args, simdir, mdir, binary, vbin, uvm_home, sim_args, design_files)


def _verilator_build(args, simdir, mdir, binary, vbin, uvm_home, vlt_files, design_files, cov_types, cocotb):
    waves = args.dump or args.dump_mem
    if not vbin:
        print_message("error", "verilator is not on PATH")
        exit(1)
    # the wrapper runs $VERILATOR_BIN; naming a wrapper there loops, and
    # make goes on calling it, so the build never ends
    if os.path.basename(os.environ.get("VERILATOR_BIN", "")) == "verilator":
        print_message("error", f"VERILATOR_BIN={os.environ['VERILATOR_BIN']} names a verilator "
                               f"wrapper, which then runs itself; unset it (the runner takes "
                               f"verilator from PATH)")
        exit(1)
    if not cocotb and not uvm_home:
        print_message("error", "no UVM 1.2 source: set UVM_HOME (the uvm-1.2 directory "
                               "that holds src/), or put Questa's vsim on PATH")
        exit(1)
    print_message("info", f"Verilator: {vbin}; "
                          + (f"cocotb module: {args.cocotb_module}" if cocotb else f"UVM: {uvm_home}")
                          + f"; build: {mdir}")
    os.makedirs(os.path.dirname(mdir), exist_ok=True)
    # one build at a time in a build folder: two tests of a design that
    # start together would write the same objects
    with open(f"{mdir}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.clean:
            shutil.rmtree(mdir, ignore_errors=True)
        os.makedirs(mdir, exist_ok=True)
        old = _read_build_record(mdir)
        # a failed build must not leave the last good one to run
        for stale in (binary, os.path.join(mdir, _BUILD_RECORD)):
            try:
                os.remove(stale)
            except OSError:
                pass
        # the file list beside the build, so its path is the same for every
        # test that builds there (Verilator then sees nothing changed)
        filelist = os.path.join(mdir, "comp_filelist.f")
        shutil.copyfile(f"{simdir}/comp_filelist.f", filelist)
        record = {"verilator": vbin, "bench": "cocotb" if cocotb else "uvm",
                  "waves": None, "coverage": False, "warnings": []}
        opts = []
        if cocotb:
            sources = ["-f", filelist]
        else:
            dpi = _uvm_dpi_source(mdir, uvm_home)
            sources = [f"+incdir+{uvm_home}/src", f"{uvm_home}/src/uvm_pkg.sv", "-f", filelist]
        if waves:
            if cocotb:
                # cocotb's runner adds --trace, and cocotb's main writes dump.vcd
                fmt = "vcd"
            else:
                # FST is smaller, but Verilator's FST writer needs lz4.h (liblz4-dev)
                fmt = "fst" if os.path.exists("/usr/include/lz4.h") else "vcd"
                opts += [f"--trace-{fmt}"]
                sources.append(_waves_source(mdir, args.top, args.dut, fmt))
            # wide buses whole (the default stops at 256 bits), structs by field
            opts += ["--trace-structs", "--trace-max-width", "65536"]
            if args.dump_mem:
                opts += ["--trace-max-array", "65536"]  # the default stops at 32 entries
            record.update(waves=fmt, waves_scope=None if cocotb else args.dut)
        if args.codecov:
            files = _source_files(vbin, [*sources, "--top-module", args.top], simdir)
            if files is None:
                print_message("error", "Verilator's preprocessor failed, so the files to cover "
                                       "are unknown (see the build below without --codecov)")
                exit(1)
            opts += _coverage_switches(cov_types)
            skip_dirs = [uvm_home, _verilator_include(vbin)]
            vlt_files = [_coverage_vlt(mdir, files, design_files, skip_dirs), *vlt_files]
            record["coverage"] = True
        # make -j: --build_jobs, else the CPUs this process may use (all of them
        # here, the job's under Slurm)
        jobs = args.build_jobs or len(os.sched_getaffinity(0))
        if cocotb:
            # the bench's own switches (warnings, assertions, X) come from its
            # config.yaml, through the file list
            spec = {"sources": [], "build_args": [*opts, *vlt_files, *sources], "jobs": jobs,
                    "hdl_toplevel": args.top, "build_dir": mdir, "waves": bool(waves)}
            comp_cmd = ["python3", _COCOTB_RUN, "build", json.dumps(spec)]
        else:
            comp_cmd = [vbin, "--binary", "--timing", "--vpi", "-j", str(jobs),
                        "-Wno-fatal", "-Wno-lint", "-Wno-style", *opts, *vlt_files, *sources, dpi,
                        "--top-module", args.top, "--Mdir", mdir]
        r = _run_logged(comp_cmd, simdir, f"{simdir}/comp.log")
        waive = vlt_comp_waive + ([rf"^%Warning-\S+: {re.escape(uvm_home)}/"] if uvm_home else [])
        status = parse_log(f"{simdir}/comp.log", vlt_comp_passed, vlt_comp_warn, vlt_comp_fail, waive, False, True)
        if (r or status in ("FAILED", "UNKNOWN") or (status == "PASSED_WARN" and args.pedant)
                or not os.access(binary, os.X_OK)):
            print_message('error', "Compilation Failed")
            print_message('error', f"LOG:  {simdir}/comp.log")
            exit(1)
        with open(f"{simdir}/comp.log", errors="replace") as f:
            text = f.read()
        record["warnings"] = [ln for ln in text.splitlines() if re.match(r"%Warning", ln)
                              and not any(re.search(w, ln) for w in waive)]
        # Verilator skips a build whose inputs did not change and then prints
        # no warning: the last real build's warnings stay with the build
        if re.search(r"Built from [\d.]+ MB sources in 0 modules", text):
            record["warnings"] = old.get("warnings", [])
        if args.prune_build:
            _prune_build(mdir)
            record["pruned"] = True
        with open(os.path.join(mdir, _BUILD_RECORD), "w") as f:
            json.dump(record, f, indent=1)


def _prune_build(mdir):
    """Delete what a good build needs no more: the precompiled headers
    (*.gch, most of the folder: 577 of 677 to 799 MB on a logion_tb bench),
    the objects and the libraries. The program and the sources stay; a
    later build of the folder compiles everything again (ccache helps)."""
    freed = 0
    for pattern in ("*.gch", "*.o", "*.a"):
        for path in glob.glob(os.path.join(mdir, pattern)):
            try:
                freed += os.path.getsize(path)
                os.remove(path)
            except OSError:
                pass
    print_message("info", f"pruned the build: {freed / 1e6:.0f} MB of headers and objects deleted")


def _check_build(args, mdir, binary, record, cocotb):
    """Stop when the build cannot run this test: none there, a bench of the
    other kind, or no waves or coverage that the test asks for."""
    if not os.access(binary, os.X_OK):
        print_message('error', f"no Verilator build at {binary}: its build failed or "
                               f"never ran (see comp.log beside it)")
        exit(1)
    if record.get("bench", "uvm") != ("cocotb" if cocotb else "uvm"):
        print_message('error', f"the build at {mdir} is for a {record.get('bench')} bench")
        exit(1)
    if (args.dump or args.dump_mem) and not record.get("waves"):
        print_message('error', f"the build at {mdir} has no waves: build it with --dump")
        exit(1)
    if args.codecov and not record.get("coverage"):
        print_message('error', f"the build at {mdir} has no coverage: build it with --codecov")
        exit(1)


def _remove(simdir, names):
    for name in names:
        try:
            os.remove(f"{simdir}/{name}")
        except OSError:
            pass


def _report_coverage(simdir, design_files, skip_dirs):
    """Keep the points that count in the run's coverage.dat, then write and
    print its totals table (coverage.txt)."""
    dat = f"{simdir}/coverage.dat"
    if not os.path.exists(dat):
        print_message("warning", "the simulation saved no coverage database")
        return
    _keep_counted_coverage(dat, simdir, design_files, skip_dirs)
    table = coverage_table(dat)
    with open(f"{simdir}/coverage.txt", "w") as f:
        f.write("\n".join(table) + "\n")
    print("\n".join(table))
    print(f"COV:  {dat}")


def _verilator_run_uvm(args, simdir, mdir, binary, vbin, uvm_home, sim_args, design_files):
    waves = args.dump or args.dump_mem
    record = _read_build_record(mdir)
    _check_build(args, mdir, binary, record, cocotb=False)
    if waves and record.get("waves_scope") != args.dut:
        print_message('warning', f"the build dumps {record.get('waves_scope')}, not {args.dut}")
    if not os.environ.get("VERILATOR_SOLVER") and not shutil.which("z3"):
        print_message("warning", "VERILATOR_SOLVER is not set and z3 is not on PATH, so "
                                 "randomize() with constraints fails")
    # Verilator wants a seed of 1 or more, so test_runner's seed N is
    # Verilator's N+1; it is a different random stream from Questa's
    # -sv_seed N in any case
    vseed = args.seed + 1
    run_cmd = [binary, f"+verilator+seed+{vseed}"]
    if waves:
        run_cmd.append("+vlsi_runners_waves")
    if args.codecov:
        run_cmd.append(f"+verilator+coverage+file+{simdir}/coverage.dat")
    if args.uvm_test:
        run_cmd += [f"+UVM_TESTNAME={args.uvm_test}", f"+UVM_MAX_QUIT_COUNT={args.uvm_max_quit}",
                    f"+UVM_VERBOSITY={args.verbosity}"]
    run_cmd += shlex.split(sim_args)
    # a program's stdout to a pipe is block-buffered, which would hold the
    # whole log until exit; line-buffered, regr_runner sees $finish live
    if shutil.which("stdbuf"):
        run_cmd = ["stdbuf", "-oL", "-eL"] + run_cmd
    expect = getattr(args, "expect_fail", None)
    # as in run_modelsim: an old log or coverage database must not answer
    # for this run
    _remove(simdir, (["run.log"] if expect else []) + (["coverage.dat", "coverage.txt"] if args.codecov else []))
    header = [f"[test_runner] Verilator seed {vseed} (+verilator+seed+{vseed}) for "
              f"--seed {args.seed}; not the random stream of Questa's -sv_seed {args.seed}"]
    r = _run_logged(run_cmd, simdir, f"{simdir}/run.log", header)
    if r and not expect:
        print_message('error', f"Simulation Failed {r}")
        exit(1)
    status = parse_log(f"{simdir}/run.log", vlt_sim_passed, vlt_sim_warn, vlt_sim_fail, vlt_sim_waive, True, True)
    print("---")
    print(f"LOG:  {simdir}/run.log")
    if waves:
        print(f"DUMP: {simdir}/waves.{record['waves']}")
    if args.codecov:
        _report_coverage(simdir, design_files,
                         [uvm_home, _verilator_include(record.get("verilator") or vbin or "verilator")])
    _exit_on_verdict(args, simdir, r, status)


def _cocotb_results(path):
    """(the tests run, the failed ones with their message) from a cocotb results.xml."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return 0, []
    n, failed = 0, []
    for case in root.iter("testcase"):
        n += 1
        for bad in list(case.iter("failure")) + list(case.iter("error")):
            failed.append(f"{case.get('name')}: {bad.get('message') or bad.text or 'failed'}".strip())
    return n, failed


def _verilator_run_cocotb(args, simdir, mdir, binary, vbin, uvm_home, sim_args, design_files):
    """Run one cocotb test against its build. The verdict, as cocotb benches
    had it in their own run.py: a failed test in results.xml, no test run,
    a `%Error` or ERROR line in the log (an assertion prints one while
    results.xml may still call the test a pass), or a runner that ended
    badly fails it; a `%Warning` or WARNING line, in the run or in its
    build, makes it pass with warnings (so --pedant fails it)."""
    waves = args.dump or args.dump_mem
    record = _read_build_record(mdir)
    _check_build(args, mdir, binary, record, cocotb=True)
    test = args.cocotb_test or args.test
    vseed = args.seed + 1  # Verilator draws a new seed for 0
    # sim_args come most specific first (the command line, then tests.yaml,
    # then config.yaml), since $value$plusargs takes the first match;
    # cocotb.plusargs keeps the last one, so a cocotb run gets them reversed
    plusargs = [f"+verilator+seed+{vseed}", *reversed(shlex.split(sim_args))]
    if args.codecov:
        plusargs.append(f"+verilator+coverage+file+{simdir}/coverage.dat")
    spec = {"pythonpath": [os.path.join(wa_root, "verif", args.block)],
            "test_module": args.cocotb_module, "hdl_toplevel": args.top, "hdl_toplevel_lang": "verilog",
            "testcase": test, "seed": args.seed,
            # the runner's own filter for a name is `.*<name>$`, which would
            # take `reorder` for `order`
            "test_filter": rf"\.{re.escape(test)}$",
            "plusargs": plusargs, "build_dir": mdir, "test_dir": simdir,
            "results_xml": f"{simdir}/results.xml", "waves": bool(waves)}
    expect = getattr(args, "expect_fail", None)
    _remove(simdir, ["results.xml", "dump.vcd"] + (["run.log"] if expect else [])
            + (["coverage.dat", "coverage.txt"] if args.codecov else []))
    # the test's Python and the program write their lines as they come
    os.environ["PYTHONUNBUFFERED"] = "1"
    run_cmd = ["python3", _COCOTB_RUN, "test", json.dumps(spec)]
    if shutil.which("stdbuf"):
        run_cmd = ["stdbuf", "-oL", "-eL"] + run_cmd
    header = [f"[test_runner] cocotb test {test} of {args.cocotb_module}, cocotb seed {args.seed}, "
              f"Verilator seed {vseed} (+verilator+seed+{vseed})"]
    r = _run_logged(run_cmd, simdir, f"{simdir}/run.log", header)
    n, failed = _cocotb_results(f"{simdir}/results.xml")
    with open(f"{simdir}/run.log", errors="replace") as f:
        lines = f.read().splitlines()
    errors = [ln for ln in lines if "%Error" in ln or re.search(r"\bERROR\b", ln)]
    warnings = [ln for ln in lines if "%Warning" in ln or re.search(r"\bWARNING\b", ln)]
    warnings += record.get("warnings", [])
    why = [f"{len(errors)} error line(s) in the log, the first: {errors[0].strip()}"] if errors else []
    if n == 0:
        why.append(f"no test ran: is there a test '{test}' in {args.cocotb_module}?")
    why += failed
    if r and not why:
        why.append(f"the cocotb runner ended with exit code {r}")
    status = "FAILED" if why else "PASSED_WARN" if warnings else "PASSED"
    print_test_status(status)
    for w in why:
        print_message('error', w)
    if status == "PASSED_WARN":
        print_message('warning', f"{len(warnings)} warning(s), the first: {warnings[0].strip()}")
    print("---")
    print(f"LOG:  {simdir}/run.log")
    if waves:
        print(f"DUMP: {simdir}/dump.vcd")
    if args.codecov:
        _report_coverage(simdir, design_files, [_verilator_include(record.get("verilator") or vbin or "verilator")])
    _exit_on_verdict(args, simdir, r if why else 0, status)


def source_project_env():
    """Source <git_root>/bin/setenv.sh and merge its exports into os.environ so the sim
    subprocesses (vlog/vsim) inherit them. Anchored on the PROJECT git root (wa_root), so
    it works whether this runner lives in bin/ or in a bin/runners/ submodule."""
    env_script = os.path.join(wa_root, "bin", "setenv.sh")
    if not os.path.isfile(env_script):
        return
    result = subprocess.run(
        ["bash", "-c", f"set -a && source {shlex.quote(env_script)} && env -0"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print_message("warning", f"Could not source {env_script}: {result.stderr.strip()}")
        return
    for entry in result.stdout.split("\0"):
        key, sep, value = entry.partition("=")
        if sep:
            os.environ[key] = value


def generate_sim_command(args):
    source_project_env()
    simdir = _resolve_simdir(args)
    if args.clean:
        shutil.rmtree(simdir, ignore_errors=True)
    os.makedirs(simdir, exist_ok=True)

    sim_args, design_files, vlt_files, cov_types = _gather_attrs_and_filelist(args, simdir)

    if args.simulator == "vcs":
        run_vcs(args, simdir, sim_args)
    elif args.simulator == "verilator":
        run_verilator(args, simdir, sim_args, vlt_files, design_files, cov_types)
    else:
        run_modelsim(args, simdir, sim_args, design_files)


def parse_test_args(block, test):
    """Retrieve compilation and simulation arguments for a specific test, and
    the name of its compilation."""
    block_dir = f"{wa_root}/verif/{block}"
    tests_config = f"{block_dir}/lib/tests.yaml"
    print_message("info", f"Parsing Tests config YAML - {tests_config}")
    content = load_yaml(tests_config) or {}

    # `tests:` / `compilations:` present-but-empty parse to None, so the ' or []'
    # is required: `.get(key, [])` only defaults a MISSING key, not a null one.
    test_context = next((t for t in (content.get('tests') or []) if t['name'] == test), None)
    if not test_context:
        print_message("error", f"Test '{test}' not found in {tests_config}")
        exit(1)

    comp_context = next((c for c in (content.get('compilations') or []) if c['name'] == test_context['comp']), None)
    if not comp_context:
        print_message("error", f"Compilation '{test_context['comp']}' not found for test '{test}'")
        exit(1)

    additional_args = []

    attributes = ["files", "args"]
    for attrib in attributes:
        if attrib not in comp_context or comp_context[attrib] is None:
            continue
        value = comp_context[attrib]
        if isinstance(value, str):
            value = [value]
        assert isinstance(value, list), f"{attrib} must be a list"
        if attrib in ["files"]:
            value = [make_path_abs(f, block_dir) for f in value]
            value = ["--files "+' '.join(value)]
        additional_args += value

    if "args" in test_context and test_context["args"] is not None:
        value = test_context["args"]
        if isinstance(value, str):
            value = [value]
        assert isinstance(value, list), f"{attrib} must be a list"
        additional_args += value

    return [word for item in additional_args for word in item.split()], test_context['comp']



def test_expectation(block, test):
    """The ``expect_fail`` string a test declares in tests.yaml, or None.

    A handful of the generated guards are `$error` assertions rather than a
    DECERR answer: an access wider than the slave, a WRAP or FIXED re-issue
    past AXI's 16, a length re-issue past 256, and a violated
    ``burst_overflow: guaranteed``. Those are live gates, and a test that
    drives one makes the run FAIL, which is correct. Without a way to say
    "this one was supposed to", they could only ever be checked at the
    generator and never in simulation (open item 50).

    A test that declares ``expect_fail`` inverts its verdict: it passes only
    when the run failed AND the log carries that text, so a guard that stops
    firing is a failure rather than a quieter green.
    """
    block_dir = f"{wa_root}/verif/{block}"
    content = load_yaml(f"{block_dir}/lib/tests.yaml") or {}
    ctx = next((x for x in (content.get('tests') or []) if x['name'] == test), None)
    return (ctx or {}).get('expect_fail')


def main():
    global wa_root
    wa_root = get_git_root()
    if not wa_root:
        print_message("error","You are not inside valid workarea.")
        exit(1)

    VERBOSITY_LEVELS = ['UVM_HIGH', 'UVM_MEDIUM', 'UVM_LOW', 'UVM_NONE']
    parser = argparse.ArgumentParser(description="UVM Test Runner Wrapper (Modelsim/Questa, VCS or Verilator)")

    parser.add_argument("-b",'--block', type=str, help="name of design or verif block", required=True)
    parser.add_argument('-t', '--test', type=str, help="test name (as defined in tests,.yaml)", required=True)
    parser.add_argument('--simulator', type=str, choices=['modelsim', 'vcs', 'verilator'], default='modelsim',
                        help="Which simulator to use (default: modelsim). The VCS path is best-effort and may need tuning for your install. "
                             "verilator takes the executable from PATH and the UVM 1.2 source "
                             "from $UVM_HOME (default: the one in the Questa install); see README 'Verilator'.")
    parser.add_argument('--top', type=str, default='tb', help="Set top module name (default: tb)")
    parser.add_argument('--dut', type=str, help="Set top module name (default: tb)")
    parser.add_argument("--clean", action="store_true", help="clean run directory")
    parser.add_argument("--pedant", action="store_true", help="Stop run when there are warnings")
    parser.add_argument("--rundir", type=str, help="Set user-defined rundir")
    parser.add_argument("--rundir-suffix", dest="rundir_suffix", type=str, help="Append a suffix to the auto-generated rundir (ignored if --rundir is set). Useful for running the same test+seed in multiple directories.")
    parser.add_argument("--files", action="append", nargs="*", help="Command arguments to pass to compilation process")

    parser.add_argument('-s', '--seed', type=int, default=0, help="Set the seed (default: 0)")
    parser.add_argument('--verbosity', type=str, choices=VERBOSITY_LEVELS, default='UVM_LOW', help="Set verbosity level (default: UVM_LOW)")
    parser.add_argument('--uvm_test', type=str, help="test name for UVM_TESTNAME")
    parser.add_argument('--uvm_max_quit', type=int, default=3, help="Set value for UVM_MAX_QUIT_COUNT")
    parser.add_argument("--compile-only", action="store_true", help="Only compile without running")
    parser.add_argument("--run-only", action="store_true", help="Only run without compiling")
    parser.add_argument("--lib", type=str, help="Work library to compile into or run "
                        "against (default: sv_tb_work in the run directory). With "
                        "--compile-only it is built once; every --run-only test then "
                        "points at it and skips both compilation and optimization. "
                        "With --simulator verilator it is the build directory (default: "
                        "obj_dir in the run directory), which holds the program V<top>.")
    parser.add_argument("--gui", action="store_true", help="Run ModelSim in GUI mode")
    parser.add_argument("--dump", action="store_true", help="Collect dump file")
    parser.add_argument("--dump-mem", action="store_true", help="Collect dump file including memories")
    parser.add_argument("--codecov", action="store_true", help="Collect code coverage "
                        "(statement, branch, condition, expression, FSM, toggle) on the "
                        "design's RTL; writes coverage.ucdb and coverage.txt in the run directory")
    parser.add_argument("--sim_args", action="append", nargs="*", help="Command arguments to pass to simulation process")
    parser.add_argument("--comp_args", action="append", nargs="*", help="Command arguments to pass to compilation process")
    parser.add_argument("--build_jobs", type=int, help="make -j of a Verilator build (default: the CPUs "
                        "this process may use: all of them locally, the job's under Slurm)")
    parser.add_argument("--prune_build", action="store_true", help="After a good Verilator build, delete "
                        "its precompiled headers, objects and libraries (most of the folder); the program "
                        "stays. A later build of the folder compiles everything again")
    parser.add_argument("--cocotb_module", type=str, help="A cocotb bench (--simulator verilator only): the "
                        "Python module with its tests, found in verif/<block>/. cocotb's runner then builds "
                        "the design and runs one test of the module (see README 'cocotb benches')")
    parser.add_argument("--cocotb_test", type=str, help="The cocotb test of --cocotb_module to run "
                        "(default: the test's name in tests.yaml)")

    original_args = sys.argv[1:]
    args = parser.parse_args()

    expect_fail, comp = None, None
    if args.test:
        expect_fail = test_expectation(args.block, args.test)
        additional_args, comp = parse_test_args(args.block, args.test)
        # A CLI --uvm_test overrides the one baked into tests.yaml (yaml args are
        # appended last and would otherwise win), so a custom test can run against an
        # existing net's compilation, e.g. --uvm_test cn_directed_example.
        if "--uvm_test" in original_args:
            while "--uvm_test" in additional_args:
                i = additional_args.index("--uvm_test")
                del additional_args[i:i+2]
        try:
            args = parser.parse_args(original_args + additional_args)
        except SystemExit:
            print_message("error",f"Additional arguments from tests.yaml failed. check error message above")
            exit(1)

    # AFTER the re-parse above, which builds a fresh namespace and would drop
    # anything set on the old one (open item 50)
    args.expect_fail = expect_fail
    args.comp = comp
    if args.cocotb_module and args.simulator != "verilator":
        print_message("error", "--cocotb_module: a cocotb bench runs with --simulator verilator only")
        exit(1)

    if args.dut is None:
        args.dut = args.top

    generate_sim_command(args)

if __name__ == "__main__":
    main()
