# Kaptain

Plugs ML/RL/LLM placement policies into a real Kubernetes scheduling path, and measures what
each decision costs.

**Every policy is written in Python**, in `bridge/`. The Go side is the integration and
nothing else: a Scheduling Framework plugin that builds the snapshot, asks the decider over
HTTP, and turns the answer into scores. It is compiled against **Kubernetes v1.31.0**, which
must match `kind-cluster.yaml` and the scheduler image.

```
                    ┌──────────────── kube-scheduler ────────────────┐
                    │                                                │
 Pod ──► Filter ────┼──► PreScore ────────────────► Score ───────────┼──► Bind ──► PostBind
       (Kubernetes) │    snapshot, then one call    lookup, per node │ (Kubernetes)    │
                    │        │            ▲                          │           log where
                    └────────┼────────────┼──────────────────────────┘           it landed
                             │            │
                      POST /decide   scores, and the name
                             │       of what actually scored
                             ▼            │
                    ┌────────────────────────────┐
                    │  bridge/  Python decider   │
                    │  the policy: heuristic,    │
                    │  RL, LLM — all of them     │
                    └────────────────────────────┘
```

**We only score.** Filtering is Kubernetes' job and stays untouched, binding is Kubernetes'
job, and the plugin expresses its decision as a score, never as a constraint. It trains
nothing, reserves nothing, evicts nothing, preempts nothing.

`PreScore` is not a second decision: `Score` is called once per node, in parallel, so the
snapshot is built and the decider called once per scored pod there instead. With only one
eligible node, kube-scheduler selects it directly in Go and skips scoring and the Python
call. Its binding log has `scored: false`; no policy decision or latency is recorded.
`PostBind` records where the pod landed — on a score tie kube-scheduler picks at random.

## Where the timers sit

Go measures the complete decider call. Python's `handler_duration` measures only
`strategy.scores()`, excluding request parsing and response serialization.

```
   scheduler ├──────── kaptain_plugin_decider_duration_seconds ────────┤
             │                                                         │
             └─ serialise ─► HTTP ─►├── kaptain_decider_ ──┤◄─ HTTP ◄──┘
                                    │  handler_duration    │
                                    └──── decider ─────────┘

   non-policy overhead = decider_duration − handler_duration
```

This overhead includes serialization, HTTP, FastAPI dispatch and response decoding; it is
not a measurement of network latency alone. Compare means over the same successful calls,
not independently computed percentiles.

Measured on kind over nine decisions: **2.95 ms** caller-side against **0.13 ms** for the
policy, so about **2.8 ms of non-policy overhead**. Indicative, not a result — one machine,
two cold calls, nine samples.

## Run

Prerequisites: Docker, `kind`, `kubectl`, `uv`.

```bash
make all-plugin      # cluster + both images + deploy + demo pod
make verify-plugin   # where the pod landed, and who scheduled it
make logs-plugin     # one JSON line per decision
make logs-decider
make down
```

The scheduler refuses to start without a decider, so `deploy-plugin` brings up both.

```bash
make metrics-plugin    # kaptain_plugin_* on the scheduler
make metrics-decider   # kaptain_decider_* on the decider
```

Plugin environment: `KAPTAIN_DECIDER_URL` (**required**) and `KAPTAIN_DECIDER_TIMEOUT`,
`KAPTAIN_STRATEGY` (the run's declared identity, and the metric label), `KAPTAIN_SEED`,
`KAPTAIN_TELEMETRY` (`kubelet` or `off`), its refresh interval and max age, `RUN_ID`,
`POLICY_VERSION`.

Measured node usage comes directly from each kubelet's `/stats/summary`, through the API
server proxy. The scheduler caches these samples in the background; a scheduling decision
never calls a kubelet. The prototype's `nodes/proxy` permission is broad and can expose other
kubelet proxy endpoints.

Decider environment: `STRATEGY`, and nothing else — it reads no cluster state, because
everything the policy needs arrives in the request body. It reports on every `/decide` the
name of what actually ran, and a disagreement with `KAPTAIN_STRATEGY` is counted in
`kaptain_plugin_strategy_mismatch_total` rather than hidden.

**Durations** accept a Go duration string (`2s`, `500ms`) or a bare number of seconds, and an
unparseable value fails at startup rather than becoming a default.

## Add a policy

Strategies live in `bridge/strategies/`, one class per file extending the
`SchedulingStrategy` ABC. A strategy is a pure function of the snapshot: no cluster call, no
state, no training. Adding one needs **no Go change and no scheduler rebuild**.

```bash
# edit STRATEGY in k8s/decider.yaml and KAPTAIN_STRATEGY in k8s/scheduler-plugin.yaml to match
kubectl apply -f k8s/decider.yaml
kubectl apply -f k8s/scheduler-plugin.yaml
```

## Layout

```
plugin/      the integration, Go — module `kaptain`, not published
  scoring/     extension points, decision, fallback, metrics
  snapshot/    the wire contract with the decider
  decider/     the HTTP client
  telemetry/   measured node usage, refreshed in the background
bridge/      the decider, Python — every policy lives here
  strategies/  one class per policy
k8s/         manifests
```

`docs/` and `experiments/` are on disk but not committed for now.

## Three rules

**A fallback is a result.** A policy that crashes, times out, or names a node outside the
candidate list must never become an invisible random baseline. The plugin validates every
answer and falls back explicitly — highest free-request ratio, then most allocatable CPU,
then node name. That rule is **in Go, because it runs exactly when the decider is what
failed**. It is logged, counted, and kept in the results.

**Absence is declared.** A value that cannot be read is `missing`, never zero. Two ages,
never one: requested resources and measured usage go stale at different rates.

**`intended_node` is not the placement.** On a score tie kube-scheduler picks at random, so
`PostBind` logs the node actually bound, beside `tie_count`.

```json
{"run_id": "unset", "integration": "plugin", "strategy": "dummy-random",
 "decider_strategy": "dummy-random", "intended_node": "worker-2", "tie_count": 1,
 "fallback": false, "durations_ms": {"snapshot": 0.012, "decider": 1.41, "total": 1.43}}
```

`fallback_reason` is `decider_error`, `invalid_score`, `invalid_choice` or `no_candidates`.
Set `RUN_ID` and `POLICY_VERSION` so the lines can be joined with a run.

## Testbed

kind runs every node as a container on **one** machine: right for wiring, fallbacks and
decision-cost pilots, wrong for placement-performance claims. The `hw-class`/`cpu-gen` labels
are cosmetic. Main results need separate Linux VMs, ideally one per node.

## Checks

```bash
make check            # gofmt, go vet, and every manifest through the Kubernetes YAML decoder
```

There is no test suite: this is a research prototype. With one implementation of each policy
there is also no parity to keep.

The extender this project used to carry as a second integration was measured against the
plugin, lost, and was removed. It is frozen on `archive/extender`.
