# In-scheduler plugin design

What the Go plugin does, extension point by extension point, and what it must never do. The
wider picture is in [architecture.md](architecture.md).

**Status, 17 September 2026: implemented** in `plugin/`, compiled against Kubernetes v1.31.0.
`PreScore`, `Score`, `NormalizeScore`, `Reserve`/`Unreserve` and `PostBind` exist, with four
local strategies and an optional external decider. Not implemented: `PreFilter`, a background
telemetry cache of its own beyond the metrics-API source, and any ML/RL/LLM policy in-process
— those reach it through the external decider.

## Purpose

The plugin compares two integrations of one policy:

```text
Extender : kube-scheduler → HTTP /prioritize → Python/ML/LLM strategy → scores
Plugin   : kube-scheduler → Score plugin → local strategy or external decider → scores
```

It does not change the scientific question. The problem stays: choose one feasible node for
the next pod, with no migration, no preemption, no replica change and no power control. What
changes is the integration point, and therefore the decision cost, the access to scheduler
metrics, and the operational failure modes.

## Extension points

**`PreScore`** builds the decision snapshot once per pod and takes the decision. Doing it here
rather than in `Score` keeps the per-node call cheap and attributes the cost to the right
phase.

**`Score`** is a lookup into that decision, converted to the framework's int64 scale. An
unknown node is counted as an invalid decision and scored lowest rather than failing the
scheduling cycle.

**`NormalizeScore`** quantises onto the extender's scale and applies the ×10 Kubernetes
applies to extender scores ([decision 0006](decisions/0006-extender-scale-quantisation.md)).
It is also where the decision line is written, because only then has every phase run — a
duration logged in `PreScore` would exclude scoring and normalisation.

**`Reserve` / `Unreserve`** record that a pod was assigned to a node before it is visible in
the cache, so two decisions taken moments apart do not both see the node as free. Entries
expire, so a lost `Unreserve` cannot leak. Nothing is reserved in the cluster and nothing is
evicted.

**`PostBind`** records the node actually bound. With ties broken at random by the scheduler,
this is the only trustworthy placement record; the intended node only says what the policy
preferred. It must be enabled in the profile — a missing `postBind` entry silently removes
the placement log, which happened once and is now asserted by a test.

**`Filter` is deliberately absent.** Admissibility stays Kubernetes' job:
[decision 0002](decisions/0002-score-not-filter.md).

**`PreFilter`** is not needed while the snapshot is built in `PreScore`. **`Bind`** is left to
Kubernetes, which keeps the experiment comparable with the extender.

## What it must not do

Eviction, preemption, autoscaling, LLM code generation, blocking Prometheus queries per
candidate, online training inside the kube-scheduler process, LLM fine-tuning, or
cluster-wide packing logic. Several of those would change the problem rather than the
integration.

## Strategies

In the order they were implemented, because each validates the integration before the next is
worth trusting: `dummy-random` with a controlled seed (does the plugin actually influence
placement?), `largest-cpu-capacity` (equivalence with the extender), `least-allocated`
(requests-aware, from the scheduler cache), `least-used` (telemetry-aware, the BT arm).

For ML, RL or LLM policies there are two options. A local Go strategy suits heuristics and
simple exported models, and has the lowest overhead. An external decider keeps the plugin
inside the scheduler and the policy outside — the shape DRS uses — and then the call must be
measured as a sub-part of the decision, never confused with the cost of the Scheduling
Framework itself.

## Metrics

Registered on the scheduler's own `/metrics`, with the extender exposing the same series
under `kaptain_extender_*`:

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `kaptain_plugin_score_duration_seconds` | histogram | `strategy`, `integration`, `status` | Whole decision for one pod, fallback included |
| `kaptain_plugin_snapshot_duration_seconds` | histogram | `strategy`, `status` | Building the snapshot |
| `kaptain_plugin_decider_duration_seconds` | histogram | `strategy`, `decider`, `status` | Local strategy or external decider call |
| `kaptain_plugin_normalize_duration_seconds` | histogram | `strategy` | Quantisation |
| `kaptain_plugin_candidate_nodes` | histogram | `strategy` | Candidates per decision |
| `kaptain_plugin_decisions_total` | counter | `strategy`, `status` | Decisions attempted, by outcome |
| `kaptain_plugin_fallback_total` | counter | `strategy`, `reason` | Explicit fallbacks |
| `kaptain_plugin_invalid_decision_total` | counter | `strategy`, `reason` | Unknown node, invalid score, missing state |
| `kaptain_plugin_bound_mismatch_total` | counter | `strategy` | Pod bound elsewhere than intended, which ties cause |
| `kaptain_plugin_cache_age_seconds` | histogram | `source` | Age of the measurements used |
| `kaptain_plugin_missing_features_total` | counter | `feature_group` | Features absent from a snapshot |

Labels stay low cardinality: no pod UID, no node name, no image. Those belong in the decision
log.

## Decision log

One JSON line per decision, and a second one per binding:

```json
{"run_id": "unset", "integration": "plugin", "strategy": "least-allocated",
 "policy_version": "unset", "pod_uid": "...", "namespace": "default", "pod_name": "demo",
 "candidate_count": 3, "intended_node": "worker-1", "tie_count": 1, "score": 0.78,
 "fallback": false, "fallback_reason": null, "decider": "local", "duration_ms": 2.5,
 "durations_ms": {"snapshot": 0.8, "decider": 1.4, "score": 0.2, "normalize": 0.1, "total": 2.5},
 "event": "decision"}
```

For large clusters the full score map is too big to keep; the chosen node, the top two scores,
the candidate count and a snapshot hash are the minimum. Debug mode keeps everything.

## Fallbacks

Identical in concept to the extender's: snapshot too incomplete, cache too old, strategy
error, invalid score, decider timeout, no valid score, or a chosen node absent from the
candidate list. The rule and its rationale are in
[decision 0004](decisions/0004-explicit-fallback.md).

## Observability constraints

The plugin must not query Prometheus or the kubelet inside `Score`. It reads a cache
refreshed in the background, and carries the age of each measurement into the snapshot — the
age of the *measurement*, taken from the source's own timestamp, not of the download.

Which collector fills that cache is configuration, not code: `metrics-api` today, `kubelet`
reserved for a direct collector that would remove metrics-server from the path. The
collector names itself, and that name reaches the startup line and the `source` label of
`cache_age_seconds`, so a result records where its numbers came from. See
[decision 0009](decisions/0009-pluggable-telemetry-collector.md).

Native scheduler metrics serve as the control: scheduling algorithm duration, extension point
durations, pending pods, attempts and errors. Ours add what Kubernetes cannot know: policy
version, fallbacks, cache age, decider time, feature validity.

## Limits to state with any result

The plugin does not fix the testbed. On kind, several Kubernetes nodes share one machine, so
per-node network and disk figures stay barely interpretable however good the plugin is.

It also couples development to a Kubernetes version: the scheduler binary, the image and the
configuration must be pinned together, and results must state `integration=plugin` and the
Kubernetes version.

## Criterion after the pilot

Keep both paths only if each earns its place: the extender for fast ML/LLM iteration, the
plugin for measurements closer to the scheduler, both for a scientific comparison of the
integration. If the plugin does not reduce cost or complicates the experiment too much, it
becomes a secondary arm. If the extender becomes unstable or too expensive, the plugin becomes
the main path for final results. The pilot that decides this is in
[experiments/pilot-integration-cost.md](experiments/pilot-integration-cost.md).
