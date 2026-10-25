# Running tests on several machines (Slurm)

`regr_runner --launcher srun` starts every design build and every test through Slurm.
Slurm keeps one queue for all the machines. It runs a job on a machine that has a free
slot and keeps the other jobs in the queue. Every session and runner that uses Slurm
shares the same limit, so no machine is asked to run more jobs than it has slots.

This page sets up Slurm in two steps:

1. lab-lnx alone. This already gives one limit for all the sessions on it.
2. More machines: a second PC, and the laptop when it is free.

The commands need sudo. They are for Ubuntu 26.04 and its packages: Slurm 25.11.2 and
munge 0.5.16. The slurm.conf below was checked on lab-lnx with a test controller and
node that ran as a normal user. The cgroup settings were not checked that way, because
they need root.

## Step 1: lab-lnx as the only machine

### Install

```bash
sudo apt install slurm-wlm munge
```

`slurm-wlm` installs the controller (`slurmctld`), the node daemon (`slurmd`) and the
commands (`srun`, `sinfo`, `squeue`, `scancel`, `scontrol`). `munge` signs the messages
between them. Its install creates the key `/etc/munge/munge.key`.

Check munge:

```bash
munge -n | unmunge | grep STATUS        # STATUS: Success (0)
```

### Write /etc/slurm/slurm.conf

`slurmd -C` prints the machine's own line. On lab-lnx it prints
`NodeName=lab-lnx CPUs=12 Boards=1 SocketsPerBoard=1 CoresPerSocket=6 ThreadsPerCore=2 RealMemory=31373`.
The file takes `CPUs` and `RealMemory` from it. Do not copy the `Boards`, `Sockets`,
`Cores` and `Threads` fields. With them, Slurm gave each one-CPU job a whole core (two
threads), and only 6 tests ran at once on the 12 threads.

```bash
sudo tee /etc/slurm/slurm.conf > /dev/null <<'EOF'
ClusterName=lab
SlurmctldHost=lab-lnx
SlurmUser=slurm
AuthType=auth/munge
CredType=cred/munge
MpiDefault=none
ProctrackType=proctrack/cgroup
TaskPlugin=task/cgroup,task/affinity
SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_CPU
ReturnToService=2
InactiveLimit=120
KillWait=10
StateSaveLocation=/var/lib/slurm/slurmctld
SlurmdSpoolDir=/var/lib/slurm/slurmd
SlurmctldPidFile=/run/slurmctld/slurmctld.pid
SlurmdPidFile=/run/slurm/slurmd.pid
SlurmctldLogFile=/var/log/slurm/slurmctld.log
SlurmdLogFile=/var/log/slurm/slurmd.log
AccountingStorageType=accounting_storage/none
JobAcctGatherType=jobacct_gather/none
MailProg=/usr/bin/true
NodeName=lab-lnx CPUs=12 RealMemory=31373 CPUSpecList=10,11 Features=questa State=UNKNOWN
PartitionName=main Nodes=ALL Default=YES MaxTime=INFINITE State=UP
EOF
```

What the important lines do:

- `CPUs=12` with `SelectTypeParameters=CR_CPU`: each hardware thread is one slot. A
  test takes one slot.
- `CPUSpecList=10,11`: CPUs 10 and 11 stay out of Slurm. They are left for the desktop,
  the Claude sessions and work that does not go through Slurm. Jobs get the other 10
  slots. Remove `CPUSpecList=10,11` to give Slurm all 12.
- `Features=questa`: this machine has a Questa license.
  `regr_runner --srun_args="--constraint=questa"` sends tests only to machines with
  this feature.
- `ProctrackType=proctrack/cgroup`: Slurm finds a job's processes by their cgroup. A
  cancel then kills all of them, also Questa's `vlm`, which leaves the test's process
  group.
- `ReturnToService=2`: a machine that was down comes back by itself when its `slurmd`
  starts again.
- `InactiveLimit=120`: when an `srun` dies (for example, `regr_runner` was killed),
  Slurm ends its job after 120 s.
- `KillWait=10`: a cancelled job gets SIGTERM, and SIGKILL 10 s later.
- `MailProg=/usr/bin/true`: Slurm sends no mail. Without this line, `slurmctld` logs
  `Configured MailProg is invalid`, because no mail program is installed.

### Write /etc/slurm/cgroup.conf

```bash
sudo tee /etc/slurm/cgroup.conf > /dev/null <<'EOF'
CgroupPlugin=autodetect
ConstrainCores=yes
ConstrainRAMSpace=no
EOF
```

`ConstrainCores=yes` keeps each job on the CPUs that Slurm gave it. A job that starts
more threads than it has slots shares its own CPUs and does not slow the other jobs.

### Start

```bash
sudo systemctl enable --now munge
sudo systemctl enable slurmctld slurmd
sudo systemctl restart slurmctld slurmd
```

The packages try to start both daemons when they are installed. That fails, because
there is no slurm.conf yet. The restart starts them with it.

### Check

```bash
sinfo                                             # main*  up  infinite  1  idle  lab-lnx
srun hostname                                     # lab-lnx
scontrol show node lab-lnx | grep -E 'CPUEfctv'   # CPUEfctv=10 CPUTot=12
```

If a daemon does not start, read `sudo journalctl -u slurmctld -u slurmd -n 50` and the
logs in `/var/log/slurm/`.

## Running a regression through Slurm

