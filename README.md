# Kaptain

Kaptain plugs ML/RL/LLM placement strategies into a real Kubernetes scheduler path and
measures what each decision costs. The integration is a **Go Scheduling Framework plugin**
(`plugin/`), compiled against **Kubernetes v1.31.0**, which must match `kind-cluster.yaml`
and the scheduler image.

A policy can run in either of two places, and the plugin is the same in both:

- **in the scheduler process**, in Go, when the policy can be written there;
- **in another process**, over HTTP, when it cannot — a Python model, an RL agent, an LLM
  call. The plugin posts the snapshot to `KAPTAIN_DECIDER_URL` and receives scores. That
  service is `bridge/`.

The project used to carry a second integration, an HTTP scheduler extender. It was measured
against the plugin, the plugin won, and it was removed. It survives on the `extender-only`
branch, and the full two-path repository on `archive/extender`; neither is maintained.

The two places a policy can run are held to the same decisions by the fixtures in
`testdata/snapshots/` and `testdata/quantisation.json`, read by the Go suite and the Python
suite alike, so a strategy cannot drift on one side only.

[DRS (Jian et al. 2024)](https://doi.org/10.1002/spe.3284) similarly separates its scheduler
from a DQN decision maker, using sockets. Our decider protocol is a different transport whose
cost is measured directly, from the caller side, inside the scheduler.

## Design notes

[The project decision README](../README.md) records the current choices by RQ, model
training, local LLM and conditional fine-tuning, observability, and open dataset selection.

The design documents — architecture, measurement protocol, decision records, literature
review, code audit, roadmap — are parked on the `docs` branch for now.

## Run

Prerequisites: Docker, `kind`, `kubectl`, `uv`.

```bash
cd kaptain
make all-plugin    # create cluster + build + load + deploy + demo pod
make verify-plugin # show where the pod was scheduled, and by whom
make logs-plugin   # one JSON line per decision
make down          # tear down
```

To run the policy out of process instead, add the decider and the scheduler that calls it:

```bash
make build-decider load-decider deploy-decider
make metrics-decider    # the kaptain_decider_* metrics
make logs-decider
```

Run **one arm at a time**: pods scheduled concurrently by different schedulers compete for
the same nodes and confound the comparison. That is why the demo workloads are separate
files.

### The two arms

| | Plugin | Plugin + decider |
|---|---|---|
| Scheduler | `kaptain-scheduler` (`k8s/scheduler-plugin.yaml`) | `kaptain-scheduler-decider` (`k8s/scheduler-plugin-decider.yaml`) |
| Pod selects it with | `schedulerName: kaptain-scheduler` | `schedulerName: kaptain-scheduler-decider` |
| Policy runs in | the scheduler process, in Go | the Python service (`k8s/decider.yaml`), over HTTP |
| Strategy switch | `KAPTAIN_STRATEGY` | `KAPTAIN_STRATEGY` **and** `STRATEGY` in `k8s/decider.yaml` |
| Default strategy | `dummy-random` — **the two must match**, or the comparison measures the policy | `dummy-random` |
| Good for | decision cost with nothing in between | transport cost, and any policy that cannot live in Go |

Plugin environment: `KAPTAIN_STRATEGY` (`dummy-random`, `largest-cpu-capacity`,
`least-allocated`, `least-used`), `KAPTAIN_SEED`, `KAPTAIN_DECIDER_URL` and
`KAPTAIN_DECIDER_TIMEOUT` (empty URL keeps the policy local), `KAPTAIN_RESERVATION_TTL`,
`KAPTAIN_TELEMETRY` and its refresh and max-age, `RUN_ID`, `POLICY_VERSION`.

Decider environment: `STRATEGY`. The service reads no configuration beyond that, because it
reads no cluster state: everything the policy needs arrives in the request body.

**Durations** accept a Go duration string (`2s`, `500ms`) or a bare number of seconds (`2`),
and an unparseable value fails at startup rather than falling back to a default — a run must
never believe it used a setting it never had.

**Replaying identical snapshots.** Replaying the same recorded snapshot through both —
`make replay-plugin` (the `kaptain-replay` binary) and `make replay-decider` (`POST /replay`)
— pins **parity**: identical snapshot in, identical scores and identical winner out, checked
by the shared fixtures. Its duration is the **policy computation** alone: neither number
contains the HTTP transport or the Scheduling Framework overhead. It is the right measurement
for inference cost and decision stability, and the wrong one for the cost of an integration.

**Measuring the transport.** The plugin times the decider call on the caller side, inside the
scheduler, in `kaptain_plugin_decider_duration_seconds`. The decider times its own handler,
in `kaptain_decider_handler_duration_seconds`. The difference is the transport, with both
halves measured around the same event. This replaces the subtraction of a Python timer from a
Go timer, which the pilot record on the `docs` branch flags as unsound.

**Telemetry age.** `metrics_age_seconds` is the age of the **measurement**, taken from the
per-node `timestamp` the metrics API sends, not from the moment we downloaded it.
metrics-server serves a cached sample, so dating it from the fetch would let a strategy rank
on minutes-old numbers while reporting an age of milliseconds. `KAPTAIN_TELEMETRY_MAX_AGE`
applies per node against that timestamp, and a sample with no usable timestamp is refused
outright: an age that cannot be known cannot be checked.

**Pod requests.** A pod's effective requests are not a plain sum: the plugin calls
`PodRequests` from `k8s.io/kubernetes/pkg/api/v1/resource` — regular containers add up,
restartable init containers (sidecars) add up and also count towards later init containers,
ordinary init containers contribute a maximum, and pod overhead is added last.
`testdata/pod-requests.json` pins it case by case.

**Score scale.** Kubernetes caps an extender score at `MaxExtenderPriority` (10) and then
multiplies it by 10. The plugin quantises raw scores onto 0..10 with that arithmetic and
applies the ×10 itself; the decider's `/replay` does the same. `testdata/quantisation.json`
pins the rule case by case and is checked by both test suites.

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

The decider exposes one series, `kaptain_decider_handler_duration_seconds`. It deliberately
does not mirror the plugin's: two numbers for one event would force the run to choose.

### Testbed limits

kind runs every Kubernetes node as a container on **one** physical machine, so nodes share
CPU, memory bandwidth, disk and network. It is right for wiring, fallbacks, logs and
decision-cost pilots, and wrong for placement-performance claims: per-node network and disk
figures are barely interpretable, and the `hw-class`/`cpu-gen` labels in
`kind-cluster.yaml` are cosmetic, not real heterogeneity. **Main results must come from
separate Linux VMs, ideally one VM per Kubernetes node**, or from bare metal with verified
cpuset isolation. Always report the arm, the Kubernetes version and the testbed with any
number.

## Switch strategy

Go strategies live in `plugin/pkg/strategies`, their Python twins in `bridge/strategies/`
(one class per file, extending the `SchedulingStrategy` ABC). A strategy is a pure function
of the snapshot on both sides; the fixtures check that the two agree.

```bash
# in-process: edit KAPTAIN_STRATEGY in k8s/scheduler-plugin.yaml, then
make build-plugin load-plugin && kubectl -n kube-system rollout restart deploy/kaptain-scheduler

# out of process: edit STRATEGY in k8s/decider.yaml, then
make build-decider load-decider && kubectl -n kube-system rollout restart deploy/kaptain-decider
```

## Fallback and decision log

A strategy that crashes, or that names a node outside the candidate list, must never become
an invisible random baseline. The plugin validates every answer — its own, and the decider's
— and on failure applies one explicit fallback: highest free-request ratio, then most
allocatable CPU, then node name (`plugin/pkg/kaptain/fallback.go`). It needs no telemetry,
since it runs exactly when something else failed. Each decision emits one JSON line:

```json
{"run_id": "unset", "integration": "plugin", "strategy": "dummy-random",
 "pod_name": "demo", "candidate_count": 3, "chosen_node": "worker-2",
 "fallback": true, "fallback_reason": "strategy_error",
 "durations_ms": {"strategy": 0.3, "total": 0.4}}
```

**Two ages, never one.** Requested resources and measured usage come from different sources
at different rates, so each decision reports `requests_age_ms` and `telemetry_age_ms`
separately, and `cache_age_seconds` is labelled by source. A single mixed age cannot say
which input was stale. The requests age is zero by construction: they come from the
scheduler cache.

**`intended_node` is not the placement.** It is the node our policy preferred. When several
nodes end up with the same normalised score, kube-scheduler picks among them at random, so
`tie_count` says how many shared the top score, and a second line (`event: "binding"`)
records the node actually bound, from `PostBind`, plus `kaptain_plugin_bound_mismatch_total`.

The reported `duration_ms` covers the whole decision — snapshot, policy, per-node scoring
and normalisation — because the line is written once normalisation has run.

`fallback_reason` is `strategy_error`, `decider_error`, `invalid_score`, `invalid_choice` or
`no_candidates`. A fallback counts as a decision of the method under test and must not be
dropped from results. Set `RUN_ID` and `POLICY_VERSION` so the lines can be joined with a run.

Tests: `make test` runs both suites (`make test-plugin`, `make test-decider`).
`make lint-manifests` parses every manifest with the Kubernetes YAML decoder.

There is no experiment runner on this branch. The pilot's was removed — its workload
generation and its Prometheus arithmetic belong to off-the-shelf tools — and is on the
`experiments` branch; the guards any replacement must keep are on the `docs` branch, in
`docs/experiments/measurement-guards.md`.

## Documentation

The design documents live on the `docs` branch:
`git show docs:docs/README.md` maps the set, or check the branch out beside this one.
