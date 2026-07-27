# Installing OpenROAD (place & route, via OpenROAD-flow-scripts)

For the backend study we need real **place-and-route** numbers — wire length,
achieved Fmax, area, and PnR runtime — not just synthesis estimates. The
open-source ASIC P&R flow is **OpenROAD**, driven through **OpenROAD-flow-scripts
(ORFS)**: a make-based RTL-to-GDS wrapper that bundles OpenROAD + yosys + the
open **Nangate45** cell library (a fake-but-consistent 45 nm library — absolute
numbers are meaningless, only the diff between two designs matters, which is
exactly what we want).

> FPGA P&R (nextpnr) is deliberately NOT used here: abutment is a hard-macro
> concept with no FPGA analog.

This is a from-source build (no Docker on this box). Budget ~30-60 min of build
time; it is RAM-heavy (needs ~a few GB per core at peak).

## Conventions

- **Source tree at `/src/tools/OpenROAD-flow-scripts`** (matches the yosys layout
  at `/src/tools/yosys`). Keep it — it is both the toolchain and the flow driver.
- ORFS builds its own pinned yosys/OpenROAD under its tree; that is separate from
  the standalone `/opt/yosys` used by `metric_runner`. The two coexist.

## Prerequisites

Build tooling: git, cmake (>= 3.16), a C++ compiler. The heavy dependencies
(boost, swig, tcl, spdlog, or-tools, eigen, lemon, KLayout deps, ...) are
installed by ORFS's own `setup.sh`.

## Install

```bash
# 1. one-time dir (owned by you so no sudo for the clone/build)
sudo mkdir -p /src/tools && sudo chown "$USER" /src/tools

# 2. clone WITH submodules (pulls OpenROAD + yosys; large)
git clone --recursive https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git \
    /src/tools/OpenROAD-flow-scripts
cd /src/tools/OpenROAD-flow-scripts

# 3. system + common dependencies  (THE ONE SUDO STEP — run it in a real
#    terminal; sudo needs a TTY for your password)
sudo ./setup.sh

# 4. build OpenROAD + tools locally (no sudo). Cap threads if RAM is tight.
./build_openroad.sh --local --threads "$(nproc)"
```

### Ubuntu 26.04 note (KLayout + running the stages by hand)

ORFS's `setup.sh` only knows Ubuntu 20.04/22.04/24.04, so on 26.04 it errors at
the **KLayout** step ("Unsupported Ubuntu version 26.04") and exits *before* the
`-common` stage. The apt base packages installed before the error do land, so on
26.04 run the two installer stages by hand:
```bash
sudo ./etc/DependencyInstaller.sh -base          # apt base packages (re-run is a no-op)
./etc/DependencyInstaller.sh -common -prefix="$PWD/dependencies"   # local libs, no sudo
```

**KLayout on 26.04 needs a source build (the prebuilt .deb does NOT work).** The
24.04 `klayout_0.30.7` .deb hard-depends on `libpython3.12` and `libruby3.2`,
which 26.04 does not provide (it ships python 3.14 / newer ruby), so
`dpkg -i` + `apt -f install` cannot satisfy it. And ORFS only has an x86_64 deb
path, no source fallback. Two choices:

1. **Defer it (recommended to start).** KLayout is only used at the flow's final
   GDS stream-out + KLayout DRC. Building OpenROAD and getting the study's
   numbers (area, WNS/Fmax, wire length — all from synth→place→route→STA) do
   **not** need it. Run the flow to the route/report stage and skip GDS for now.
2. **Build KLayout 0.30.7 from source** (Qt5 5.15 is present on 26.04):
   ```bash
   sudo apt install -y qtbase5-dev qttools5-dev qttools5-dev-tools \
        libqt5svg5-dev libqt5xmlpatterns5-dev ruby-dev python3-dev \
        zlib1g-dev libgit2-dev
   git clone --depth=1 -b v0.30.7 https://github.com/KLayout/klayout.git
   cd klayout && ./build.sh -j"$(nproc)"          # builds into ./bin-release
   sudo cp -r bin-release/* /usr/local/bin/        # or add bin-release to PATH
   klayout -v                                      # -> 0.30.7
   ```

Clean up the failed .deb attempt (leaves a harmless `rc` dpkg record) with
`sudo dpkg --purge klayout` if you like.

If a compile gets OOM-killed, rerun with fewer threads, e.g.
`./build_openroad.sh --local --threads 6` (the build resumes).

## Put the tools on PATH

ORFS ships an env file that adds `openroad`, `yosys`, `sta`, `klayout`, etc.:

```bash
source /src/tools/OpenROAD-flow-scripts/env.sh
openroad -version          # sanity check
```
Add that `source` line to `~/.bashrc` if you want it always available. NOTE it
puts ORFS's yosys ahead of `/opt/yosys/current` on PATH; that is fine (they are
independent), just be aware which yosys a shell is using.

