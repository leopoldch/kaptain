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
| Good for | fast iteration, ML/LLM in Python | decision-cost measurement close to the scheduler |

Plugin environment: `KAPTAIN_STRATEGY` (`dummy-random`, `largest-cpu-capacity`,
`least-allocated`), `KAPTAIN_SEED`, `KAPTAIN_DECIDER_URL` and `KAPTAIN_DECIDER_TIMEOUT`
(empty URL keeps the policy local), `KAPTAIN_RESERVATION_TTL`, `RUN_ID`, `POLICY_VERSION`.

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
- the **integration overhead** can only be measured in cluster, and from the scheduler's
  side: `scheduler_framework_extension_point_duration_seconds` and
  `scheduler_e2e_scheduling_duration_seconds` for both paths, next to our own
  `kaptain_*_score_duration_seconds`. The difference between the two pairs is the transport,
  and it is exactly what the plugin exists to remove.
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

## Evaluation protocol

**Planned protocol, updated September 13, 2026. No benchmark results are claimed here.** This section
defines the comparison contract for the IFT-7026 project; historical alternatives in the
literature notes are not additional mandatory experiments.

The main question is whether a placement method improves **application performance at an
acceptable decision cost**, including when applications have never run before. A lower
prediction error, faster model or higher CPU utilization alone does not establish success.

We take Raith's separation between workload profile, placement prediction and decision as
inspiration. The proposed extension estimates an uncertain initial profile from workload
metadata, then corrects it with actual measurements. ML performs fast placement; an LLM can
supply profiles asynchronously or periodically update bounded scoring weights. A direct LLM
arm measures the alternative of asking a language model for every placement.

### What is available today

| Component | Current status |
|---|---|
| Default Kubernetes scheduler | Available as the reference scheduler |
| HTTP extender | Available; native Score plugins are disabled for its scheduler profile |
| Go Scheduling Framework plugin | Available (`plugin/`, Kubernetes v1.31.0); PreScore/Score/NormalizeScore/Reserve, local strategies and optional external decider |
| `dummy-random` | Available as an integration/debugging control |
| `largest-cpu-capacity` | Available, but reads static allocatable CPU and ignores occupancy; not the resource-aware baseline |
| `least-allocated` | Available in both paths. The plugin reads requested resources from the scheduler cache; the extender reads them from the Kubernetes API in the background (`KAPTAIN_REQUESTS_SOURCE=api`, `bridge/cluster.py`) and refuses to start without a source rather than produce a run of pure fallbacks |
| `least-used` | Available in both paths with `KAPTAIN_TELEMETRY=metrics-api` (needs metrics-server). This is the BT arm: it ranks on **measured** usage, not on requests |
| Live resource usage (telemetry) | Available in both paths from the metrics API, off by default. A node without a fresh sample reports `telemetry` as missing and the strategy falls back rather than reading zero |
| Decision metrics | Both paths export the same series: `kaptain_plugin_*` on the scheduler's `/metrics`, `kaptain_extender_*` on the extender's `/metrics` |
| Resource accounting, telemetry, ML/LLM models, workload runner and result collector | To implement |

`make all` runs an integration demo with two concurrent sleeping pods. It is not an
experimental comparison: those pods neither execute representative work nor isolate the
two schedulers. `make verify` checks placement, not performance or correctness under load.

### Baselines and experimental methods

All custom methods choose **one feasible node for the next pod**. They have the same hard
constraints and no additional migration, preemption, power-control or replica-scaling rights.
Disable preemption for all comparison profiles, including the default-scoring reference,
and document that restriction. Keep the cluster size and replica policy fixed within a run.

| ID | Method | Purpose |
|---|---|---|
| B0 | Kubernetes default scoring/plugins, with the shared action restrictions | Practical reference: does our method improve the existing placement policy? |
| B1 | Least-allocated CPU/memory heuristic | Simple resource-aware spreading reference |
| B2 | Most-allocated CPU/memory heuristic | Packing reference: controls for improvements caused merely by consolidation |
| BT | Fixed heuristic using the same measured resource telemetry as ML/RL | Separate the benefit of observing more resources from learning |
| B3 | DRS-style DQN, reproducing its resource state and utilization/balance reward where feasible | Recent literature baseline; document implementation and environment departures from DRS |
| M1 | ML-only: tabular predictor (XGBoost) and small neural predictor, using the same candidate features and labels | Compare model families and establish the fast model without an LLM |
| L1 | Direct LLM ranking of filtered candidates, including a validated response and explicit fallback | Project Approach A; measure actual inference and queueing cost |
| H1 | Selected M1 scorer + asynchronous workload profiles from an LLM | Isolate the cold-start profile contribution; keep scoring weights fixed in this comparison |
| H2 | Selected M1 scorer + periodic LLM-generated bounded scoring weights | Project Approach B; first hold the profile source equal to M1, then test H1 + H2 separately |