```bash
python3 bin/runners/regr_runner.py --tests <regr.yaml> --name <run> \
    --launcher srun --srun_args="--constraint=questa" --max_parallel 40
```

- `--max_parallel` is how many tests `regr_runner` hands to Slurm at once. Slurm runs
  as many as there are free slots and keeps the others in its queue.
- `--timeout` counts from the moment a test starts on its machine. Time in the queue
  does not count.
- The first line of each test's `test.log` says where the test ran:
  `[regr_runner] started: host lab-lnx, Slurm job 29, session 3344136`.
- `squeue` lists the jobs. Each job name is `<test>.<seed>.r<regr_runner pid>.<n>`.
  `scancel <job id>` ends one. `regr_runner` treats a test that someone else cancelled,
  or whose machine failed, as a lost run and reruns it (`--reruns`).
- Ctrl-C on `regr_runner` cancels all its jobs, also the ones still in the queue.
- Every session that runs regressions should use `--launcher srun`. A session that does
  not use it runs outside Slurm's limit.

## Step 2: adding a machine

Do this for each new machine (the second PC, the laptop).

### What the machine needs

1. **The same Ubuntu release** (26.04), so that the same programs run.
2. **The same user name and UID.** Run `id idan` on both machines. Slurm runs a job
   with the same UID on the other machine.
3. **The same tools at the same paths.** Questa is one directory (7.5 GB):
   ```bash
   sudo mkdir -p /opt/altera_lite/25.1std && sudo chown idan /opt/altera_lite/25.1std
   rsync -a lab-lnx:/opt/altera_lite/25.1std/questa_fse /opt/altera_lite/25.1std/
   ```
   `test_runner` also needs `python3-yaml` and `python3-jinja2`
   (`sudo apt install python3-yaml python3-jinja2`). Copy the `PATH` and
   `SALT_LICENSE_SERVER` lines of `~/.bashrc`. srun passes the environment of the shell
   that started `regr_runner` to the job.
4. **Its own Questa license**, at the same path as on lab-lnx, if the machine should run
   Questa tests. Intel ties the license to the machine's NIC ID, so the license file of
   lab-lnx does not work there. A machine without a license gets no `Features=questa`.
5. **The work area at the same path.** Share `/home/idan/projects` from lab-lnx over
   NFS (next section).
6. **Names.** Every machine must find the others by name. Add them to `/etc/hosts` on
   every machine, or use the names from the router's DNS.
7. **Clocks** that agree within a few minutes. munge rejects older messages. Ubuntu's
   default `systemd-timesyncd` keeps the clocks right.

### NFS: share the work area

On lab-lnx:

```bash
sudo apt install nfs-kernel-server
echo '/home/idan/projects 192.168.1.0/24(rw,sync,no_subtree_check)' | sudo tee -a /etc/exports
sudo exportfs -ra
```

On the new machine (`/home/idan/projects` must be empty there):

```bash
sudo apt install nfs-common
mkdir -p /home/idan/projects
echo 'lab-lnx:/home/idan/projects /home/idan/projects nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
sudo mount /home/idan/projects
```

Connect a machine that runs tests by cable. A compile reads and writes many small
files, and NFS over Wi-Fi is slow for that.

### munge and Slurm on the new machine

```bash
sudo apt install slurmd munge
```

Every machine needs the same munge key. On lab-lnx:

```bash
sudo install -o idan -m 400 /etc/munge/munge.key ~/munge.key
scp ~/munge.key <new>: && rm ~/munge.key
```

On the new machine:

```bash
sudo install -o munge -g munge -m 400 ~/munge.key /etc/munge/munge.key && rm ~/munge.key
sudo systemctl restart munge
munge -n | ssh lab-lnx unmunge | grep STATUS      # STATUS: Success (0)
```

### Add the machine to slurm.conf

Run `slurmd -C` on the new machine. Add a line for it to `/etc/slurm/slurm.conf` on
lab-lnx, after the lab-lnx line. Take `CPUs` and `RealMemory` from `slurmd -C`. Add
`Features=questa` only if the machine has a Questa license. For example:

```
NodeName=newpc CPUs=32 RealMemory=63000 Features=questa State=UNKNOWN
```

`slurm.conf` and `cgroup.conf` must be the same on every machine. Copy both to
`/etc/slurm/` on the new machine. Then:

```bash
# on lab-lnx
sudo systemctl restart slurmctld slurmd
# on the new machine
sudo systemctl enable --now slurmd
```

Check on lab-lnx: `sinfo -N -o '%N %c %f %t'` lists both machines with their CPUs,
features and state. `srun -w newpc hostname` runs on the new machine.

If ufw is on, run `sudo ufw allow from 192.168.1.0/24` on every machine. srun receives
a test's output on ports that it opens itself, so the Slurm ports 6817 and 6818 alone
are not enough.

### The laptop

While you use the laptop yourself, stop new jobs from going to it:

```bash
sudo scontrol update nodename=<laptop> state=drain reason="in use"
```

The jobs that already run there finish. To give the laptop back:
`sudo scontrol update nodename=<laptop> state=resume`. When the laptop is off, Slurm
marks it down and sends it nothing. A test that ran on it when it went off is a lost
run, and `regr_runner` reruns it on another machine. `ReturnToService=2` brings the
laptop back when it starts again.

## Removing Slurm

```bash
sudo systemctl disable --now slurmctld slurmd munge
sudo apt remove slurm-wlm munge
```

`regr_runner` without `--launcher srun` runs everything on the local machine, as before.
