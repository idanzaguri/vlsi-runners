#!/usr/bin/env python3
import argparse
import yaml
import os
import re
import sys
import signal
import threading
import subprocess
import concurrent.futures
import time
import shutil
import shlex
import getpass
import inspect
import itertools
from pathlib import Path
import random
from proj_utils import *
from log_utils import *
import datetime
from pathlib import Path
from tqdm import tqdm
from test_runner import (ms_sim_passed, ms_sim_warn, ms_sim_fail, ms_sim_waive,
                         vcs_sim_passed, vcs_sim_warn, vcs_sim_fail, vcs_sim_waive)


# ----------------------------------------------------------------------
# A simulator that stops answering.
#
# Questa's vsim front end (vish) runs the simulation kernel (vsimk) and a
# license process (vlm, which runs salt_mgls_asynch). vlm moves itself into
# a process group of its own. So a kill of the test's process group missed
# it: vlm noticed its vsim was gone, printed "** Fatal: Read failure in vlm
# process (0,0)" and exited. Every test the runner killed at its timeout
# therefore ended in that line, which made the license look like the cause
# of the hang it only reported (checked on Questa FSE 2025.2: a process
# group kill of a running vsim prints exactly that line, nothing else does).
#
# What the runner does now: it watches the output while the test runs. A
# test whose simulator lives on --finish_grace seconds after $finish is
# killed and judged from its output; a test lost before $finish (killed at
# --timeout, or its license process failed) is rerun with the same seed
# (--reruns). Every kill takes the test's whole session, license processes
# included, and writes what each process was doing into the test's log.
# ----------------------------------------------------------------------

# The line a simulator prints when $finish runs: Questa's "** Note: $finish
# : <file>(<line>)", VCS's "$finish called from file ...". Questa flushes
# its output when the run stops, so the runner reads this line while vsim
# still lives.
_FINISH_RE = re.compile(r"\*\* Note: \$finish\b|\$finish called from")

# What Questa prints when its license machinery fails: vlm itself when it
# loses the pipe to its vsim ("Read failure in vlm process", "Write failure
# in vlm process"), and the vsim front end when vlm dies under it ("FATAL
# ERROR in license manager", exit code 43) or a periodic check fails
# ("license lost").
_LICENSE_FAIL_RE = re.compile(r"(Read|Write) failure in vlm process|FATAL ERROR in license manager"
                              r"|[Ll]icense lost|Unable to kill and restart license")

# test_runner's own rules for a simulation log, per simulator
_SIM_LOG_RULES = {
    "modelsim": (ms_sim_passed, ms_sim_warn, ms_sim_fail, ms_sim_waive),
    "vcs": (vcs_sim_passed, vcs_sim_warn, vcs_sim_fail, vcs_sim_waive),
}


def _head_before_finish(stdout):
    """The output before the simulator's $finish line, or None when the run
    never reached $finish."""
    m = _FINISH_RE.search(stdout)
    return stdout[:m.start()] if m else None


def _verdict_from_output(head, simulator="modelsim", expect_fail=None):
    """The verdict test_runner would have given a run whose exit code is
    lost, read from the simulator's output up to $finish (``head``).

    A run loses it when its simulator lives on after $finish and the runner
    kills it, or when Questa's license process fails after $finish and vsim
    exits on that. The test had finished, so its own output decides, by
    test_runner's rules for a simulation log: the UVM report summary must be
    there; a UVM error or fatal, or a simulator error, fails it; a UVM
    warning, or a simulator warning that is not waived, makes it
    PASSED_WARN. The summary's counts must read UVM_ERROR : 0 and
    UVM_FATAL : 0, and a UVM_WARNING count above 0 makes it PASSED_WARN,
    which --pedant fails as it fails test_runner's exit code 2. A simulator
    Fatal before $finish fails it. A test that declares expect_fail passes
    only when the output failed and carries its text, as in test_runner."""
    passed, warn, fail, waive = _SIM_LOG_RULES.get(simulator, _SIM_LOG_RULES["modelsim"])
    status = scan_log_lines(head.splitlines(), passed, warn, fail, waive)[0]
    if status in ("PASSED", "PASSED_WARN"):
        if not (re.search(r"UVM_ERROR\s*:\s*0\b", head) and re.search(r"UVM_FATAL\s*:\s*0\b", head)):
            status = "FAILED"
        elif re.search(r"^#? ?\*\* Fatal", head, re.M):
            status = "FAILED"
        elif re.search(r"UVM_WARNING\s*:\s*[1-9]", head):
            status = "PASSED_WARN"
    if expect_fail:
        failed = status not in ("PASSED", "PASSED_WARN")
        return "PASSED" if failed and expect_fail in head else "FAILED"
    return status if status in ("PASSED", "PASSED_WARN") else "FAILED"


def _session_members(sid):
    """{pid: (ppid, pgid, state)} of every live process in session ``sid``.

    Each test runs in a session of its own (start_new_session), so its
    session holds everything it started, the license processes too, which
    leave the test's process group but cannot leave its session."""
    members = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                # after the "(command)": state ppid pgrp session ...
                fields = f.read().rpartition(")")[2].split()
        except OSError:
            continue
        if len(fields) > 3 and fields[3] == str(sid) and fields[0] != "Z":
            members[int(entry)] = (int(fields[1]), int(fields[2]), fields[0])
    return members


def _signal_session(sid, sig):
    """Send ``sig`` to every process of a test's session, the processes
    outside the test's own process group (the license processes) first, so
    none of them outlives its vsim. Returns how many there were."""
    members = _session_members(sid)
    for pid in sorted(members, key=lambda p: members[p][1] == sid):
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    try:
        os.killpg(sid, sig)  # anything forked since the listing
    except OSError:
        pass
    return len(members)


