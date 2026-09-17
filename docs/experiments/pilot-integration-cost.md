# Pilot: cost of the integration, at equal policy

**Version of 17 September 2026. Specification, not results.** Nothing here has been run.

This pilot answers one question, and only one:

> On this testbed, with the same placement policy, what does the integration itself cost —
> HTTP extender against in-scheduler plugin, idle and under load?

It says nothing about placement quality, and nothing about ML or LLM policies. Those need a
decision quality metric and models; this needs neither. It is the first experiment the
repository can honestly run, because both paths already produce identical decisions on
identical snapshots (`testdata/`), so any difference left is the integration.

## Design

| Element | Choice | Why |
|---|---|---|
| Strategy | `dummy-random` on both paths | Identical decisions, near-zero policy cost: what is measured is the path, not the thinking. Both manifests default to it, and the run record must show the same value on both arms. |
| Reproducibility | Same `KAPTAIN_SEED`, same `kaptain.io/task-id` per pod | The seeded draw keys on the task id, so both arms place the same workload on the same nodes. |
| Submission plan | Generated once, saved, replayed for every run | The "dataset" of this pilot is the submission plan: id, arrival offset, image, requests, limits. No industrial trace is needed for this question. |
| Scenario A | Light pods, no background load | The integration cost with little contention. |
| Scenario B | Same plan, plus a fixed `stress-ng` load per worker | Does the cost change when the machine is busy? |
| Arrivals | Steady rate, then identical bursts | Bursts are where queueing and freshness differ most. |
| Repetitions | 10 paired runs, alternating extender/plugin | Paired differences, and alternation absorbs drift in machine state. |

`stress-ng` is background load, deliberately: its own documentation says it is not a precise
benchmark suite. Same image, same arguments, same limits on both arms, or it is not a
control.

## What we measure, and with which metric

The scheduler's own metrics are the reference, because ours cannot see the transport.
**Which scheduler metric contains the extender call is not obvious, and getting it wrong
would invalidate the comparison**, so it was checked in the Kubernetes 1.31 source:

- extenders are called from `findNodesThatPassExtenders` and inside `prioritizeNodes`
  (`pkg/scheduler/schedule_one.go`), which sit **outside** the framework extension points;
- therefore `scheduler_framework_extension_point_duration_seconds` does **not** include the
  extender round trip. It covers our plugin's `PreScore`/`Score`/`NormalizeScore`, and for
  the extender arm it covers only the native plugins.

So the metric that is comparable across both arms is the one that brackets the whole
scheduling algorithm:

| Metric | What it covers | Use here |
|---|---|---|
| `scheduler_scheduling_algorithm_duration_seconds` | "Scheduling algorithm latency" — filter, extenders, scoring | **Primary.** The only one that contains the extender call and the plugin work alike. |
| `scheduler_scheduling_attempt_duration_seconds` | Algorithm + binding | Secondary: shows whether binding drowns the difference. |
| `scheduler_pod_scheduling_sli_duration_seconds` | Queue entry → scheduled, retries included | The user-visible wait. `pod_scheduling_duration_seconds` is deprecated since 1.29; use the SLI one. |
| `scheduler_framework_extension_point_duration_seconds` | Per extension point | Plugin arm only: how much of the plugin cost is PreScore versus Score versus NormalizeScore. |
| `kaptain_plugin_*` / `kaptain_extender_*` | Our own phases | Decomposition inside the decision. **Never** compare the Python handler timer against the Go timer as if they were the same quantity: the first excludes the HTTP transport. That comparison is the mistake this pilot exists to avoid. |

Also recorded, from the decision logs: submission → binding delay, placement throughput,
fallbacks (`fallback_total`, by reason), invalid decisions, `tie_count`, and
`requests_age_ms` for the extender arm.

### How each number is obtained, and what it is worth

**The mean is Δ_sum / Δ_count** between the scrape taken before the run and the one taken
after. That is exactly the mean of the observations recorded in the window, under two
conditions that the runner checks rather than assumes: the scheduler did not restart (a
restart resets the counters, the delta goes backwards, and the run is marked unusable instead
of producing a number), and nothing else used that scheduler during the window (one arm at a
time, a dedicated scheduler per arm).

**The quantiles are estimated from bucket counts**, as `histogram_quantile` does. They are
approximations, and here they hit a hard floor: the native histograms of Kubernetes 1.31 use
`ExponentialBuckets(0.001, 2, 15)`, so **the first bucket is 1 ms**. If most decisions are
faster than that — which is plausible for `dummy-random` on the plugin path — every quantile
lands in that bucket and means only "below 1 ms". The analysis reports such a quantile as
`<1.0ms` and prints the share of observations that fell in the first bucket. The mean stays
usable; the quantiles do not, and must not be dressed up as measurements.

**Paired differences are computed at the level of runs.** Ten pairs give ten differences, and
the interval is over those, reported beside the individual values.

