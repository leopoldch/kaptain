# Workloads and experiments

Each workload is a plain Pod manifest in `pods/`, runnable on its own with `kubectl apply`.
An experiment is a TOML file in `experiments/`. `kexp.py` copies the manifests it needs,
setting the name, namespace, CPU (and optionally memory) and arrival time, submits them on
schedule and collects what happened. Python standard library and kubectl only.

Images are upstream, pinned by multi-arch digest (the nodes are amd64 and arm64). Stress work is a
fixed operation count, calibrated on 2026-10-03 at 500m (bogo ops/s, Pi / Jetson): cpu 115 / 71,
vm 36,700 / 21,200, hdd 410 / 280, so a stress pod runs about a minute on the Pi.

```bash
python3 kexp.py list                      # every experiment, validated
python3 kexp.py manifests drs-even        # the exact pods, as a Kubernetes List
python3 kexp.py run smoke --out results   # on the master; KEXP_KUBECTL=kubectl elsewhere
python3 kexp.py cleanup                   # delete namespaces left by killed runs
```

On GitHub: **Actions → Run experiment → Run workflow**, choose the experiment and optionally a
policy to deploy first. The report lands in the run summary, the raw files in the artifact.

## Experiment file

```toml
name = "drs-even"
seed = 7                 # same seed, same pods, same arrival times
description = "..."
source = "Jian et al. 2024 (DRS), section 5.2"
count = 60
timeout_s = 3600         # the whole run

[arrival]                # fixed: interval_s | normal: mean_s, stddev_s | poisson: mean_s
process = "fixed"        # bursts: steady_count, steady_interval_s, bursts, burst_size, burst_gap_s
interval_s = 20

[mix]                    # integer weights of the manifests in pods/, repeated then shuffled
cpu = 1
memory = 1
disk = 1

[resources]              # uniform draws; requests = limits
cpu_millis = [200, 500]
memory_mib = [64, 1024]  # optional, otherwise the manifest's value
```

| Experiment | Taken from | Departures |
|---|---|---|
| `drs-even`, `drs-random`, `drs-cpu` | Jian et al. 2024 (DRS) §5.2: 1:1:1 or 4:1:1 mix, 20 s or N(20, 1) s arrivals, 200–500m CPU | stress-ng instead of DRS's amd64-only images; memory replaces network; 60 pods, not 300 |
| `packing-random` | Christensen et al. 2025: pods with random CPU and RAM | ranges and Poisson rate are ours |
| `decision-cost` | Zhou et al. 2026: no-op pods | shape of the September pilot |
| `web` | Zhou et al. 2026: NGINX services | 60 s lifetime, Poisson rate are ours |
| `smoke` | pipeline check only | — |

## Output

