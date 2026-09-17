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

**Paired differences are computed at the level of runs**, and matched by an explicit
`pair_id`, never by position: dropping the aborted runs and zipping what is left would
compare run 3 of one arm against run 4 of the other. A pair where one arm aborted, or where
a mean is missing, is reported as unusable — a missing mean is never read as zero, which
would invent a difference the size of the other arm's latency. Ten pairs give ten
differences, and the interval is over those, reported beside the individual values.

**The order inside a pair alternates.** Always running the extender first would hand the
plugin a machine the extender run just warmed up or disturbed, and that bias would land
entirely in the difference being measured.

Our own `kaptain_*` series decompose a decision (snapshot, policy, normalisation) and are
exact, but they never settle the comparison: on the extender path they exclude the transport,
which is the whole question.

## Guards, without which the numbers mean nothing

Each of these aborts the run with a reason. A run that quietly measured the wrong thing is
worse than a run that failed.

1. **At least two nodes with room, and proof that scoring ran.** Checked in the 1.31
   source: when a single node survives filtering, `schedulePod` returns it directly and
   **never calls scoring** (`schedule_one.go`, "When only one node after predicate, just use
   it"). Readiness is not enough to rule that out — the runner computes allocatable minus
   bound requests per node and requires at least two nodes able to hold the plan's largest
   task. Afterwards it verifies that scoring actually happened: one decision line per placed
   pod, and every decision seeing at least two candidates. Fewer decisions than pods means
   some placements were never scored by either integration, and the run is unusable.
2. **The scheduler must be the same process throughout.** A restart resets the histogram
   counters, and a busy scheduler then climbs back past its earlier values, so a delta that
   looks sane proves nothing. The runner compares `process_start_time_seconds` between the
   two scrapes, plus the scheduler pod's UID and container restart count, and aborts if any
   changed.
3. **Decision logs belong to one run.** The deployment keeps serving other pods and the
   previous run's lines are still in the log, so the collector reads from the run's start
   instant **and** keeps only the lines whose `pod_uid` belongs to a pod this run created.
   Without that filter, fallback counts and our internal means silently include earlier runs.
4. **One arm at a time.** Two schedulers placing concurrently race for the same nodes.
5. **Images preloaded** into every node (`kind load docker-image`), or the first pods
   measure image pulls.
6. **Initial state restored** between runs: delete the workload, wait for the node totals to
   return to baseline, and record that it did.
7. **Same strategy, same seed, same task ids** on both arms — otherwise policy and
   integration are mixed.
8. Record with every run: Kubernetes version, plugin image digest, extender image digest,
   `integration`, strategy, seed, scenario, the telemetry collector, and the features each
   path had (`/healthz` for the extender, the plugin's startup line). **Check the two arms
   report the same strategy** before comparing anything.
9. **Record the real submission instants and their lag against the plan.** The runner uses
   one `kubectl` per pod, and a process start costs tens of milliseconds: a burst submitted
   that way may not have been a burst. `run.json` holds the planned and actual offset of
   every pod, and `summary.csv` carries the worst lag of the run. A pair whose lag is large
   relative to the burst window says something about the runner, not about the schedulers.
10. **`kubectl` acts with the permissions of the current kubeconfig**, and the run record
   states the server version it talked to. A run made with different permissions, or against
   a different cluster, is a different run.
11. Report `requests_age_ms` and `telemetry_age_ms` per arm. They are separate fields
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

**Smoke runs passed on both arms, 17 September 2026** (kind, Kubernetes v1.31.0, 3 workers,
10 pods per arm, `dummy-random`):

- 10 pods submitted, 10 placed, 10 decisions recorded on each arm — so scoring really ran,
  and every decision saw 3 candidates;
- **the two arms placed all 10 tasks on the same nodes**, which is the fixture parity holding
  on real pods;
- the plugin's `PostBind` matched its intended node 10 times out of 10, with no score ties;
- no fallbacks, no invalid decisions, scheduler identity stable across the run;
- worst submission lag 0.31 s (extender) and 0.26 s (plugin), against a 0.5 s steady interval:
  usable, but close enough to the interval that the burst shape must be checked per run.

The smoke run also caught a real defect that no unit test could: the extender image did not
copy `config.py`, `metrics.py` and `telemetry.py`, so the container crash-looped on startup.
Tests run outside the container; only a deployment finds this.

Indicative numbers from those two runs, **not a result** — one run per arm, not ten pairs:
`scheduler_scheduling_algorithm_duration_seconds` averaged 4.54 ms (extender) against 0.45 ms
(plugin). Note that every plugin observation fell in the first 1 ms bucket, so its quantiles
print as `<1.0ms` and only the mean is usable, exactly as anticipated.

## Result, scenario A (light pods, no background load), 17 September 2026

Ten paired runs per configuration, 60 pods per run, `dummy-random` on both arms, kind with
3 workers, Kubernetes v1.31.0. **40 runs, 0 aborted, 20 usable pairs.**

The scenario was run twice, because the first configuration was measuring a cost we had
inflicted on ourselves. Our `/filter` handler is a pass-through — admissibility stays
Kubernetes' job in both integrations — but the profile declared `filterVerb`, so
kube-scheduler paid **a second HTTP round trip per pod** for an answer that never changed
anything. Measured in the logs: 610 `/filter` calls for 610 `/prioritize` calls. Removing the
verb is the configuration the extender should have had all along.

| Configuration | extender | plugin | paired difference |
|---|---|---|---|
| `A-light` — two round trips per pod (`filterVerb` declared) | 5.60 ms | 0.44 ms | **5.16 ms** [4.84, 5.48] |
| `A-lean` — one round trip per pod | 3.20 ms | 0.41 ms | **2.79 ms** [2.52, 3.06] |

**`A-lean` is the result to cite**: extender at its best against the plugin. `A-light` is kept
because the gap between the two rows is worth a sentence of its own — a single superfluous
extender verb cost **2.4 ms per pod**, comparable to the entire remaining transport.

### A-lean in detail

| | extender | plugin |
|---|---|---|
| `scheduling_algorithm_duration_seconds`, mean | **3.20 ms** | **0.41 ms** |
| `scheduling_attempt_duration_seconds` (+ binding), mean | 6.40 ms | 3.58 ms |
| `pod_scheduling_sli_duration_seconds`, mean | 6.5 ms | 3.6 ms |
| Our own decision time (excludes transport) | 0.218 ms | 0.089 ms |
| Decisions per run | 60 | 60 |
| Fallbacks, invalid decisions | 0 | 0 |
| Share of observations in the first 1 ms bucket | 0 % | 97 % |

**Paired difference on the primary metric: 2.79 ms [2.52, 3.06] at 95 %**, over ten pairs, and
every pair went the same way. In `A-light` the same figure was 5.16 ms [4.84, 5.48].

Two checks that make the number mean what it says:

- **600 of 600 tasks were placed on the same node by both arms**, in each configuration. The
  policy really was held equal, on real pods, across the whole pilot — not only in the
  fixtures.
- **600 of 600 plugin bindings matched the intended node**, with no score ties, so no
  placement was settled by the scheduler's random tie-break.
- 1 200 decisions per configuration, zero fallbacks, zero invalid decisions.

### Reading it

What is measured is a **difference of totals**, not the transport itself: no kube-scheduler
metric times the extender call (`prioritizing_extender` only labels a goroutine gauge). The
attribution comes from subtracting our own timers, which is sound but is a subtraction.

Those timers say the decision itself costs 0.218 ms in the extender and 0.089 ms in the
plugin — a 0.13 ms difference — while the scheduler sees 2.79 ms. The remaining ~2.66 ms is
the HTTP round trip, the JSON serialisation of the node list, and the scheduler-side handling
of the extender call: **about twenty times the cost of the policy itself** at this cluster
size. That is exactly the quantity the plugin exists to remove.

The plugin's quantiles are not reported: 97 % of its observations fall in the first 1 ms
bucket of the native histogram, so P50/P95/P99 print as `<1.0ms` and nothing finer can be
claimed. The mean, being Δsum/Δcount, is unaffected.

### What this does not establish

- **This testbed only.** kind runs the three workers as containers on one machine, so the
  transport crosses a loopback interface, not a network. On separate VMs the absolute gap
  would change — plausibly upward — and the same protocol has to be re-run there before any
  number reaches the paper.
- **Three candidate nodes.** The node list serialised on every extender call is tiny here,
  and `nodeCacheCapable: false` means the full node objects travel on every call. The cost of
  that serialisation grows with the cluster, so this gap is a lower bound on what a larger
  cluster would show — and `nodeCacheCapable: true` is the next configuration lever to test,
  as removing `filterVerb` was.
- **A trivial policy.** `dummy-random` costs almost nothing, which is what isolates the
  integration. It says nothing about what an ML or LLM policy would cost, and nothing about
  placement quality — the two arms placed identically by construction.
- **Submission lag.** The worst per-pod lag was 0.87 s against a 1 s steady interval, so the
  arrival pattern was respected but not by a wide margin. The bursts in particular were
  submitted one `kubectl` at a time and are therefore softer than the plan describes.

### Next

Scenario B — the same plan under a fixed `stress-ng` background load — has not been run. It
is the one that says whether the gap holds when the machine is busy.

```bash
make up build load deploy build-plugin load-plugin deploy-plugin
make smoke-extender      # then check runs/smoke-extender/
make smoke-plugin        # then check runs/smoke-plugin/
make pilot PAIRS=10 SCENARIO=A-light
make analyse
```
