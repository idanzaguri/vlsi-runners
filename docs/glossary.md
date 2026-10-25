# Glossary: the terms behind the coverage and place-and-time runners

Written for a verification engineer who has not done physical design.
Absolute numbers from the free libraries used here mean nothing; every
runner reports them so two forms of one design can be compared.

## Simulation and coverage (Questa)

- **Code coverage**: the simulator counts which parts of the RTL executed.
  It needs no testbench code, only a switch, and it tells you what the
  tests never touched. Types:
  - **Statement**: each line/statement, executed or not.
  - **Branch**: each arm of an `if`/`case`, taken or not.
  - **Condition**: inside a boolean expression, each term seen both true
    and false (short-circuiting counted).
  - **Expression**: like condition, for the right-hand side of assignments.
  - **FSM**: for a state machine the tool recognizes, which states were
    visited and which transitions happened.
  - **Toggle**: each signal bit seen going 0 to 1 and 1 to 0.
- **Functional coverage**: coverage you define yourself in SystemVerilog
  (`covergroup`, `coverpoint`, bins). It needs the `svverification` license
  feature, which the Starter Edition lacks, together with `randomize()`.
- **Assertion coverage**: how many times each assertion passed or failed.
- **UCDB**: Unified Coverage Database, Questa's binary file holding one
  run's coverage (`coverage save`). Merged across runs with `vcover merge`.
- **vcover**: Questa's coverage utility: merge databases, print text or
  HTML reports.
- **viewcov**: `vsim -viewcov file.ucdb`, the GUI mode that opens a
  database without a simulation.
- **Design unit (DU)**: a compiled module, interface or package in the
  work library. `vdir -l` lists them with their source file.
- **Instrumenting**: compiling a unit with coverage counters in it.
  `-cover` instruments everything; `+cover=<types>+<unit>` only that unit.
- **Snapshot / optimized design**: the `vopt` output that `vsim` runs
  without optimizing again (`<top>_opt`).

## Regressions and simulator hangs (Questa)

- **$finish**: the SystemVerilog call that ends a simulation. UVM makes it
  after the report summary; Questa prints `** Note: $finish` when it runs.
