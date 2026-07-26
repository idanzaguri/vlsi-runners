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
