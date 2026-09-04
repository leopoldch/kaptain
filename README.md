# Kaptain

A Kubernetes scheduler extender that plugs ML/LLM placement strategies into a
real cluster, so we can compare them against the default scheduler.

We use an HTTP extender because our strategies are Python ML/LLM models. This is
not a shortcut: even work that integrates via the Scheduling Framework runs the
model in a separate process and calls out to it — [Christensen et al. 2025](https://arxiv.org/abs/2511.08373)
invoke an OR-Tools Python solver over an HTTP API, and [DRS (Jian et al. 2024)](https://doi.org/10.1002/spe.3284)
run their DQN in a decision maker reached over sockets. The HTTP boundary also
gives a clean point to measure decision latency.

## Run

Prerequisites: Docker, `kind`, `kubectl`, `uv`.

```bash
cd kaptain
make all       # create cluster + build + deploy + run demo pods
make verify    # show where each pod was scheduled
make logs      # watch the extender being called
make down      # tear down
```

## Switch strategy

Strategies live in `bridge/strategies/` (one class per file, extending the
`SchedulingStrategy` ABC). Pick the active one via the `STRATEGY` env var in
`k8s/extender.yaml`, then:

```bash
make build load && kubectl -n kube-system rollout restart deploy/scheduler-extender
```