## Smoke test

The flow lives in `flow/`. Run the built-in tiny design end to end:

```bash
cd /src/tools/OpenROAD-flow-scripts/flow
make DESIGN_CONFIG=./designs/nangate45/gcd/config.mk
```
Success = it runs synth → floorplan → place → CTS → route → finish and writes
results under `results/`, `reports/`, and `logs/nangate45/gcd/base/`.

## The numbers that matter (where PnR reports them)

After a run, for design `<d>` on `nangate45`, look under
`flow/{logs,reports,results}/nangate45/<d>/base/`:

| what | where |
|---|---|
| **Design area / utilization** | end of `logs/.../6_report.log` (and the final summary make prints) |
| **Worst slack (WNS) / TNS / achieved period → Fmax** | `reports/.../*finish*` and `logs/.../6_report.log` STA section |
| **Total / max wire length (routed)** | `logs/.../5_route.log` and the detailed-route report |
| **Machine-readable rollup** | `metrics.json` / `metadata-base-ok.json` in the design's dir (area, wirelength, worst slack, power) |

`Fmax` is derived: if you constrain the clock at period `T` (in the design's
`.sdc`) and the worst slack is `WNS`, the design closes at `T - WNS`, i.e.
`Fmax ≈ 1 / (T - WNS)`. For a relative flat-vs-tiled comparison, run both at the
same target `T` and compare WNS (and the routed max wire length).

### Without KLayout (deferred on 26.04): the flow errors at the last step

Verified on the built toolchain with `make DESIGN_CONFIG=.../gcd/config.mk`: the
whole flow runs (synth → floorplan → place → CTS → global+detailed route →
fill → report) and only the final **`check-klayout`** GDS target fails
(`Error: KLayout not found`). All the study's numbers are already written by then
(example gcd run: area 683 um^2 @ 63% util, routed wire length ~18173 um, timing
slack positive/met). Two clean ways to avoid the non-zero exit:

- Run only through routing/reporting, e.g. `make DESIGN_CONFIG=... route`
  (stops before GDS), or
- point ORFS at a KLayout if you build one: `export KLAYOUT_CMD=/path/to/klayout`.

Note: because the run stops at the GDS step, the consolidated
`metadata-base-ok.json` is NOT written; read the numbers from
`logs/nangate45/<design>/base/*.log` (as above) until KLayout is installed or the
final target is set to the report stage.

## Running YOUR design

Add a design dir under `flow/designs/nangate45/<name>/` with:

- `config.mk` — points at the RTL and sets the top + clock:
  ```makefile
  export DESIGN_NAME     = <top_module>
  export PLATFORM        = nangate45
  export VERILOG_FILES   = $(sort $(wildcard <path>/*.v <path>/*.sv))
  export SDC_FILE        = $(DESIGN_HOME)/$(PLATFORM)/<name>/constraint.sdc
  export DIE_AREA        = 0 0 <w> <h>     # or use CORE_UTILIZATION instead
  export CORE_AREA       = ...
  ```
- `constraint.sdc` — at least a clock:
  ```tcl
  create_clock -name clk -period <T_ns> [get_ports clk]
  ```

Then:
```bash
cd /src/tools/OpenROAD-flow-scripts/flow
make DESIGN_CONFIG=./designs/nangate45/<name>/config.mk
```

> RTL must be **synthesis-clean** first. The generated NoC fabric currently is
> not (a latch in the 2×2 switch arbiter); see the backend-study handoff doc in
> noc_proj (`docs/backend-metrics-handoff.md`). Fix that before P&R.

## How this maps to the abutted-tile study

- Harden **one cluster tile** → its area, achieved Fmax, and routed wire length.
- Run the **flat butterfly** at a few sizes → watch wire length grow and Fmax
  drop as N increases.
- The idea is proven if the tile's numbers stay flat while the flat butterfly's
  degrade with N. Same "diff, not absolute" philosophy as `metric_runner`, one
  level down (placement instead of synthesis).

Planned follow-up: an OpenROAD backend for `metric_runner` that parses these
reports (WNS / wirelength / area) into the same two-design delta table.

## Updating

```bash
cd /src/tools/OpenROAD-flow-scripts
git pull
git submodule update --init --recursive
./build_openroad.sh --local --threads "$(nproc)"   # incremental
```

## Troubleshooting

- **OOM during build** → fewer `--threads` (6, then resume).
- **`openroad: command not found`** → you did not `source env.sh` in that shell.
- **Which yosys?** → `command -v yosys`; ORFS's env prepends its own. For the
  standalone metric flow, use `/opt/yosys/current/bin/yosys` explicitly.
- Exact report/target names shift between ORFS versions; `make help` in `flow/`
  lists targets, and the `logs/.../` files are the ground truth.
