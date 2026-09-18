# Kaptain

Kaptain compares two ways of plugging ML/RL/LLM placement strategies into a real
Kubernetes scheduler path:

- an **HTTP scheduler extender**, available today, convenient for Python strategies and
  local LLM calls;
- a **Go Scheduling Framework plugin** (`plugin/`), available today, so we can measure
  whether moving the policy in-scheduler reduces overhead or improves reliability.

Both paths run the same strategies on the same snapshot and apply the same fallback rule,
and every result line and metric sample carries `integration=extender|plugin`. The shared
fixtures in `testdata/snapshots/` are checked by both test suites, so a strategy cannot
drift on one side only. The plugin is compiled against **Kubernetes v1.31.0**, which must
match `kind-cluster.yaml` and the scheduler image.

[DRS (Jian et al. 2024)](https://doi.org/10.1002/spe.3284) similarly separates its
scheduler from a DQN decision maker, using sockets. Our HTTP extender is a different
transport whose cost must be measured. The Go plugin gives us the comparison point where
the scheduler integration itself changes while the placement policy stays as close as
possible. Caller-side timing captures the RPC in the extender path; server-side handler
timing alone does not capture transport or scheduler queueing.

## Design notes

[The project decision README](../README.md) records the current choices by RQ, model
training, local LLM and conditional fine-tuning, observability, and open dataset selection.
It is the September 13, 2026 decision summary for the presentation and implementation.

[The evaluation protocol below](#evaluation-protocol) defines the planned comparisons,
metrics and baselines. These experiments are not implemented yet. The planned Go plugin is
described in [`docs/plugin-design.md`](docs/plugin-design.md).

[`docs/related-work.md`](docs/related-work.md) is the running record of the project's reasoning, kept up to date as
decisions are made. It holds the literature review (7 research papers and a survey), and — the part
that governs day-to-day work here:

- **what we compare to today and what we should compare to**, with each candidate baseline
  mapped to the paper it comes from and what it would cost to add;
- **how to measure each metric** — where the number comes from and what makes it wrong;
- **testbed constraints** — what this hardware can and cannot support a claim about;
- **a code audit** of this repo, and the current priority list.

Read it before adding a strategy or reporting any number.

## Run

Prerequisites: Docker, `kind`, `kubectl`, `uv`.

```bash
cd kaptain
make all       # create cluster + build + deploy + run demo pods
make verify    # show where each pod was scheduled
make logs      # watch the extender being called
make down      # tear down
```

Those targets exercise the extender path. The plugin path has its own:

```bash
make build-plugin load-plugin deploy-plugin   # compile, load into kind, deploy
make demo-plugin                              # one pod with schedulerName: kaptain-scheduler
make verify-plugin                             # where it landed, and who scheduled it
make logs-plugin                               # one JSON line per decision
make metrics-plugin                            # the kaptain_plugin_* metrics
```

Run **one arm at a time**: pods scheduled concurrently by different schedulers compete for
the same nodes and confound the comparison. That is why the demo workloads are separate
files.

### Choosing a path

| | Extender (path A) | Plugin (path B) |
|---|---|---|
| Scheduler | `ml-scheduler` (`k8s/scheduler.yaml`) | `kaptain-scheduler` (`k8s/scheduler-plugin.yaml`) |
| Pod selects it with | `schedulerName: ml-scheduler` | `schedulerName: kaptain-scheduler` |
| Policy runs in | the Python service, over HTTP | the scheduler process, or an external decider |
| Strategy switch | `STRATEGY` env in `k8s/extender.yaml` | `KAPTAIN_STRATEGY` env in `k8s/scheduler-plugin.yaml` |
| Default strategy | `dummy-random` — **the two must match**, or the comparison measures the policy | `dummy-random` |
| Good for | fast iteration, ML/LLM in Python | decision-cost measurement close to the scheduler |

Plugin environment: `KAPTAIN_STRATEGY` (`dummy-random`, `largest-cpu-capacity`,
`least-allocated`, `least-used`), `KAPTAIN_SEED`, `KAPTAIN_DECIDER_URL` and
`KAPTAIN_DECIDER_TIMEOUT` (empty URL keeps the policy local), `KAPTAIN_RESERVATION_TTL`,
`KAPTAIN_TELEMETRY` and its refresh and max-age, `RUN_ID`, `POLICY_VERSION`.

**Durations** accept a Go duration string (`2s`, `500ms`) or a bare number of seconds (`2`)
in **both** deployments, and an unparseable value fails at startup rather than falling back
to a default — a run must never believe it used a setting it never had.

Extender environment: `STRATEGY`, `KAPTAIN_SEED`, `RUN_ID`, `POLICY_VERSION`, plus
`KAPTAIN_REQUESTS_SOURCE=api|none` with `KAPTAIN_REQUESTS_REFRESH` and
`KAPTAIN_REQUESTS_MAX_AGE`. With `api`, a background thread aggregates the requests of the
bound pods per node, so the extender sees what the plugin reads from the scheduler cache;
a decision never calls the API, it reads the last snapshot and reports its age. Stale data
beyond the max age is refused, which shows up as an explicit fallback.

**Replaying identical snapshots.** On a live cluster the two paths do not only differ by
their integration: the plugin sees its own reservations in the scheduler cache, while the
extender refreshes bound pods every `KAPTAIN_REQUESTS_REFRESH` seconds and learns about
other schedulers' placements only at the next refresh. During a burst, successive decisions
can therefore be taken on different views of the same cluster, and the staleness threshold
does not fix that — it only makes it visible. Two consequences:

- replaying the same recorded snapshot through both paths — `make replay-plugin` (the
  `kaptain-replay` binary) and `make replay-extender` (`POST /replay`) — pins **parity**:
  identical snapshot in, identical scores and identical winner out, checked by the shared
  fixtures. Its duration is the **policy computation** alone: `kaptain-replay` runs outside
  kube-scheduler and `/replay` skips the extender protocol, so neither number contains the
  HTTP transport, the JSON serialisation of the NodeList, or the Scheduling Framework
  overhead. It is the right measurement for inference cost and decision stability, and the
  wrong one for the cost of an integration.
- the **integration overhead** can only be measured in cluster, from the scheduler's side,
  and with the right metric. Extenders are called from `findNodesThatPassExtenders` and
  inside `prioritizeNodes`, both **outside** the framework extension points, so
  `scheduler_framework_extension_point_duration_seconds` does *not* contain the extender
  round trip — it covers the plugin's work and, on the extender arm, only the native
  plugins. The metric comparable across both paths is
  **`scheduler_scheduling_algorithm_duration_seconds`** (filter, extenders and scoring),
  with `scheduler_scheduling_attempt_duration_seconds` and
  `scheduler_pod_scheduling_sli_duration_seconds` beside it. The protocol is in
  [`experiments/README.md`](experiments/README.md).
- in cluster, report the freshness difference rather than assume it away: each extender
  decision logs `requests_age_ms`, which is 0 in the plugin because the scheduler cache is
  current. The extender holds its own in-flight decisions until the API refresh observes
  that exact pod **on a node**. Neither weaker rule works: releasing by timestamp, or as
  soon as the pod exists in the API, both free the node while the pod is still Pending. The
  TTL is only the backstop for a pod that never gets placed.
- one thing the extender cannot hold at all: a placement it only guessed. It scores, the
  scheduler places, and on a tie Kubernetes picks among the top nodes at random — so when
  `tie_count > 1` the extender reserves nothing and logs `reserved: false` with
  `reserved_skipped_reason: "tie"`, rather than charging a load to a node that may never
  receive the pod. The plugin has no such problem: its `Reserve` is called by the framework
  with the node actually selected.

**Telemetry age.** `metrics_age_seconds` is the age of the **measurement**, taken from the
per-node `timestamp` the metrics API sends, not from the moment we downloaded it.
metrics-server serves a cached sample, so dating it from the fetch would let a strategy rank
on minutes-old numbers while reporting an age of milliseconds. `KAPTAIN_TELEMETRY_MAX_AGE`
applies per node against that timestamp, and a sample with no usable timestamp is refused
outright: an age that cannot be known cannot be checked.

**Resource quantities.** Both paths parse quantities the way Kubernetes does, rounding
towards +infinity: `123456789n`, which is how the metrics API reports CPU, is **124**
millicores, not 123. The extender uses exact rationals rather than floats for this.
`testdata/quantities.json` is generated by apimachinery itself (`plugin/hack/qcheck`) and
checked by the extender's tests.

**Pod requests.** Both paths compute a pod's effective requests with the same rule, and it
is not a plain sum: the plugin calls `PodRequests` from
`k8s.io/kubernetes/pkg/api/v1/resource`, and `bridge/snapshot.py` ports that algorithm —
regular containers add up, restartable init containers (sidecars) add up and also count
towards later init containers, ordinary init containers contribute a maximum, and pod
overhead is added last. `testdata/pod-requests.json` pins both implementations against the
same cases.

**What is comparable, and what is not.** The four strategies run on both paths with the
same snapshot, the same request calculation and the same quantisation, so the integration is
the only difference. `GET /healthz` lists the features the extender actually has, and the
plugin logs the same list at startup: record both with a run.

What the extender still cannot measure is the **transport** — the scheduler-side cost of
calling it. That number comes from kube-scheduler's own
`scheduler_framework_extension_point_duration_seconds` and must be reported next to ours,
never replaced by the handler duration. This is the one asymmetry left between the paths,
and it is the reason the plugin exists.

**Score scale.** Kubernetes caps an extender score at `MaxExtenderPriority` (10) and then
multiplies it by 10. Both paths therefore quantise the raw scores onto 0..10 with identical
arithmetic, and the plugin applies the same ×10 itself. This is deliberate: a finer scale
in the plugin would let it break ties the extender cannot express, and the two paths would
place the same pod differently. `testdata/quantisation.json` pins the rule case by case and
is checked by both test suites.

**Seeded draws.** `dummy-random` keys on a task identity that survives pod recreation: the
`kaptain.io/task-id` label or annotation, else `namespace/name`, never the pod UID, which
changes on every recreation. Set the label in the runner to replay an experiment exactly.

### What the plugin does, and does not

It implements `PreScore` (snapshot and decision once per pod), `Score` (a lookup, so the
cost lands in the right phase), `NormalizeScore` (rescale into the Kubernetes range) and
`Reserve`/`Unreserve` (account for pods chosen but not yet visible in the cache, with a
TTL so a lost `Unreserve` cannot leak).

`Filter` is deliberately untouched: admissibility stays Kubernetes' job. **The plugin
expresses its decision as a score, never as a constraint.** DRS does the opposite — its
`dqn-plugin` is a `Filter` that marks every node but the chosen one unschedulable, which
makes the scoring phase inert. No model is trained inside kube-scheduler, and the plugin
never evicts, preempts, migrates or autoscales.

Metrics are registered on the scheduler's own `/metrics`: `kaptain_plugin_score_duration_seconds`,
`_snapshot_duration_seconds`, `_decider_duration_seconds`, `_normalize_duration_seconds`,
`_candidate_nodes`, `_decisions_total`, `_fallback_total`, `_invalid_decision_total`,
`_cache_age_seconds`, `_missing_features_total`. The three latencies are separate on
purpose: total decision, external decider call, and normalisation. Labels stay
low-cardinality — no pod UID, no node name, no image; those go to the decision log.

### Testbed limits

kind runs every Kubernetes node as a container on **one** physical machine, so nodes share
CPU, memory bandwidth, disk and network. It is right for wiring, fallbacks, logs and
decision-cost pilots, and wrong for placement-performance claims: per-node network and disk
figures are barely interpretable, and the `hw-class`/`cpu-gen` labels in
`kind-cluster.yaml` are cosmetic, not real heterogeneity. **Main results must come from
separate Linux VMs, ideally one VM per Kubernetes node**, or from bare metal with verified
cpuset isolation. Always report `integration`, the Kubernetes version and the testbed with
any number.

## Switch strategy

Strategies live in `bridge/strategies/` (one class per file, extending the
`SchedulingStrategy` ABC). Pick the active one via the `STRATEGY` env var in
`k8s/extender.yaml`, then:

```bash
make build load && kubectl -n kube-system rollout restart deploy/scheduler-extender
```

## Fallback and decision log

A strategy that crashes, or that names a node outside the candidate list, must never become
an invisible random baseline. Both integrations validate every answer and, on failure, apply
the same explicit fallback: highest free-request ratio, then most allocatable CPU, then node
name (`bridge/fallback.py`, `plugin/pkg/kaptain/fallback.go`). It needs no telemetry, since
it runs exactly when something else failed. In the extender path per-node requests are
unknown, so the first criterion is flat and capacity decides; that is a consequence of the
extender protocol, not a different policy. Each decision emits one JSON line:

```json
{"run_id": "unset", "integration": "extender", "strategy": "dummy-random",
 "pod_name": "demo", "candidate_count": 3, "chosen_node": "worker-2",
 "fallback": true, "fallback_reason": "strategy_error:ValueError",
 "durations_ms": {"strategy": 0.3, "total": 0.4}}
```

**Two ages, never one.** Requested resources and measured usage come from different sources
at different rates, so each decision reports `requests_age_ms` and `telemetry_age_ms`
separately, and `cache_age_seconds` is labelled by source. A single mixed age cannot say
which input was stale. In the plugin the requests age is zero by construction: they come
from the scheduler cache.

**`intended_node` is not the placement.** It is the node our policy preferred. When several
nodes end up with the same normalised score, kube-scheduler picks among them at random, so
`tie_count` says how many shared the top score, and in the plugin a second line
(`event: "binding"`) records the node actually bound, from `PostBind`, plus
`kaptain_plugin_bound_mismatch_total`. The extender cannot observe the binding at all: for
that path, the actual node must be read from the pod or its events by the runner.

The reported `duration_ms` covers the whole decision — snapshot, policy, per-node scoring
and normalisation — because the line is written once normalisation has run.

`fallback_reason` is `strategy_error:<ExceptionType>`, `invalid_score` or `invalid_choice` in
the extender, and `strategy_error`, `decider_error`, `invalid_score`, `invalid_choice` or
`no_candidates` in the plugin. Extender counters are served at `GET /stats`; plugin counters
are Prometheus metrics. A fallback counts as a decision of the method under test and must not
be dropped from results. Set `RUN_ID` and `POLICY_VERSION` in both deployments so the lines
can be joined with a run.

Tests: `make test` runs both suites (`make test-extender`, `make test-plugin`).

The plugin uses the same contract at the data boundary: pod, candidate nodes, telemetry
snapshot, policy version and fallback rule (`plugin/pkg/snapshot`, mirrored by
`bridge/snapshot.py`). It can implement a policy locally in Go or call the same
out-of-process decider as the extender, with a timeout. Results must record
`integration=extender|plugin`.

## Documentation

| Question | Document |
|---|---|
| How is this put together, and why two paths? | [docs/architecture.md](docs/architecture.md) |
| What do we compare, and how do we measure it? | [docs/measurement-protocol.md](docs/measurement-protocol.md) |
| What is the first experiment? | [docs/experiments/pilot-integration-cost.md](docs/experiments/pilot-integration-cost.md) |
| Why is it built this way? | [docs/decisions/](docs/decisions/) |
| What does the literature establish? | [docs/related-work.md](docs/related-work.md) |
| What does the code actually do today? | [docs/code-audit.md](docs/code-audit.md) |
| What is next? | [docs/roadmap.md](docs/roadmap.md) |

Start from [docs/README.md](docs/README.md), which maps the whole set.
