#!/usr/bin/env python3
"""Build or run a cocotb bench on Verilator through cocotb's own runner.

    python3 cocotb_run.py build '<json>'
    python3 cocotb_run.py test  '<json>'

test_runner --simulator verilator --cocotb_module <module> calls it with the
project's Python (the first python3 on PATH after bin/setenv.sh), so the
cocotb release is the project's. cocotb_tools.runner keeps cocotb's build
recipe (its verilator.cpp, its VPI library, -cc --exe --vpi
--public-flat-rw) and sets the environment a test needs (COCOTB_TEST_MODULES,
the seed, PYTHONPATH from sys.path). The JSON holds the keyword arguments
of Runner.build or Runner.test; "pythonpath" is put first on sys.path, and
"jobs" is the build's make -j.

Output goes to stdout (test_runner writes it to comp.log or run.log). The
exit code is 0 when the runner finished, 1 when it raised: a failed build,
a failed test or a crash (test_runner reads results.xml and the log for
the verdict).
"""

import json
import sys


def main(argv):
    if len(argv) != 3 or argv[1] not in ("build", "test"):
        print(__doc__.split("\n\n")[1])
        return 2
    kwargs = json.loads(argv[2])
    sys.path[:0] = kwargs.pop("pythonpath", [])
    jobs = kwargs.pop("jobs", None)
    import cocotb_tools.runner
    from cocotb_tools.runner import get_runner
    # the runner's make -j is min(4, cpu_count()): cpu_count() counts the
    # machine's CPUs, not the ones a Slurm job has, and the runner takes no
    # argument for it; set it where the runner reads it (a private function
    # of cocotb 2.1: without it, cocotb's own count stays)
    if jobs and hasattr(cocotb_tools.runner, "_get_max_parallel_build_jobs"):
        cocotb_tools.runner._get_max_parallel_build_jobs = lambda: jobs
    runner = get_runner("verilator")
    try:
        if argv[1] == "build":
            runner.build(**kwargs)
        else:
            runner.test(**kwargs)
    except (RuntimeError, SystemExit, ValueError) as exc:
        print(f"[cocotb_run] the cocotb runner stopped: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
