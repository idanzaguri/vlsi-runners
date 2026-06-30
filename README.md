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

`test_runner` selects the test from `verif/<block>/lib/tests.yaml`; a CLI `--uvm_test`
overrides the one baked into the entry (handy for many flavors of one test class).
`regr_runner --tests` takes a YAML list of `{name, block}` entries.

## Use in a project

```bash
git submodule add git@github.com:idanzaguri/vlsi-runners.git bin/runners
# fresh clones: git clone --recurse-submodules ...  (or: git submodule update --init)
```

## Notes
- `setenv.sh` (project's `bin/setenv.sh`) is sourced automatically so `vlog`/`vsim`
  inherit the project env.
- Updating the runner: edit + push here, then in each project `git -C bin/runners pull`
  and commit the new submodule pointer.
