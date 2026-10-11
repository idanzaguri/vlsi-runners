# vlsi-runners

Shared VLSI flow runners, used as a git submodule (usually at `bin/runners/`) across
projects. Each runner anchors on the project's git root and reads project config from
there, so the same code works in every project. Project-specific config lives in the
**project** (`bin/setenv.sh`, `verif/<block>/lib/tests.yaml`, design attributes); only
the runner code lives here.

## Runners

| script | what it does | example |
|---|---|---|
| `test_runner.py` | compile + run one UVM test (Questa or VCS) | `python3 bin/runners/test_runner.py -b <block> -t <test> [--uvm_test <name>] [--sim_args +X]` |
| `regr_runner.py` | run a list of tests in parallel, summarize, merge coverage | `python3 bin/runners/regr_runner.py --tests <regr.yaml> --name <run> [--timeout 300]` |
| `synth_runner.py` | yosys synthesis for a block (templates in `synth/`) | `python3 bin/runners/synth_runner.py -b <block> [--top <mod>]` |
| `metric_runner.py` | synthesize + report relative area/timing, diff two designs | `python3 bin/runners/metric_runner.py -b <block_a> --vs <block_b>` |
| `pnr_runner.py` | place-and-time on a public library (OpenROAD), diff two designs | `python3 bin/runners/pnr_runner.py -b <block_a> --vs <block_b> --period 1.0` |

`test_runner` selects the test from `verif/<block>/lib/tests.yaml`; a CLI `--uvm_test`
overrides the one baked into the entry (handy for many flavors of one test class).
`regr_runner --tests` takes a YAML list of `{name, block}` entries.

### One compile per design, not per test (Questa)

Questa optimizes a design inside `vsim`, so a design with five tests used to be
optimized five times. `test_runner` now runs `vopt` after `vlog` and names the result
`<top>_opt`; `vsim` runs that unit and optimizes nothing. Measured on one design:
**4.8 s per test becomes 1.8 s.**

`regr_runner` uses it across a whole list: it builds one work library per DESIGN
(`<regression>/_build/<block>/<comp>`) before the run, then every test of that design
runs against it with `--run-only --lib <that>/sv_tb_work`.

| flag | where | what |
|---|---|---|
| `--lib <path>` | `test_runner` | compile into, or run against, this work library instead of the run directory's own |
| `--compile-only` | `test_runner` | build the library and its snapshot, run nothing |
| `--no-shared-lib` | `regr_runner` | compile inside every test, as before |

A wave run (`--dump`) keeps the old path: the debug database is baked at optimization
time, so `vsim` optimizes that one itself. A library built before this change has no
snapshot and is run exactly as it used to be; a recompile clears the marker
(`<lib>.vopt`) that records the snapshot's name.

### Code coverage (Questa)

`--codecov` on either runner collects code coverage: statement, branch, condition,
expression, FSM and toggle. **It works on the free Questa FPGA Starter Edition**
(checked on FSE 2025.2): every type above, assertion counts, `coverage save`,
`vcover merge` and `vcover report`, text and HTML. What the Starter Edition refuses
is the `svverification` license feature, so covergroups, `randomize()`, `randcase`
and `randsequence` fail at load time; functional coverage has to wait for a full
license. (The HTML report also warns that test-plan data needs a `qvman` license;
there is none to include, so ignore it.)