def _kill_session(sid):
    """SIGKILL a test's whole session until nothing of it is left."""
    for _ in range(50):
        if not _signal_session(sid, signal.SIGKILL):
            return
        time.sleep(0.1)


def _session_snapshot(sid):
    """What each process of a test's session was doing: state, CPU seconds
    used so far, the kernel function it waits in (WCHAN), its command. The
    runner writes it into the log of a test it kills, so the next hang
    names the process that stopped, and a stuck simulator (asleep, its CPU
    time flat) can be told from a busy one."""
    tick = os.sysconf("SC_CLK_TCK")
    rows = [f"{'PID':>8} {'PPID':>8} {'PGID':>8} ST {'CPU_S':>8}  {'WCHAN':<24} COMMAND"]
    for pid, (ppid, pgid, state) in sorted(_session_members(sid).items()):
        try:
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().rpartition(")")[2].split()
            cpu = (int(fields[11]) + int(fields[12])) / tick  # utime + stime
            with open(f"/proc/{pid}/wchan") as f:
                # "0" while running; drop the compiler's ".constprop.0" suffixes
                wchan = f.read().strip().split(".")[0]
            wchan = wchan if wchan not in ("", "0") else "-"
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except (OSError, ValueError, IndexError):
            continue
        rows.append(f"{pid:>8} {ppid:>8} {pgid:>8} {state:>2} {cpu:8.1f}  {wchan[:24]:<24} {cmd[:110]}")
    return rows


class _Output:
    """A test's stdout and stderr, read as they arrive by two threads, so
    the runner sees the simulator's $finish line while the simulator still
    runs."""

    def __init__(self, proc, job=None):
        self.out, self.err = [], []
        self.finish_at = None  # when the $finish line arrived
        self.job = job         # the test's _SlurmJob, which reads its start line here
        self._threads = [threading.Thread(target=self._pump, args=(proc.stdout, self.out, True), daemon=True),
                         threading.Thread(target=self._pump, args=(proc.stderr, self.err, False), daemon=True)]
        for t in self._threads:
            t.start()

    def _pump(self, stream, sink, watch):
        for line in iter(stream.readline, ""):
            sink.append(line)
            if watch and self.job and self.job.started_at is None:
                self.job.saw(line)
            if watch and self.finish_at is None and _FINISH_RE.search(line):
                self.finish_at = time.time()
        stream.close()

    def closed(self, timeout):
        """Wait up to ``timeout`` seconds for both pipes to reach their end;
        True when they did."""
        end = time.time() + timeout
        for t in self._threads:
            t.join(max(0.0, end - time.time()))
        return not any(t.is_alive() for t in self._threads)


# ----------------------------------------------------------------------
# Tests on other machines (--launcher srun).
#
# With --launcher srun the runner starts every design build and every test
# through Slurm's srun. Slurm picks the machine and keeps the test in its
# queue until a machine has a free slot, so every runner and session that
# uses Slurm shares one limit. Every machine must see the work area at the
# same path (NFS) and have the same tools.
#
# A test run this way is treated as a local one: --timeout counts from the
# moment the test starts on its machine, not from the moment it was queued;
# a kill takes the test's whole session on that machine and writes what
# each process was doing into the test's log (both through a second srun
# step inside the test's job, --overlap), then scancel ends the job. A test
# that Slurm ended before $finish (its machine failed, or someone cancelled
# it) is LOST and rerun, as a test killed at --timeout is.
# ----------------------------------------------------------------------

launcher = "local"  # "local" or "srun" (see --launcher)
srun_args = []      # extra srun options (see --srun_args)

# The program srun runs on the test's machine, ahead of the test. A task of
# slurmstepd leads a process group of its own but shares slurmstepd's
# session, and setsid() refuses a group leader, so it forks: the child takes
# a session of its own, prints where it runs and becomes the test; the
# parent waits and returns the test's exit code. The parent passes a
# SIGTERM or SIGINT on to the test instead of dying of it, because without
# cgroups Slurm finds a job's processes through their parents.
_SRUN_TASK = r"""
import os, signal, socket, sys
pid = os.fork()
if pid == 0:
    os.setsid()
    print(f"[regr_runner] started: host {socket.gethostname()}, Slurm job "
          f"{os.environ.get('SLURM_JOB_ID', '?')}, session {os.getpid()}", flush=True)
    os.execvp(sys.argv[1], sys.argv[1:])
def pass_on(sig, frame):
    try:
        os.killpg(pid, sig)
    except OSError:
        pass
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, pass_on)
code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
sys.exit(code if code >= 0 else 128 - code)
"""
_STARTED_RE = re.compile(r"^\[regr_runner\] started: host (\S+), Slurm job (\S+), session (\d+)")

# What srun and slurmstepd print when Slurm, not the test, ended a job: its
# machine failed ("Node failure on <host>"), or someone cancelled it
# ("*** STEP 24.0 ON <host> CANCELLED AT <time> ...")
_SLURM_LOST_RE = re.compile(r"srun: error: Node failure on \S+|\*\*\* (?:JOB|STEP) \S+ ON \S+ CANCELLED AT .*")


def _session_tool():
    """The source of a small program that does, on the machine of a test
    started through srun, what the runner does to a local test it kills:
    print what each process of the test's session was doing (argument
    "snapshot"), then kill the session. It is built from the runner's own
    functions, so it needs only python3 on that machine."""
    funcs = (_session_members, _signal_session, _kill_session, _session_snapshot)
    return ("import os, signal, sys, time\n"
            + "".join(inspect.getsource(f) for f in funcs)
            + "sid = int(sys.argv[1])\n"
              "if 'snapshot' in sys.argv[2:]:\n"
              "    print('\\n'.join(_session_snapshot(sid)), flush=True)\n"
              "_kill_session(sid)\n")


