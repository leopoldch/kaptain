# DRS reproduction

Runs DRS (Jian et al. 2024) on a Kaptain cluster. The pieces live in the repository: the `drs`
policy and its baselines in `bridge/strategies/`, the DRS monitor in `monitor/`, the authors' apps
in `workloads/pods/` (`video`, `net`, `drsdisk`) and the experiments in `workloads/experiments/`
(`calib-*`, `drsA-*`, `drsB-*`). This folder sets up the cluster and runs them.

## Requirements

- Kaptain running (`kaptain-scheduler` and `kaptain-decider` in `kube-system`). `../deploy-policy.sh`,
  which every run calls, builds the decider image with Docker and imports it with `ctr`: set
  `KUBECTL`, `KAPTAIN_CTR` and `KAPTAIN_MASTER` in `cluster.env` (its defaults are our setup, K3s
  in the Docker container `kaptain-k3s`).
- The workers, and only them, labelled `kaptain.io/experiment-node=true`:
  `kubectl label node <worker> kaptain.io/experiment-node=true`.
- Python 3.11+ and bash 4.4+ on the host running the scripts (not macOS's bash 3.2), Docker on
  the host of the network server.

## Configure

Run every command from this folder, on a host that can run `kubectl` and `../deploy-policy.sh`.

```bash
cp cluster.env.example cluster.env   # then edit it: kubectl command, network server, interfaces, disks
```

## Set up the cluster (once)

| Step | Command | Why |
|---|---|---|
| 1. Video | `python3 -I setup/extract-video.py` then `bash setup/install-video.sh` | The `video` pods read the authors' `input.mp4` from `/opt/drs` on their node. The video is not in the repository: `extract-video.py` downloads it from the authors' image `jolyonjian/apps:cpu-1.0` on Docker Hub (internet access only, no Docker needed), checks its sha256 and writes `input.mp4` here |
| 2. Network server | on a host the workers reach: `cd net-server && NET_SERVER_BIND=<ip> NET_SERVER_PORT=<port> docker compose up -d` (port 5301 by default) | The `net` pods talk to it, at `DRS_NET_SERVER` |
| 3. Images | `bash setup/preload-images.sh` | No run should measure a pull (ffmpeg weighs several hundred MB). Presence is read from the node status, which lists 50 images at most: on a worker with more, check by hand |
| 4. Monitors | `bash setup/apply-monitors.sh` | DRS's state; they run in every arm. Its output names the interface and disk measured on each worker |
| 5. Check | `bash preflight.sh calibration --variant A --with-pods` | Everything above, from the cluster's side |

## Calibrate

DRS normalises each metric by a cap and describes each app by a vector, both measured on the
bench. `calibrate.sh` runs each app alone on each worker, and `make-config.py` derives them.

```bash
bash calibrate.sh A                                   # about 40 min per worker
python3 make-config.py --variant A results/           # writes drs-config-A.json
bash setup/apply-drs-config.sh drs-config-A.json
```

For the authors' 8.2 GB disk task: `MIN_FREE_GB=20 APPS="drsdisk-8g" bash calibrate.sh A`, then
`python3 make-config.py --variant A --disk-8g results/`. Variant B (stress-ng apps) works the same way
with `B`.

The other settings come from `../k8s/drs-config.template.json`: the hyper-parameters of the
authors' code and the paper's reward (`alpha` 1, `beta` 10). In our runs we changed two of them, by editing the JSON before applying it: `alpha` 100 and `beta`
1000 (the paper's reward in percent instead of fractions), and `memory_capacity` and
`target_replace_iter` 20 instead of 100, so the agent learns from the 21st pod.

## Run

Each run takes about 2 h (300 pods, one every 20 s): use `tmux`. `drsA-8g-even` writes 16.4 GB
per disk pod: run it with `MIN_FREE_GB=20`.

```bash
bash run.sh drs drsA-even                   # one arm
bash run-arms.sh drsA-even                  # drs-greedy, dummy-random, round-robin, kube-scheduler, drs
DRS_RUNS=3 bash run-arms.sh drsA-even drs   # one agent learning over 3 x 300 pods
```

`run.sh` deploys the policy before each run, so every `drs` run starts from a fresh agent unless
`KEEP_AGENT=1`. Each run writes `results/<experiment>-<arm>-<time>/`: kexp's files (`meta.json`,
`pods.json`, `watch.csv`, `telemetry.csv`, `decisions.jsonl`, `report.md`), the monitors' and the
decider's logs (`monitor.jsonl`, `decider.jsonl`), and `netserver.jsonl` when the network server
runs on the same host.

| Experiment | Content |
|---|---|
| `drsA-even`, `drsA-random`, `drsA-cpu` | The paper's three workloads with the authors' apps: video/net/disk at 1:1:1 or 4:1:1, one pod every 20 s or N(20 s, 1 s) |
| `drsA-8g-even` | `drsA-even` with the 8.2 GB disk task |
| `drsB-even`, `drsB-random`, `drsB-cpu` | The same workloads with stress-ng apps (cpu, memory, disk) |
| `drsA-pilote` | 30 pods, to check the whole chain before a long run |
| `calib-<app>` | One app alone on the `--node` worker, three times |
| `calib-idle-monitor`, `calib-idle-no-monitor` | 10 idle minutes with and without the monitors (`setup/apply-monitors.sh --remove`), for the monitor's CPU overhead |
