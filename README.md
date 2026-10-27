# vlsi-runners

Shared VLSI flow runners, used as a git submodule (usually at `bin/runners/`) across
projects. Each runner anchors on the project's git root and reads project config from
there, so the same code works in every project. Project-specific config lives in the
**project** (`bin/setenv.sh`, `verif/<block>/lib/tests.yaml`, design attributes); only
the runner code lives here.

## Runners

| script | what it does | example |
|---|---|---|
| `test_runner.py` | compile + run one UVM test (Questa, VCS or Verilator) | `python3 bin/runners/test_runner.py -b <block> -t <test> [--uvm_test <name>] [--sim_args +X]` |
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
| `regr_runner ... --codecov` | per block, from the passing runs only: `merged.ucdb`, `coverage.txt` (also appended to `summary.txt`) and `coverage_html/index.html` under `sim/regression/<name>/<block>/` |

To browse a database in the GUI: `vsim -viewcov <file>.ucdb`. To exclude something
from the figures, `vcover` and the GUI's exclusion commands apply to the merged file.

`metric_runner` reads the same `design/<block>/lib/config.yaml` as `synth_runner`. It
maps the RTL with yosys and reports **relative** proxies (there is no real PDK here, so
absolute values are meaningless, only the diff between two designs matters): chip area +
cell count (`stat`) and critical-path logic depth (`ltp`). `--vs <block>[:<top>]`
synthesizes a second design and prints a delta table; each run also drops a
`metrics.json` under `metrics/<block>/<top>/`.

### A simulator that hangs (`regr_runner`, Questa)

A Questa run sometimes stops answering: the test reaches `$finish` with a clean UVM
report and `vsim` never exits, or the simulation stalls midway and the same seed runs
green alone. `regr_runner` reads each test's output while it runs and handles both.

| what happened | what the runner does |
|---|---|
| the simulator still runs `--finish_grace` seconds (default 60) after `$finish` | kills it and judges the test from its output up to `$finish` |
| `--timeout` fires after `$finish` | the same: the verdict comes from the output |
| the license process fails after `$finish` (vsim exits 43) | the same |
| `--timeout` fires before `$finish`, or the license process fails before it | the run is lost, not failed: it runs again with the same seed (`--reruns`, default 1), and a second loss is FAILED |

