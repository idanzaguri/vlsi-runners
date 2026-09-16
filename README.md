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

`metric_runner` reads the same `design/<block>/lib/config.yaml` as `synth_runner`. It
maps the RTL with yosys and reports **relative** proxies (there is no real PDK here, so
absolute values are meaningless, only the diff between two designs matters): chip area +
cell count (`stat`) and critical-path logic depth (`ltp`). `--vs <block>[:<top>]`
synthesizes a second design and prints a delta table; each run also drops a
`metrics.json` under `metrics/<block>/<top>/`.

## Use in a project

```bash
git submodule add git@github.com:idanzaguri/vlsi-runners.git bin/runners
# fresh clones: git clone --recurse-submodules ...  (or: git submodule update --init)
```

## Notes
- Installing yosys (needed by `synth_runner`/`metric_runner`): see
  [docs/install-yosys.md](docs/install-yosys.md). Build from source (the apt package
  lags); source tree is expected at `/src/tools/yosys`.
- `setenv.sh` (project's `bin/setenv.sh`) is sourced automatically so `vlog`/`vsim`
  inherit the project env.
- Updating the runner: edit + push here, then in each project `git -C bin/runners pull`
  and commit the new submodule pointer.
