# Workloads and experiments

Each workload is a plain Pod manifest in `pods/`, runnable on its own with `kubectl apply`.
An experiment is a TOML file in `experiments/`. `kexp.py` copies the manifests it needs,
setting the name, namespace, CPU (and optionally memory) and arrival time, submits them on
schedule and collects what happened. Python standard library and kubectl only.

Images are upstream, pinned by multi-arch digest (the workers are arm64). Stress work is a
fixed operation count, calibrated on 2026-10-03 at 500m (bogo ops/s, Pi / Jetson): cpu 115 / 71,
vm 36,700 / 21,200, hdd 410 / 280, so a stress pod runs about a minute on the Pi.

```bash
python3 kexp.py list                      # every experiment, validated
python3 kexp.py manifests drs-even        # the exact pods, as a Kubernetes List
python3 kexp.py run smoke --out results   # on the master; KEXP_KUBECTL=kubectl elsewhere
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
regenerates what was planned), `submissions.json` (when each create request was sent), `decisions.jsonl` (the plugin's decision and binding lines for this run),
`telemetry.csv` (kubelet CPU per node every 5 s), `meta.json`, `report.md`.

The report gives:

| Metric | Boundary |
|---|---|
| Pods succeeded / failed / unfinished | final pod phase |
| Makespan | first pod created → last pod finished; only when every pod succeeded |
| Completion time per workload and per node (mean, p95, max) | pod created (API server) → container finished (kubelet) |
| Placement wait (median, p95, max) | create request sent (runner) → pod bound (plugin log), ms; both on the master's clock; includes the create round trip, reported alongside |
| Decisions, fallbacks, plugin time (median, p95) | the plugin's own millisecond timer: snapshot + decider call |
| CPU utilization per node (mean, peak) | kubelet usage / allocatable, refreshed by the kubelet every 10–15 s |
| Load imbalance | population std. dev. of the node mean CPU %. CPU only and over run means: DRS instead sums, over six resources, the weighted across-node std. dev. at each instant |

API server and kubelet timestamps have 1 s resolution and come from different machines'
clocks. Images are pulled on first use, so the first pod of a type on a node also pays the
download.