Only the **design's RTL** is instrumented: the units whose source file belongs to a
`design` block in the dependency tree, selected by name with `vopt +cover=bcefsx+<unit>`
(the library's `vdir -l` says which unit came from which file). A plain `-cover` would
instrument the bench and the VIPs too, and the report drowns in VIP toggles nobody will
close. A block tree without a `design` block, such as a VIP's own bench, covers everything.

| run | what you get |
|---|---|
| `test_runner ... --codecov` | the totals table on the console, `coverage.txt` and `coverage.ucdb` in the run directory (`coverage save -instance <--dut>`) |
| `regr_runner ... --codecov` | per block: `merged.ucdb`, `coverage.txt` (also appended to `summary.txt`) and `coverage_html/index.html` under `sim/regression/<name>/<block>/` |

To browse a database in the GUI: `vsim -viewcov <file>.ucdb`. To exclude something
from the figures, `vcover` and the GUI's exclusion commands apply to the merged file.

`metric_runner` reads the same `design/<block>/lib/config.yaml` as `synth_runner`. It
maps the RTL with yosys and reports **relative** proxies (there is no real PDK here, so
absolute values are meaningless, only the diff between two designs matters): chip area +
cell count (`stat`) and critical-path logic depth (`ltp`). `--vs <block>[:<top>]`
synthesizes a second design and prints a delta table; each run also drops a
`metrics.json` under `metrics/<block>/<top>/`.

### Place-and-time numbers (`pnr_runner`)

`metric_runner` stops at logic depth on a toy library, which says nothing about
wire delay or about what a real library makes of a carry chain. `pnr_runner` takes
the same design through OpenROAD-flow-scripts (ORFS) on **Nangate45** (a public 45 nm
library: absolute numbers are meaningless, only the diff between two forms of one
module matters) and reports what placement knows:

| group | what |
|---|---|
| timing | WNS, TNS, the minimum period (so Fmax), per clock |
| critical path | start and end register, cells on it (this flow's logic depth), arrival, slack |
| cells | count, flops, cell area, utilization |
| wire | HPWL of every net summed and the longest one, ORFS's estimated length, and in `full` mode the routed length |
| nets | for each net named with `--nets`: pins, HPWL, routed length |

```bash
# one design, fast: synth, floorplan, place, STA on placement parasitics (minutes)
bin/runners/pnr_runner.py -b bfly8 --clock clk --period 1.25
# a filelist instead of a block; two periods; name a few nets
bin/runners/pnr_runner.py -f ni.f --top ni_top --clock clk --period 1.0,0.8 --nets u_x.req_q[3]
# A against B: one delta table per period
bin/runners/pnr_runner.py -f a.f --top a_mod --vs b.f:b_mod --clock clk --period 1.0
# through CTS and detailed route, for routed wire lengths
bin/runners/pnr_runner.py -b bfly8 --mode full
```

Each run lands in `pnr/<design>/<top>/<pdk>_<mode>_p<period>/`: `metrics.json` (the
numbers above), `report.txt`, `constraint.sdc` and `config.mk` as given to ORFS,
`make.log`, `report/paths.txt` (the worst path in full), and `orfs/` with ORFS's own
`logs`, `reports` and `results` (the ODB files, which `openroad -gui` opens). A `--vs`
run also writes the table under `pnr/compare/`.

The RTL is read with yosys's **slang** frontend (`SYNTH_HDL_FRONTEND=slang`, with
`SYNTHESIS` defined), since the generated SystemVerilog-2017 is beyond the plain
reader. Seeds are fixed (`--seed`, default 42) and `SYNTH_REPEATABLE_BUILD` is on, so
two runs of one design print the same numbers. A design that crosses clock domains
must pass its own SDC with `--sdc`; it is sourced after the runner's clocks, with its
Vivado spellings translated for OpenSTA (see the docstring of `write_sdc`), and any
constraint that matched nothing in the netlist is listed in `metrics.json` under
`sdc_unmatched` and warned about, since a bound on a missing cell constrains nothing.
One caveat the runner does not hide: that SDC also declares the clocks asynchronous
(`set_clock_groups -asynchronous`), and in OpenSTA, as in PrimeTime, clock groups take
precedence over `set_max_delay`, so the bounded crossing paths are not timed at all;
the bounds only act when the groups are removed, and then every crossing the bounds
miss is timed clock-to-clock. Which of the two the measurement wants is the SDC
author's call. Net names for `--nets` are the flat netlist's (`u_rd.rmask_q[15]`, as
in `orfs/results/.../1_2_yosys.v` without the leading backslash). `--pdk asap7` runs
the same flow on ASAP7 (its times are in ps; the runner converts); checked on one NI.

The toolchain is the ORFS checkout at `/src/tools/OpenROAD-flow-scripts` (or
`$ORFS_HOME`, or `--orfs`): see [docs/install-openroad.md](docs/install-openroad.md).

## Use in a project

```bash
git submodule add git@github.com:idanzaguri/vlsi-runners.git bin/runners
# fresh clones: git clone --recurse-submodules ...  (or: git submodule update --init)
```

## Glossary

[docs/glossary.md](docs/glossary.md) defines every term the runners print or take as
an option (coverage types, the flow stages, WNS, HPWL, SDC exceptions ...), written
for a verification engineer who has not done physical design. **It is kept in step
with the runners: a change that introduces a term, a metric, an option or a tool
name adds it there in the same commit.**

## Notes
- Installing yosys (needed by `synth_runner`/`metric_runner`): see
  [docs/install-yosys.md](docs/install-yosys.md). Build from source (the apt package
  lags); source tree is expected at `/src/tools/yosys`.
- `setenv.sh` (project's `bin/setenv.sh`) is sourced automatically so `vlog`/`vsim`
  inherit the project env.
- Updating the runner: edit + push here, then in each project `git -C bin/runners pull`
  and commit the new submodule pointer.