A verdict read from the output follows test_runner's own rules for a simulation log:
the UVM report summary must be there and read `UVM_ERROR : 0` and `UVM_FATAL : 0`, a
simulator error or Fatal fails it, and a UVM warning (or a `UVM_WARNING` count above 0)
or an unwaived simulator warning makes it PASSED_WARN, which `--pedant` fails. A test
that declares `expect_fail` passes only when the output failed and carries its text.
`summary.txt` lists both kinds: `Reruns:` (seed, why, the lost run's log
`test.try1.log`, the rerun's result) and `Judged from the output:`.

Every kill takes the test's whole session, and the killed test's log ends with what
each of its processes was doing: state, CPU seconds, and the kernel function it waited
in (WCHAN). A stalled simulator sleeps with its CPU time flat; a busy one is running.
Why the session: `vsim`'s front end (`vish`) runs the kernel (`vsimk`) and a license
process (`vlm`, which runs `salt_mgls_asynch`), and `vlm` puts itself in a process group
of its own. The process group kill used before missed it, and `vlm` printed
`** Fatal: Read failure in vlm process (0,0)` as it noticed its `vsim` gone. That line
in an older log marks a test the runner killed; it is not what made the test hang.

### Verilator (`--simulator verilator`)

`test_runner` and `regr_runner` run UVM 1.2 tests on Verilator. Verilator builds the
design and the bench into one program. A build takes minutes, most of them for the UVM
library. A test is one run of that program. `regr_runner` builds each design once and
runs every test with that build, as it does with a Questa snapshot.

```bash
python3 bin/runners/test_runner.py -b <block> -t <test> --simulator verilator
python3 bin/runners/regr_runner.py --tests <regr.yaml> --name <run> --simulator verilator
```

| setting | what it is | default |
|---|---|---|
| `PATH` (environment) | where `verilator` is found. A project puts another build first on PATH, for example in `bin/setenv.local.sh`. Do not set `VERILATOR_BIN`: Verilator's own wrapper reads it as the name of its `verilator_bin`. | on lab-lnx, put `/opt/verilator/current/bin` first (see below) |
| `UVM_HOME` (environment) | the UVM 1.2 source tree: the directory that holds `src/` | `<vsim dir>/../verilog_src/uvm-1.2`, when Questa's `vsim` is on PATH |
| `VERILATOR_SOLVER` (environment) | the SMT solver command that the built program runs for `randomize()` with constraints | `z3` on PATH. lab-lnx has no z3; ovip-dev uses `/opt/yosys/v0.67/bin/yices-smt2 --incremental`. |
| `verilator_config` (config.yaml) | Verilator configuration files (`.vlt`), relative to the block, as `files` are | none |

The build is
`verilator --binary --timing --vpi -j <CPUs> -Wno-fatal -Wno-lint -Wno-style +incdir+$UVM_HOME/src <verilator_config files> $UVM_HOME/src/uvm_pkg.sv -f comp_filelist.f <uvm_dpi.cc> --top-module <top> --Mdir obj_dir`:

- `comp_filelist.f` is the same file list that Questa gets. For something that only one
  simulator should get, use the jinja variable `TOOL` in config.yaml. It is `vsim`,
  `vcs` or `verilator`.
- The UVM DPI code is a copy of `$UVM_HOME/src/dpi/` with the Verilator HDL backend of
  [verilator/uvm12_dpi/](verilator/uvm12_dpi/README.md) over it. With it,
  `uvm_hdl_force` and `uvm_hdl_read` work through VPI. A signal that a test forces needs
  `forceable` and `public_flat_rw` in a `.vlt` file.
- `-j` is `--build_jobs`, else the number of CPUs that the process may use: all of them
  on the local machine, the job's under Slurm.
- In a regression, Verilator builds go `--build_parallel` at a time (default 1), each with
  `--build_cpus` CPUs (default: the machine's CPUs divided by `--build_parallel`). Under
  `--launcher srun` a build job asks Slurm for `--build_cpus` CPUs; a test job asks for one.
  One build with all 12 CPUs of lab-lnx took about 8.4 GB of RAM on a logion_tb bench, so
  more builds at once can run out of memory (logion_tb issue 24).
- A regression's builds are pruned when they succeed (`--prune_build`): the precompiled
  headers (`*.gch`), objects and libraries go, the program stays. A logion_tb build folder
  goes from 677 to 799 MB to about 25 MB. A `test_runner` run keeps its shared build whole,
  so the next build of it only relinks.
- In `comp.log`, `%Error`, `make: ***` and compiler `error:` lines fail the build.
  `%Warning` lines are warnings, except the warnings inside `$UVM_HOME`.

The run is `obj_dir/V<top> +verilator+seed+<seed+1> +UVM_TESTNAME=<test> ... <sim_args>`.
It runs line-buffered (`stdbuf -oL`), so `regr_runner` sees the `$finish` line while the
program still runs. Its output goes to `run.log`.

- Verilator wants a seed of 1 or more, so `--seed N` runs Verilator seed N+1. The first
  line of `run.log` says so. It is not the random stream of Questa's `-sv_seed N`.
- `run.log` follows the Questa rules (the UVM report summary, `UVM_ERROR`, `UVM_FATAL`),
  and also `%Error` (fail) and `%Warning` (warning).
- Waves (`--dump`, `--dump-mem`): the build adds tracing and a module, bound into the
  top, that calls `$dumpvars(0, <--dut>)` when the run has `+vlsi_runners_waves`. The run
  writes `waves.vcd` in the run directory. It writes `waves.fst` instead when `lz4.h` is
  installed (package `liblz4-dev`), because Verilator's FST writer needs it. Buses are
  dumped up to 65536 bits wide. `--dump` leaves out arrays of more than 32 entries, as
  Questa's `--dump` leaves out memories; `--dump-mem` dumps them up to 65536 entries.
- Code coverage (`--codecov`): the build adds `--coverage` (line, branch, expression,
  toggle and FSM) and counts only the design's files, as on Questa: Verilator's
  preprocessor lists every file of the build, and a generated `.vlt` file turns coverage
  off in each one that is not a `design` block's (and always in UVM). Without a `design`
  block, everything but UVM counts. The run writes `coverage.dat` and the totals table
  `coverage.txt` in the run directory. `regr_runner` merges each block's passing runs with
  `verilator_coverage` into `merged.dat`, writes `coverage.txt` (also appended to
  `summary.txt`), and makes `coverage_html/index.html` with `genhtml` (package `lcov`).
  Do not rely on the `fsm_state` and `fsm_arc` rows yet. Verilator's FSM coverage is new:
  on one test, 5.052 and Verilator main of 2026-10-25 gave different figures, and neither
  recorded the state machine's real transitions.
- The coverage types are all of them (`--coverage`) unless the block's config.yaml names
  some under `verilator_coverage`: `line` (with branches), `toggle`, `expr`, `fsm`,
  `user`.
- Where the build goes: a `regr_runner` regression builds each design once in its own
  `_build/`. A `test_runner` run without `--lib` shares one build per design,
  compilation and kind (`default`, `coverage`, `waves`, `waves_mem`, or two of them) in
  `sim/<block>/_build/<comp>/<kind>/`, with a lock, so a second test of the design only
  relinks: Verilator skips the rest when no input changed. `--clean` rebuilds it.
- A build records what it can do in `vlsi_runners_build.json` beside it. A run with
  `--dump` or `--codecov` stops with an error when its build was made without it.
- Verilator is 2-state. A test that checks X or Z cannot pass.
- The Verilator for the runners on lab-lnx is `/opt/verilator/current`: Verilator main
  of 2026-10-25 (`verilator --version`: rev v5.052-597-g8022612fe), built from
  `/src/tools/verilator`, installed in `/opt/verilator/v5.052-597-g8022612fe`. The
  release on PATH, `/usr/local/bin/verilator` (5.052), fails on parameterized classes
  in ovip_axi. A project puts `/opt/verilator/current/bin` first on PATH in its
  `bin/setenv.local.sh`. On it, ovip_apb_testbench passes 7/7 and ovip_axi_testbench
  51/52 (ovip_axi_xz_test checks X).

### cocotb benches (`--cocotb_module`, Verilator)

A cocotb bench has its tests in Python. `--cocotb_module <module>` (with
`--simulator verilator`) makes test_runner build the design with cocotb's own runner
(`cocotb_tools.runner`, through `verilator/cocotb_run.py`) and run one test of the module
against it. cocotb comes from the project: `verilator/cocotb_run.py` runs with the first
`python3` on PATH after `bin/setenv.sh`, so a project puts its `.venv/bin` first there.

```yaml
# verif/<d>/lib/tests.yaml
compilations:
  - name: default
    files:
    args: ["--top <d>_top", "--cocotb_module test_<d>"]
tests:
  - { name: smoke, comp: default }                                      # cocotb test smoke
  - { name: traffic_e60, comp: default, args: ["--cocotb_test traffic", "--sim_args +error_pct=60"] }
```

- The test module is found in `verif/<block>/`, which goes first on PYTHONPATH.
- The build takes the file list of the Questa and UVM Verilator paths: the design blocks'
  files and the bench's (checkers, `.cpp` sources), `comp_args`, `verilator_config`. The
  bench's own Verilator switches (`--assert`, `--x-initial unique`, `-Wno-fatal`,
  `--timescale 1ns/1ps`) go in its config.yaml `comp_args`; the runner adds none.
- The run: cocotb seed `--seed`, Verilator seed `--seed` + 1, the plusargs of config.yaml,
  tests.yaml and the command line. The most specific wins: `$value$plusargs` reads the first
  match, so they come command line first; `cocotb.plusargs` keeps the last, so a cocotb run
  gets them reversed. The run writes `run.log` and `results.xml` in the run directory.
- The verdict: a failed test in `results.xml`, no test run, a `%Error` or `ERROR` line in
  the log (an assertion prints one while `results.xml` may still say pass), or a runner that
  ended badly fails the test. A `%Warning` or `WARNING` line, in the run or in its build,
  makes it pass with warnings, which `--pedant` fails.
- `--codecov` and `--dump` work as for UVM benches. The waves are cocotb's: `dump.vcd`.

### Regression files (`regr_runner --tests`)

A regression file is one list of entries (`tests:`), all of which run:

```yaml
tests:
  - { block: apb_cdc, tests: [smoke, traffic, "reset*"] }   # a set: several tests of one block
  - { block: axi_x, test: smoke, weight: 5 }                # one test
```

or several named regressions (`regressions:`), one of which runs:

```yaml
held:                       # blocks kept out of every `blocks: all` (block: why)
  axi_x: "finding VNOC-9 is open"
keep: [cov.txt]             # more files a passing run keeps
hooks:                      # commands after the runs (and the coverage merge)
  - { per: block, run: "python3 bin/cov_merge.py {block} {passed} {block_dir}" }
  - { per: regression, when: codecov, run: "python3 bin/rtl_holes.py {regression_dir}" }
regressions:
  check:
    - { blocks: all, tests: [smoke, rw, "traffic*"], seeds: [0] }
  errors:
    - { blocks: all, tests: [traffic], seeds: "11-14", sim_args: [+rounds=40, +error_pct=60] }
  nightly:
    - { include: check }
    - { include: errors }
    - { block: apb_cdc, test: smoke, weight: 5 }   # one test
```

- `--regression <name>` picks one; the run folder is `sim/regression/<name>/` unless
  `--name` says otherwise. `--list` prints the runs and exits.
- An entry is one test or a set. One test has `block` and `test`. A set has `blocks`
  (`all`, or a list) or `block` (one), and `tests` (names or globs). One test may add
  `seed`; a set may add `seeds` (a seed, a list, or a range `"a-b"`). Both may add
  `weight` and `sim_args` (plusargs on top of the test's own). A `tests:` list takes the
  same entries as a named regression, except `include`. A key that the entry does not
  take is an error.
- Without a seed, a test runs once with a random seed; with `weight`, `weight` times
  `--repeat` times, each with its own random seed.
- `blocks: all` skips a block that lacks a test; a block named in the entry must have it.
  A block, test and seed that comes twice runs once; with two different `sim_args` it is
  an error, since both would share one run folder.
- A hook runs in the work area's root after `bin/setenv.sh`, with `{block}`,
  `{block_dir}`, `{regression_dir}`, `{wa_root}` and `{passed}` (a file listing the
  passing runs' folders) replaced. `per: block` runs once for each block with a passing
  run. Its output goes to `hooks.log`, its last line into `summary.txt`.
- The exit code is 1 when a test failed (with `--pedant`, also one that passed with
  warnings) or a hook failed, and 0 otherwise.

### Tests on other machines (`regr_runner --launcher srun`, Slurm)

With `--launcher srun`, `regr_runner` starts every design build and every test through
Slurm's `srun`. Slurm runs each job on a machine with a free slot and keeps the others
in its queue. All the sessions and runners that use Slurm share one limit. The setup
(lab-lnx first, then more machines) is in [docs/slurm.md](docs/slurm.md).

```bash
python3 bin/runners/regr_runner.py --tests <regr.yaml> --name <run> \
    --launcher srun --srun_args="--constraint=questa" --max_parallel 40
```

| option | what it does |
|---|---|
| `--launcher local\|srun` | `local` (the default) runs everything on this machine. `srun` runs it through Slurm. |
| `--srun_args "<options>"` | extra `srun` options for every build and test. `--constraint=questa` keeps the tests on machines with a Questa license. Never `--job-name`: the runner names each job. |

What changes with `srun`:

- `--max_parallel` is how many tests the runner hands to Slurm at once.
- `--timeout` counts from the moment a test starts on its machine, not from the moment
  it entered the queue. The summary's times are run times too.
- The first line of `test.log` says where the test ran:
  `[regr_runner] started: host lab-lnx, Slurm job 29, session 3344136`.
- A kill works as on the local machine. A second `srun` step inside the test's job
  (`--overlap`) writes what each process was doing into `test.log` and kills the test's
  whole session on its machine. Then `scancel` ends the job.
- A test that Slurm ended before `$finish` (its machine failed, or someone cancelled
  it) is a lost run and runs again with the same seed (`--reruns`). After `$finish`,
  the verdict comes from the output.
- Ctrl-C cancels all the runner's jobs, also those still in the queue.

Every machine needs the same Ubuntu release, the same user and UID, the same tools at
the same paths, and the work area at the same path (NFS). A machine that runs Questa
tests needs its own Questa license.

### Place-and-time numbers (`pnr_runner`)

`metric_runner` stops at logic depth on a toy library, which says nothing about
wire delay or about what a real library makes of a carry chain. `pnr_runner` takes
the same design through OpenROAD-flow-scripts (ORFS) on **Nangate45** (a public 45 nm
library: absolute numbers are meaningless, only the diff between two forms of one
module matters) and reports what placement knows:

| group | what |
|---|---|
| timing | WNS and TNS, over everything and between registers; the minimum period (so Fmax), per clock |
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
# two filelists with one name (d4/ni.f, d8/ni.f): name the runs
bin/runners/pnr_runner.py -f d4/ni.f --top ni --name ni_d4 --vs d8/ni.f:ni --vs-name ni_d8
# a FIFO above 4096 bits: raise ORFS's bound (any ORFS variable goes this way)
bin/runners/pnr_runner.py -f ni.f --top ni --var SYNTH_MEMORY_MAX_BITS=65536
# through CTS and detailed route, for routed wire lengths
bin/runners/pnr_runner.py -b bfly8 --mode full
```

Each run lands in `pnr/<name>/<top>/<pdk>_<mode>_p<period>/`: `metrics.json` (the
numbers above), `report.txt`, `constraint.sdc` and `config.mk` as given to ORFS,
`make.log`, `report/paths.txt` (the worst path in full), and `orfs/` with ORFS's own
`logs`, `reports` and `results` (the ODB files, which `openroad -gui` opens). A `--vs`
run also writes the table under `pnr/compare/`.

The name is the block, the filelist's stem, or `--name` (`--vs-name` for the second
design). Two filelists both called `ni.f` that hold different RTL would land in one
folder, so before writing anything the runner compares the folder's `config.mk` and
`constraint.sdc` with what this run would write: the same setup again is an incremental
rerun (make skips the stages that are done), another setup on top of a measured run
is refused with the differing variables named (give it a `--name`, or `--clean` to
replace), and another setup on top of a run that never reached `metrics.json` replaces
it.

`--var NAME=VALUE` writes any ORFS variable into `config.mk` after the runner's own, so
it wins and the folder records it (`flow/scripts/variables.yaml` in ORFS lists them
all). The one that comes up: ORFS stops synthesis at an inferred memory above
`SYNTH_MEMORY_MAX_BITS` (4096 bits), its guard against a RAM that should be a macro
turning into flops. A NoC FIFO is meant to be flops, so raise the bound; the runner
spots that failure in the log and prints the size and the `--var` to use.

The RTL is read with yosys's **slang** frontend (`SYNTH_HDL_FRONTEND=slang`, with
`SYNTHESIS` defined), since the generated SystemVerilog-2017 is beyond the plain
reader. Seeds are fixed (`--seed`, default 42) and `SYNTH_REPEATABLE_BUILD` is on, so
two runs of one design print the same numbers.

The port paths sit under the runner's IO budget, one virtual clock at the primary
period with 20% of it on each side, which times a port of another clock domain against
the wrong clock, so they are noise: the numbers to compare are the register-to-register
ones (`reg2reg_wns_ns`, `reg2reg_tns_ns`, the critical path) and the minimum period,
which leaves port paths out. A `set_input_delay` or `set_output_delay` in the design's
own SDC replaces the runner's on that port, for a port that deserves a real constraint.

A design that crosses clock domains
must pass its own SDC with `--sdc`; it is sourced after the runner's clocks, with its
Vivado spellings translated for OpenSTA (see the docstring of `sdc_text`), and any
pattern that matched nothing in the netlist is listed in `metrics.json` under
`sdc_unmatched`, with the number of constraints dropped for it under `sdc_dropped`, and
warned about, since a bound on a missing pin constrains nothing. (OpenSTA says so once,
at the first stage that reads the SDC, which in ORFS is synthesis; that stage writes the
SDC back without the dropped constraints, so no later log mentions them.) Reset ports
(`--reset-ports`, default `*rst*,*reset*`) get no IO budget and are false paths: the
contract is a common reset window with the clocks stopped, and a budgeted reset fails
its recovery check against every other clock and takes over WNS.

The hierarchy the RTL asks to keep (`keep_hierarchy` on the crossing and synchronizer
modules) survives yosys and, since the runner links with `OPENROAD_HIERARCHICAL`,
OpenROAD too, so the SDC can bound through a kept module's pins
(`get_pins {u_x_cdc/u_wsync/d[*]}`) exactly as it does under Design Compiler or Vivado;
a design yosys flattens fully prints the same numbers either way (checked on the
butterfly). One thing to know: ORFS hands the SDC from stage to stage as OpenSTA writes
it back, and that copy has such pins expanded to the leaf pins of the moment, which the
next stage's resizer renames, so a bound handed down the flow loses bits on the way.
The runner therefore measures on the SDC as given (`pnr_runner.sdc` in the results),
never on the last stage's copy; the optimization steps inside ORFS still see the copies.
A caveat the runner does not hide: an SDC that declares the clocks asynchronous
(`set_clock_groups -asynchronous`) silences its own `set_max_delay` bounds, in OpenSTA
as in PrimeTime, since clock groups take precedence; without the groups every crossing
the bounds miss is timed clock-to-clock, and clocks with no common period are warned
about. Which of the two the measurement wants is the SDC author's call. Net names for
`--nets` are the netlist's: `u_rd.rmask_q[15]` where yosys flattened (as in
`orfs/results/.../1_2_yosys.v` without the leading backslash), `u_x_cdc/wbin[3]` inside
a kept module. `--pdk asap7` runs the same flow on ASAP7 (its times are in ps; the
runner converts); checked on one NI.

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
