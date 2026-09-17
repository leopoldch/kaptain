# Measurement protocol

**Planned protocol. No results are claimed here.** This is the canonical description of what
we compare, what we measure, and what makes a number trustworthy. The literature that
justifies these choices is in [related-work.md](related-work.md); the pilot that applies them
first is [experiments/pilot-integration-cost.md](experiments/pilot-integration-cost.md).

Two rules govern everything below:

1. **A number without its measurement boundary is not a result.** Say where the clock starts
   and stops, and what the boundary excludes.
2. **A fallback is a decision of the method under test.** It is logged, counted and kept in
   the results, never dropped.

---

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
[the literature review](related-work.md#candidate-contributions-by-paper). DRS is
[Jian et al.](https://doi.org/10.1002/spe.3284); the profile/what-if inspiration is
[Raith et al.](https://dsg.tuwien.ac.at/team/sd/papers/CCGrid_2024_P_Raith.pdf).

---

## Comparison protocol

Updated 2026-09-09. This section states *what* is compared and *how*. The mechanics live in
the three sections that follow it: the arm inventory in "What we compare to", the
instrumentation in "How to measure each metric", the platform limits in "Testbed".

Gap A says nobody measures decision cost comparably. That gap is not closed by a better
model — it is closed by a protocol. This section is therefore the contribution, not
scaffolding for it.

### The single knob

**Exactly one thing varies across arms: the decision function.** Everything else is held
fixed, and any result where something else moved is not a result.

| Held fixed | Varies |
|---|---|
| Candidate nodes after filtering | the strategy that scores or selects among them |
| Constraints (affinities, taints, requests) | |
| State representation offered to the arm | |
| Workload: composition, arrival process, seed | |
| Cluster: size, node specs, starting state | |
| Metric definitions and collection path | |

Two rules follow, and both are violated by papers in this corpus:

**Information parity.** If one arm sees an input, every arm that could use it is given it.
If the LLM arm reads pod-spec metadata (image name, labels, command, request shape), the
ML and heuristic arms receive the same metadata. Otherwise the experiment measures access
to information, not the model class. This is the controlled experiment Pei did not run and
that gap C asks for.

**Action-space parity.** Every arm chooses a placement for the next pod among the same
candidates. An arm that also resizes, evicts or batches is answering a different question
and must be reported separately (this is why PAX is not a placement comparison).

### Confounds, and the paper that commits each

Each of these is a real failure in the corpus, not a hypothetical. Avoiding them is
individually cheap and collectively the reason our numbers are comparable when theirs
are not.

| Confound | Committed by | What it does | Our rule |
|---|---|---|---|
| Training data differs across arms | Pei (DQN on 1 scenario, LLM on 10) | conflates model class with data diversity; the generalisation claim is unattributable | same data, same scenarios, all arms |
| More than placement changes | PAX (replica count *and* placement) | the gain cannot be attributed to placement | placement-only arms; scaling arms reported apart |
| Metric mechanically favours one arm | Zhou (mean CPU % over nodes rewards consolidation by construction) | the "learned" gain is an artefact of the metric | check every metric for degenerate optima before running |
| Proxy reported without outcome | DRS (+27 % utilisation, makespan 1.03–1.06× **worse**) | a proxy win can hide an application loss | proxy and outcome always reported together |
| Arms run concurrently | `k8s/demo-workload.yaml` (ours) | arms compete for resources and perturb each other's observed state | one run per strategy, cluster reset and stabilised between runs |
| Silent degradation | `app.py` fallback (ours) | a crashed strategy becomes an invisible random baseline, since native scoring is disabled | fallback counted and reported as a metric |

### What we compare

Arms ordered by decision cost, each a reimplementation of a strategy *class* under our
protocol — never a reproduction of another paper's numbers (see "The framing that
matters"). Full feasibility table in "Arms we should add".

| Arm | Role |
|---|---|
| Default kube-scheduler | reference floor; cheap and stateless by design |
| Random, Round Robin | low controls; both appear as baselines in DRS and Pei |
| Resource-aware heuristic (LeastAllocated / Spreading / MostAllocated) | the honest heuristic, **tuned at its best** (Decima's lesson: beating an untuned heuristic proves nothing) |
| Lightweight ML (XGBoost / RF / small MLP) | plan Phase 1 |
| LLM direct (Approach A) | where synchronous LLM placement breaks, and at what cost |
| LLM-generated policy + light executor (Approach B) | the hybrid; RQ4 |
| Bayesian optimisation | PAX's method; the sample-efficient ML baseline with no pretraining |
| Offline CP-SAT oracle | **ceiling, not a scheduler** — supplies the denominator |

The oracle is what turns "+3 % vs default" into "X % of the gain that was actually
available". Christensen's 19 % (default already provably optimal on hard instances) is the
reason an unbounded delta means little.

### What we measure

Three families. **Reporting one without the others is the corpus's characteristic failure.**

1. **Workload outcome** — makespan, JCT, throughput, pending pods, SLO violations. Never
   optional. Its absence is the whole criticism of Zhou.
2. **Resource proxy** — requested-resource utilisation (Σ requests ÷ allocatable; preferred
   over live metrics on kind), inter-node imbalance, fragmentation. Reported *beside*
   outcome, never in place of it.
3. **Decision cost, decomposed** — transport / inference / serialisation / bind, kept
   separate. DRS's 38.2 ms is 35 ms of transport; aggregating the four would charge the
   model for a cost that is not its own. Plotted against the interval between scheduling
   events (Decima Fig. 15b), not in absolute terms.

For LLM arms, decision cost also includes tokens, wall time and $ or GPU-seconds per
decision — the number RQ4 depends on and that nobody has published.

### Unit of analysis

The **run**, not the pod. Pods within a run are correlated: each placement changes the state
the next decision observes, so pods are not independent samples and treating them as such
inflates significance.

- One run = one (arm × scenario × seed), on a reset and stabilised cluster.
- Seeds shared across arms, so arms see identical arrival sequences.
- ≥ 5 runs per cell as a starting budget — a starting point, not a guarantee of power.
- Report mean **and** dispersion, plus a significance test on any claimed difference.
- Record commit, image digest, cluster config and workload seed per run.

Zhou is the standing counter-example: 5 trials, CV up to 5.35 %, claimed gaps of 10–20 %,
no test — and a headline mean that does not reconcile with its own table.

### Scenarios

- lightly, moderately and heavily loaded cluster;
- CPU-, memory-, IO- and network-intensive workloads;
- homogeneous and heterogeneous mixes;
- regular, Poisson and bursty arrivals;
- abrupt load change (regime shift);
- **new nodes or never-seen workloads** (cold start) — gap D, where the default has no
  history to fall back on and the LLM's information advantage is structural.

### Adaptation

Compare a fixed policy, periodic adaptation, and adaptation triggered only on detected
change. **Report the gain net of adaptation cost and of the disruption caused.** PAX is the
evidence this matters: freezing a good configuration beat reconfiguring every 2 minutes by
5×, so adaptation frequency belongs inside the objective function, not outside it.

### What makes this executable

The protocol above is currently blocked on four items in the code audit: no latency
instrumentation, non-terminating no-op pods, concurrent arms, and the silent fallback.
Until those are fixed, no arm in the table can produce a comparable number.

---

## What we compare to, and what we should compare to

### The framing that matters

**Do not attempt to reproduce other papers' numbers.** Nothing in this corpus is
reproducible: different clusters, workloads, metrics and objectives. That is Senjab's
complaint and DRS's own admission, quoted above.

What we compare is **our reimplementation of their strategy class, under our protocol**.
"Our Round Robin scores X on our bench" is honest; "we reproduce DRS's 27.29 %" is not.
The common-protocol version is also the *stronger* claim, because the missing common
protocol is precisely the gap we are targeting.

### Arms available today

| Arm | Implementation | What it establishes |
|---|---|---|
| Default kube-scheduler | in-cluster, pods without `schedulerName` | reference point, free |
| Random | `dummy_random.py` | faithful to the Random baseline in DRS and Pei |
| Largest CPU capacity | `largest_cpu_capacity.py` | **not a baseline yet** — see below |

`largest-cpu-capacity` reads `status.allocatable.cpu`, i.e. **static node capacity**. On a
homogeneous kind cluster every worker reports the same value, so `max()` returns the first
node in list order. It is a constant, not a policy. It becomes meaningful only on a cluster
with genuinely different node sizes, and even then it ignores current load.

**Current honest status: the harness proves the plumbing works. It produces no comparable
number yet.**

### Arms we should add, in order of cost

| Arm | Source paper(s) | Feasible now? | What it needs |
|---|---|---|---|
| Round Robin | DRS, Pei | yes | ~10 lines; appears as a baseline in **two** papers |
| LeastAllocated (resource-aware) | Christensen, kube-scheduler's own policy | yes, with work | allocatable − Σ requests of pods already bound → needs a K8s API client in the extender |
| MostAllocated / packing | Raith (Packing), Zhou (SDQN-n consolidation) | yes, with work | same accounting, opposite direction |
| Spreading | Raith (Spreading = default behaviour stand-in) | yes, with work | same accounting |
| Bayesian optimisation | PAX | later | offline trial loop, needs a metric to optimise first |
| Offline CP-SAT oracle | Christensen | later | not a scheduler — a **ceiling** for reporting |
| DRS-style DQN | DRS | Phase 1 | per-node net/disk telemetry + training loop |
| Raith / PAX / Pei as published | — | **no** | different problem setups (energy hardware, replica scaling, VM simulator) |

Adding Round Robin, LeastAllocated, MostAllocated and Spreading gives four arms that
correspond to strategy classes named across the corpus, all on one protocol. That alone is a
publishable table.

### Confounds in our own bench, to remove before any number is reported

Design-level confounds — and the paper that commits each — are tabulated in
[Comparison protocol](#comparison-protocol). These four are specific to the current kaptain
setup.

1. **Concurrent arms.** `k8s/demo-workload.yaml` runs `demo-ours` and `demo-default`
   simultaneously in the same cluster. They compete for the same resources and each one's
   placement changes the state the other observes. Every paper in the corpus runs arms
   **sequentially on a reset cluster**. Fix: one run per strategy, cluster (or at minimum
   namespace) reset and stabilised between runs.
2. **No-op workload.** Two `busybox sleep 3600` pods consume nothing, have no duration
   variance and no arrival process. This is exactly what Zhou is criticised for above.
3. **Cosmetic heterogeneity.** `hw-class: fast/slow` labels have no physical backing on kind.
   Any heterogeneity result would be fabricated.
4. **Silent fallback.** See code audit.

---

## How to measure each metric

The stack currently measures nothing. This section is the reference for what to instrument,
where the number comes from, and what makes it wrong.

### Decision cost — our central contribution (gap A)

| Metric | Source | Pitfall |
|---|---|---|
| **Transport latency** | timestamp on entering `/prioritize` minus scheduler-side send; or measure round-trip scheduler→extender and subtract in-handler time | Cannot be read from inside the extender alone. Simplest proxy: total handler time vs `scheduler_extender_duration_seconds` observed by kube-scheduler. |
| **Inference latency** | wrap `strategy.select()` in `time.perf_counter()` | This is the number that must scale with model size; keep it isolated from JSON handling. |
| **Serialisation** | time around request parse and `JSONResponse` build | Grows with cluster size (the whole NodeList is serialised per pod). Worth reporting as a function of node count. |
| **Total extender time** | handler entry → response | |
| **End-to-end scheduling latency** | kube-scheduler's own Prometheus metric `scheduler_e2e_scheduling_duration_seconds`, and `scheduler_framework_extension_point_duration_seconds` | Requires scraping the scheduler's `/metrics`; gives the framework's view, independent of our instrumentation. Use it to cross-check. |
| **Pod pending time** | `PodScheduled` condition `lastTransitionTime` − `metadata.creationTimestamp` | Includes queueing, not just decision. Report separately from decision latency. |

Report **mean and P95/P99**, and follow Decima Fig. 15b: plot decision latency **against the
interval between scheduling events**, not in absolute terms. A 250 ms decision is cheap if
events arrive every 20 s and fatal if they arrive every 100 ms.

### Placement quality

| Metric | How to capture | Pitfall |
|---|---|---|
| **Node utilisation** (CPU, mem) | metrics-server (`kubectl top nodes`) or Prometheus + node-exporter | See testbed constraints below — on kind these are not isolated per node. |
| **Requested-resource utilisation** | Σ requests of bound pods ÷ allocatable, per node | Independent of the runtime, so it works even where live metrics are unreliable. Prefer it on kind. |
| **Imbalance** | std dev across nodes, per resource; DRS's weighted sum `Σ_k weight_k · std_k` | Must state which resources and which weights, or it is not comparable. |
| **Fragmentation** | per node: allocatable − Σ requests; report the distribution, and how many pending pods would have fit in the aggregate free space | This is Christensen's whole premise and it is cheap to compute. |
| **Pending pods** | count of pods in `Pending` with reason `Unschedulable` | The clearest failure signal, and the one Christensen optimises. |

### Workload outcome

| Metric | How to capture | Pitfall |
|---|---|---|
| **Job completion time (JCT)** | `metadata.creationTimestamp` → `status.containerStatuses[].state.terminated.finishedAt` | Only meaningful with pods that actually terminate. Use `restartPolicy: Never` / Jobs, not `sleep 3600`. |
| **Makespan** | first pod creation → last pod completion for one run | Sensitive to stragglers; report alongside JCT, never alone. |
| **Throughput** | pods completed per unit time | |
| **SLO violations** | fraction of requests above a latency target | Needs a **serving** workload (DeathStarBench + `wrk2`), not batch pods. Out of scope until then — do not claim SLO results with batch pods. |

### Robustness and cost of the policy

| Metric | How to capture | Why it matters |
|---|---|---|
| **Fallback rate** | counter incremented in the `except` branch of `/prioritize`, exposed on `/healthz` or a `/metrics` endpoint | A crashing strategy currently degrades to an invisible tie. This is a reportable metric, not just hygiene. |
| **Decision stability** | replay identical cluster states and count distinct placements; or count placement changes / evictions across a run | Named in the project plan; measured by nobody in the corpus. |
| **Number of moves / reconfigurations** | eviction and rebinding events | Christensen minimises exactly this; PAX shows reconfiguration can cost more than it earns. |
| **Telemetry cost** | CPU of the monitoring path | DRS reports 3.27 % — a directly comparable figure. |
| **Inference cost** | for LLM arms: tokens, wall time, and $ or GPU-seconds per decision | Nobody has published this. It is the number RQ4 depends on. |
| **% of achievable gain** | offline CP-SAT oracle over the same instance (Christensen's method) | Turns "+X % vs default" into "X % of the gain that was actually available". The strongest reporting upgrade available to us. |

### Statistical protocol

- Fixed seeds for arrival processes and for `dummy-random`.
- ≥ 5 repetitions per (arm × scenario), report mean **and** dispersion.
- A significance test on any claimed difference. Zhou's 5 trials with CV up to 5.35 % cannot
  support a claimed 10–20 % gap; do not repeat that mistake.
- Record the exact commit, image digest, cluster config and workload seed per run.

---

## Testbed: what each platform can support a claim about

The stack (kind config, extender image, scheduler profile) is platform-independent, so
migrating is cheap. What changes is **which claims become defensible**.

### The limit that moving OS does *not* fix

**kind "nodes" are containers sharing one kernel, on every platform.** Moving from macOS to
Linux does not give per-node resource isolation by itself. A pod that saturates CPU on
`worker-2` steals from `worker-1`, because they are the same machine.

This matters because half the corpus reports **per-node utilisation and imbalance** (DRS's
whole objective is `α·AvgUtil − β·Imbalance`). Measuring imbalance across nodes that are not
actually isolated produces a number that describes the host, not the placement.

Two real fixes, in increasing order of fidelity:

1. **cpuset pinning per node container** — on Linux, give each kind node container a disjoint
   set of physical cores (`--cpuset-cpus`) and a memory limit. Cores stop being shared, so
   per-node CPU numbers become meaningful. Cheap, and it also lets us build *genuine*
   heterogeneity (4 cores vs 2 cores) instead of the cosmetic `hw-class` labels.
2. **Separate VMs, one node each** (KVM/libvirt, or cloud instances). Real kernels, real
   isolation. **This is exactly DRS's setup — 5 VMs with deliberately different core counts,
   memory and disk speeds** — and is therefore the most faithful match to the closest paper
   in the corpus.

### Platform comparison

| Platform | Per-node isolation | Energy (RAPL/Kepler) | Metrics fidelity | Verdict |
|---|---|---|---|---|
| **kind on macOS** (current) | no — one Docker VM | **no** | cgroup view is the VM's, not the host's | correctness and plumbing only |
| **kind on Windows** (WSL2 / Docker Desktop) | no — still a VM | **no** — WSL2 does not pass through RAPL | VM's view | no real gain over macOS; not worth the move on its own |
| **kind on bare-metal Linux** | with cpuset pinning: yes | **yes**, if `/sys/class/powercap/intel-rapl` exists | real host cgroups | **best single-machine option** |
| **Several Linux VMs / machines** | yes, genuinely | only if the hypervisor exposes it | real | most faithful to DRS; best for imbalance claims |
| **Cloud VMs** | yes (separate instances) | usually **no** — RAPL is normally not exposed to guests; bare-metal instance types are the exception | real | good for scale and isolation, not for energy |
| **KWOK** (any platform) | n/a — no workload actually runs | n/a | n/a | scale-out scenarios only (Christensen's choice), never for performance or energy |

### What bare-metal Linux unlocks

- **Energy measurement**, and with it the whole Raith / PAX / "green scheduling" line of
  comparison — which is currently entirely out of scope.
  Verify first that `/sys/class/powercap/intel-rapl` (Intel) or the AMD equivalent is
  present and readable. **Caution: Kepler falls back to a model-based estimator when RAPL is
  unavailable.** That produces plausible-looking numbers that are not measurements. Confirm
  Kepler is in a real-counter mode before reporting any watt figure.
- **Trustworthy utilisation and imbalance metrics** — cAdvisor and node-exporter read real
  host cgroups instead of a VM's view. This is what makes a DRS-style comparison possible at
  all.
- **Genuine heterogeneity** via cpuset/memory limits per node, replacing the cosmetic labels.
- **More nodes per machine**, though KWOK stays the answer beyond ~8.

### Which papers become reachable

| Paper | Setup | Reachable on bare-metal Linux? |
|---|---|---|
| DRS | 5 VMs, heterogeneous (2–4 cores, 2–4 GB, disks 269–678 MB/s) | **yes** — closest match in the corpus, and the one worth targeting |
| Zhou | 4 workers, 50 pods | **yes** |
| Christensen | KWOK, 4–32 nodes | **yes**, already (KWOK is platform-agnostic) |
| Raith | 3 nodes, dual AMD 7443, one A100X | energy measurement yes; their hardware no |
| PAX | CloudLab, 2014 vs 2021 servers, embodied-carbon argument | no, unless old hardware is available |
| Pei | VM simulator | n/a — not a cluster experiment |

### Rules that follow, regardless of platform

- **All arms of a comparison must run on the same machine.** Decision latency is
  machine-dependent, so a number from the laptop and a number from the desktop are not
  comparable. Record CPU model, core count, kernel and container runtime with every run.
- **Report the isolation mode** (shared kind / cpuset-pinned kind / separate VMs) next to any
  per-node utilisation or imbalance result. Without it the number is uninterpretable.
- **Do not mix**: pick one testbed per research question, and re-run all arms on it.

### Recommended split

- **Bare-metal Linux, cpuset-pinned kind or local VMs** → placement quality, imbalance,
  decision cost, energy. The main testbed.
- **KWOK on anything** → scale-out (16/32 nodes), pending-pod and fragmentation scenarios.
- **macOS laptop** → development and correctness only. No reported numbers.


---

---

## Reusable experimental assets

| Asset | Source | Use |
|---|---|---|
| KWOK (Kubernetes WithOut Kubelet) | Christensen | reproducible large-scale scheduling sim |
| Deterministic default scheduler (lexicographic Score plugin, `parallelism=1`, preemption off) | Christensen | reproducible baseline traces |
| `jolyonjian/apps:{cpu,net,io}-1.0` | DRS | three resource archetypes |
| Workload mixes 1:1:1 / 4:1:1, `T ~ N(20,1)` | DRS | arrival patterns |
| `stress-ng` profiles (cpu, memrate, vm, iomix) | Raith | synthetic resource phases for training data |
| Rodinia (LavaMD, Leukocyte, srad) | Raith | short compute jobs, 10–100 s |
| DeathStarBench hotel reservation + `wrk2` | PAX | 19-microservice P99 workload |
| Kepler (eBPF + Prometheus) | PAX | per-node power — needs RAPL, so **bare-metal Linux only**; verify it is not in estimator mode |
| cAdvisor + NodeExporter + Prometheus (1 s scrape) | Raith | container + host metrics |
| ACT | PAX | embodied carbon accounting |
| Alibaba cluster-trace-v2018 | Decima, KEIDS | production trace replay |
| `github.com/henrikdchristensen/scheduler-plugins` | Christensen | CP-SAT plugin reference impl — **not pursued** (2026-09-17): outside the study direction |
| `github.com/JolyonJian/DRS` (no licence) | DRS | state/action/reward to reimplement as baseline B3; see [`reproduction/README.md`](../../reproduction/README.md) |
| `github.com/hongzimao/decima-sim` (no licence, TF1) | Decima | simulator study only, not a Kubernetes baseline |
| `scheduler_e2e_scheduling_duration_seconds`, `scheduler_framework_extension_point_duration_seconds` | kube-scheduler `/metrics` | independent cross-check on our own timing |

---
