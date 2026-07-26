import argparse
import json
import os
import re

_SYNTH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "synth")
from proj_utils import *
from log_utils import *

wa_root = None

# ----------------------------------------------------------------------
# What we measure
# ----------------------------------------------------------------------
# There is no real PDK / sign-off STA in this flow (yosys maps onto its toy
# cmos_cells.lib), so ABSOLUTE numbers are meaningless. These are relative
# proxies whose only purpose is to diff one RTL design against another:
#
#   area   -> chip area from `stat -liberty` (float, toy-lib units)
#   cells  -> mapped cell count from `stat`   (int, PDK-independent)
#   timing -> critical-path logic depth from `ltp` (int levels between regs,
#             PDK-independent, deterministic)
#
# Each metric carries the direction that counts as "better" so the compare
# table can flag regressions. For all three, smaller is better.
METRICS = [
    ("area",   "chip area",     "float"),
    ("cells",  "cell count",    "int"),
    ("timing", "logic depth",   "int"),
]


def parse_metrics_log(log_path):
    """Extract {area, cells, timing} from a yosys metrics log. Any metric that
    cannot be found is returned as None (e.g. liberty missing -> no chip area)."""
    metrics = {name: None for name, _, _ in METRICS}
    try:
        with open(log_path, "r") as f:
            text = f.read()
    except OSError as e:
        print_message("error", f"Could not read metrics log {log_path}: {e}")
        return metrics

    area_block   = _between(text, "METRIC_AREA_BEGIN",   "METRIC_AREA_END")
    timing_block = _between(text, "METRIC_TIMING_BEGIN", "METRIC_TIMING_END")

    m = re.search(r"Chip area for (?:top )?module\s+'[^']*':\s*([\d.]+)", area_block)
    if m:
        metrics["area"] = float(m.group(1))

    # Cell count. Newer yosys `stat` prints a columnar total line
    # "<count> <area> cells"; older versions printed "Number of cells: <n>".
    m = (re.search(r"(\d+)\s+\d+(?:\.\d+)?\s+cells\b", area_block)
         or re.search(r"Number of cells:\s*(\d+)", area_block))
    if m:
        metrics["cells"] = int(m.group(1))

    # `ltp` prints e.g. "Longest topological path in \top (length=8):"
    m = re.search(r"length=(\d+)", timing_block)
    if m:
        metrics["timing"] = int(m.group(1))

    return metrics


def _between(text, begin, end):
    """Return the slice of text between the first `begin` and the next `end`
    marker, or "" if the pair is not present."""
    i = text.find(begin)
    if i < 0:
        return ""
    j = text.find(end, i)
    return text[i:j if j >= 0 else None]


def synth_and_measure(args, block, top):
    """Synthesize `block` (top module `top`) and return its metrics dict, or
    None if synthesis failed. Mirrors synth_runner's attribute parsing so the
    same design/<block>/lib/config.yaml drives both."""
    print_message("info", f"Measuring block '{block}' (top '{top}')")

    jinja2_variables = {
        "TOOL": "yosys",
        "WA_ROOT": wa_root,
        "TOP_BLOCK": block,
        **os.environ,
    }
    attributes_to_parse = ["include_dirs", "defines", "files", "comp_args"]
    attributes = parse_attributes(block, "design", attributes_to_parse, jinja2_variables)

    compargs_list = list(attributes["comp_args"])
    for incdir in attributes["include_dirs"]:
        compargs_list.append(f"-I{incdir}")
    for define in attributes["defines"]:
        compargs_list.append(f"-D{define}")

    synthargs_list = []
    if args.flatten:
        synthargs_list.append("-flatten")

    tcl_lines = [
        f"set top {top}",
        f"set comp_args {{{' '.join(compargs_list)}}}",
        f"set rtl_files {{{' '.join(attributes['files'])}}}",
        f"set synth_args {{{' '.join(synthargs_list)}}}",
    ]

    rundir = os.path.join(wa_root, "metrics", block, top)
    if args.clean:
        shutil.rmtree(rundir, ignore_errors=True)
    os.makedirs(rundir, exist_ok=True)

    replace_lines_in_pattern_file(
        os.path.join(_SYNTH_DIR, "yosys_metrics.tcl"),
        f"{rundir}/yosys_metrics.tcl", "AUTO_GEN_SYNTH", tcl_lines)

    r = run_command("yosys -t yosys_metrics.tcl --logfile metrics.log -q", rundir)
    if r:
        print_message("error", f"Synthesis failed for block '{block}' (top '{top}')")
        print_message("error", f"LOG: {rundir}/metrics.log")
        return None

    metrics = parse_metrics_log(f"{rundir}/metrics.log")
    with open(f"{rundir}/metrics.json", "w") as f:
        json.dump({"block": block, "top": top, "metrics": metrics}, f, indent=2)
    print_message("info", f"LOG: {rundir}/metrics.log")
    print_message("info", f"METRICS: {rundir}/metrics.json")
    return metrics


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------
def _fmt(value, kind):
    if value is None:
        return "n/a"
    return f"{value:.1f}" if kind == "float" else str(value)