class _SlurmJob:
    """One test or design build started through srun: its job name, unique
    to this runner, and, once it started, where it runs (the line
    _SRUN_TASK prints)."""

    _serial = itertools.count(1)

    def __init__(self, label):
        self.name = f"{label}.r{os.getpid()}.{next(self._serial)}"
        self.host = self.job_id = self.sid = None
        self.started_at = None  # when the start line arrived

    def command(self, cmd_args, minutes=None):
        """srun, its options, then ``cmd_args``. ``minutes`` is Slurm's own
        time limit for the job, which ends it if the runner is no longer
        there to; a --time in --srun_args wins. Only the options the runner
        needs are set here, since srun gives a mixed result for an option
        given twice."""
        cmd = ["srun", f"--job-name={self.name}", "--input=none", "--unbuffered", "--quit-on-interrupt"]
        if minutes and not any(a.startswith(("-t", "--time")) for a in srun_args):
            cmd.append(f"--time={minutes}")
        return cmd + srun_args + ["python3", "-c", _SRUN_TASK] + cmd_args

    def saw(self, line):
        """Read the start line, if ``line`` is it."""
        m = _STARTED_RE.match(line)
        if m:
            self.host, self.job_id, self.sid = m.group(1), m.group(2), int(m.group(3))
            self.started_at = time.time()

    def stop(self, snapshot=False):
        """Kill the job's session on its machine (after printing what each
        of its processes was doing, with ``snapshot``), then cancel the
        job, which also takes it out of the queue if it never started.
        Returns the snapshot's rows."""
        rows = []
        if self.sid is not None:
            cmd = (["srun", f"--jobid={self.job_id}", "--overlap", "--input=none",
                    "python3", "-c", _session_tool(), str(self.sid)] + (["snapshot"] if snapshot else []))
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                   stdin=subprocess.DEVNULL)
                rows = r.stdout.splitlines()
                if snapshot and not rows:
                    rows = [f"(no snapshot from {self.host}: {r.stderr.strip()[:200]})"]
            except (OSError, subprocess.TimeoutExpired) as e:
                rows = [f"(no snapshot from {self.host}: {e})"]
        try:
            subprocess.run(["scancel", f"--user={getpass.getuser()}", f"--name={self.name}"],
                           capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return rows if snapshot else []


global wa_root

_TEST_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_runner.py")

# Registry of currently running test subprocesses so a Ctrl-C handler can kill them.
_running_procs = {}
_running_procs_lock = threading.Lock()
_abort_event = threading.Event()  # set on interrupt; stops new tests from launching
test_timeout = 300  # per-test wall-clock timeout in seconds (see --timeout)
finish_grace = 60   # seconds a simulator may live on after $finish (see --finish_grace)


def _terminate_running_procs(grace=2):
    """Kill every in-flight test (test_runner, its vlog/vsim children and
    the license processes). Each test runs in a session of its own, so the
    session reaches the whole tree. A test started through srun is stopped
    on its machine and its job cancelled first, all at once."""
    with _running_procs_lock:
        _abort_event.set()  # block any not-yet-launched worker from spawning a new test
        procs = list(_running_procs.values())
    stoppers = [threading.Thread(target=p.slurm.stop) for p in procs if getattr(p, "slurm", None)]
    for t in stoppers:
        t.start()
    for t in stoppers:
        t.join(150)
    for proc in procs:
        _signal_session(proc.pid, signal.SIGTERM)
    time.sleep(grace)  # give them a chance to exit cleanly...
    for proc in procs:
        _kill_session(proc.pid)  # ...then force-kill any stragglers
    return len(procs)


def run_test(block, test, seed, rundir, codecov=False, simulator="modelsim",
             lib=None):
    """Runs a single test instance with the given parameters.

    ``lib`` is a work library built once for this test's DESIGN (see
    :func:`build_libs`): the test then compiles nothing and optimizes
    nothing, it just runs the snapshot already in there.

    Returns (block, test, seed, status, seconds, note). The status is
    PASSED, PASSED_WARN or FAILED, or LOST: the simulator stopped before
    $finish for a reason that is not the test's (the runner killed it at
    the timeout, its license process failed, or Slurm ended its job), and
    the caller reruns it.
    ``note`` says, in a few words, what the runner did beyond reading
    test_runner's exit code, or is None."""
    cmd = f"python3 {_TEST_RUNNER} --block {block} --test {test} --seed {seed} --rundir {rundir} --simulator {simulator}"
    if lib:
        cmd += f" --run-only --lib {lib}"
    if codecov:
        cmd += " --codecov"

    cmd_args = shlex.split(cmd)
    job = None
    if launcher == "srun":
        # Slurm's own time limit, past --timeout, ends a test whose runner died
        job = _SlurmJob(f"{test}.{seed}")
        cmd_args = job.command(cmd_args, minutes=test_timeout // 60 + 2)
    # Register under the lock and bail out if an abort is already in progress, so a
    # worker that hasn't launched yet won't spawn an unkillable process after the
    # interrupt handler has already swept _running_procs.
    # start_new_session=True puts this test (and everything it starts) in a
    # session of its own, which is what a kill of the test takes. With srun,
    # that session holds srun; the test's own session is on its machine.
    t_start = time.time()
    with _running_procs_lock:
        if _abort_event.is_set():
            return block, test, seed, "FAILED", 0.0, None
        proc = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL if job else None,
                                text=True, errors="replace", start_new_session=True)
        proc.slurm = job
        _running_procs[proc.pid] = proc
    output = _Output(proc, job)
    killed, snapshot = None, []
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.time()
            # a test still in Slurm's queue has not started yet
            started = job.started_at if job else t_start
            if finish_grace and output.finish_at is not None and now - output.finish_at >= finish_grace:
                killed = "after_finish"
            elif started is not None and now - started >= test_timeout:
                killed = "timeout"
            if killed:
                if job:
                    snapshot = job.stop(snapshot=True)
                    try:
                        proc.wait(timeout=60)  # srun ends with its job
                    except subprocess.TimeoutExpired:
                        _kill_session(proc.pid)
                else:
                    snapshot = _session_snapshot(proc.pid)
                    _kill_session(proc.pid)
                proc.wait()
                break
        # the pipes reach their end once every process holding them is gone:
        # a license process outlives its vsim by a moment, and one that hangs
        # must not hang the runner with it
        if not output.closed(30):
            _kill_session(proc.pid)
            output.closed(10)
    finally:
        with _running_procs_lock:
            _running_procs.pop(proc.pid, None)

    stdout, stderr = "".join(output.out), "".join(output.err)
    log_text = stdout + "\n" + stderr
    head = _head_before_finish(stdout)
    expect = expect_fail_of(block, test)
    notes, note = [], None
    if killed:
        if killed == "timeout":
            notes.append(f"TIMEOUT: test killed after {test_timeout}s")
            note = f"killed at the {test_timeout}s timeout"
        else:
            notes.append(f"HUNG AFTER $finish: the simulator was still running "
                         f"{finish_grace}s after $finish, killed")
            note = f"killed {finish_grace}s after $finish"
        if head is not None:
            status = _verdict_from_output(head, simulator, expect)
            notes.append(f"the test had reached $finish, so its output up to there "
                         f"decides: {status}")
        else:
            status = "LOST"
            note += " before $finish"
            notes.append("the simulator never reached $finish")
    elif proc.returncode == 0:
        status = "PASSED"
    elif proc.returncode == 2:
        status = "PASSED_WARN"
    elif job and _SLURM_LOST_RE.search(stderr):
        why = _SLURM_LOST_RE.search(stderr).group(0).strip()
        if head is not None:
            status = _verdict_from_output(head, simulator, expect)
            note = "Slurm ended its job after $finish"
            notes.append(f"Slurm ended the job after $finish (srun exit code {proc.returncode}: "
                         f"{why}), so the output up to $finish decides: {status}")
        else:
            status = "LOST"
            note = "Slurm ended its job before $finish"
            notes.append(f"Slurm ended the job before $finish (srun exit code {proc.returncode}: {why})")
    elif _LICENSE_FAIL_RE.search(log_text):
        if head is not None:
            status = _verdict_from_output(head, simulator, expect)
            note = "its license process failed after $finish"
            notes.append(f"the simulator's license process failed after $finish (test_runner "
                         f"exit code {proc.returncode}), so the output up to $finish decides: {status}")
        else:
            status = "LOST"
            note = "its license process failed before $finish"
            notes.append(f"the simulator's license process failed before $finish (test_runner "
                         f"exit code {proc.returncode})")
    else:
        status = "FAILED"
    if killed and head is not None and codecov and not os.path.exists(os.path.join(rundir, "coverage.ucdb")):
        notes.append("no coverage database: the simulator was killed before it saved one")
        note += ", no coverage database"
    if snapshot:
        notes.append("its processes when killed:")
        notes.extend(f"    {row}" for row in snapshot)

    log_path = os.path.join(rundir, "test.log")
    with open(log_path, "w") as log_file:
        log_file.write(log_text)
        if notes:
            log_file.write("\n" + "".join(f"[regr_runner] {n}\n" for n in notes))
    # the test's own time: a wait in Slurm's queue is not part of it
    t_run = job.started_at if job and job.started_at else t_start
    return block, test, seed, status, time.time() - t_run, note


def clean_test_files(rundir, success, keep_patterns = ["test.log", "test.try*.log", "coverage.ucdb"]):
    rundir = Path(rundir)  # Ensure rundir is a Path object.
    if not rundir.is_dir():
        raise ValueError(f"{rundir} is not a valid directory.")

    if success:
        # First, delete files that do not match any of the allowed patterns.
        for item in rundir.rglob('*'):
            if item.is_file():
                if not any(item.match(pattern) for pattern in keep_patterns):
                    try:
                        item.unlink()
                    except Exception as e:
                        print(f"Could not delete file {item}: {e}")
        # Next, remove empty directories.
        # Sorting in reverse ensures we remove deeper directories before their parents.
        for directory in sorted(rundir.rglob('*'), key=lambda d: len(d.parts), reverse=True):
            if directory.is_dir():
                try:
                    # If directory is empty, remove it.
                    if not any(directory.iterdir()):
                        directory.rmdir()
                except Exception as e:
                    print(f"Could not delete directory {directory}: {e}")


_comp_cache = {}
_xfail_cache = {}


def comp_of_tests(block):
    """{test name: compilation name} from the block's own tests.yaml.

    Several tests share one compilation (one design, several UVM tests), and
    that is the unit a work library is built for. Parsed once per block: the
    file is 200 KB, the parse 0.4 s, and a 780-member list asked for it
    twice per member on the main thread (10 minutes of a 25-minute run).
    Read the way test_runner reads it: Jinja rendered first, then YAML."""
    if block in _comp_cache:
        return _comp_cache[block]
    path = Path(wa_root) / "verif" / block / "lib" / "tests.yaml"
    if not path.is_file():
        return {}
    data = load_yaml(str(path)) or {}
    _comp_cache[block] = {t.get("name"): t.get("comp")
                          for t in (data.get("tests") or [])
                          if t.get("name") and t.get("comp")}
    _xfail_cache[block] = {t.get("name"): t.get("expect_fail")
                           for t in (data.get("tests") or [])
                           if t.get("name") and t.get("expect_fail")}
    return _comp_cache[block]


def expect_fail_of(block, test):
    """The ``expect_fail`` text a test declares in its block's tests.yaml, or
    None (parsed with the compilation names, once per block)."""
    comp_of_tests(block)
    return _xfail_cache.get(block, {}).get(test)


def build_lib(block, comp, test, build_dir, codecov=False, simulator="modelsim"):
    """Compile and optimize one design ONCE, into its own work library."""
    build_dir.mkdir(parents=True, exist_ok=True)
    cmd = (f"python3 {_TEST_RUNNER} --block {block} --test {test} "
           f"--compile-only --rundir {build_dir} --simulator {simulator}")
    if codecov:
        cmd += " --codecov"
    cmd_args = shlex.split(cmd)
    if launcher == "srun":
        cmd_args = _SlurmJob(f"build.{comp}").command(cmd_args)
    r = subprocess.run(cmd_args, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL if launcher == "srun" else None)
    ok = r.returncode == 0
    if not ok:
        (build_dir / "build.log").write_text(r.stdout + r.stderr)
    return (block, comp), ok


def parse_test_list(file_path):
    jinja2_variables = {}

    try:
        with open(file_path, 'r') as f:
            raw_content = f.read()
    except:
        print_message("error", f"Filed to open regr file - {file_path}")
        exit(1)

    # Apply Jinja2 rendering first
    template = Template(raw_content, undefined=StrictUndefined)
    rendered_content = template.render(jinja2_variables)
 
    try:
        content = yaml.safe_load(rendered_content) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Error parsing YAML after Jinja2 rendering: {e}")
    
    return content.get("tests", [])

def summarize_results(results, summary_path, total_tests, start_time, pedant=False, reruns=()):
    """Generates a summary of all test results.

    ``results`` holds (block, test, seed, status, log, seconds, note): the
    note is set when the verdict came from the test's output and not from
    test_runner's exit code (the simulator was killed, or lost its license,
    after $finish). ``reruns`` holds (block, test, seed, why, seconds, log)
    for every run lost before $finish and run again with the same seed."""
    def line(t):
        # test, seed, log, and the member's wall time (compile is per design,
        # so this is the vsim run plus the runner's own overhead)
        secs = t[5] if len(t) > 5 else 0.0
        return f"{t[1]} (seed {t[2]}) {secs:6.1f}s {t[4]}"
    passed      = [line(t) for t in results if t[3] == "PASSED"]
    passed_warn = [line(t) for t in results if t[3] == "PASSED_WARN"]
    failed      = [line(t) for t in results if t[3] == "FAILED"]
    judged      = [f"{t[1]} (seed {t[2]}) {t[3]}: {t[6]}, {t[4]}"
                   for t in results if len(t) > 6 and t[6]]
    final = {(t[0], t[1], t[2]): t[3] for t in results}
    rerun_lines = [f"{test} (seed {seed}) {why}, {secs:.1f}s, {log}; "
                   f"rerun: {final.get((block, test, seed), 'running')}"
                   for block, test, seed, why, secs, log in reruns]
    slowest = sorted(results, key=lambda t: -(t[5] if len(t) > 5 else 0.0))[:10]
    duration = time.time() - start_time

    effective_passed = len(passed) + (0 if pedant else len(passed_warn))
    effective_failed = len(failed) + (len(passed_warn) if pedant else 0)

    summary_content = (
        f"{'='*40}\n"
        f"  Regression Summary  \n"
        f"{'='*40}\n"
        f"Total tests: {total_tests}\n"
        f"Passed: {len(passed)} ({len(passed)/total_tests:.1%})\n"
        f"Passed with warnings: {len(passed_warn)} ({len(passed_warn)/total_tests:.1%})\n"
        f"Failed: {len(failed)} ({len(failed)/total_tests:.1%})\n"
        f"Reruns: {len(rerun_lines)} (lost before $finish, run again with the same seed)\n"
        f"Judged from the output: {len(judged)} (killed or lost its license after $finish)\n"
        f"Pedant mode: {pedant} (warnings treated as {'failures' if pedant else 'passes'})\n"
        f"Effective pass rate: {effective_passed/total_tests:.1%}\n"
        f"Duration: {duration:.2f} seconds\n"
        f"{'='*40}\n"
        f"Slowest members:\n" + "\n".join(line(t) for t in slowest) + "\n"
        f"{'-'*40}\n"
        f"Passed Tests:\n" + "\n".join(passed) + "\n"
        f"{'-'*40}\n"
        f"Passed with Warnings:\n" + "\n".join(passed_warn) + "\n"
        f"{'-'*40}\n"
        f"Failed Tests:\n" + "\n".join(failed) + "\n"
        f"{'-'*40}\n"
        f"Reruns:\n" + "\n".join(rerun_lines) + "\n"
        f"{'-'*40}\n"
        f"Judged from the output:\n" + "\n".join(judged) + "\n"
    )

    with open(summary_path, "w") as summary_file:
        summary_file.write(summary_content)

def merge_coverage(regression_dir):
    """Merge every member's coverage database, one merged file per block,
    and report on it.

    A block is one design, so the merge stops at the block: adding two
    designs' bins together would mean nothing. Each block gets
    ``merged.ucdb``, ``coverage.txt`` (the totals table, also printed and
    appended to the regression summary) and ``coverage_html/`` to browse;
    ``merge.log`` holds the tools' own output. Returns ``{block: table}``,
    the table as the lines of coverage.txt, for the blocks that had any
    database to merge. A failed tool step is reported and skipped, never
    fatal: the regression's verdict is already in."""
    tables = {}
    for block_dir in sorted(regression_dir.iterdir()):
        # `_build` holds the shared design libraries, not a block's runs
        if not block_dir.is_dir() or block_dir.name.startswith("_"):
            continue
        ucdb_files = sorted(block_dir.glob("**/coverage.ucdb"))
        if not ucdb_files:
            continue
        merged = block_dir / "merged.ucdb"
        report = block_dir / "coverage.txt"
        # a list file: 780 members would overflow a command line
        inputs = block_dir / "merge_inputs.txt"
        inputs.write_text("".join(f"{f}\n" for f in ucdb_files))
        steps = [
            ["vcover", "merge", "-inputs", str(inputs), "-out", str(merged)],
            ["vcover", "report", "-summary", "-output", str(report), str(merged)],
            ["vcover", "report", "-html", "-output", str(block_dir / "coverage_html"), str(merged)],
        ]
        print_message("info", f"merging {len(ucdb_files)} coverage database(s) of {block_dir.name}")
        merge_log = block_dir / "merge.log"
        with open(merge_log, "w") as log_file:
            for cmd in steps:
                r = subprocess.run(cmd, stdout=log_file, stderr=log_file)
                if r.returncode:
                    print_message("warning", f"{cmd[0]} {cmd[1]} failed for {block_dir.name}, "
                                             f"see {merge_log}")
                    break
        try:
            tables[block_dir.name] = [l.rstrip() for l in report.read_text().splitlines() if l.strip()]
        except OSError:
            pass
    return tables


def report_coverage(tables, regression_dir, summary_path):
    """Print each block's totals and append them to the summary."""
    if not tables:
        print_message("warning", "no coverage database was found to merge")
        return
    lines = []
    for block, table in tables.items():
        block_dir = regression_dir / block
        lines += [f"Block: {block}", *table,
                  f"UCDB: {block_dir / 'merged.ucdb'}",
                  f"HTML: {block_dir / 'coverage_html' / 'index.html'}", ""]
    text = "\n".join(lines)
    print(text)
    with open(summary_path, "a") as f:
        f.write(f"{'-'*40}\nCoverage:\n{text}\n")



def print_block_table(block, tests):
    total = len(tests)
    # Determine column widths: consider header and data lengths.
    test_col_width = max(len("Test"), *(len(test) for test, _ in tests))
    rpts_col_width = max(len("Repetitions"), *(len(str(rpts)) for _, rpts in tests))
    
    # Prepare table border and header
    border = f"+{'-'*(test_col_width+2)}+{'-'*(rpts_col_width+2)}+"
    header = f"| {'Test'.ljust(test_col_width)} | {'Repetitions'.ljust(rpts_col_width)} |"
    
    print(f"Block: {block} | Total tests: {total}")
    print(border)
    print(header)
    print(border)
    
    # Print each row in the table
    for test, rpts in tests:
        row = f"| {test.ljust(test_col_width)} | {str(rpts).center(rpts_col_width)} |"
        print(row)
    print(border)
    print()  # Blank line for spacing





def main():
    parser = argparse.ArgumentParser(description="Regression test runner")
    parser.add_argument("--tests", required=True, help="YAML file containing test list")
    parser.add_argument("--name", type=str , help="regression name")
    parser.add_argument("--repeat", type=int, default=1, help="Number of times to run each test")
    parser.add_argument("--max_parallel", type=int, default=16, help="Maximum parallel tests")
    parser.add_argument("--stop_on_fail", action="store_true", help="Stop on first failure")
    parser.add_argument("--codecov", action="store_true", help="Collect code coverage on every "
                        "member and merge it per block (see test_runner --codecov)")
    parser.add_argument("--keep", action="store_true", help="Disable passesd tests files folders cleaning")
    parser.add_argument("--pedant", action="store_true", help="Treat tests that pass with warnings as failures (affects pass rate and stop_on_fail)")
    parser.add_argument("--timeout", type=int, default=300, help="Per-test wall-clock timeout in seconds; a test exceeding it is killed. One that had reached $finish is judged from its output, one that had not is rerun (see --reruns) and then FAILED (default: 300)")
    parser.add_argument("--finish_grace", type=int, default=60, help="Seconds a simulator may keep running after $finish (coverage save, quit) before it is killed and the test judged from its output; 0 waits for --timeout instead (default: 60)")
    parser.add_argument("--reruns", type=int, default=1, help="How many times a test whose simulator stopped before $finish for a reason that is not the test's (killed at --timeout, or its license process failed) is rerun with the same seed before it counts as FAILED; 0 never reruns (default: 1)")
    parser.add_argument("--simulator", type=str, choices=["modelsim", "vcs"], default="modelsim", help="Simulator to use for every test in the regression (default: modelsim)")
    parser.add_argument("--no-shared-lib", action="store_true", help="Compile and optimize inside every test, as before the shared design libraries (slower; use if a library build is in the way)")
    parser.add_argument("--launcher", choices=["local", "srun"], default="local", help="Where each design build and test runs: local runs it on this machine; srun runs it through Slurm, which picks a machine with a free slot and queues it until there is one. The work area must have the same path on every machine (see README 'Tests on other machines'). With srun, --max_parallel is how many tests the runner hands to Slurm at once, and --timeout counts from the moment a test starts on its machine (default: local)")
    parser.add_argument("--srun_args", type=str, default="", help="Extra srun options for every build and test with --launcher srun, as one string, e.g. \"--constraint=questa\" to keep Questa runs on the machines that have a Questa license. Never --job-name: the runner names each job itself (default: none)")
    args = parser.parse_args()

    if args.name is None:
        args.name = datetime.datetime.now().strftime("%Y_%m_%d_%H%M%S")
    ascii_art = r"""
       ___                           _             ___                        
      / _ \___ ___ ________ ___ ___ (_)__  ___    / _ \__ _____  ___  ___ ____
     / , _/ -_) _ `/ __/ -_|_-<(_-</ / _ \/ _ \  / , _/ // / _ \/ _ \/ -_) __/
    /_/|_|\__/\_, /_/  \__/___/___/_/\___/_//_/ /_/|_|\_,_/_//_/_//_/\__/_/   
             /___/ 
    """
    # ANSI escape codes for colored output
    BOLD_CYAN = "\033[1;36m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"   # stopped: was running when interrupted
    GRAY = "\033[90m"      # cancelled: still queued, never started
    RESET = "\033[0m"
    print(f"{BOLD_CYAN}{ascii_art}{RESET}")


    global wa_root, test_timeout, finish_grace, launcher, srun_args
    test_timeout = args.timeout
    finish_grace = args.finish_grace
    launcher = args.launcher
    srun_args = shlex.split(args.srun_args)
    wa_root = get_git_root()
    if not wa_root:
        print_message("error","You are not inside valid workarea.")
        exit(1)
    if launcher == "srun":
        if not (shutil.which("srun") and shutil.which("scancel")):
            print_message("error", "--launcher srun: srun and scancel are not on PATH (is Slurm installed?)")
            exit(1)
        if any(a.startswith(("-J", "--job-name")) for a in srun_args):
            print_message("error", "--srun_args: leave out --job-name, the runner names each job itself")
            exit(1)
        print_message("info", f"builds and tests start through srun {' '.join(srun_args)}".rstrip())
    
    test_list = parse_test_list(args.tests)
    
    for entry in test_list:
        if "seed" in entry:
            entry["repetitions"] = 1
        elif "weight" in entry:
            entry["repetitions"] = entry["weight"] * args.repeat
        else:
            entry["repetitions"] = 1
        

    regression_dir = Path(f"{wa_root}/sim/regression/{args.name}")
    os.makedirs(str(regression_dir), exist_ok=True)
    
    results = []
    total_tests = sum(entry["repetitions"] for entry in test_list)
    start_time = time.time()
    running_tests = 0
    passed_tests = 0
    passed_warn_tests = 0
    failed_tests = 0
    pending_tests = total_tests
    
    test_queue = []
    grouped_tests = defaultdict(list)
    for entry in test_list:
        block, test, repetitions = entry["block"], entry["test"], entry["repetitions"]
        grouped_tests[block].append((test, entry["repetitions"]))
        
        if "seed" in entry:
            unique_seeds = [entry["seed"]] # FIXME: add support for list of seeds...
        else:
            unique_seeds = random.sample(range(0, 999999), entry["repetitions"])
        for seed in unique_seeds:
            rundir = regression_dir / block / test / f"seed_{seed}"
            test_queue.append((block, test, seed, rundir, 1))
    random.shuffle(test_queue)

    # Build each DESIGN once (Idan, 2026-09-16). Questa optimizes the design
    # inside vsim, so every test of one design used to re-optimize it; a work
    # library with a named snapshot is built here instead, and the tests just
    # run it. Measured on one design: 4.8 s per test becomes 1.8 s.
    libs, build_failed = {}, set()
    if args.simulator == "modelsim" and not args.no_shared_lib:
        wanted = {}
        for block, test, _seed, _rundir, _attempt in test_queue:
            comp = comp_of_tests(block).get(test)
            if comp:
                wanted.setdefault((block, comp), test)
        if wanted:
            print_message("info", f"building {len(wanted)} design librar"
                                  f"{'y' if len(wanted) == 1 else 'ies'} once")
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=args.max_parallel) as pool:
                jobs = [pool.submit(build_lib, b, c,
                                    wanted[(b, c)],
                                    regression_dir / "_build" / b / c,
                                    args.codecov, args.simulator)
                        for (b, c) in wanted]
                for job in tqdm(concurrent.futures.as_completed(jobs),
                                total=len(jobs), desc="compiling",
                                bar_format='{l_bar}{bar} | {elapsed}'):
                    (b, c), ok = job.result()
                    if ok:
                        libs[(b, c)] = regression_dir / "_build" / b / c / "sv_tb_work"
                    else:
                        build_failed.add((b, c))
                        tqdm.write(f"{c}: {RED}compilation failed{RESET} "
                                   f"({regression_dir / '_build' / b / c / 'build.log'})")

    # Print tables for each block
    for block, tests in grouped_tests.items():
        print_block_table(block, tests)
    
    reruns = []  # (block, test, seed, why, seconds, kept log) of every rerun
    status = lambda: f"Total: {total_tests}, Running: {running_tests}, Passed: {passed_tests}, Warned: {passed_warn_tests}, Failed: {failed_tests}, Pending: {pending_tests}, Reruns: {len(reruns)}"
    pbar = tqdm(total = total_tests, bar_format='{l_bar}{bar} | Elapsed: {elapsed} | ETA: {remaining}')

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.max_parallel)
    futures = {}
    pbar.set_description(status())
    try:
                
        while test_queue or futures:
            
            while test_queue and len(futures) < args.max_parallel:
                block, test, seed, rundir, attempt = test_queue.pop(0)
                rundir.mkdir(parents=True, exist_ok=True)
                pending_tests -= 1
                running_tests += 1
                lib = libs.get((block, comp_of_tests(block).get(test)))
                future = executor.submit(run_test, block, test, seed, rundir,
                                         args.codecov, args.simulator, lib)
                futures[future] = (block, test, seed, rundir, attempt)
                pbar.set_description(status())
                pbar.update(0)
            
            done, not_done = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_COMPLETED)
            
            for future in done:
                block, test, seed, rundir, attempt = futures.pop(future)
                test_status, elapsed, note = future.result()[3:6]
                test_log = rundir / "test.log"
                running_tests -= 1
                if test_status == "LOST":
                    # the simulator stopped before $finish for a reason that is
                    # not the test's: the same seed again, the lost run's log
                    # kept beside the rerun's
                    if attempt <= args.reruns:
                        kept = rundir / f"test.try{attempt}.log"
                        try:
                            with open(test_log, "a") as f:
                                f.write(f"[regr_runner] rerun {attempt} of {args.reruns}, same seed; "
                                        f"this log is kept as {kept.name}\n")
                            os.replace(test_log, kept)
                        except OSError:
                            pass
                        reruns.append((block, test, seed, note, elapsed, kept))
                        test_queue.insert(0, (block, test, seed, rundir, attempt + 1))
                        pending_tests += 1
                        tqdm.write(f"{test} @ {seed} {MAGENTA}lost{RESET} ({note}), rerunning with the same seed")
                        summarize_results(results, regression_dir / "summary.txt", total_tests, start_time,
                                          args.pedant, reruns)
                        pbar.set_description(status())
                        continue
                    try:
                        with open(test_log, "a") as f:
                            f.write(f"[regr_runner] lost before $finish on its "
                                    f"{'rerun' if attempt > 1 else 'only run (--reruns 0)'}: FAILED\n")
                    except OSError:
                        pass
                    test_status, note = "FAILED", None
                results.append((block, test, seed, test_status, test_log, elapsed, note))
                if not args.keep:
                    clean_test_files(rundir, test_status != "FAILED")

                how = f" ({note})" if note else ""
                if test_status == "PASSED":
                    passed_tests += 1
                    tqdm.write(f"{test} @ {seed} {GREEN}passed{RESET}{how}")
                elif test_status == "PASSED_WARN":
                    passed_warn_tests += 1
                    tqdm.write(f"{test} @ {seed} {YELLOW}passed with warnings{RESET}{how}")
                else:
                    failed_tests += 1
                    tqdm.write(f"{test} @ {seed} {RED}failed{RESET}{how}")
                summarize_results(results, regression_dir / "summary.txt", total_tests, start_time,
                                  args.pedant, reruns)
                pbar.set_description(status())
                pbar.update(1)

            effective_failed = failed_tests + (passed_warn_tests if args.pedant else 0)
            if args.stop_on_fail and effective_failed:
                pbar.close()
                print()
                print_message("warning","Abort regression due to failed test!")
                executor.shutdown(wait=False, cancel_futures=True)
                break
    except KeyboardInterrupt:
        # Ignore further Ctrl-C so the report below prints atomically even if the
        # user keeps mashing the key (a second SIGINT here would abort the cleanup).
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        pbar.close()
        print()
        print_message("warning", "Regression interrupted by user (Ctrl-C)!")

        stopped   = list(futures.values())   # tests running when interrupted
        cancelled = list(test_queue)         # tests still queued, never started

        print(f"\n{MAGENTA}Stopped (running when interrupted): {len(stopped)}{RESET}")
        for _block, _test, _seed, _rundir, _attempt in stopped:
            print(f"  {MAGENTA}{_test} @ {_seed}{RESET}")

        print(f"\n{GRAY}Cancelled (never started): {len(cancelled)}{RESET}")
        for _block, _test, _seed, _rundir, _attempt in cancelled:
            print(f"  {GRAY}{_test} @ {_seed}{RESET}")

        print(f"\nCompleted before interrupt: {GREEN}{passed_tests} passed{RESET}, "
              f"{YELLOW}{passed_warn_tests} warned{RESET}, {RED}{failed_tests} failed{RESET}")

        # Kill the in-flight simulations (test_runner, its vlog/vsim children
        # and their license processes).
        print_message("warning", f"Stopping {len(stopped)} running simulation(s)...")
        killed = _terminate_running_procs()
        print_message("info", f"Stopped {killed} simulation(s).")

        summarize_results(results, regression_dir / "summary.txt", total_tests, start_time,
                          args.pedant, reruns)
        print_message("info", f"Partial report: {regression_dir}/summary.txt")
        executor.shutdown(wait=False, cancel_futures=True)
        sys.stdout.flush()
        sys.exit(130)

    executor.shutdown(wait=False, cancel_futures=True)
    summarize_results(results, regression_dir / "summary.txt", total_tests, start_time,
                      args.pedant, reruns)

    pbar.close() 
    print()
    if args.codecov:
        report_coverage(merge_coverage(regression_dir), regression_dir,
                        regression_dir / "summary.txt")
    
    effective_passed = passed_tests + (0 if args.pedant else passed_warn_tests)
    effective_failed = failed_tests + (passed_warn_tests if args.pedant else 0)
    pass_rate = (effective_passed/total_tests)*100
    if pass_rate == 100 and passed_warn_tests == 0:
        print_message("info",f"PASS_RATE: {pass_rate}%")
    elif effective_failed == 0:
        print_message("warning",f"PASS_RATE: {pass_rate:.2f}% ({passed_warn_tests} test(s) passed with warnings)")
    else:
        print_message("warning",f"PASS_RATE: {pass_rate:.2f}%")

    if effective_failed == 0:
        color = "\033[93m" if passed_warn_tests > 0 else "\033[92m"
        print(f"{color}  _       __  __  _  _  \033[0m")
        print(f"{color} |_) /\\  (_  (_  |_ | \\ \033[0m")
        print(f"{color} |  /--\\ __) __) |_ |_/ \033[0m")
    else:
        color = "\033[91m"
        print(f"{color}  _     ___     _  _    \033[0m")
        print(f"{color} |_ /\\   |  |  |_ | \\   \033[0m")
        print(f"{color} | /--\\ _|_ |_ |_ |_/   \033[0m")

    print() 
    print_message("info", f"Rregression Report: {regression_dir}/summary.txt")
if __name__ == "__main__":
    main()