Our own `kaptain_*` series decompose a decision (snapshot, policy, normalisation) and are
exact, but they never settle the comparison: on the extender path they exclude the transport,
which is the whole question.

## Guards, without which the numbers mean nothing

1. **At least two feasible nodes at all times.** Checked in the 1.31 source: when a single
   node survives filtering, `schedulePod` returns it directly and **never calls scoring**
   (`schedule_one.go`, "When only one node after predicate, just use it"). Neither our
   plugin nor the extender would run, and the run would silently measure nothing. Size the
   pods so at least two workers always fit, and count decisions against pods placed.
2. **One arm at a time.** Two schedulers placing concurrently race for the same nodes.
3. **Images preloaded** into every node (`kind load docker-image`), or the first pods
   measure image pulls.
4. **Initial state restored** between runs: delete the workload, wait for the node totals to
   return to baseline, and record that it did.
5. **Same strategy, same seed, same task ids** on both arms — otherwise policy and
   integration are mixed.
6. Record with every run: Kubernetes version, plugin image digest, extender image digest,
   `integration`, strategy, seed, scenario, the telemetry collector, and the features each
   path had (`/healthz` for the extender, the plugin's startup line). **Check the two arms
   report the same strategy** before comparing anything.
7. **Record the real submission instants and their lag against the plan.** The runner uses
   one `kubectl` per pod, and a process start costs tens of milliseconds: a burst submitted
   that way may not have been a burst. `run.json` holds the planned and actual offset of
   every pod, and `summary.csv` carries the worst lag of the run. A pair whose lag is large
   relative to the burst window says something about the runner, not about the schedulers.
8. **`kubectl` acts with the permissions of the current kubeconfig**, and the run record
   states the server version it talked to. A run made with different permissions, or against
   a different cluster, is a different run.
9. Report `requests_age_ms` and `telemetry_age_ms` per arm. They are separate fields
   precisely so that a difference in freshness cannot hide inside one averaged number.

## What this pilot cannot conclude

kind runs every node as a container on one machine. The workers share CPU, memory
bandwidth, disk and network, so `stress-ng` on "one worker" is really load on the whole
host, and the `hw-class` labels create no hardware difference. The honest conclusion is
therefore bounded:

> On this testbed, at identical policy, the extender path costs X ms more per scheduling
> attempt than the plugin path at rest, and Y ms under background load.

Not "the extender is X ms slower" in general, and nothing at all about placement quality.
The same protocol on separate Linux VMs is what turns this into a result for the paper.

## The two arms are not perfectly symmetric

They are identical where it matters — same strategy, same snapshot, same request calculation,
same score quantisation, same fallback, same seeded identity, all pinned by shared fixtures —
and they differ in ways the collector has to handle rather than ignore:

| | Extender | Plugin |
|---|---|---|
| Metric prefix | `kaptain_extender_*` | `kaptain_plugin_*` |
| Own metrics served by | the extender service, port 8888, HTTP | the scheduler itself, port 10259, HTTPS |
| Scheduler metrics | `ml-scheduler` — **the only place the HTTP round trip appears** | `kaptain-scheduler` |
| Identity reported by | `GET /healthz` | the plugin's startup line |
| Knows the bound node | no | yes, via `PostBind` |
| Requests age | real, reported | zero by construction (scheduler cache) |

Histogram buckets are deliberately identical between `kaptain_extender_*` and
`kaptain_plugin_*`, so the two can be read side by side without conversion. Both schedulers
expose `/metrics` without a token, which the extender arm's scheduler needed as well — its
numbers are the only evidence of the transport cost.

## Outputs

One directory per run: `run.json` (configuration, versions, seeds, scenario, arm),
`decisions.jsonl` (the decision log of the arm under test), `pods.csv` (id, submitted,
scheduled, bound node, phase transitions), `scheduler_metrics.txt` (scraped before and
after), and `summary.csv` (one row per run). Paired analysis reads `summary.csv` only.

## Status

**The runner exists** (`experiments/`, standard library only): `plan.py` generates and
replays the submission plan, `collect.py` reads the metrics, placements and decision logs,
`run.py` executes one arm with its guards, `analyse.py` produces `summary.csv` and the paired
differences. Ten tests cover the parts that need no cluster — parsing, the restart guard,
the unresolved-quantile case, plan replay, pairing and the refusal to compare arms running
different strategies.

**Nothing has been run.** The plugin image has never been built and the plugin has never
placed a pod: the Docker daemon was unavailable while this was written. The order is a smoke
run on each arm first — a handful of pods, checking placements and counters — and only then
the ten pairs.

```bash
make up build load deploy build-plugin load-plugin deploy-plugin
make smoke-extender      # then check runs/smoke-extender/
make smoke-plugin        # then check runs/smoke-plugin/
make pilot PAIRS=10 SCENARIO=A-light
make analyse
```
