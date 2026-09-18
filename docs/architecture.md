# Architecture

Kaptain plugs a placement policy into a real Kubernetes scheduling path, twice, so that the
cost of the integration can be measured with the policy held equal. Why both paths exist is
[decision 0001](decisions/0001-two-integration-paths.md).

```text
Path A — HTTP extender
  Pod → Filter (Kubernetes) → candidates → POST /prioritize → strategy → scores → placement

Path B — in-scheduler plugin
  Pod → Filter (Kubernetes) → candidates → PreScore → strategy or external decider
                                         → Score → NormalizeScore → placement
```

The Kubernetes default scheduler stays untouched beside them, as the native reference.

## What the two paths share

Everything that could otherwise drift and be mistaken for an integration effect.

**The snapshot.** One contract, `plugin/pkg/snapshot` and `bridge/snapshot.py`: pod identity
and effective requests, per-node allocatable, requested and measured resources, pod count,
readiness, the age of each measurement, and the list of feature groups that are *missing*.
Absence is declared, never encoded as zero. See
[decision 0003](decisions/0003-shared-snapshot-contract.md).

**The telemetry collector.** Selected by configuration behind a stable interface, and named
in the run record: `off`, `metrics-api`, and `kubelet` reserved for reading the kubelet
endpoints directly, without metrics-server. See
[decision 0009](decisions/0009-pluggable-telemetry-collector.md).

**The strategies.** `dummy-random` (seeded control), `largest-cpu-capacity` (static
capacity), `least-allocated` (requests-aware), `least-used` (telemetry-aware, the BT arm of
the protocol). A strategy is a pure function of the snapshot: no cluster call, no state, no
training. Each declares the feature groups it needs, and a deployment that cannot provide
them refuses to start rather than producing a run made of fallbacks.

**The fallback.** Same rule, same reasons, logged and counted on both sides:
[decision 0004](decisions/0004-explicit-fallback.md).

**The score scale.** Both quantise raw scores onto the extender's 0..10 integers:
[decision 0006](decisions/0006-extender-scale-quantisation.md).

**The identity used for seeded draws.**
[decision 0005](decisions/0005-stable-task-identity.md).

**The configuration units.** Durations accept a Go duration string or a bare number of
seconds on both sides, and an unparseable value fails at startup instead of silently
becoming a default. The active strategy must be the same in both deployments; the manifests
say so where the value is set.

These are pinned by fixtures in `testdata/`, read by both test suites, so a change on one side
only fails a test instead of quietly changing a result.

## Where they necessarily differ

| | Extender (A) | Plugin (B) |
|---|---|---|
| Runs in | its own process, Python | the scheduler process, Go |
| Transport per decision | HTTP, plus JSON serialisation of the node list | none, unless an external decider is configured |
| Sees the cluster through | a periodic API refresh, plus its own in-flight ledger | the scheduler cache, plus `Reserve` |
| Age of requested resources | real, reported as `requests_age_ms` | zero by construction |
| Knows where the pod landed | no — the protocol ends at the score | yes, `PostBind` |
| Can time its own transport | no | not applicable |
| Version coupling | loose | compiled against Kubernetes v1.31.0 |

The freshness difference is a property of the integration, not a defect to hide: the extender
reports `requests_age_ms` on every decision, and replaying identical snapshots removes the
difference when the question is decision cost alone.

## Decision flow, plugin side

`PreScore` builds the snapshot and decides once per pod, so `Score` is a map lookup and the
measured cost lands in the right phase. `NormalizeScore` quantises, and is also where the
decision is logged, because by then every phase has run. `Reserve`/`Unreserve` account for
pods chosen but not yet visible in the cache, with a TTL so a lost `Unreserve` cannot leak.
`PostBind` records the node actually bound — which is not always the intended one, since
kube-scheduler breaks score ties at random.

`Filter` is deliberately not implemented:
[decision 0002](decisions/0002-score-not-filter.md).

Detail, extension point by extension point: [plugin-design.md](plugin-design.md).

## Observability

Both paths emit one JSON line per decision with the same fields (`run_id`, `integration`,
`strategy`, `policy_version`, pod identity, `candidate_count`, `intended_node`, `tie_count`,
`fallback`, `fallback_reason`, per-phase durations), and the same Prometheus series under two
prefixes: `kaptain_plugin_*` on the scheduler's `/metrics`, `kaptain_extender_*` on the
extender's.

What neither can measure is the transport cost of the extender path. That comes from the
scheduler's own `scheduler_scheduling_algorithm_duration_seconds` — and *not* from
`scheduler_framework_extension_point_duration_seconds`, which excludes extender calls. See
[measurement-protocol.md](measurement-protocol.md).

## Testbed

kind runs every node as a container on one machine: shared CPU, memory bandwidth, disk and
network, and the `hw-class` / `cpu-gen` labels create no hardware difference. It is right for
wiring, fallbacks, logs and decision-cost pilots, and wrong for placement-performance claims.
Main results need separate Linux VMs, ideally one per Kubernetes node.
