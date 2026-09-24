# Kaptain — archive/extender

**Frozen. Not maintained.** This branch is the HTTP scheduler extender, kept whole and
runnable after it was removed from the project. Development continues on the plugin, which
is the only integration there.

```
                        ┌──────── ml-scheduler ────────┐
                        │                              │
   Pod ──► Filter ──────┼──► prioritizeNodes ──┐       ├──► Bind
         (Kubernetes)   │   (native scorers    │       │
                        │    all disabled)     │       │
                        └──────────────────────┼───────┘
                                               │  HTTP + JSON of the whole NodeList
                                               ▼
                             bridge/  POST /prioritize  ──► scores 0..10
                                      Python, + a background
                                      Kubernetes API refresh
```

The extender protocol carries no per-node requested resources, which is why `bridge/` has its
own API refresh (`KAPTAIN_REQUESTS_SOURCE=api`) and reports a real `requests_age_ms` on every
decision. The plugin reads the same numbers from the scheduler cache, for free.

## Why it was removed

At equal policy and with identical placements, over 40 runs and 2 400 pods on kind:

| | extender | plugin |
|---|---|---|
| `scheduling_algorithm_duration_seconds`, mean | **3.20 ms** | **0.41 ms** |
| identical placements | 600 / 600 | 600 / 600 |
| fallbacks | 0 | 0 |

The gap is the integration, not the policy. The plugin was kept; the extender was kept here
only after the plugin learned to call an out-of-process decider over HTTP, which covered the
one thing the extender was still needed for — running a policy that cannot live in Go.

## What is not here

`plugin/`, the plugin manifests, and every `*-plugin` Make target went with the plugin. Two Go
tools went with them and have no replacement on this branch:

- `make lint-manifests` — manifests here are no longer parsed by the Kubernetes YAML decoder;
- `make quantities` — `testdata/quantities.json` is kept, but cannot be regenerated here.

`docs/` and `experiments/` are not committed on any branch.

## Run

```bash
make all       # cluster + build + deploy + demo pod
make verify    # where the pod landed
make logs      # watch the extender being called
make down

make test      # 54 Python tests
```

Kubernetes **v1.31.0**, which must match `kind-cluster.yaml`. kind runs every node as a
container on one machine: right for wiring and decision-cost pilots, wrong for
placement-performance claims.
