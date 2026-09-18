# Code audit

What the code in this repository actually does, as opposed to what the protocol says it
should do. Refresh this file when the code changes; it goes stale faster than anything else
here.

---

## kaptain code audit

State as of `997c92e` (post-merge of PR #1). Two earlier findings were fixed by that merge
and are recorded here as resolved, because they change how results must be read.

### Fixed upstream

- **`d5faf26` — native Score plugins disabled** (`profiles[].plugins.score.disabled: ["*"]`).
  Before this, the extender's 0–10 score at `weight: 5` merely *biased* the default
  scheduler's own scores, so any measurement would have been "default scheduler plus a
  nudge". Now the extender owns scoring entirely and the strategy is the policy. **This is
  what makes comparison meaningful at all.**
- **`d2e1841` — `spread_cpu` renamed to `largest_cpu_capacity`**, acknowledging it ranks on
  static capacity and does not spread. The name no longer misleads; the behaviour is still
  not a resource-aware baseline (see above).
- **`270d0f7` — handlers made synchronous.** `strategy.select()` is blocking; leaving the
  handlers `async` would have stalled the event loop during inference. This matters more once
  a real model sits behind `select()`.

### Fixed in this workspace

- **Silent fallback removed (2026-09-17).** `except Exception: chosen = None` made every node
  score 0; with native scoring disabled all nodes were then **perfectly tied**, so a strategy
  crash silently became a random baseline inside the results. `bridge/decision.py` now
  validates the strategy's answer (it must name one of the candidates), applies an explicit
  deterministic fallback (`bridge/fallback.py`: most allocatable CPU, ties broken by node
  name), logs one JSON line per decision with `fallback` and `fallback_reason`, and counts
  fallbacks at `/stats`. The same failure mode exists in DRS's own plugin, where a failed
  call to the decider lets every node pass the filter — see
  [`reproduction/README.md`](../../reproduction/README.md). Covered by
  `bridge/tests/test_decision.py`, the repository's first tests.

- **In-scheduler plugin implemented (2026-09-17).** `plugin/` is a Scheduling Framework
  plugin (`KaptainScore`, Kubernetes v1.31.0) with PreScore/Score/NormalizeScore and
  Reserve/Unreserve, the three local strategies, an optional external decider with a
  timeout, Prometheus metrics for the decision phases, and the same snapshot, fallback rule
  and JSON decision log as the extender. Both suites check the shared fixtures in
  `testdata/snapshots/`, so a strategy cannot drift on one side only. This gives gap A its
  second measurement point: the same policy under two integrations. Unlike DRS's `Filter`
  plugin, the decision is expressed as a score, and `Filter` is left to Kubernetes.

- **Review fixes (2026-09-17).** Five findings from a code review of the plugin, all fixed:
  the logged node is now `intended_node` with a `tie_count`, and a `PostBind` line plus
  `bound_mismatch_total` record where the pod actually landed, since kube-scheduler breaks
  score ties at random; score validation now requires a complete map of finite values under
  `maxRawScore`, so a missing candidate can no longer be read as zero and outrank a negative
  score, and the int64 conversion is clamped; the reported duration is written after
  normalisation and covers every phase; `least-allocated` is refused at extender startup
  instead of silently producing a run of fallbacks; and `make metrics-plugin` scrapes through
  `port-forward`, since the distroless image has neither shell nor wget.

- **Second review round (2026-09-17).** Four more findings, fixed: `postBind` was missing
  from the shipped profile, so the binding was never logged; the two paths quantised scores
  differently (0..10 with rounding versus 0..100 with truncation), which could place the
  same pod on different nodes — both now quantise the **raw** scores onto the extender's
  0..10 scale with identical arithmetic, pinned case by case in
  `testdata/quantisation.json` and checked by both suites; the seeded draw keyed on the pod
  UID, which changes on recreation, and now keys on a stable task identity
  (`kaptain.io/task-id`, else `namespace/name`); and the extender can finally read per-node
  requested resources from the Kubernetes API (`bridge/cluster.py`, read-only RBAC), so the
  three strategies run on both paths and the resource-aware comparison is real rather than
  merely guarded. Quantising raw values instead of the scaled int64 ones also fixed a
  precision collapse for scores below 1/scorePrecision, found by the shared table.

- **Third review round (2026-09-17).** Two findings. The effective pod requests diverged:
  the Python side ignored init containers, and neither side handled restartable init
  containers (sidecars) or pod overhead. The plugin now calls upstream `PodRequests`, the
  extender ports that exact algorithm, and `testdata/pod-requests.json` pins both. Second,
  the two paths see the cluster with different freshness during bursts (scheduler cache
  versus periodic API refresh), which no staleness threshold can fix: added a replay path on
  both sides (`kaptain-replay`, `POST /replay`) so decision cost can be compared on
  identical snapshots, an in-flight ledger in the extender mirroring the plugin's `Reserve`,
  and `requests_age_ms` on every extender decision so the residual difference is reported
  rather than assumed away.

- **Remaining audit items closed (2026-09-17).** Decision metrics now exist on both sides
  (`kaptain_extender_*` on a new `/metrics` endpoint, same names, labels and phases as
  `kaptain_plugin_*`). Measured node usage is available in both paths from the metrics API
  (`pkg/telemetry`, `bridge/telemetry.py`, off by default, RBAC added), with the `least-used`
  strategy as the protocol's BT arm and a shared fixture where requests and usage disagree so
  the two rank differently. The demo workload no longer runs two arms concurrently: one
  manifest per arm. The kind labels are documented in place as cosmetic. The stale macOS
  duplicate files are deleted.

- **Fourth review round (2026-09-17).** Three bugs, all fixed. The extender's quantity
  parser rejected nanocores (`123456789n`), the unit the metrics API actually uses for CPU,
  so telemetry was broken on a real cluster while the plugin accepted it: the parser now uses
  exact rationals and Kubernetes' round-towards-+infinity, pinned by
  `testdata/quantities.json`, generated by apimachinery. The extender reserved the node it
  preferred, but on a score tie Kubernetes places at random, so the following decisions ran
  on a fictitious load: it now reserves only when `tie_count == 1` and logs the skip.
  Reservations were released by timestamp, dropping a pod still waiting for its binding: they
  are now reconciled by observed pod UID, with the TTL only as a backstop. Also corrected in
  the documentation: replay measures the policy computation, not the transport, so it pins
  parity and inference cost but cannot measure integration overhead — that comes from
  kube-scheduler's own extension-point metrics, in cluster.

- **Fifth review round (2026-09-17).** Two bugs, both in code added the same day. The
  reservation release still fired too early: it keyed on the pod merely existing in the API,
  but a pod exists from creation and sits Pending until its binding, so a reserved node went
  back to looking free. Release now requires observing that pod **on a node**, with the TTL
  as the only backstop, and `refresh()` itself is tested with a Pending pod, a pod that lands
  where expected, and one that lands elsewhere. Second, the telemetry age measured the
  download rather than the measurement, in both languages: metrics-server serves a cached
  sample, so a fresh fetch of a five-minute-old value looked fresh. Both now use the
  per-node `timestamp` from the API, refuse a sample without one, and clamp negative ages
  from clock skew.

- **Sixth review round (2026-09-17).** Three findings from the PR review. The two
  deployments defaulted to different strategies, which would have made the first comparison
  measure the policy instead of the integration; both now default to `dummy-random` and the
  manifests say the values must match. Duration settings used incompatible units — Python
  read bare seconds, Go a duration string, so `"2"` in the plugin manifest was silently
  replaced by the default and `"5"` would not have worked at all; both now accept either
  form and refuse what they cannot parse instead of defaulting. The extender reported one
  mixed `requests_age_ms` built from the maximum of two unrelated ages; requests and
  telemetry now have their own field, all the way into the decision log and the
  `cache_age_seconds` labels.

### Open

1. **Transport cost is only measurable from the scheduler side, and not with the extension
   point metric.** Checked in the 1.31 source: extenders are called outside the framework
   extension points, so `scheduler_framework_extension_point_duration_seconds` excludes the
   extender round trip. The comparable metric is
   `scheduler_scheduling_algorithm_duration_seconds`. The pilot that settles this is
   specified in `experiments/README.md`; it is the first experiment this repository can
   honestly run.
2. **No experiment runner.** No arrival generator, no `run.json` / `pods.csv` /
   `summary.csv`, so nothing aggregates the per-decision logs into a result yet. This is the
   blocking item, and `experiments/README.md` now says exactly what it has to produce.
3. **No ML/RL/LLM policy.** They reach either path through the external decider, which has
   no service behind it yet.
4. **Whole `NodeList` serialised per pod** with `nodeCacheCapable: false`. Fine at 3 nodes;
   it becomes part of the measured cost at scale. Report serialisation as a function of node
   count, and consider `nodeCacheCapable: true` once behaviour is understood.
5. **Testbed.** kind remains one machine: wiring and decision-cost pilots only, main
   results on separate Linux VMs.

---
