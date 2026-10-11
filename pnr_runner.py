#!/usr/bin/env python3
"""Place-and-time a design on a public library with OpenROAD-flow-scripts.

The numbers metric_runner cannot give: a design's timing after placement on a
real cell library, with wire delay in it, and what the wires are. Relative
numbers only (Nangate45 is a public, not a product, library); every question
the runner answers is a diff between two forms of one module, so `--vs` is
the main entry.

    pnr_runner.py -b <block>[:<top>] --clock clk --period 1.0
    pnr_runner.py -f <filelist> --top <module> --clock clk --period 1.0,0.8
    pnr_runner.py ... --vs <block>[:<top>]        # delta table, A against B
    pnr_runner.py ... --mode full                 # through detailed route

Fast mode runs synthesis, floorplan and placement and times the placed design
with placement parasitics: minutes on a 30k-cell NI. Full mode goes through
CTS and detailed route for routed wire lengths, and times on the extracted
parasitics. One JSON per run under pnr/<design>/<top>/<pdk>_<mode>_p<period>/.

The toolchain is the ORFS checkout in /src/tools/OpenROAD-flow-scripts (see
docs/install-openroad.md): its own OpenROAD and its own yosys, read through
the slang frontend because the generated RTL is SystemVerilog-2017 that the
plain reader rejects.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from proj_utils import *
from log_utils import *

_PNR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pnr")
ORFS_DEFAULT = "/src/tools/OpenROAD-flow-scripts"

# the library's time unit, in ns: the SDC is written in it and the reports
# are read back in it
PDK_TIME_NS = {"nangate45": 1.0, "asap7": 0.001}
PDK_CORE_MARGIN = {"nangate45": 2, "asap7": 0.5}

wa_root = None


# ----------------------------------------------------------------------
# What is measured, and which way is better
# ----------------------------------------------------------------------
# (key path in metrics.json, label, kind, smaller-is-better)
METRICS = [
    (("cells", "total"),             "cells",            "int",   True),
    (("cells", "flops"),             "flops",            "int",   True),
    (("cells", "area_um2"),          "cell area um2",    "float", True),
    (("cells", "utilization"),       "utilization",      "pct",   None),
    (("timing", "wns_ns"),           "WNS ns",           "float3", False),
    (("timing", "tns_ns"),           "TNS ns",           "float3", False),
    (("timing", "period_min_ns"),    "min period ns",    "float3", True),
    (("timing", "fmax_mhz"),         "Fmax MHz",         "float", False),
    (("critical_path", "cells"),     "path cells",       "int",   True),
    (("critical_path", "arrival_ns"), "path arrival ns", "float3", True),
    (("wire", "hpwl_total_um"),      "HPWL total um",    "float", True),
    (("wire", "hpwl_longest_um"),    "HPWL longest um",  "float", True),
    (("wire", "est_wirelength_um"),  "est. wire um",     "float", True),
    (("wire", "routed_wirelength_um"), "routed wire um", "float", True),
]


def _get(d, path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _fmt(v, kind):
    if v is None:
        return "n/a"
    if kind == "int":
        return str(int(v))
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "float3":
        return f"{v:.3f}"
    return f"{v:.1f}"


# ----------------------------------------------------------------------
# The design: what to read, from where
# ----------------------------------------------------------------------
class Design:
    """A top module and the files, include dirs and defines that build it."""

    def __init__(self, name, top, files, include_dirs=(), defines=()):
        self.name = name
        self.top = top
        self.files = [os.path.abspath(f) for f in files]
        self.include_dirs = [os.path.abspath(d) for d in include_dirs]
        self.defines = list(defines)

    @property
    def label(self):
        return f"{self.name}/{self.top}"


def design_from_block(spec):
    """`<block>[:<top>]` from design/<block>/lib/config.yaml, the file
    metric_runner and synth_runner read, so the three agree on a block."""
    block, _, top = spec.partition(":")
    top = top or block
    jinja2_variables = {"TOOL": "openroad", "WA_ROOT": wa_root, "TOP_BLOCK": block,
                        **os.environ}
    attrs = parse_attributes(block, "design", ["include_dirs", "defines", "files"],
                             jinja2_variables)
    if not attrs["files"]:
        print_message("error", f"design block '{block}' lists no files")
        exit(1)
    return Design(block, top, attrs["files"], attrs["include_dirs"], attrs["defines"])


def design_from_filelist(path, top):
    """A plain filelist: one source file per line, relative to the list's own
    directory, plus `+incdir+<dir>`, `+define+<name>[=<value>]` and `-f <list>`
    lines, `#` and `//` comments. The simulator's format, so a filelist that
    compiles is a filelist that places."""
    files, incdirs, defines = [], [], []

    def read(p):
        base = os.path.dirname(os.path.abspath(p))
        try:
            lines = open(p).read().splitlines()
        except OSError as e:
            print_message("error", f"cannot read filelist {p}: {e}")
            exit(1)
        for line in lines:
            line = line.split("//", 1)[0].split("#", 1)[0].strip()
            if not line:
                continue
            for word in shlex.split(line):
                if word.startswith("+incdir+"):
                    incdirs.extend(os.path.join(base, d) for d in word[8:].split("+") if d)
                elif word.startswith("+define+"):
                    defines.extend(d for d in word[8:].split("+") if d)
                elif word == "-f":
                    continue
                elif word.endswith(".f") and not os.path.splitext(word)[0].endswith(".sv"):
                    read(os.path.join(base, word))
                else:
                    files.append(os.path.join(base, word))

    read(path)
    if not files:
        print_message("error", f"filelist {path} names no source file")
        exit(1)
    name = os.path.splitext(os.path.basename(path))[0]
    return Design(name, top, files, incdirs, defines)


def parse_design_spec(spec, top=None):
    """`-b`/`--vs` syntax: a block (`<block>[:<top>]`) or a filelist
    (`<path>.f[:<top>]`, anything with a slash or an .f suffix)."""
    if "/" in spec or spec.endswith(".f") or ".f:" in spec:
        path, _, t = spec.rpartition(":") if ".f:" in spec else (spec, "", "")
        t = t or top
        if not t:
            print_message("error", f"a filelist needs a top module: {spec}:<top> or --top")
            exit(1)
        return design_from_filelist(path, t)
    return design_from_block(spec)


# ----------------------------------------------------------------------
# Constraints
# ----------------------------------------------------------------------
def write_sdc(path, design, clocks, period_ns, time_ns, user_sdc):
    """The clocks at this run's period, the IO budget, then the design's own
    SDC rewritten for this flow.

    A clock-crossing design ships an SDC that bounds its gray-pointer paths
    (noc_builder's `<base>.sdc`); it MUST be in, or STA times those paths
    as ordinary transfers and the number means nothing. It is written for
    Vivado and Synopsys, so three things are translated on the way in:
    `-datapath_only` (OpenSTA: `-ignore_clock_latency`), the `_reg` register
    suffix (a yosys flop is `<signal>[<bit>]$_DFF..._`, so the bit comes
    before the suffix), and `/` hierarchy in cell names (the netlist is
    flat, with `.` in the names). A `create_clock` on a port the runner
    already clocks is dropped, so the period sweep wins."""
    scale = 1.0 / time_ns
    lines = ["# written by pnr_runner", f"current_design {design.top}", ""]
    # the design's SDC names its clocks (`corenet` on port clk) and its clock
    # groups and bounds use those names, so a clock the runner takes over
    # keeps the name it had there
    names = {}
    if user_sdc:
        for m in re.finditer(r"create_clock\s+-name\s+(\S+).*?\[get_ports\s+\{?(\S+?)\}?\]",
                             open(user_sdc).read()):
            names[m.group(2)] = m.group(1)
    primary = None
    for port, cperiod in clocks:
        p = (cperiod if cperiod else period_ns) * scale
        name = names.get(port, port)
        lines.append(f"create_clock -name {name} -period {p:g} [get_ports {{{port}}}]")
        primary = primary or (name, p)
    if primary:
        name, p = primary
        lines += ["",
                  "# IO: a virtual clock at the primary period, 20% of it on each side",
                  f"create_clock -name vclk_{name} -period {p:g}",
                  f"set_input_delay {0.2 * p:g} -clock vclk_{name} [all_inputs -no_clocks]",
                  f"set_output_delay {0.2 * p:g} -clock vclk_{name} [all_outputs]"]
    if user_sdc:
        lines += ["", f"# {user_sdc}, rewritten for OpenSTA on a flat yosys netlist"]
        clocked = {port for port, _ in clocks}
        for raw in open(user_sdc).read().splitlines():
            line = raw
            if re.match(r"\s*set\s+NOC_MAXDELAY_OPTS\b", line):
                line = 'set NOC_MAXDELAY_OPTS "-ignore_clock_latency"'
            elif re.match(r"\s*set\s+NOC_REG_SUFFIX\b", line):
                line = 'set NOC_REG_SUFFIX ""'
            m = re.search(r"create_clock\b.*\[get_ports\s+\{?(\S+?)\}?\]", line)
            if m and m.group(1) in clocked:
                line = "# (clocked by the runner) " + raw
            # inside get_cells {...}: hierarchy and the register suffix
            def cells(mm):
                pat = mm.group(1).replace("/", ".")
                pat = pat.replace("$NOC_REG_SUFFIX[*][*]", "[*][*]$_*DFF*")
                pat = pat.replace("$NOC_REG_SUFFIX[*]", "[*]$_*DFF*")
                pat = pat.replace("$NOC_REG_SUFFIX", "$_*DFF*")
                return "get_cells {" + pat + "}"
            line = re.sub(r"get_cells\s+\{([^}]*)\}", cells, line)
            lines.append(line)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ----------------------------------------------------------------------
# ORFS
# ----------------------------------------------------------------------
def orfs_env(orfs):
    env = dict(os.environ)
    env["PATH"] = ":".join([f"{orfs}/tools/install/OpenROAD/bin",
                            f"{orfs}/tools/install/yosys/bin", env.get("PATH", "")])
    return env


def write_config(path, design, pdk, sdc, util, jobs, seed):
    files = " ".join(design.files)
    lines = [
        "# written by pnr_runner",
        f"export DESIGN_NAME = {design.top}",
        f"export PLATFORM = {pdk}",
        f"export VERILOG_FILES = {files}",
        "export SYNTH_HDL_FRONTEND = slang",
        f"export SDC_FILE = {sdc}",
        f"export CORE_UTILIZATION = {util}",
        "export CORE_ASPECT_RATIO = 1",
        f"export CORE_MARGIN = {PDK_CORE_MARGIN.get(pdk, 2)}",
        "export PLACE_DENSITY_LB_ADDON = 0.20",
        "export TNS_END_PERCENT = 100",
        # fixed seeds: two runs of one design print the same number
        "export SYNTH_REPEATABLE_BUILD = 1",
        f"export GPL_RANDOM_SEED = {seed}",
        f"export OR_SEED = {seed}",
    ]
    if design.include_dirs:
        lines.append("export VERILOG_INCLUDE_DIRS = " + " ".join(design.include_dirs))
    if design.defines:
        lines.append("export VERILOG_DEFINES = " + " ".join(f"-D {d}" for d in design.defines))
    if jobs:
        lines.append(f"export NUM_CORES = {jobs}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def run_orfs(orfs, rundir, config, work, target, log, extra=()):
    cmd = ["make", "-C", f"{orfs}/flow", f"DESIGN_CONFIG={config}",
           f"WORK_HOME={work}", *extra, target]
    with open(log, "a") as f:
        f.write("$ " + " ".join(cmd) + "\n")
        f.flush()
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=orfs_env(orfs))
    return r.returncode


def load_vars(orfs, path):
    """ORFS's `make vars` writes `export K="V"` lines; the values may name
    ${FLOW_HOME}, so a shell expands them with that set, from the flow
    directory where the relative ones (./scripts) resolve."""
    flow = f"{orfs}/flow"
    r = subprocess.run(["bash", "-c", f"cd {shlex.quote(flow)} && export FLOW_HOME={shlex.quote(flow)} "
                                      f"&& set -a && source {shlex.quote(path)} && env -0"],
                       capture_output=True, text=True)
    env = {}
    for entry in r.stdout.split("\0"):
        k, sep, v = entry.partition("=")
        if sep:
            env[k] = v
    return env


# ----------------------------------------------------------------------
# Reading the results
# ----------------------------------------------------------------------
def read_kv(path):
    d = {}
    try:
        for line in open(path):
            k, sep, v = line.rstrip("\n").partition("=")
            if sep:
                d[k] = v
    except OSError:
        pass
    return d


def _float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_paths(path, time_ns):
    """The two worst paths report_checks printed: over everything, and from
    register to register (after the `=== register to register ===` line).
    Returns (worst, reg2reg); each is its ends, its arrival, its slack, and
    the cells on it. The cells are the instances between the launching and
    the capturing point, one line per pin in the report, so the distinct
    instance names are counted and the registers at the ends taken off:
    that is this flow's logic depth."""
    try:
        text = open(path).read()
    except OSError:
        return None, None
    first, _, second = text.partition("=== register to register ===")
    return _worst_path(first, time_ns), _worst_path(second, time_ns)


def _worst_path(text, time_ns):
    blocks = re.split(r"(?m)^Startpoint: ", text)[1:]
    worst = None
    for b in blocks:
        start = re.match(r"(\S+)", b).group(1)
        m = re.search(r"(?m)^Endpoint: (\S+)", b)
        end = m.group(1) if m else None
        m = re.search(r"(?m)^Path Group: (.*)$", b)
        group = m.group(1).strip() if m else None
        m = re.search(r"(?m)^\s*([-\d.]+)\s+data arrival time", b)
        arrival = _float(m.group(1)) if m else None
        m = re.search(r"(?m)^\s*([-\d.]+)\s+slack \((MET|VIOLATED)\)", b)
        slack = _float(m.group(1)) if m else None
        insts = []
        for pin in re.findall(r"(?m)^\s*[-\d.]+\s+[-\d.]+\s+[\^v]\s+(\S+)/\S+ \(", b):
            if pin not in insts:
                insts.append(pin)
        # a port at either end is not an instance, so only registers come off
        regs = sum(1 for x in (start, end) if x in insts)
        cells = max(0, len(insts) - regs) if insts else None
        entry = {"startpoint": start, "endpoint": end, "path_group": group,
                 "arrival_ns": arrival * time_ns if arrival is not None else None,
                 "slack_ns": slack * time_ns if slack is not None else None,
                 "cells": cells}
        if worst is None or (slack is not None and (worst["slack_ns"] is None
                                                     or slack * time_ns < worst["slack_ns"])):
            worst = entry
    return worst


def read_stage_json(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def unmatched_sdc_objects(log):
    """OpenSTA warns `instance 'x' not found` for every SDC pattern that hits
    nothing: a crossing bound that silently constrains nothing (the trap)."""
    names = []
    try:
        for line in open(log, errors="ignore"):
            m = re.search(r"STA-\d+\] (?:instance|net|pin|port|cell) '([^']+)' not found", line)
            if m and m.group(1) not in names:
                names.append(m.group(1))
    except OSError:
        pass
    return names


# ----------------------------------------------------------------------
# One run
# ----------------------------------------------------------------------
def place_and_time(args, design, period_ns, nets):
    pdk = args.pdk
    time_ns = PDK_TIME_NS[pdk]
    tag = f"{pdk}_{args.mode}_p{period_ns:g}"
    rundir = os.path.join(wa_root, "pnr", design.name, design.top, tag)
    if args.clean:
        shutil.rmtree(rundir, ignore_errors=True)
    os.makedirs(rundir, exist_ok=True)
    work = os.path.join(rundir, "orfs")
    config = os.path.join(rundir, "config.mk")
    sdc = os.path.join(rundir, "constraint.sdc")
    make_log = os.path.join(rundir, "make.log")
    report_dir = os.path.join(rundir, "report")
    os.makedirs(report_dir, exist_ok=True)

    print_message("info", f"{design.label}: {args.mode} on {pdk} at {period_ns:g} ns -> {rundir}")
    write_sdc(sdc, design, args.clock, period_ns, time_ns, args.sdc)
    write_config(config, design, pdk, sdc, args.util, args.jobs, args.seed)
    open(make_log, "w").close()

    t0 = time.time()
    orfs = args.orfs
    if args.mode == "fast":
        r = run_orfs(orfs, rundir, config, work, "place", make_log)
    else:
        # finish without the GDS: that needs KLayout, which this box lacks,
        # and the numbers are all written before it (6_report)
        r = run_orfs(orfs, rundir, config, work, "finish", make_log, ["GDS_FINAL_FILE="])
    if r:
        print_message("error", f"ORFS failed ({r}); log: {make_log}")
        return None
    r = run_orfs(orfs, rundir, config, work, "vars", make_log)
    if r:
        print_message("error", f"ORFS vars failed ({r}); log: {make_log}")
        return None
    flow_elapsed = time.time() - t0

    base = os.path.join(work, "{}", pdk, design.top, "base")
    logs, results, objects, reports = (base.format(d) for d in ("logs", "results", "objects", "reports"))
    env = load_vars(orfs, os.path.join(objects, "vars.sh"))
    env.update(orfs_env(orfs))
    # vars.sh writes the output directories relative to WORK_HOME; the script
    # runs from the flow directory, so it gets them absolute
    env.update({"WORK_HOME": work, "RESULTS_DIR": results, "LOG_DIR": logs,
                "OBJECTS_DIR": objects, "REPORTS_DIR": reports})
    env.update({
        "PNR_ODB": "3_place.odb" if args.mode == "fast" else "6_final.odb",
        "PNR_SDC": "3_place.sdc" if args.mode == "fast" else "6_final.sdc",
        "PNR_MODE": args.mode,
        "PNR_NETS": " ".join(nets),
        "PNR_OUT": report_dir,
    })
    sta_log = os.path.join(report_dir, "openroad.log")
    with open(sta_log, "w") as f:
        r = subprocess.run(["openroad", "-exit", "-no_splash",
                            os.path.join(_PNR_DIR, "pnr_report.tcl")],
                           cwd=f"{orfs}/flow", env=env, stdout=f, stderr=subprocess.STDOUT)
    if r.returncode:
        print_message("error", f"measurement failed ({r.returncode}); log: {sta_log}")
        return None

    sta = read_kv(os.path.join(report_dir, "sta.txt"))
    worst_path, reg2reg = parse_paths(os.path.join(report_dir, "paths.txt"), time_ns)
    stage = "3_5_place_dp" if args.mode == "fast" else "6_report"
    sj = read_stage_json(os.path.join(logs, f"{stage}.json"))
    prefix = "detailedplace" if args.mode == "fast" else "finish"
    route = read_stage_json(os.path.join(logs, "5_2_route.json")) if args.mode == "full" else {}

    # the sta:: values in sta.txt are seconds; the report_checks text is in
    # the library's unit (ns on nangate45, ps on asap7)
    S = 1e9
    wns = _float(sta.get("wns"))
    tns = _float(sta.get("tns"))
    pmins = {k.split(":", 1)[1]: _float(v) * S
             for k, v in sta.items() if k.startswith("period_min:") and _float(v)}
    primary = args.clock[0][0] if args.clock else None
    pmin = pmins.get(primary) if primary in pmins else (max(pmins.values()) if pmins else None)

    per_net = {}
    for k, v in sta.items():
        if not k.startswith("net:"):
            continue
        name = k[4:]
        if v == "missing":
            per_net[name] = None
            continue
        hpwl, pins, routed = (v.split(",") + ["", ""])[:3]
        per_net[name] = {"hpwl_um": _float(hpwl), "pins": int(pins) if pins else None,
                         "routed_um": _float(routed)}

    metrics = {
        "design": design.name, "top": design.top, "pdk": pdk, "mode": args.mode,
        "period_ns": period_ns,
        "clocks": {port: (p or period_ns) for port, p in args.clock},
        "utilization_target": args.util,
        "timing": {
            "wns_ns": wns * S if wns is not None else None,
            "tns_ns": tns * S if tns is not None else None,
            "hold_wns_ns": (_float(sta.get("hold_wns")) or 0) * S,
            "period_min_ns": pmin,
            "fmax_mhz": 1000.0 / pmin if pmin else None,
            "period_min_by_clock_ns": pmins,
            "parasitics": sta.get("parasitics"),
        },
        # the number the questions are about: the deepest register-to-register
        # path. The worst path over everything (ports under the IO budget) is
        # kept beside it.
        "critical_path": reg2reg,
        "worst_path": worst_path,
        "cells": {
            "total": int(sta["cells"]) if "cells" in sta else None,
            "flops": int(sta["flops"]) if "flops" in sta else None,
            "area_um2": _float(sta.get("area")),
            "utilization": _float(sta.get("utilization")),
            "stdcell_count_orfs": sj.get(f"{prefix}__design__instance__count__stdcell"),
            "stdcell_area_orfs": sj.get(f"{prefix}__design__instance__area__stdcell"),
        },
        "wire": {
            "hpwl_total_um": _float(sta.get("hpwl_total")),
            "hpwl_longest_um": _float(sta.get("hpwl_longest")),
            "hpwl_longest_net": sta.get("hpwl_longest_net"),
            "est_wirelength_um": sj.get(f"{prefix}__route__wirelength__estimated"),
            "routed_wirelength_um": route.get("detailedroute__route__wirelength"),
        },
        "nets": per_net,
        "sdc_unmatched": unmatched_sdc_objects(os.path.join(logs, "2_1_floorplan.log")),
        "run": {
            "rundir": rundir, "make_log": make_log, "sta_log": sta_log,
            "paths": os.path.join(report_dir, "paths.txt"),
            "orfs_logs": logs, "orfs_results": results,
            "flow_elapsed_s": round(flow_elapsed, 1),
            "files": design.files, "sdc": sdc, "user_sdc": args.sdc,
            "seed": args.seed,
        },
    }
    with open(os.path.join(rundir, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    with open(os.path.join(rundir, "report.txt"), "w") as f:
        f.write(render_single(metrics))
    if metrics["sdc_unmatched"]:
        print_message("warning", f"{len(metrics['sdc_unmatched'])} SDC object(s) matched "
                                 f"nothing in the netlist, so they constrain nothing: "
                                 f"{', '.join(metrics['sdc_unmatched'][:4])}"
                                 f"{' ...' if len(metrics['sdc_unmatched']) > 4 else ''}")
    print_message("info", f"METRICS: {rundir}/metrics.json")
    return metrics


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------
def render_single(m):
    out = [f"{m['design']}/{m['top']}: {m['mode']} on {m['pdk']} at {m['period_ns']:g} ns "
           f"(target utilization {m['utilization_target']}%, parasitics: {m['timing']['parasitics']})",
           f"  {'metric':<18}{'value':>14}",
           f"  {'-' * 32}"]
    for key, label, kind, _ in METRICS:
        v = _get(m, key)
        if v is None and key[0] == "wire" and key[1] in ("routed_wirelength_um", "est_wirelength_um"):
            continue
        out.append(f"  {label:<18}{_fmt(v, kind):>14}")
    for title, cp in (("critical path (register to register):", m.get("critical_path")),
                      ("worst path over everything (ports under the IO budget):", m.get("worst_path"))):
        if cp:
            out += ["", title,
                    f"  from  {cp.get('startpoint')}",
                    f"  to    {cp.get('endpoint')}",
                    f"  group {cp.get('path_group')}, {cp.get('cells')} cells, "
                    f"arrival {_fmt(cp.get('arrival_ns'), 'float3')} ns, "
                    f"slack {_fmt(cp.get('slack_ns'), 'float3')} ns"]
    if m.get("nets"):
        out += ["", "nets:"]
        for name, n in m["nets"].items():
            if n is None:
                out.append(f"  {name}: not in the netlist")
            else:
                routed = f", routed {n['routed_um']:.1f} um" if n.get("routed_um") else ""
                out.append(f"  {name}: {n['pins']} pins, HPWL {n['hpwl_um']:.1f} um{routed}")
    if m.get("sdc_unmatched"):
        out += ["", "SDC objects that matched nothing (they constrain nothing):"]
        out += [f"  {x}" for x in m["sdc_unmatched"]]
    out += ["", f"logs: {m['run']['make_log']}", f"      {m['run']['orfs_logs']}",
            f"path: {m['run']['paths']}"]
    return "\n".join(out) + "\n"


def render_compare(ma, mb):
    """B against A, one row per metric; the sign of 'better' depends on the
    metric (more Fmax is better, fewer cells is better)."""
    name_a, name_b = f"{ma['design']}/{ma['top']}", f"{mb['design']}/{mb['top']}"
    col = 14
    out = [f"B against A: {ma['mode']} on {ma['pdk']} at {ma['period_ns']:g} ns (delta = B - A)",
           f"  A = {name_a}",
           f"  B = {name_b}",
           f"  {'metric':<18}{'A':>{col}}{'B':>{col}}{'delta':>{col}}{'%':>9}  better",
           f"  {'-' * (18 + 3 * col + 17)}"]
    for key, label, kind, smaller in METRICS:
        a, b = _get(ma, key), _get(mb, key)
        if a is None and b is None:
            continue
        if a is None or b is None:
            delta_s, pct_s, verdict = "n/a", "n/a", ""
        else:
            d = b - a
            delta_s = _fmt(d, kind)
            if d > 0:
                delta_s = "+" + delta_s
            pct_s = f"{d / a * 100:+.1f}%" if a else "n/a"
            if smaller is None or d == 0:
                verdict = "same" if d == 0 else ""
            else:
                verdict = "B" if (d < 0) == smaller else "A"
        out.append(f"  {label:<18}{_fmt(a, kind):>{col}}{_fmt(b, kind):>{col}}"
                   f"{delta_s:>{col}}{pct_s:>9}  {verdict}")
    for m, tag in ((ma, "A"), (mb, "B")):
        cp = m.get("critical_path") or {}
        if cp:
            out.append(f"  {tag} path: {cp.get('startpoint')} -> {cp.get('endpoint')} "
                       f"({cp.get('cells')} cells)")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------
def main():
    global wa_root
    wa_root = get_git_root()
    if not wa_root:
        print_message("error", "You are not inside valid workarea.")
        exit(1)

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.split("\n\n")[1:]))
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("-b", "--block", metavar="BLOCK[:TOP]",
                     help="design block (design/<block>/lib/config.yaml), top defaults to the block name")
    src.add_argument("-f", "--filelist", metavar="FILE",
                     help="a filelist (paths relative to it, +incdir+, +define+); needs --top")
    p.add_argument("--top", help="top module (with --filelist)")
    p.add_argument("--clock", action="append", metavar="PORT[=PERIOD_NS]", default=[],
                   help="a clock port; the first is the primary one. Without =PERIOD it "
                        "takes the --period sweep (default: clk)")
    p.add_argument("--period", default="1.0", metavar="NS[,NS...]",
                   help="clock period(s) in ns to run, one run each (default: 1.0)")
    p.add_argument("--mode", choices=["fast", "full"], default="fast",
                   help="fast: synth, floorplan, place, STA on placement parasitics. "
                        "full: through CTS and detailed route, STA on extracted parasitics")
    p.add_argument("--pdk", choices=sorted(PDK_TIME_NS), default="nangate45")
    p.add_argument("--sdc", help="the design's own SDC (a clock-crossing design's <base>.sdc), "
                                 "rewritten for OpenSTA and sourced after the runner's clocks")
    p.add_argument("--nets", default="", metavar="NET[,NET...]",
                   help="nets to measure one by one (flat netlist names, '.' for hierarchy)")
    p.add_argument("--nets-file", help="a file with one net name per line")
    p.add_argument("--util", type=int, default=50, help="core utilization %% (default: 50)")
    p.add_argument("--vs", metavar="BLOCK[:TOP] | FILELIST.f:TOP",
                   help="a second design to run the same way and diff against")
    p.add_argument("--jobs", type=int, help="threads for OpenROAD (default: all)")
    p.add_argument("--seed", type=int, default=42, help="placement and routing seed (default: 42)")
    p.add_argument("--orfs", default=os.environ.get("ORFS_HOME", ORFS_DEFAULT),
                   help=f"OpenROAD-flow-scripts checkout (default: $ORFS_HOME or {ORFS_DEFAULT})")
    p.add_argument("--clean", action="store_true", help="remove the run directory first")
    args = p.parse_args()

    if not os.path.isfile(os.path.join(args.orfs, "flow", "Makefile")):
        print_message("error", f"no OpenROAD-flow-scripts at {args.orfs} (see docs/install-openroad.md)")
        exit(1)
    if args.filelist and not args.top:
        p.error("--filelist needs --top")
    if args.sdc and not os.path.isfile(args.sdc):
        p.error(f"no such SDC: {args.sdc}")
    args.sdc = os.path.abspath(args.sdc) if args.sdc else None

    clocks = []
    for c in args.clock or ["clk"]:
        port, _, per = c.partition("=")
        clocks.append((port, float(per) if per else None))
    args.clock = clocks
    periods = [float(x) for x in args.period.split(",") if x]

    nets = [n for n in args.nets.split(",") if n]
    if args.nets_file:
        nets += [l.strip() for l in open(args.nets_file) if l.strip() and not l.startswith("#")]

    a = design_from_block(args.block) if args.block else design_from_filelist(args.filelist, args.top)
    b = parse_design_spec(args.vs, args.top) if args.vs else None

    failed = False
    for period in periods:
        ma = place_and_time(args, a, period, nets)
        if ma is None:
            failed = True
            continue
        if b is None:
            print()
            print(render_single(ma))
            continue
        mb = place_and_time(args, b, period, nets)
        if mb is None:
            failed = True
            continue
        text = render_compare(ma, mb)
        print()
        print(text)
        cmp_dir = os.path.join(wa_root, "pnr", "compare")
        os.makedirs(cmp_dir, exist_ok=True)
        stem = f"{a.name}_{a.top}__vs__{b.name}_{b.top}__{args.pdk}_{args.mode}_p{period:g}"
        with open(os.path.join(cmp_dir, stem + ".txt"), "w") as f:
            f.write(text)
        with open(os.path.join(cmp_dir, stem + ".json"), "w") as f:
            json.dump({"a": ma, "b": mb}, f, indent=2)
        print_message("info", f"COMPARE: {cmp_dir}/{stem}.txt")
    exit(1 if failed else 0)


if __name__ == "__main__":
    main()