- **PASSED_WARN**: a test that passed with a UVM or simulator warning
  (test_runner's exit code 2). `--pedant` counts it as a failure.
- **Timeout** (`--timeout`): the wall-clock seconds a test may take before
  the runner kills it.
- **Grace after $finish** (`--finish_grace`): how long a simulator may
  keep running after `$finish` (coverage save, quit) before the runner
  kills it and judges the test from its output. Default 60 s; 0 waits for
  the timeout instead.
- **Judged from the output**: a verdict read from the test's own output up
  to `$finish`, by test_runner's log rules, when test_runner's exit code was
  lost to a kill or to a license failure after `$finish`.
- **Lost run**: a run whose simulator stopped before `$finish` for a reason
  that is not the test's: killed at the timeout, or its license process
  failed. It is not a failure until its rerun fails too.
- **Rerun** (`--reruns`): a lost run starts again with the same seed. Its
  log stays as `test.try1.log` beside the rerun's `test.log`. Default 1;
  0 never reruns.
- **vish, vsimk**: the two processes of a running `vsim`, the front end
  that reads commands and the simulation kernel.
- **vlm, salt_mgls_asynch**: Questa's license process, one per `vsim`, and
  the helper it runs for the periodic license check.
- **Read failure in vlm process**: what `vlm` prints when its `vsim`
  disappears. In a log from before the runner killed whole sessions, it
  marks a test the runner killed at its timeout.
- **Session, process group**: the runner starts each test in a session of
  its own and kills the whole session. `vlm` leaves the test's process group
  but not its session.
- **WCHAN**: the kernel function a sleeping process waits in, from
  `/proc/<pid>/wchan`. A killed test's log lists it for each process.

## Tests on other machines (Slurm)

- **Slurm**: a job scheduler. It keeps one queue for several machines and runs
  each job on a machine with a free slot. Setup: [slurm.md](slurm.md).
- **Launcher** (`--launcher`): how `regr_runner` starts each design build and
  test. `local` runs it on this machine; `srun` runs it through Slurm.
- **srun**: the Slurm command that runs one job and waits for it. It passes on
  the job's output and exit code. `regr_runner` starts one `srun` per build and
  per test.
- **--srun_args**: extra `srun` options that `regr_runner` adds to every build
  and test, for example `--constraint=questa`.
- **scancel**: the Slurm command that cancels a job, running or still in the
  queue.
- **squeue, sinfo, scontrol**: Slurm commands that list the jobs, list the
  machines, and show or change a job or a machine.
- **slurmctld, slurmd, slurmstepd**: Slurm's controller (one, on lab-lnx), its
  daemon on every machine, and the process that runs one job step on a
  machine.
- **Node**: a machine in Slurm.
- **Partition**: a named group of nodes that jobs go to. The setup has one,
  `main`.
- **Slot**: one CPU that Slurm can give to a job. A test takes one.
- **Feature, constraint**: a word that slurm.conf gives a node
  (`Features=questa`) and that a job asks for (`--constraint=questa`). The job
  runs only on nodes that have it.
- **Queue, pending**: a job that waits for a free slot is in the queue;
  `squeue` shows it as PENDING.
- **Job name**: `regr_runner` names each job `<test>.<seed>.r<pid>.<n>`, so it
  can cancel exactly its own jobs.
- **Start line**: the first line a test prints through `srun`:
  `[regr_runner] started: host <machine>, Slurm job <id>, session <id>`.
  `--timeout` counts from it.
- **Overlap step** (`srun --jobid=<id> --overlap`): a second command run inside
  a job that is already running. `regr_runner` uses it to list and kill a
  test's processes on the test's machine.
- **Drain**: a node state (`scontrol update nodename=<n> state=drain`). The node
  gets no new jobs, and the jobs on it finish.
- **munge**: the service that signs the messages between Slurm's daemons and
  commands. Every machine needs the same key, `/etc/munge/munge.key`.
- **cgroup**: a Linux group of processes. With `proctrack/cgroup`, Slurm finds
  all the processes of a job by their cgroup and can kill all of them.
- **CPUSpecList**: CPUs of a node that Slurm keeps out of its slots, for work
  outside Slurm.
- **NFS**: a network file system. Every machine mounts the work area at the
  same path, so a job sees the same files on any machine.
- **NIC ID**: the network card address that Intel ties a Questa Starter
  Edition license to. Each machine needs its own license.

## From RTL to a placed chip

- **RTL**: the SystemVerilog you write (registers and the logic between
  them).
- **Synthesis**: turning RTL into a **netlist** of standard cells (AND,
  NAND, flops ...) from a library. Done by **yosys** here.
- **slang**: a SystemVerilog frontend built into yosys 0.67 (`read_slang`).
  The generated RTL uses SV-2017 that the plain reader rejects.
- **Inferred memory**: an array in the RTL (`logic [W-1:0] mem [D]`) that
  yosys recognizes as a memory. This flow has no RAM macros, so it becomes
  one flop per bit, which is what a NoC FIFO is anyway.
- **SYNTH_MEMORY_MAX_BITS**: ORFS's bound on an inferred memory, 4096 bits
  by default, its guard against a RAM that should be a macro turning into
  flops. Synthesis stops above it; `--var SYNTH_MEMORY_MAX_BITS=<n>` raises
  it, `--var SYNTH_MOCK_LARGE_MEMORIES=1` shrinks the memory to one row
  instead (the rest of the design is then measured without it).
- **ORFS variable / config.mk**: the make variables that drive the flow
  (`flow/scripts/variables.yaml` in ORFS documents every one). The runner
  writes the ones it chooses into the run's `config.mk`; `--var NAME=VALUE`
  adds any other, last, so it wins.
- **Standard cell library**: the set of pre-designed gates of a process,
  with their timing and area. The `.lib` file (Liberty) carries timing,
  the `.lef` file carries the physical shape.
- **Toy library**: yosys's `cmos_cells.lib`, a handful of fake gates;
  what `metric_runner` maps to. Logic depth is the only honest number it
  gives.
- **Nangate45**: a free, realistic-looking 45 nm library (FreePDK45). Not
  a real foundry process, but consistent, so diffs between designs mean
  something.
- **ASAP7**: a free academic 7 nm-class library. Smaller and faster; its
  time unit is picoseconds.
- **PDK**: process design kit, the library plus technology rules for one
  process. The runner's `--pdk` picks nangate45 or asap7.
- **OpenROAD**: the open-source place-and-route tool. **OpenSTA** is its
  timing engine (also available alone as `sta`).
- **ORFS**: OpenROAD-flow-scripts, the make-based flow that drives yosys
  and OpenROAD stage by stage from RTL to a layout. Lives at
  `/src/tools/OpenROAD-flow-scripts`.
- **Flow stages**, in order:
  1. **Synthesis**: RTL to netlist.
  2. **Floorplan**: decide the die size and the core (the area cells may
     occupy); place the power grid and tap cells.
  3. **Placement**: put every cell at an (x, y). **Global placement**
     spreads cells roughly, **detailed placement** legalizes them onto
     rows. Timing-driven placement uses timing to keep critical cells
     close.
  4. **CTS**: clock tree synthesis, building the buffer tree that delivers
     the clock to every flop.
  5. **Routing**: connect the pins with metal wires. **Global route**
     decides corridors, **detailed route** draws the actual wires.
  6. **Finish**: fill cells, final timing and reports, optionally the GDS.
- **fast mode** (runner): stages 1 to 3, then timing with wire delay
  estimated from placement. **full mode**: through routing, timing on
  the wires as drawn.
- **Die area / core area**: the chip outline and the region inside it
  where cells go. **Core margin** is the gap between them.
- **Utilization**: cell area divided by core area. The runner targets 50%
  (`--util`); the reported value is what came out.
- **Aspect ratio**: core height over width (1 = square).
- **Placement density**: how tightly the placer packs cells; ORFS derives
  it from utilization.
- **Fill cells / tap cells**: cells with no logic, added to satisfy
  physical rules. Excluded from the runner's cell count.
- **ODB**: OpenROAD's database file (`3_place.odb`, `6_final.odb`); opens
  in `openroad -gui`.
- **GDS**: the final mask layout file. Needs **KLayout**, which this
  machine lacks, so the flow stops just before it.
- **DRC**: design rule check, geometric rules the layout must obey.
- **DBU**: database unit, the integer grid of the ODB (2000 per micron
  on Nangate45). The runner converts to microns.

## Timing

- **STA**: static timing analysis. Without simulating, compute how long
  every path takes and compare with what the clock allows.
- **Clock period**: the time between clock edges. 1.25 ns = 800 MHz.
  `--period` sets it; a sweep runs once per period.
- **Path**: logic from a launching point to a capturing point.
  **Startpoint** and **endpoint** name those two.
- **Register-to-register (reg-to-reg) path**: flop to flop inside the
  design. The number the open items are about.
- **Port path / IO path**: from an input port to a flop, or a flop to an
  output port. Depends on an assumed outside world, so it is noise when
  measuring a module alone.
- **IO budget / input and output delay**: the assumption about that
  outside world. The runner gives 20% of the period on each side through a
  **virtual clock** (a clock with no port, used only to time the ports).
- **Arrival time**: when the signal gets to the endpoint.
  **Required time**: when it had to be there (period minus setup time).
- **Setup time**: how early data must be stable before the clock edge.
  **Hold time**: how long after the edge it must stay stable.
- **Slack**: required minus arrival. Positive is good (met), negative is a
  violation.
- **WNS**: worst negative slack, the single worst path. 0 or positive
  means timing closes at that period.
- **TNS**: total negative slack, the sum over all violating endpoints. How
  much is broken, not only how badly.
- **Register-to-register WNS / TNS** (`reg2reg_wns_ns`, `reg2reg_tns_ns`):
  the same over the paths between registers only, which leaves out the
  port paths and their made-up IO budget. The numbers to compare.
- **Critical path**: the path with the worst slack.
- **Minimum period / Fmax**: the shortest period the worst reg-to-reg
  path allows (period minus its slack, with skew); Fmax = 1 / min period.
  Independent of where the constraint was set, so it is the number to
  compare.
- **Logic depth / path cells**: how many cells sit between the two
  registers of a path. The toy-library measure (`ltp`) counted gate
  levels; the runner counts cells on the placed critical path.
- **Parasitics**: the resistance and capacitance of the wires, which add
  delay. **Estimated** from placement (fast mode) or **extracted** from
  the drawn wires into a **SPEF** file (full mode).
- **Wire delay**: the part of a path's delay that comes from wires rather
  than gates. Invisible before placement; the reason this runner exists.
- **Clock skew**: the clock arriving at different flops at different
  times.

## Constraints (SDC)

- **SDC**: Synopsys Design Constraints, the Tcl file that tells STA about
  clocks and exceptions. ORFS reads it at every stage.
- **create_clock**: defines a clock on a port with a period.
- **set_input_delay / set_output_delay**: the IO budget.
- **Clock domain crossing (CDC)**: a signal moving between two
  unrelated clocks. Needs a synchronizer, here a FIFO with
  **gray-code pointers** (only one bit changes per step, so a late bit
  cannot produce a wrong count).
- **set_clock_groups -asynchronous**: declares clocks unrelated; STA then
  times no path between them.
- **set_max_delay**: a bound on a path's delay regardless of clocks. The
  generated SDC uses it to keep gray-pointer paths short ("placed but
  never cut").
- **set_false_path**: tells STA to ignore a path.
- **Exception precedence**: when two constraints cover one path, clock
  groups and false paths win over max delay, which wins over multicycle.
  This is why the gray bounds time nothing while the clocks are declared
  asynchronous.
- **-datapath_only** (Vivado) and **-ignore_clock_latency** (OpenSTA):
  the same idea, time only the data path of a max-delay bound.
- **Register naming**: yosys names a flop after its output net plus the
  flop type, `u_x.wgray[3]$_DFF_PN0_`; Vivado and Synopsys use
  `u_x/wgray_reg[3]`. The runner translates the SDC patterns.
- **Hierarchical pin**: a pin of a module instance rather than of a leaf
  cell (`u_x_cdc/u_wsync/d[3]`). It exists only while the hierarchy does:
  the runner keeps what the RTL marks `keep_hierarchy` through yosys and
  through OpenROAD's link (`OPENROAD_HIERARCHICAL`), so an SDC can bound
  through it.
- **SDC write-back**: the SDC a stage writes out after reading one
  (`2_floorplan.sdc`, `3_place.sdc`); ORFS hands it to the next stage.
  OpenSTA writes a hierarchical pin back as the leaf pins behind it, so
  the copy decays as the resizer renames cells. The runner measures on the
  SDC as given.
- **Unmatched SDC pattern**: a pattern that names nothing in the netlist.
  The constraint that used it is dropped, silently, at the first stage
  that reads the SDC; the runner lists the patterns and counts the
  dropped constraints (`sdc_unmatched`, `sdc_dropped`).
- **Recovery / removal check**: the setup and hold checks of an
  asynchronous reset: it must release early enough (recovery) and late
  enough (removal) relative to the clock. A reset port with an IO budget
  gets this check against every clock in the design.
- **Reset ports**: inputs the runner leaves unconstrained (a false path,
  `--reset-ports`, default `*rst*,*reset*`), since the contract is a
  common reset window with the clocks stopped.

## Wires and area

- **HPWL**: half-perimeter wire length, the width plus height of the box
  around a net's pins. A placement-only estimate of how long the net will
  be, summed over all nets as the design's total.
- **Routed wire length**: the length of the wires actually drawn, per net
  or in total (full mode only).
- **Net**: one electrical connection, from a driver to its loads.
  **Pins** are where it touches cells or ports. A high-pin-count net
  (reset, enable) is usually the longest.
- **Cell area**: the sum of the cells' areas in square microns. The one
  area number a library makes meaningful.
- **Flops**: the registers (`all_registers` in OpenSTA).

## Determinism and bookkeeping

- **Seed**: the random seed of the placer and router. Fixed by the runner
  (`--seed`, default 42) so two runs of one design print the same numbers.
- **SYNTH_REPEATABLE_BUILD**: an ORFS switch that removes the remaining
  sources of run-to-run variation in synthesis.
- **Run name**: the first level under `pnr/`: the block, the filelist's
  stem, or `--name` (`--vs-name` for the second design). A folder that holds
  a measured run of another design or setup is refused, not overwritten;
  the same setup again is an incremental rerun.