`results/<experiment>-<run>/`: `pods.json` (what Kubernetes did; `kexp.py manifests`
regenerates what was planned), `watch.csv` (every pod change streamed by the API, timed by the runner; an `ENDED` row if the stream stopped before the run), `decisions.jsonl` (the plugin's decision and binding lines for this run),
`telemetry.csv` (kubelet CPU and memory working set per node every 5 s, later when a busy kubelet is slow to answer;
each with the kubelet's own timestamp), `network.csv` (from the same answers: cumulative bytes received and sent, for every
interface, since the one carrying the cluster's traffic differs between nodes), `meta.json` (with each node's allocatable
CPU and memory), `report.md`.

The report gives:

| Metric | Boundary |
|---|---|
| Images | scheduler and decider images actually running, by digest; flagged when the scheduler image is not tagged with a commit |
| Pods succeeded / failed / unfinished | final pod phase |
| Makespan | first pod created → last pod finished; only when every pod succeeded |
| Completion time per workload and per node (mean, p95, max) | pod created (API server) → container finished (kubelet) |
| Placement wait (median, p95, max) | pod first seen → first seen with a node, in the API watch stream, both timed by the runner on the master, ms |
| Decisions, fallbacks, plugin time (median, p95) | the plugin's own millisecond timer: snapshot + decider call |
| CPU and memory utilization per node (samples, mean, peak) | kubelet CPU usage and memory working set / allocatable, refreshed by the kubelet every 10–15 s; memory `missing` for runs before 2026-10-09 |
| Load imbalance | population std. dev. of the node mean CPU %. CPU only and over run means: DRS instead sums, over six resources, the weighted across-node std. dev. at each instant |

API server and kubelet timestamps have 1 s resolution and come from different machines'
clocks. Images are pulled on first use, so the first pod of a type on a node also pays the
download.

The first pod is submitted only once the watch has streamed a probe pod, `kexp-watch-probe`,
which names no existing scheduler, so it never runs, and is deleted at once.

Placement statistics require an unbound `ADDED` event followed by a bound event for every
expected pod. Otherwise the report shows `missing` or `incomplete` with the coverage (n/N pods),
without placement statistics. A watch that stops early is not resumed, since replayed events
would be timed when they arrive; the report says so. Plugin time is `missing` when no decisions
were logged.
A run that fails partway is still reported: status `aborted` with the error, and whatever
could not be collected shows as `missing`.
A cancelled run (SIGTERM, Ctrl-C) stops at once: its files are collected, with status `aborted`
and error `cancelled`, but no report.

## Plots

`kplot.py` draws one or more runs, locally: it needs matplotlib, which `uv` installs from the
script header, while `kexp.py` stays standard library only. It takes run folders, or any folder
containing them, such as downloaded artifacts:

```bash
gh run download <run-id> -D runs/       # once per workflow run
uv run kplot.py runs/ --out results/plots
```

Runs are sorted by experiment, then strategy, and labelled by strategy, plus the run id when a
strategy appears more than once. A strategy keeps its colour across figures and invocations.

| Figure | Shows |
|---|---|
| `makespan.png` | one bar per run, as in the report: only when every pod succeeded |
| `completion.png` | every succeeded pod's completion time, one panel per workload, coloured by the node it ran on |
| `pods-per-node.png` | how many pods each node ran |
| `placement-wait.png` | every pod's placement wait per run, with the box of its quartiles and the coverage (n/N pods) |
| `plugin-time.png` | the plugin's time per decision (snapshot + decider call), with the fallbacks counted |
| `imbalance.png` | std. dev. of CPU % across nodes at each instant, every 5 s, each node holding its last reading |
| `cpu.png`, `memory.png` | one panel per run, CPU or memory working set % of allocatable per node; dashed where the last pod finished. No `memory.png` when no run has memory samples |

Distributions switch to a log scale when the values span more than 20×. Time runs from the first
pod created, and CPU is drawn at the kubelet's timestamp, each reading once: sampled every 5 s, a
kubelet that refreshes every 10–15 s answers the same reading twice.

The imbalance is DRS's Imbalance_t restricted to CPU, at each instant, where the report gives the
std. dev. of the run means: two nodes busy in turn balance out in the latter, not in the former.
`--imbalance-without alex-master` (repeatable) leaves a node out of it, such as the master,
whose control plane keeps it busy whatever the policy.

## Isolation

Each run gets its own namespace, labelled `kaptain.io/experiment`, with a deny-all
NetworkPolicy. Pods run as non-root, without a ServiceAccount token, with every capability
dropped and the default seccomp profile. A run deletes its namespace, and waits for it, when it
ends: only the one carrying its random `kaptain.io/run-owner` label, so a reused run id cannot
make it delete another run's. It refuses to start while any labelled namespace exists, which
may be another run's. A live run renews a `kaptain.io/heartbeat` annotation every minute;
`kexp.py cleanup`, which the workflow calls before deploying a policy, deletes only the
namespaces silent for more than 5 minutes, those of killed runs. A run also refuses to start
unless the experiment nodes are idle: no pod outside `kube-system` on one of them, and no pod
using `kaptain-scheduler`. Background load, if ever wanted, has to be part of the experiment.

Experiments run on the nodes labelled `kaptain.io/experiment-node=true`, and only there: the pods
select that label and tolerate the control-plane taint, and `kexp.py` measures and checks the
same nodes. Since 2026-10-09 these are `alex-master`, `leopold-raspberrypi` and
`leopold-pve-worker-1` to `-3`; `alex-jetson` is left out. The master also runs the control plane,
Kaptain's scheduler and decider, the runner and `kexp.py` itself: its measured load includes them.

Seeded policies key on `kaptain.io/task-id = <experiment>/<pod>`, so the same seed places the
same pods the same way in every run.

Known limits (2026-10-03): the NetworkPolicy is enforced on `leopold-raspberrypi` but not on
`alex-jetson`, whose K3s agent does not apply it. The scheduler image is built by hand, not
by the deployment workflow, so the report can only flag that it is not tied to a commit.