For B1/B2, define projected requested occupancy for candidate node `n` and resource `r` as
`u(n,r) = (reserved_requests(n,r) + incoming_request(r)) / allocatable(n,r)`.
Use equal CPU/memory weights: B1 minimizes `(u(n,CPU) + u(n,memory)) / 2`, B2 maximizes it
among feasible nodes. Track bound pods and in-flight reservations, and break ties with a
recorded seed. These are explicit request-based heuristics, not live-usage policies or exact
reproductions of every native plugin detail. Native resource scoring is described in the
[Kubernetes bin-packing documentation](https://kubernetes.io/docs/concepts/scheduling-eviction/resource-bin-packing/).

Use BT as the equal-telemetry heuristic in the focused DRS comparison: choose the node
with the lowest weighted mean normalized CPU/memory/network/disk pressure. Freeze the
normalization scales and weights using training/validation data. This separates learning
benefits from benefits of simply observing more resources.

Train M1 predictors on measured execution/slowdown labels from controlled profiling and
co-location runs, with candidate-node and workload features. Choose the node with the lowest
predicted completion cost. Use the same label budget and downstream selection rule across
model families. Ordinary logs do not supply outcomes for alternative nodes that were never
chosen; collect those alternatives through repeated controlled runs. Final evaluation must
run the policy in the cluster, not merely measure agreement with training labels.

DRS is the literature performance reference; Raith motivates the profile mechanism. The full
Raith energy scheduler is an optional comparator only with matching measurements and actions.
Decima controls Spark executors, Christensen can relocate existing pods, PAX adjusts replicas,
and LarS operates in a VM simulator: their published numbers are not interchangeable baselines
for our placement-only experiment. A restricted adaptation must be labeled as such. RL
refinement and LLM-to-ML distillation are optional extensions, not prerequisites.

### Metrics: application quality, decision latency and cost

For batch work, the primary outcome is **mean arrival-to-completion time**; P95 and the
completion/failure fraction prevent a mean improvement from hiding harmed or unfinished jobs.
For serving, the primary outcome is **SLO violation rate** at a fixed offered load and a
latency target chosen before the test. Report the following separately:

| Dimension | Definition / measurement | Reporting |
|---|---|---|
| Batch completion time (JCT) | Workload arrival recorded by the generator → terminal application completion; use one terminating task per pod initially | Mean and P95 in seconds; distinguish successful, failed and timed-out tasks |
| Batch makespan | First workload arrival → last completion for a fully completed finite batch | Seconds; otherwise mark the batch incomplete and report failed/unfinished work at timeout |
| Throughput | Successful completions divided by the declared observation duration | Jobs/s; serving uses successful requests/s |
| Serving latency and SLO | Client-observed request duration; bad requests are late, failed or timed out, counted once, divided by all issued requests | P50/P95/P99 in ms, violation %, achieved versus offered load; separate error rate |
| Placement wait | Pod API creation → first successful `PodScheduled` condition | Mean/P95/P99 in ms; includes queueing, backoff and retries, unlike pure inference |
| Model inference | Monotonic elapsed time immediately around model evaluation | P50/P95/P99 in ms, per model invocation |
| Full decision path | Scheduler-side elapsed time around each scheduling attempt and candidate-scoring RPC, with serialization, validation and retries recorded | P50/P95/P99 in ms, attempt counts and outcomes; failed attempts remain visible |
| Control-plane overhead | CPU-seconds and peak memory for scheduler, extender, monitoring and local inference processes | Per run, per submitted pod and per successful completion; report components separately |
| LLM usage / money | All calls, input/output/cache tokens, retries and background profile/policy generation | Calls and tokens; dated provider rates and currency; total cost and cost per 1,000 submitted pods |
| Training / preparation | Profiling, label generation, training, tuning and model loading | Separate one-time wall time, CPU/GPU-hours and billed cost from online operation |
| Utilization / balance | Actual resource usage plus requested/allocatable ratios, reported separately; across-node standard deviation per resource | Time series and time-weighted run summaries; account for heterogeneous capacities |
| Robustness / stability | Invalid choices, fallbacks, unschedulable pods, timeouts; repeated identical snapshots with the same model/policy version | Counts/rates, backlog, decision agreement and policy-update count |
| Energy, if supported | Integrate measured power over the full experiment, including idle nodes and background inference | Wh/run and joules/successful job, with completion performance and measured components stated |

Capture generator arrival, API creation, scheduling, container start and application finish
as distinct timestamps. Report submission/startup delays separately. For measurements crossing
machines, synchronize clocks and record their accuracy; use monotonic timers for local spans.
Watch pod state to retain timestamps before cleanup. A container's `finishedAt` is adequate
for the initial single-task workload; multi-container jobs need an explicit completion rule.

Scrape kube-scheduler and application/process metrics into Prometheus, and use Kubernetes
state watches plus client-side logs for individual completion/SLO events. Validate the metric
names and semantics exposed by the **pinned cluster version**; histogram latency is not the
same as API-create-to-scheduled wait. An extender handler timer alone cannot measure external
transport, scheduler queueing or request parsing that happened before handler entry. Use
middleware and scheduler-side spans for those boundaries. See the official
[system metrics guide](https://kubernetes.io/docs/concepts/cluster-administration/system-metrics/)
and [metrics reference](https://kubernetes.io/docs/reference/instrumentation/metrics/).

For a local LLM, report GPU/CPU time and hardware rather than inventing an API price. Report
cluster rental cost only when the experiment actually incurs it; do not double-count a VM's
rental charge as an additional GPU charge. Asynchronous inference still has a cost. Report
cold-cache and warm-cache operation separately, and measure from first workload arrival in
the cold-start experiment. The hybrid must use its fallback while a profile is unavailable,
not wait for an LLM while claiming the call is outside the critical path.

### How we isolate what the LLM adds

Freeze the selected M1 predictor and placement rule. On the same unseen workloads, change
only the source of its input profile:

1. Resource requests and a generic fallback profile.
2. Handwritten metadata rules.
3. A supervised metadata predictor, without an LLM.
4. An LLM-derived profile with uncertainty calibrated on validation workloads.
5. Short measured profiling, with profiling delay and compute charged to the method.

An already measured full signature is a privileged reference for headroom, not a fair
no-history baseline. Remove uncertainty weighting and online measurement correction in
separate ablations. A language model's stated confidence is not automatically calibrated.
Profile accuracy (resource prediction error and interval coverage) explains the mechanism;
JCT and SLO measurements establish whether it helps placement.

Metadata-capable controls receive the same manifests, commands and available history as the
LLM. Test opaque image names, different inputs for the same image and entirely held-out
application families. Split by application family before collecting training labels; do not
put replicas of the same application in both train and test. Log time-to-first-profile,
fallback frequency and results for the first 1, 5 and 10 executions per unseen application,
alongside steady state. Reset caches between independent cold-start runs; retain only history
that would truly be available within that run.

### Shared experimental procedure

1. **Freeze the setup.** Pin Kubernetes, images, model/checkpoint, prompt, sampling settings,
   feature schema, resource requests/limits, normalization and all policy-update rules.
   Verify equivalent feasible-node constraints and available candidate sampling across
   profiles. Default scoring remains native. Custom arms first use the extender boundary;
   once the plugin exists, repeat selected arms through both `extender` and `plugin`
   integrations with the same strategy configuration.
2. **Use actual workloads.** Run terminating CPU-, memory- and IO-intensive jobs, first
   separately and then mixed. Add a serving workload with fixed replica policy for SLO tests.
   Use real isolated Linux nodes/VMs, or validate CPU/memory isolation before reporting
   placement-performance results. The current kind labels do not create hardware differences.
3. **Replay exogenous arrivals.** Reuse workload definitions, input sizes and arrival times
   across arms. Submit independently of the scheduler's progress so a slow method cannot
   silently reduce its offered load. Observe live cluster state: it will legitimately differ
   because placements differ. Identical-state replay is a separate inference/stability test.
4. **Separate arms.** Run one method at a time, restore the initial cluster state and
   standardize image/data cache conditions. Record resource reservations and telemetry age.
   Randomize arm order within each seed block. Include fallbacks in the method's results.
5. **Exercise representative regimes.** Choose low/high load from a baseline capacity pilot,
   then freeze arrival rates for every arm. Test homogeneous/mixed workloads, steady/bursty
   arrivals and sustained regime changes. Hold out workload families and cluster sizes or
   hardware configurations for generalization; tune only on training/validation scenarios.
6. **Repeat and pair.** Estimate variance in pilot runs, then initially budget 5–10 independent
   runs per chosen scenario/method, with paired workload seeds and multiple training seeds.
   Report paired differences and 95% confidence intervals across runs. Pods within a run are
   correlated and must not be treated as independent experimental repetitions. Increase the
   budget if the pilot cannot resolve a practically relevant difference.
7. **Preserve failures and cost.** Set timeouts and invalid-output fallback rules before test.
   Do not drop unfinished jobs to improve the mean. If runs finish different fractions of
   work, show censored/unfinished outcomes and throughput instead of claiming a win from
   completed-only JCT. Record all decision attempts, paid calls and background tasks.

Use KWOK only for feasibility/placement-scale tests: it does not execute the application
workload. Energy experiments require actual instrumentation with stated coverage; CPU
utilization and model-estimated power are not measured energy. A local or remote inference
machine belongs in the reported cost boundary even if it is outside the Kubernetes cluster.

### Comparisons and evidence required for a claim

| Question | Comparison | Evidence |
|---|---|---|
| Does ML improve placement? (RQ1) | M1 vs B0/B1/B2/B3 | Lower batch JCT or serving SLO violations at the same offered load, with tails/failures and decision cost reported |
| Is a direct LLM competitive? (RQ2/RQ3) | L1 vs M1 and the baselines | Quality–latency–cost trade-off including queueing, invalid responses and all inference calls |
| What are the overall trade-offs and generalization limits? (RQ3) | All retained methods, including B3 and hybrids, on common scenarios | Absolute quality, utilization, decision latency, cost and stability; held-out shifts with frozen models, and hybrid adaptation reported separately |
| Does the cold-start profile help? | H1 vs the same M1 with rules or supervised metadata profiles | Better first-execution outcomes on held-out families; profile-source ablations explain the gain |
| Does asynchronous policy adaptation help? (RQ4) | H2 vs fixed weights and a simple threshold-triggered weight policy | Net benefit under regime changes, including update cost, stability and fallback behavior |
| Does combining mechanisms help? | H1 + H2 vs H1 and H2 separately | Additional gain beyond either component alone |

For a lower-is-better metric, report `gain(%) = 100 × (baseline − method) / baseline`;
for throughput, use `100 × (method − baseline) / baseline`. Show raw values and the named
baseline alongside percentages. For violation rates, show percentage-point changes too;
relative improvement is undefined when the baseline is zero. Pair effects by seed and do
not average percentages across unrelated scenarios into a headline result.

Plot application quality against total decision P95 and online cost, with separate curves
for offered load and cold/warm operation. Use a Pareto comparison rather than an arbitrary
weighted score hiding regressions. Predeclare the primary metric, meaningful improvement,
acceptable tail/failure degradation and operational latency budget after the pilot but before
the held-out tests. A failed hypothesis or an LLM that adds no value is a valid result.

### Execution order and outputs

First implement resource accounting, explicit fallback logging and timings; validate feasible
placement, no-candidate states, timeouts and asynchronous cache updates. Then build B1/B2,
the runner and a small batch pilot. Test metadata predictive value early, add M1/B3, and only
then run direct and hybrid LLM arms. Add serving and focused ablations once collection works.
This sequence is planned work: no `experiment.py` or benchmark CLI is provided yet.

The runner should produce `run.json` (configuration, hardware, seeds, versions and cost
rates), `jobs.csv`, `decisions.csv`, `llm_calls.csv`, serving request logs when applicable,
and a per-run `summary.csv` plus metric snapshots. Keep per-event records for audit and
per-run records for statistical analysis. Run inexpensive pilots across the scenario matrix,
then preselect representative scenarios for expensive LLM comparisons; rerun every compared
arm on those same scenarios.

Paper-specific rationale, limitations and alternative extensions are recorded in
[the literature review](docs/related-work.md#candidate-contributions-by-paper). DRS is
[Jian et al.](https://doi.org/10.1002/spe.3284); the profile/what-if inspiration is
[Raith et al.](https://dsg.tuwien.ac.at/team/sd/papers/CCGrid_2024_P_Raith.pdf).