def print_single(name, metrics):
    print()
    print(f"metrics for {name}")
    print(f"  {'metric':<14}{'value':>12}")
    print(f"  {'-'*14}{'-'*12}")
    for key, label, kind in METRICS:
        print(f"  {label:<14}{_fmt(metrics.get(key), kind):>12}")
    print()


def print_compare(name_a, m_a, name_b, m_b):
    """Print a metric-by-metric diff of B relative to A. Only the delta matters,
    so absolute values are shown just as context; smaller is better for all."""
    col = max(12, len(name_a) + 2, len(name_b) + 2)
    print()
    print(f"comparing '{name_b}' against baseline '{name_a}'  (delta = {name_b} - {name_a}, negative is better)")
    header = f"  {'metric':<14}{name_a:>{col}}{name_b:>{col}}{'delta':>{col}}{'%':>10}"
    print(header)
    print(f"  {'-'*(14 + 3*col + 10)}")
    for key, label, kind in METRICS:
        a = m_a.get(key)
        b = m_b.get(key)
        a_s = _fmt(a, kind)
        b_s = _fmt(b, kind)
        if a is None or b is None:
            delta_s, pct_s = "n/a", "n/a"
        else:
            delta = b - a
            delta_s = _fmt(delta, kind)
            if delta > 0 and not delta_s.startswith("+"):
                delta_s = "+" + delta_s
            pct_s = f"{(delta / a * 100):+.1f}%" if a else "n/a"
        print(f"  {label:<14}{a_s:>{col}}{b_s:>{col}}{delta_s:>{col}}{pct_s:>10}")
    print()


def main():
    global wa_root
    wa_root = get_git_root()
    if not wa_root:
        print_message("error", "You are not inside valid workarea.")
        exit(1)

    parser = argparse.ArgumentParser(
        description="RTL metric runner: synthesize design blocks with yosys and "
                    "compare relative area/timing between two designs.")
    parser.add_argument("-b", "--block", type=str, required=True,
                        help="design block to measure (design/<block>/lib/config.yaml)")
    parser.add_argument("--top", type=str,
                        help="top module name (default: block name)")
    parser.add_argument("--vs", type=str, metavar="BLOCK[:TOP]",
                        help="second design to synthesize and diff against --block")
    parser.add_argument("--clean", action="store_true", help="clean run directory")
    parser.add_argument("--flatten", action="store_true", help="flatten synthesis")
    args = parser.parse_args()

    top = args.top or args.block
    metrics = synth_and_measure(args, args.block, top)
    if metrics is None:
        exit(1)

    name = f"{args.block}/{top}"

    if not args.vs:
        print_single(name, metrics)
        return

    vs_block, _, vs_top = args.vs.partition(":")
    vs_top = vs_top or vs_block
    vs_metrics = synth_and_measure(args, vs_block, vs_top)
    if vs_metrics is None:
        exit(1)

    print_compare(name, metrics, f"{vs_block}/{vs_top}", vs_metrics)


if __name__ == "__main__":
    main()
