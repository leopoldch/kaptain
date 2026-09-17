# Related Work — ML/LLM Scheduling for Kubernetes

Structured literature review for IFT-7026 (Université Laval, supervisor: Prof. Mohamed
Aymen Saied). Covers the 7 research papers and the survey in the project folder, plus adjacent work
found online.

Merged from two independent reading passes (Claude + Codex). Where the two passes disagree
on a number, both are recorded under **Contested** rather than silently resolved.

For each paper: what is new, what it actually measures, what it does *not* establish, and
what it changes for us.

**Start with [Plan alignment and recommended scope](#plan-alignment-and-recommended-scope)**
for the current recommendation, then [Candidate contributions by paper](#candidate-contributions-by-paper).
The September 8 anchor decision is retained as historical reasoning, not a newly confirmed choice.

Last updated: 2026-09-09.

**Source PDFs** live in `../../` (`/Users/leo/Documents/ULaval/IFT7026/`), not tracked here.

---

## Contents

- [Evaluation grid](#evaluation-grid)
- [Comparison table](#comparison-table)
- [**Plan alignment and recommended scope**](#plan-alignment-and-recommended-scope)
- [Candidate contributions by paper](#candidate-contributions-by-paper)
- [**Contribution and anchor decision**](#contribution-and-anchor-decision)
- [1. Mao et al. 2019 — Decima](#1-mao-et-al-2019--decima)
- [2. Jian et al. 2024 — DRS](#2-jian-et-al-2024--drs)
- [3. Christensen et al. 2025 — Constraint-based pod packing](#3-christensen-et-al-2025--constraint-based-pod-packing)
- [4. Raith et al. 2024 — Energy-aware GNN scheduler](#4-raith-et-al-2024--energy-aware-gnn-scheduler)
- [5. Dong et al. 2025 — PAX](#5-dong-et-al-2025--pax)
- [6. Pei et al. 2025 — LarS](#6-pei-et-al-2025--lars)
- [7. Zhou et al. 2026 — SDQN / SDQN-n](#7-zhou-et-al-2026--sdqn--sdqn-n)
- [8. Senjab et al. 2023 — Survey](#8-senjab-et-al-2023--survey)
- [Adjacent work found online](#adjacent-work-found-online)
- [What the corpus actually establishes](#what-the-corpus-actually-establishes)
- [Synthesis: the four gaps](#synthesis-the-four-gaps)
- [Recommended research question](#recommended-research-question)
- [**Comparison protocol**](#comparison-protocol)
- [What we compare to, and what we should compare to](#what-we-compare-to-and-what-we-should-compare-to)
- [How to measure each metric](#how-to-measure-each-metric)
- [Testbed: what each platform can support a claim about](#testbed-what-each-platform-can-support-a-claim-about)
- [kaptain code audit](#kaptain-code-audit)
- [Reusable experimental assets](#reusable-experimental-assets)
- [Priorities](#priorities)
- [Open questions / to verify](#open-questions--to-verify)

---

## Evaluation grid

The recurring failure of this literature is that every paper optimises a different
objective on a different cluster with a different workload, so results are not comparable
(Senjab says so explicitly; DRS says so explicitly). Whenever we read or cite a scheduler,
record these eight fields:

1. **observed information** — what the scheduler can see;
2. **allowed actions** — placement only? replicas? preemption? migration?
3. **optimised objective** — and whether it is a proxy or a user-visible metric;
4. **decision cost** — split into transport / inference / total;
5. **cluster size and type** — real, simulated, homogeneous, heterogeneous;
6. **workload** — synthetic, benchmark suite, production trace;
7. **repetition protocol** — trials, seeds, variance, statistical test;
8. **robustness** — behaviour under distribution shift, unseen apps, unseen cluster sizes.

This grid is itself a candidate contribution: no paper in the corpus fills all eight.

---

## Comparison table

| Paper | Approach | Integration | State representation | Objective | Decision latency | Eval scale |
|---|---|---|---|---|---|---|
| Decima (2019) | RL (REINFORCE) + GNN | Spark RPC service | DAG graph embeddings (node/job/global) | avg JCT, makespan | **< 15 ms** (12.7k params, 50 KB) | 25-node Spark + Alibaba trace (20k jobs, 30k executors sim) |
| DRS (2024) | DQN | K8s Scheduling Framework, 2nd scheduler; decision maker over sockets | 6 metrics/node flat vector (CPU, mem, net rx/tx, disk r/w) + pod request | `α·AvgUtil − β·Imbalance` | **~38 ms total, ~35 ms of it transport** (contested, see below); kube-scheduler 4.3 ms | 5 VMs, 300 pods |
| Christensen (2025) | CP-SAT (OR-Tools), no learning | Scheduler plugin, 5 extension points; Python solver over HTTP | Explicit multi-knapsack model per priority tier | max placed pods per priority, then min moves | **1–20 s** (configurable timeout) | KWOK sim, 4/8/16/32 nodes |
| Raith (2024) | HGT (heterogeneous GNN) power prediction | Custom Python scheduler via K8s client | **Heterogeneous host graph** (Host/CPU/Mem/Disk/Net/GPU/FPGA/Fan/Temp/Pressure/SmartNIC) + app signature | min estimated energy | **~250 ms** | 3 nodes (dual AMD 7443, 1× A100X) |
| PAX (2025) | Bayesian optimisation (ax-platform) | Bypasses default scheduler, `patch_namespaced_deployment` | Black-box: (replicas per pod type, node placement) | min P99 latency | n/a (offline trial budget, then frozen) | 2–4 nodes CloudLab, DeathStarBench 19 microservices |
| LarS (2025) | DRL-filtered GPT-4o trajectories → LoRA fine-tuned LLaMA-2-13B | **Simulator only, not Kubernetes** | Text prompt (VM states, queue waits, objectives) | cost + QoS success rate | **NOT MEASURED** | Simulated 8/11/20/40 VMs |
| SDQN (2026) | DQN (6→32→1 MLP) | Replaces default scheduler | 6 scalars (CPU%, mem%, pod ratio, health, uptime, pod count) | hand-written points table | not measured | 4 worker nodes, 50 no-op pods |

Decision cost on one axis: **15 ms → ~38 ms → 250 ms → 1–20 s → LLM = unknown.**
Nobody has placed these on a common axis, and the LLM number does not exist anywhere.

---

## Plan alignment and recommended scope

**September 9 assessment, requested after the per-paper contribution discussion.** This is
a recommendation to test, not a claim that the author has approved a new research anchor.
Source of requirements: `../../Plan_projet_IFT-7026.pdf`, especially sections 3–7.

### Recommended approach

The best fit for the semester is **a fast placement model assisted by an asynchronous LLM**,
with **cold-start workload estimation inspired by Raith** as the candidate scientific
contribution. DRS provides a practical literature baseline. Keep the action space to placement
of the next pod, as in the project's Filter → Score architecture.

Research hypothesis: metadata from an application that has never run can provide a useful
initial resource profile; an empirically calibrated confidence mechanism determines how much
the placement model trusts it, and actual measurements progressively replace that prior.
The intended outcome is better first placements without an LLM call blocking each decision.
This is a hypothesis, not evidence that image names determine resource consumption.

The full Raith energy scheduler need not be reproduced for this hypothesis. Without matching
its energy objective and experimental conditions, describe our method as inspired by its
missing-signature problem; do not claim to outperform Raith itself. A DRS reproduction or
clearly documented DRS adaptation satisfies the plan's requirement for a recent literature
approach, while the semantic-profile ablations establish the specific contribution.

### Coverage of the plan

| Plan requirement | Concrete implementation / experiment | Status in proposed scope |
|---|---|---|
| Phase 0: reproducible real Kubernetes testbed, default + resource-aware + recent-paper baseline | Default scheduler; resource-aware heuristic; DRS reference implementation or documented reimplementation; seeded workload generator and metric collection | Required |
| RQ1 / Phase 1: ML placement and comparison of model families | Fast candidate scorer using requests, node state and workload profile; compare a tabular model such as XGBoost with a small neural scorer on the same features and training split | Required; RL is optional in the plan |
| RQ2 / Approach A: LLM direct | Give the LLM the pod, filtered candidate nodes and the same observable cluster state; validate its output, time the entire call and record fallbacks | Required experimental arm, even if it performs poorly |
| RQ3: ML vs LLM quality, cost, generalization | Same arrivals, hardware, feasible actions and observable data; compare completion / serving metrics and total decision overhead on unseen workload families and cluster configurations | Required |
| RQ4 / Approach B: asynchronous LLM policy adaptation | LLM periodically analyzes telemetry/history and emits bounded scoring weights or a structured policy consumed by the fast scorer; cache workload priors separately | Required to cover Approach B literally |
| Candidate contribution: cold start | Unseen workload → metadata-derived profile + calibrated uncertainty → conservative weighting → correction with first observations | Main research hypothesis |
| Approach C: LLM → ML distillation | Train a student from LLM decisions if A/B first show useful signal worth transferring | Explicitly optional in the plan |
| Model size, inference cost, stability | Small controlled comparison of model sizes; repeated identical-state decisions; transport/inference/total timings; fallback and policy-change counts | Required characterization |
| GPU workloads | Add only if appropriate hardware is available | Explicitly conditional in the plan |
| SLO violations | Add a small serving workload with a declared latency target and controlled request generator | Needed for SLO claims; batch jobs alone cannot measure this |

**Important distinction:** generating a resource profile once is not, by itself, the periodic
policy-generation experiment described in Approach B. Implement the bounded weight-update
arm as well, and separate its effect from the cold-start profile through ablations. Keep the
policy language small (validated numeric weights, not generated executable code).

### Experimental arms and attribution

Use a shared filtering/constraint path and a documented scoring configuration for all custom
arms. Keep the unmodified default scheduler as the production reference.

| Arm | Purpose |
|---|---|
| Kubernetes default | Required practical reference |
| Resource-aware heuristic | Tests whether additional resource information alone suffices |
| DRS reproduction / documented adaptation | Recent literature reference; report departures from the paper |
| ML-only, tabular and small neural variants | RQ1 and lightweight comparator for RQ3 |
| Direct LLM (Approach A) | RQ2 and actual quality/latency/cost trade-off |
| Fast scorer + periodic LLM weight updates (Approach B) | Isolates asynchronous policy adaptation |
| Same hybrid + uncertain cold-start profiles | Tests the proposed contribution |

Give non-LLM controls access to the same metadata. In a focused cold-start experiment,
compare semantic LLM profiles with rules, a supervised metadata predictor and short measured
profiling (including profiling cost). Use an already measured signature only as a privileged
reference, not as a deployable no-history baseline. Remove uncertainty weighting and online
correction separately. Calibrate uncertainty on validation data; a self-reported LLM
confidence score is not sufficient evidence of calibration.

The first decision must not wait for the hybrid LLM: use a fallback profile until an
asynchronous result is ready. Measure fallback rate, time until useful profile availability,
and benefit during the first executions. Precomputing profiles is allowed only if deployment
metadata would actually be available that early, with the lead time reported equally.

### Tests, metrics and execution order

1. **Integration correctness:** pods always go to feasible nodes; pending/no-candidate states,
   invalid model responses and model timeouts are observable; cached profile/policy updates
   take effect without blocking the placement path. Verify that custom scores actually
   control the configured scheduler and account for concurrent resource reservations.
2. **Pilot benchmark:** default, resource-aware heuristic and ML-only on terminating CPU,
   memory and IO jobs. Verify timestamps, isolation, completion and reset between runs before
   training or running expensive LLM experiments. Current `largest_cpu_capacity` uses static
   capacity and is not yet the required resource-aware baseline.
3. **Cold-start feasibility gate:** before the full hybrid, test whether metadata predicts
   resource behavior on held-out application families better than simple rules. Vary inputs
   and commands, include opaque image names, and prevent application-family leakage. If no
   placement-relevant signal survives, stop treating semantic profiles as the contribution;
   retain A/B for plan coverage and investigate interference prediction as the alternative.
4. **Main batch experiment:** homogeneous and heterogeneous mixes, low/high load, bursts
   and regime changes. Measure arrival-to-completion mean/P95, makespan for finite batches,
   throughput, pending time and resource use. Report decision P50/P95/P99 and split transport,
   model inference and total overhead. Do not equate utilization with application performance.
5. **Generalization experiment:** hold out application families and cluster configurations;
   keep tuning on training/validation only. Distinguish application, input-size and hardware
   shifts. Measure cold-start benefit over the first executions and steady-state behavior.
6. **Serving experiment:** one manageable service benchmark, fixed SLO, controlled request
   rate and load shifts. Compare P99 and violation rate with the same replica policy across
   placement arms; do not silently add autoscaling to only one arm.
7. **Focused ablations and cost:** profile source, uncertainty, measurement correction and
   periodic policy updates; compare selected model sizes. Report LLM tokens, total calls,
   wall time and monetary or GPU-time cost, including asynchronous work. Repeated-state
   stability uses an identical cached policy version so legitimate adaptation is not noise.

Use common workload seeds across arms, independent training seeds for learned methods,
separate/reset runs and recorded commits/configurations. Start with a small pilot to estimate
variance, then plan repeated runs (initially 5–10 per chosen scenario) and report paired
effect sizes and confidence intervals. This count is a starting budget, not a statistical
guarantee. Use complete runs as experimental units rather than treating correlated pods as
independent repetitions. Stage expensive direct-LLM experiments on predeclared representative
scenarios; all compared arms must run those same scenarios.

For application-performance claims, use isolated Linux nodes/VMs or a validated CPU-pinned
testbed. KWOK supports placement/scale experiments, not application execution. Energy is an
optional extension requiring suitable instrumentation; it is not necessary to make this
project a literal reproduction of Raith. Record real measurements versus energy estimates.

### Semester scope and decision

- **September:** choose the cold-start hypothesis, freeze comparison rules and validate data access.
- **October:** build instrumentation, workload generator, isolation and baselines; run the
  metadata feasibility gate before committing the remaining semester to it.
- **November:** compare tabular/neural placement, implement direct LLM and bounded asynchronous
  policy generation, then add the profile mechanism if the feasibility gate passed.
- **Late November–December:** run focused batch/serving experiments, generalization and
  ablations; analyze and write. Distillation, RL and GPU remain extensions.

This covers the plan without requiring the combined construction of a simulator, a global
relocation controller, CP-SAT distillation, RL refinement and a semantic LLM. Christensen's
learned partial optimization is a strong separate research option, but introduces actions
outside the current placement extender. PAX also requires replica control for a direct
comparison. DRS interference prediction is the most compatible alternative if metadata-based
cold-start prediction fails its early experiment.

## Candidate contributions by paper

**September 9 discussion recorded at the author's request.** These are proposed mechanisms
and falsifiable hypotheses, not demonstrated gains or verified first-of-their-kind claims.
Compare on the same available information, action space, hardware and decision budget.

### 1. Mao / Decima — reliability-aware policy selection

- **Add:** learn when to use the RL policy versus a tuned heuristic from prediction
  disagreement and recent observed errors; retain the original DAG scheduling action space.
- **Hypothesis:** preserve familiar-workload gains while reducing tail completion times and
  starvation after distribution shifts.
- **Baselines:** original Decima, multi-distribution-trained Decima, tuned weighted fair,
  and a simple threshold-based switching rule.
- **Test:** sudden changes in job sizes and arrivals; mean/P95 JCT, starvation and recovery.
- **Fit:** a direct extension needs a DAG execution environment; Kaptain would transfer the
  mechanism rather than reproduce Decima. Robustness training alone is already discussed in
  [Decima](https://web.mit.edu/decima/content/sigcomm-2019.pdf).

### 2. Jian / DRS — placement interference prediction

- **Add:** predict the slowdown of the arriving pod and affected resident pods for each
  candidate node, then minimize aggregate predicted completion cost.
- **Hypothesis:** workload compatibility explains consequences that current resource
  utilization alone misses, especially for shared disk/network bottlenecks.
- **Baselines:** DRS, the same DQN with completion-based reward, equal-telemetry heuristic,
  and a simple slowdown predictor with greedy selection.
- **Test:** CPU/memory/IO/network combinations, including unseen combinations; JCT and
  slowdown relative to isolated execution. Obtain labels from controlled co-location runs;
  unobserved alternative placements are not directly present in ordinary telemetry.
- **Fit:** strong placement-only alternative; separate the contribution of interaction
  modeling from reward changes and richer observations.

### 3. Christensen — learned partial reoptimization

- **Add:** select a small set of pods/nodes contributing to a blockage; let CP-SAT optimize
  that neighborhood with the remaining placements fixed, expanding it if necessary.
- **Hypothesis:** better admission/disruption trade-off within a fixed compute budget.
- **Baselines:** full CP-SAT at equal timeout, random neighborhoods, fragmentation-based
  neighborhoods, and a learned initial solution supplied to the full solver.
- **Test:** increasing cluster sizes and priority tiers; admissions per priority,
  relocations, feasibility and decision P95. Subproblem optimality is not global optimality.
- **Fit:** requires relocation capabilities beyond the current scoring extender. A
  placement-only distilled teacher must instead be restricted to the same placement actions.
  CP-SAT labels can be merely feasible at timeout; retain solver status and bounds.
- **Novelty boundary:** [learning-guided neighborhood search](https://arxiv.org/abs/2302.13797)
  already exists; distinguish Kubernetes priorities, fragmentation and disruption costs.
  Anchor: [Priority Matters](https://arxiv.org/abs/2511.08373).

### 4. Raith — uncertain cold-start signatures

- **Add:** estimate an initial profile from manifest/command/image metadata, optionally
  using an LLM; calibrate uncertainty and replace the estimate with initial measurements.
- **Hypothesis:** improve first placements of unseen applications without blocking every
  scheduling decision on LLM inference.
- **Baselines:** Raith's fallback, family rules, equal-metadata tabular predictor, short
  measured profiling including its cost, and measured signatures as a privileged reference.
- **Test:** held-out application families, first-execution performance and convergence;
  energy only if measured appropriately. Do not infer actual demand from image identity alone.
- **Fit:** best semantic LLM hypothesis for the plan. A resource-profile experiment with a
  different objective is an adaptation, not a direct win over the full Raith scheduler.
  Sources: [Raith](https://dsg.tuwien.ac.at/team/sd/papers/CCGrid_2024_P_Raith.pdf),
  [LLM placement-preference interpretation](https://arxiv.org/abs/2601.09282).

### 5. Dong / PAX — adaptation that accounts for transition cost

- **Add:** contextual Bayesian optimization with a reconfiguration trigger based on
  expected benefit over its useful lifetime minus restarts, cache warmup and trial costs.
- **Hypothesis:** adapt to lasting changes without chasing transient spikes.
- **Baselines:** frozen PAX, periodic reoptimization, latency-threshold triggering and
  HPA/default placement; equal trial budgets across PAX variants.
- **Test:** load plateaus, short spikes and workload-mix shifts; whole-run P99, SLO
  violations, energy and reconfigurations, including exploration and transition periods.
- **Fit:** clear hypothesis, but a direct [PAX](https://hotcarbon.org/assets/2025/paper-80.pdf)
  comparison also needs replica control. A drift detector alone is a weaker contribution
  than modeling whether changing configuration is worth its cost.

### 6. Pei / LarS — compact cross-scenario expert transfer

- **Add:** distill multi-scenario experts into a shared candidate scorer with a global
  set representation, supporting variable node counts and permutation-equivariant scores.
- **Hypothesis:** retain cross-scenario transfer with lower inference overhead than an LLM.
- **Baselines:** LarS, a small network trained on identical demonstrations, multi-scenario
  DQN, resource-aware heuristic, and an ablation without the set representation.
- **Test:** unseen cluster sizes/configurations and reordered node identifiers; control
  data diversity, information and actions, and include actual inference in response time.
- **Fit:** compatible with the ML/generalization track; distillation alone is not new.
  Anchor: [LarS](https://link.springer.com/article/10.1186/s13677-025-00822-0).

### 7. Zhou / SDQN-n — dynamic active-node count

- **Add:** choose active-node count from expected load, wakeup delay, transition energy
  and a performance target rather than favoring a fixed number of nodes.
- **Hypothesis:** consolidate only when an idle interval amortizes the power transition,
  and restore capacity before saturation.
- **Baselines:** fixed SDQN-n, bin packing, hysteresis threshold controller and the same
  controller without forecasting.
- **Test:** alternating quiet/busy phases; joules per completed job, JCT and power cycles.
  Average CPU utilization is not energy evidence.
- **Fit:** requires node power control and measurement beyond placement-only Kaptain.

### 8. Senjab / Survey — contextual scheduler portfolio

- **Add:** select among complementary policies using fragmentation, heterogeneity and
  load, charging the selection/decision overhead to the objective.
- **Hypothesis:** exploit policies' different operating regimes under changing workloads.
- **Baselines:** best single policy selected on validation, manual selection rules, a
  simple contextual bandit and a retrospective oracle as an indicative bound.
- **Test:** unseen regimes and transitions; cumulative regret and selection overhead.
- **Fit:** the algorithmic contribution would be the selector and transfer mechanism;
  the benchmark is supporting evidence. The [survey](https://link.springer.com/article/10.1186/s13677-023-00471-1)
  itself is not an algorithmic baseline.

## Contribution and anchor decision

**Historical framing from 2026-09-08; reconsidered on September 9.** See
[Plan alignment and recommended scope](#plan-alignment-and-recommended-scope) before using
this section as an implementation plan. The earlier proposal was a
**solver-distilled placement policy with RL refinement**, and the paper it extends is
**Christensen et al. 2025**.

### The one-paragraph version

> Christensen produces provably optimal Kubernetes placements, but needs 1–20 seconds per
> decision and degrades sharply at 32 nodes. We distil that solver into a candidate-scoring
> network that decides in milliseconds: bootstrap by imitation of the solver, then refine
> with RL against the real application objective (makespan, SLO violations), with a KL leash
> to the teacher to prevent collapse. Because the teacher is optimal only for the packing
> proxy — not for makespan or latency — RL has genuine headroom above it. An LLM off the
> critical path supplies what neither the solver nor the telemetry knows: how a pod that has
> never run will behave.

### Why Christensen is the paper we extend

We take his contribution as-is and fix the weaknesses **he names himself**:

| His limitation | What we do with it |
|---|---|
| 1–20 s per decision | distilled into a ~90k-parameter scorer, millisecond inference |
| fails / times out at 32 nodes | the model scores, it does not solve, at inference time |
| optimises "pods placed" only, no application metric | RL on the application objective |
| re-runs the solver every time, learns nothing | the solver becomes a **teacher** |

Roles of the other papers under this framing:

- **DRS** — the baseline we beat, and the closest architectural precedent. No longer the
  thesis (see decision trail below).
- **Decima** — ablation structure and how to report decision latency.
- **Raith** — heterogeneous graph state, `merge` what-if, and the cold-start hole we fill.
- **PAX** — Bayesian optimisation baseline; the "freeze vs adapt" question.
- **Pei** — the ML/LLM trade-off framing, to be redone inside Kubernetes.
- **Zhou** — motivation only, as an example of the protocol weakness we correct.

### Learning formulation — get the terminology right

"RL with a teacher" is not a formulation. Three clean ones exist, and the distinction decides
what we can claim:

| Formulation | What it is | Ceiling |
|---|---|---|
| **Behavioural cloning** | supervised on teacher decisions — what `learning-to-schedule` does today | the teacher |
| **DAgger** | replay our own policy, have the teacher relabel the states *we* visit, retrain | the teacher |
| **RL with KL anchoring** | policy gradient on the true reward, penalised for drifting from the teacher | **above the teacher** |

**Why RL is the right direction here.** Imitation caps us at the teacher — the
`learning-to-schedule` README says it plainly: *"imitating CFS caps the model at CFS
quality"*. In Kubernetes the CP-SAT teacher is optimal for **bin packing on CPU/memory**, not
for makespan, latency or SLOs. So headroom above the teacher exists, and RL is what captures
it. **DRS provides the accidental proof**: better utilisation, *worse* makespan (1.03–1.06×)
— the proxy and the real objective diverge.

Name for the paper: **solver-distilled policy with RL refinement**. Same shape as AlphaGo
(expert imitation then RL) and RLHF (SFT then PPO with KL to a reference). That analogy is
worth using — a reviewer reads it instantly.

### Why this also replaces reward engineering

Every learned scheduler in the corpus hand-crafts a reward: DRS tunes `α` and `β` "according
to cluster state", Zhou writes a points table, Pei uses `(1+e^{−k·Cost})·QoS`. Pei names the
problem directly: *"the performance of DRL models heavily depends on manually crafted reward
functions"*. Starting from an **optimal teacher** sidesteps reward design for the packing part
and confines the reward to the part that actually needs it — the application objective.

### The LLM's role: cold start

Every predictive scheduler in the corpus needs a history, and each admits the gap:

| System | How it learns a pod's profile | With no history |
|---|---|---|
| Raith | application signature measured on a past run | **falls back to a heuristic** — stated explicitly |
| DRS | config requirements + "running history" | no profile |
| Decima | profiling of recurring Spark jobs | no estimate |
| Pei | numeric features supplied | n/a |

Nobody reads the **pod spec**: image name (`postgres:16`, `ffmpeg`, `nginx`), labels,
annotations, command, request shape, mounted volumes. That is semantic information a language
model can use and a counter-based model cannot. The LLM runs **once per workload type, cached,
off the critical path**, producing a resource signature the fast plane consumes.

Nearest neighbour to be explicit about in related work:
[arXiv:2601.09282](https://arxiv.org/abs/2601.09282) puts an LLM in a Kubernetes extender, but
it parses **user-written placement preferences**. We infer **resource behaviour from an
ordinary manifest**. Different problem; say so.

### Prior work we own: `learning-to-schedule`

<https://github.com/leopoldch/learning-to-schedule> — same author, Linux CPU scheduling
(CFS/EEVDF). Not published, but it is the method this project ports, and it must be cited as
prior work.

**Note: it is imitation learning / learning-to-rank, not RL.** Heuristic teacher, loss
`CE 0.2 + KL listwise 1.0 + pairwise BCE 0.2`. No reward, no exploration.

What transfers:

1. **The candidate formulation is the extender's shape.** Their model takes `N × 34` candidate
   features + 13 global features → masked logits → argmax. `/prioritize` receives N candidate
   nodes and must score them. Near drop-in.
2. **The teacher hierarchy converges with Christensen** — this is the key convergence:

   | `learning-to-schedule` | Kubernetes equivalent |
   |---|---|
   | CFS teacher (one-hot) | kube-scheduler (one-hot) |
   | Heuristic teacher (continuous score) | resource-aware heuristic |
   | **Oracle teacher (counterfactual replay, "very expensive")** | **Christensen's CP-SAT** |

3. **The gate** — per-candidate weighting of history / task-set / global context. No equivalent
   in the Kubernetes corpus.
4. **Closed-loop evaluation** — the model actually drives the scheduler and consequences are
   measured, not agreement. A **higher standard than DRS**, which reports only utilisation.
5. **Teacher→student distillation** already built (KL, T=4, α=0.5) — that is Approach C of the
   project plan.
6. **Covariate shift / DAgger** named as their next step #1. Same problem in Kubernetes, and
   **nobody in the Kubernetes corpus addresses it**.

Reported results, for reference: −10.5 % average response time vs CFS in closed loop (60 runs,
12 workloads × 5 seeds), 91.6 % top-1 / 99.9 % top-3 imitation at ~90k parameters; Track A
distillation 76.17 % top-1 at 7.8× smaller.

What does **not** transfer:

- **Feature availability.** Their 34 features include `vruntime`, remaining burst, deadline
  slack — the kernel knows these. A Kubernetes extender does not know how long a pod will run.
  **This deficit is exactly the motivation for the LLM track.**
- **Distillation's motivation.** Theirs serves a microsecond hot path. We have 15–250 ms of
  budget, so a 1M-parameter model is fine. Keep distillation for the LLM→student track, not
  for size.
- **The emulator.** See risk below.

### Main engineering risk: no Kubernetes emulator

An oracle teacher needs counterfactual replay, and RL needs an environment to explore in.
They have a Linux scheduler emulator; **no Kubernetes equivalent exists** — KWOK simulates
placement, not execution. Decide this early. Three options:

1. **Build a light placement simulator** — they have done exactly this for Linux, so the skill
   exists.
2. **DAgger on a real cluster** — slower, but no simulator needed.
3. **Offline RL on collected traces** — no exploration required.

If time runs short, **DAgger alone is already a contribution**: nobody in the Kubernetes corpus
treats covariate shift.

### Decision trail (keep — it explains the why)

1. **First framing: extend DRS**, with the anchor experiment being an ablation separating the
   information effect from the learning effect (DRS adds telemetry *and* a DQN at once, then
   credits the DQN for the +27.29 %).
2. **Rejected by the author**: that is a replication/benchmark study, not a new contribution.
   Correct objection — it evaluates someone else's work rather than adding something.
3. **Second framing: LLM semantic cold-start signatures** — novel, and it targets the hole
   every predictive scheduler in the corpus admits to.
4. **Third framing (current)**: `learning-to-schedule` supplies a proven candidate-scoring
   architecture and, crucially, a teacher hierarchy whose top tier *is* Christensen's solver.
   That makes **solver distillation** the spine, LLM cold-start the differentiator, RL the way
   to exceed the teacher, and DRS the baseline.

The DRS information-vs-learning ablation is **not discarded** — it becomes a control experiment
in the evaluation section, justifying that the ML arm is correctly implemented. It is no longer
the headline.

---

## 1. Mao et al. 2019 — Decima

*Learning Scheduling Algorithms for Data Processing Clusters*, SIGCOMM '19, MIT CSAIL.
Source: `2019_Mao_Decima-RL-cluster-scheduling.pdf`

### Innovation

Not an off-the-shelf RL application. The contribution is making the problem
*representable, scalable and trainable* despite variable sizes and continuous arrivals.
Three parts:

1. **Scalable GNN for DAGs.** Per-node, per-job, and global embeddings computed by reusing
   the same small non-linear transforms at every node and every message-passing step. Key
   detail: aggregation is `e_v = g(Σ_{u∈ξ(v)} f(e_u) + x_v)` — the **second** non-linear
   transform `g` is essential. Without it the GNN cannot express a max over children, hence
   **cannot compute a DAG's critical path**. Standard GNN architectures (`Σ f(e_u)` only)
   perform poorly here.
2. **Action decomposition.** Instead of assigning all executors at once (exponential action
   space) or one at a time (very long sequences), each action is 2-D:
   `⟨stage v, parallelism limit l_i for v's job⟩`. The same score function `w(y_i, z, l)` is
   reused for all jobs and all limits — `l` is an input, so no per-limit parameters.
   Job-level (not stage-level) parallelism was chosen deliberately: same performance, much
   faster training.
3. **Training under continuous stochastic arrivals.**
   - *Curriculum via memoryless termination.* Early episodes terminate after `τ ~ Exp(...)`,
     mean growing over training. Termination must be **non-deterministic**, otherwise the
     agent learns to defer large jobs until the known episode end — which at runtime becomes
     indefinite starvation.
   - *Input-driven variance reduction.* Fix the job arrival sequence across several episodes
     and compute a **separate baseline per arrival sequence**, removing the reward variance
     caused by arrival randomness.

### Results

- Batched TPC-H arrivals: **−21 % avg JCT** vs the best tuned heuristic (optimal weighted
  fair), −45 % vs FIFO, −19 % vs fair.
- Continuous arrivals at 85 % load: −29 % avg JCT; **~2× faster** during busy hours 7–9.
- Multi-resource (Alibaba trace, 4 executor memory classes): **−32 %** vs Graphene\*;
  **−43 %** on TPC-H. Uses 39 % more of the largest executor class on the smallest 20 % of
  jobs — deliberately trades memory fragmentation for queue drain. Fragmentation within
  4–13 % of Tetris while avg JCT is 52 % lower.
- Model: 12,736 parameters (50 KB). Training iteration ≈ 5 s. Decision **< 15 ms**,
  ~50× smaller than the interval between scheduling events.

### Ablation (Fig. 14) — the table our paper needs

Removing **any single** component pushes Decima *below* the tuned weighted-fair heuristic
at high load:

- no parallelism control → unstable even at 55 % load (all executors to one stage);
- no graph embedding → cannot estimate remaining work or cluster load, unstable as load rises;
- no variance reduction → **2× worse** avg JCT above 75 % load;
- trained on batch arrivals only → systematically defers large jobs, starves them under
  continuous arrivals, loses to the heuristic above 65 % load.

### Generalisation (Table 2) — directly relevant to RQ3

| Setup (IAT = inter-arrival time) | Avg JCT (s) |
|---|---|
| Opt. weighted fair (best heuristic) | 91.2 ± 23.5 |
| Decima, trained on test workload (IAT 45 s) | 65.4 ± 28.7 |
| Decima, trained on anti-skewed workload (IAT 75 s) | **104.8 ± 37.6** ← loses to the heuristic |
| Decima, trained on mixed workloads | 82.3 ± 31.2 |
| Decima, mixed + inter-arrival time as input feature | 76.6 ± 33.4 |

Train/test distribution mismatch makes the learned policy *worse than the heuristic*.
Making the shift observable as a feature recovers +16 %.

### Limitations

- Controls Spark stages and executors; kaptain places Kubernetes pods. The transposition is
  **not direct**.
- Results depend on profiling information (task durations from past runs) and a carefully
  built simulator.
- No preemption: executors are only removed after a stage completes.
- Fairness is not an objective.

### What it gives us

- Methodological reference, **not a baseline to reproduce**.
- Steal the ablation structure (Fig. 14) — our paper needs the equivalent.
- Steal the way decision cost is presented (Fig. 15b): plot **decision latency vs interval
  between scheduling events**, not absolute latency.
- Table 2 is a ready-made argument for an adaptive layer: learned policies are brittle to
  workload shift unless the shift is observable.
- Transferable question: **can a small, well-represented model generalise as well as an LLM
  on unseen clusters and workloads?**
- Their own future directions we could pick up: meta-learning for online adaptation,
  deadline-aware reward shaping, tail-latency objectives, preemption via multi-agent RL.

---

## 2. Jian et al. 2024 — DRS

*DRS: A Deep Reinforcement Learning enhanced Kubernetes Scheduler for Microservice-based
System*, Software: Practice & Experience 54(10), 2102–2126. DOI 10.1002/spe.3284.
Nankai University. Source PDF is the **Authorea preprint dated 4 Jan 2023**, despite the
`2024` filename — cite the journal version, note the preprint if quoting page numbers.

**This is the closest architectural precedent and our main baseline** — no longer the paper
we extend; see [Contribution and anchor decision](#contribution-and-anchor-decision) for the
decision trail. The cracks below are still the ones our evaluation targets.

### Innovation

Core observation: **kube-scheduler only looks at CPU and memory**, and only at single-node
balance, causing fragmentation and imbalance for network- and IO-intensive microservices.

DRS adds network and disk awareness through **6 cheap-to-obtain metrics**: CPU util,
memory util, packet receive rate, packet transmit rate, disk read rate, disk write rate.
Chosen explicitly for low monitoring cost (`top`, `/proc/meminfo`, `/proc/net/dev`,
`iotop`), averaged over the last `AT` samples (1 Hz sampling threads + per-resource queues).

MDP:

- `State_t = [Node¹_t, ..., Nodeⁿ_t, Pod_t]`, each `Node^i_t` = the 6 metrics, normalised to [0,100].
- `Action_t ∈ {node_i}` — one schedulable node.
- `Reward_t = α·AvgUtil_t − β·ImBalance_t`, `ImBalance_t = Σ_k weight_k · std_k` across nodes.
  `α, β` are empirical scaling constants.

### Architecture (validates kaptain's design)

- **DRS scheduler** — K8s Scheduling Framework, deployed as a *second* scheduler
  (`spec.schedulerName`), custom Filter + Score.
- **DRS decision maker** — a process **independent of Kubernetes**, runs DQN, talks to the
  scheduler and monitors over **TCP sockets**.
- **DRS monitor** — runs on every worker node.

DQN: experience pool N = 300, target network copy every K = 50 iterations, `T_i` = 5 s wait
after an action before reading the next state (so the pod is bound and running).

### Results

- **+27.29 %** avg resource utilisation vs kube-scheduler; **−2.90×** load imbalance (mean
  over 3 workloads).
- Per workload vs kube-scheduler: utilisation +41.67 % / +31.31 % / +8.90 %; imbalance
  4.39× / 3.06× / 1.25×. kube-scheduler only looks good on the CPU-heavy workload.
- vs Round Robin: +26.92 % / +16.43 % / +23.66 %.
- Overhead: **3.27 % CPU**, 0.648 % communication latency as a fraction of makespan.
- Makespan 1.03–1.06× kube-scheduler (DRS does not optimise makespan).

### Contested — decision latency

Two readings of the same paper disagree, because Table 3 (latency breakdown) did not
extract cleanly from the PDF:

- **Reading A (from body text):** *"the scheduling latency of Kube-scheduler is about
  4.3 ms. The scheduling latency of the DRS is about 38.2 ms, of which the communication
  latency is 35 ms."* → DRS decision proper ≈ 3.2 ms.
- **Reading B:** decision latency ≈ 1.6 ms, communication overhead ≈ 10 ms.

Both agree on the qualitative point, which is the one that matters for us: **the model is
cheap, the process boundary is not.** Resolve against Table 3 in the published SPE version
before citing a number.

### Testbed

5 VMs, K8s v1.23.4, Ubuntu 22.04. Node0 master (4 × R7-4800H, 8 GB); workers deliberately
heterogeneous (2–4 cores, 2–4 GB, disks 269–678 MB/s).

Three microservices, published as Docker images:

| App | Type | What it does | Image |
|---|---|---|---|
| Video Scale | CPU-intensive | ffmpeg rescale | `jolyonjian/apps:cpu-1.0` |
| Transmission | Network-intensive | send/receive data to a server | `jolyonjian/apps:net-1.0` |
| Data Write | IO-intensive | `dd` read + write a copy | `jolyonjian/apps:io-1.0` |

Three workloads, `limit.CPU` randomised in 200–500m:

1. **Even** — 1:1:1, one pod every 20 s.
2. **Random** — 1:1:1, interval `T ~ N(μ=20, σ=1)`.
3. **CPU** — 4:1:1, one pod every 20 s.

### Limitations

- 4 workers, 3 application profiles, fairly regular arrivals.
- **Utilisation is not an application-level improvement.** Higher utilisation with a 1.06×
  makespan may be worse for the user. No latency or SLO metric.
- No affinity constraints, no bursts, no regime change.

### What it gives us

- **Direct architectural validation of kaptain.** Their decision maker is a separate process
  over sockets. Our README's claim is correct and this is the citation. And the latency
  split is the reason to **instrument transport and inference separately** in our extender.
- Workload design (three resource archetypes, three mixes, three arrival patterns) is
  directly reusable.
- **The ablation to run**: give a heuristic and a model *exactly the same telemetry*, then
  remove network, disk and history one at a time. This separates "the gain comes from
  learning" from "the gain comes from seeing more metrics" — which DRS never disentangles.
- **Quotable admission:** *"the research on the Kubernetes scheduling algorithm lacks
  standardized evaluation methods. Due to the differences between scheduling targets and
  workloads, these research are usually isolated and difficult to compare fairly."*
- Their future work: GPU/FPGA awareness, distributed / multi-level scheduling.

---

## 3. Christensen et al. 2025 — Constraint-based pod packing

*Priority Matters: Optimising Kubernetes Clusters Usage with Constraint-Based Pod Packing*,
arXiv:2511.08373. University of Southern Denmark + University of Bologna.
Source: `2025_Christensen_Priority-constraint-pod-packing.pdf`

Strategically the most useful paper in the corpus.

### Innovation

**No machine learning at all.** CP-SAT (OR-Tools) invoked as a **fallback**: the default
scheduler handles everything as long as it can; when pods go Pending *but would fit in the
available resources*, the solver is called (periodically or via an HTTP API) to find a
placement satisfying all priority and resource constraints.

Optimisation loop, per priority tier `pr` from highest (0) to lowest (`p_max`):

1. add multi-dimensional bin-packing constraints for pods of priority ≤ `pr`;
2. **maximise** the number of placed pods at that priority;
3. **minimise** disruption — pod moves, weight 2 for "stays on its current node", 1 for
   "moves elsewhere";
4. if OPTIMAL, freeze with an equality constraint; if only FEASIBLE (timeout), freeze with
   an inequality.

Time budget: wall-clock `T_total`, fraction `α` per tier,
`get_timeout() = α·T_total/(p_max+1) + unused`, split in half between the two solve phases.
CP-SAT has no incremental push/pop, so the model is re-solved each step, warm-started with
solution hints.

They also implement **cross-node preemption**, which Kubernetes does not support natively.
Go plugin across 5 extension points (PreEnqueue, PreFilter, PostFilter, Reserve/Unreserve,
PostBind) with `DefaultPreemption` disabled; the Go plugin **calls a Python script running
OR-Tools inside the scheduler container**.

### Key conceptual point

**A local placement failure does not prove the cluster lacks resources.** Resources may be
fragmented by earlier decisions. This is the argument for a hybrid mechanism: fast heuristic
in the normal case, global optimisation only in the hard cases.

### Results

Categories per instance: Better&Optimal / Better / KWOK-Optimal / No Calls / Failures.

- **1 s window**: better than default in **44 %** of scenarios where the default fails to
  place all pods; **proves the default is already optimal in 19 %**.
- **10 s window**: better in **73 %**; still 19 % already-optimal.
- CPU/memory utilisation gain: consistently **2–4 %**.
- Solver duration grows with cluster size: 0.9–1.2 s at 4–8 nodes, 2.4–6 s at 16 nodes,
  hits the 10 s cap at 32 nodes. Failures rise sharply from 8 → 32 nodes.
- More priority tiers → **more** optimal outcomes (clearer search ordering) but longer solves.
- Diminishing returns past 10 s.

Evaluation uses **KWOK** (Kubernetes WithOut Kubelet). Default scheduler forced
deterministic for dataset generation: lexicographic Score plugin, `parallelism=1`,
`DefaultPreemption` disabled.
Code: `github.com/henrikdchristensen/scheduler-plugins`, dir `pkg/mypriorityoptimizer`.

### Limitations

- KWOK + random requests, up to 32 nodes. **No application performance measured at all** —
  the objective is pods placed, not service quality.
- Eviction and relocation costs are not fully accounted for.
- The solver is **global** (all pods, scheduled and pending), whereas a scoring extender
  like kaptain normally acts on the *next* pod. Different decision granularity.

### What it gives us — three things

1. **The architectural pattern is ours.** "Fast default path, expensive path invoked only
   when needed" is exactly the *reasoning plane off the critical path* of RQ4. Replace
   CP-SAT with an LLM and you get Approach B. Citing this defeats the obvious "an LLM is too
   slow to schedule" objection: a 10-second constraint solver has already been published.
2. **The 19 % is the single most important number in the corpus for us.** In 19 % of hard
   cases, kube-scheduler's placement is *provably optimal*, so part of the headroom RL papers
   claim does not exist. This gives us a ceiling and a way to measure it: run an **offline
   exact solver as an oracle** and report ML/LLM gains as a **fraction of achievable gain**
   rather than % vs default. Nobody in this corpus does that.
3. Cross-node preemption + the 5-extension-point recipe if we go beyond pure placement; and
   **KWOK** as the path to cluster sizes beyond a laptop.

Related work they position against: SAGE, BOREAS (both ignore priorities and preemption and
assume resources suffice for all pods), Santos et al. (network-aware), Nguyen et al.
(traffic distribution), Kaur et al. / James & Schien (power and green energy).

---

## 4. Raith et al. 2024 — Energy-aware GNN scheduler

*Opportunistic Energy-Aware Scheduling for Container Orchestration Platforms Using Graph
Neural Networks*, IEEE CCGrid 2024, DOI 10.1109/CCGrid59990.2024.00042.
TU Wien + Hewlett Packard Labs. Source: `2024_Raith_Energy-aware-GNN-scheduler.pdf`

### Innovation

A **heterogeneous graph model of the host**, not of the workload — the opposite of Decima.

- Node types: `{Host, CPU, Memory, Disk, Network, Pressure, GPU, Fan, Temperature, FPGA, SmartNIC}`.
- Edge types: `Host-*` plus `Temperature-CPU`, `Temperature-Memory`.
- Sensors at 1 Hz grouped into **time frames of n = 10 s**. Counters aggregated as totals,
  gauges as means. One graph per host per frame, so the model sees **resource phases over an
  application's lifetime**, not just an average.
- Features may be static (GPU model) or dynamic (current GPU frequency).
- Container-level metrics deliberately **excluded** as graph nodes — modelling each container
  as a node hurt generalisation.

**Application signature** `as_{a,h}`: isolated resource usage of app `a` on host `h` (CPU,
Memory, Disk, Network via cAdvisor), also a sequence of graphs. Plus
`merge: (G, G) → G` (sum counters, mean gauges) enabling **what-if estimation**: *what would
this host's power be if I placed this container on it?*

Model: 2 stacked **Fast HGT** conv layers → 3 global poolings (mean, max, add) → 4 linear
layers → predicts (min, max, avg) watts for the frame.

Three algorithms on top:

- **GNN** — merge signature into each host graph, predict, pick min total energy.
- **GNN-Aware** — also accounts for *remaining* runtime / future phases of running apps
  (requires signatures for all of them).
- **GNN-Packing** — take the **second**-lowest host if within a **5 % threshold** of the
  lowest, to prefer already-busy nodes.

### The separation that matters

The paper cleanly splits two functions: (1) **predict the consequences of a placement**;
(2) **select a placement from the prediction**. This makes each independently evaluable and
lets you swap the predictor without rewriting the policy. Worth adopting in kaptain.

### Results

- Power prediction **RMSE 7.5 %** (5-fold, std ≈ 3 %).
- Distinguishes two hosts with identical CPU/RAM where one has an **Nvidia A100X**
  (+~50 W idle, ~20 %) — because the GPU is a node in the input graph.
- Long-running experiment: GNN-Packing **139.17 Wh** vs Spreading **148.38 Wh** →
  **−6.2 % energy**, −5.27 % mean watts, **without increasing makespan** (332 s vs 335 s).
  Spreading is the stand-in for default Kubernetes behaviour.
- **Scheduling overhead ≈ 250 ms** (preprocessing → selection), CPU inference.

**Explicit accuracy/performance trade-off measured**: extra HGT layers or LSTM variants gave
**2× slower inference for ~2 % RMSE improvement**; they chose the weaker, faster model.

### Training data

`stress-ng` stressors: `cpu (1-88)`, `memrate (1-4, vm-bytes 256M)`,
`vm (1-10, vm-bytes 5%-50%)`, `iomix (1-4, 256M)`. 100 s each, 72 tests per host.
Evaluation workload: **Rodinia** (LavaMD, Leukocyte, srad v1) at 4 and 8 cores, ~10–100 s.
Two sinusoidal submission patterns: 72 containers in 209 s (short), 128 in 234 s (long).

Monitoring: Prometheus (1 s scrape), cAdvisor (16 dynamic features), NodeExporter (37),
HPE iLO exporter (fans/temp), Nvidia DCGM (22), Xilinx xbutil (6 FPGA), 3 SmartNIC.
Scheduler in Python with the K8s client; GNN in PyTorch 2.0.1 + PyTorch Geometric 2.3.1.

### Limitations

- **3 nodes only.**
- **Training and test sets contain the same hosts**, so generalisation to unseen hardware is
  not demonstrated.
- **Cold start**: an unseen application has no signature → fall back to a heuristic (Packing).
  Continuous learning / MLOps fine-tuning is listed as future work, not done.
- Results flip between the short and long scenario: **plain Packing sometimes wins**.
  The GNN's value is balancing energy against makespan, not dominating on energy.
- Only mean accuracy is reported — no uncertainty, no per-application error.

### What it gives us

- **Best state representation in the corpus.** Compare: Zhou = 6 scalars, DRS = 6 metrics ×
  n nodes flattened, Raith = a structure that absorbs hardware heterogeneity and accelerators
  with no re-engineering. Exactly what our Phase 1 calls for.
- **What-if via `merge` is the mechanism our `Pod → filter → ML scoring` pipeline is
  missing.** It turns node scoring into a counterfactual prediction rather than a similarity
  score. Candidate adaptation: apply the same trick to *placement quality* instead of power.
- The perf/accuracy trade-off is measured once — reproduce and extend it for our "model size
  vs scheduler latency" question. And **measure uncertainty and unseen applications**, not
  just mean accuracy.
- **Cold start is our niche.** An LLM can reason over an image name, labels, annotations, a
  manifest — semantic information no model here uses. **Best answer to RQ2.**
- Their other future work: clustering similar workloads via the GNN, interference-aware
  placement, carbon-aware strategies, edge-cloud continuum.

---

## 5. Dong et al. 2025 — PAX

*Towards Performance and Energy Aware Kubernetes Scheduler*, ACM SIGENERGY Energy
Informatics Review 5(2), July 2025, pp. 69–73. PEAKS project (Hamilton College, Red Hat,
Boston University, IBM Research). Source: `2025_Dong_PAX-perf-energy-K8s-scheduler.pdf`

### Innovation

1. **Joint optimisation of replica count *and* node placement** — so autoscaling and
   placement are not fully separated. HPA and Cilantro only scale replicas; the default
   scheduler then places them. PAX treats `(replicas per pod type, node per pod type)` as one
   search space, driven by **Bayesian optimisation** (`ax-platform`), bypassing the default
   scheduler via `patch_namespaced_deployment`. Objective: minimise P99 of DeathStarBench
   hotel reservation (19 microservices: 6 MongoDB, 3 KV stores, nginx + consul…), load from
   `wrk2`.
2. **Deliberate hardware heterogeneity, argued on embodied carbon.**

| | Processor | Node | Release | CPUs | TDP | RAM | CO₂ (kg) | Cost |
|---|---|---|---|---|---|---|---|---|
| Server-2014 | Intel E5-2630 v3 | 22 nm | Q3'14 | 2×16 | 2×85 W | 128 GB | 118.4 | $599 |
| Server-2021 | Intel Xeon Silver 4314 | 10 nm | Q2'21 | 2×32 | 2×135 W | 256 GB | 221.9 | $6080 |

Embodied carbon via ACT; ~half of data-centre emissions, and extending server lifetime beats
recycling. Search space: 19 pod types on 2 nodes ≈ **half a million configurations**; at
2 min per evaluation, brute force ≈ **2 years of compute**.

### Results (5-hour runs, avg P99 / avg power)

| Cluster | HPA | Cilantro | PAX |
|---|---|---|---|
| 2× Server-2021 | 1171 ms / 371 W | 3169 ms / 334 W | **237 ms / 345 W** |
| 4× Server-2014 | 1305 ms / 274 W | 5188 ms / 235 W | **680 ms / 199 W** |
| 2× 2014 + 1× 2021 (mixed) | 994 ms / 321 W | 1373 ms / 260 W | **193 ms / 238 W** |

- PAX: **up to 5× lower P99** than both HPA and Cilantro.
- **The mixed cluster beats the all-new cluster** for every scheduler. Cilantro: 2.3× lower
  P99 and 28 % less power than 2× Server-2021. PAX: 193 ms vs 237 ms, 238 W vs 345 W.
- Power measured with **Kepler** (eBPF + Prometheus).

### The counter-intuitive finding

- **Cilantro** reconfigures every 2 min (UCB) → most power variance, **worst** latency.
- **HPA** re-evaluates every 15 s but **quickly settles** on one configuration → **4× lower
  P99 than Cilantro**.
- **PAX** goes further: BO with a **fixed trial budget**, then **freeze** → 5× better than both.

Conclusion: *aggressive reconfiguration costs more than it earns, and the cost of
reconfiguration belongs in the objective.* Cilantro at ~40 min was on track to beat HPA, then
deviated into a worse configuration.

### Limitations

- **One application, one cluster family.** Generalisation not shown.
- PAX is **not adaptive**: fixed trial budget then frozen. Authors concede an adversarial
  scenario would expose it, and BO's initial stochasticity could leave it in a poor local
  minimum.
- `patch_namespaced_deployment` forces all replicas of a pod type onto a single node.
- PAX changes *replica counts*, so it is not a placement-only comparison. Say so if citing.

### What it gives us

- **Strongest support for our hybrid design.** If freezing a good configuration beats
  continuous reconfiguration, a *slow LLM off the critical path* + *light execution plane* is
  not a compromise but the right design. RQ4 gets much easier to defend, and "adapt only when
  needed" is an explicitly available contribution.
- **Bayesian optimisation is the ML baseline our project plan does not list and should** —
  sample-efficient, no pre-training, beats two modern systems. The missing point of comparison
  between XGBoost/RF and RL.
- The three-way comparison to run: **fixed policy / periodic adaptation / change-triggered
  adaptation**, each reported **net of its adaptation cost and the disruption it causes**.

---

## 6. Pei et al. 2025 — LarS

*LLM-based cost-aware task scheduling for cloud computing systems*, Journal of Cloud
Computing 14:81, DOI 10.1186/s13677-025-00822-0. North China Electric Power University et al.
Source: `2025_Pei_LLM-cost-aware-task-scheduling.pdf`

Central paper for RQ2 / RQ3 / Approach C. Must be read critically.

### Innovation

**DRL → LLM distillation**, in that direction:

1. Train a **DQN agent per cloud scenario** (10 training environments varying High-CPU /
   High-IO VM counts). Prioritized experience replay, target network, linear ε-greedy
   (ε 0.01 → 0.9, +0.003/step), RMSProp lr 5e-3, γ 0.9, buffer 1600, batch 60, target update
   every 150 steps.
2. **GPT-4o** generates trajectories with CoT reasoning from a structured prompt
   `X = Prompt(o, d_scenario, d_task, d_knowledge, A)`.
3. **Filter**: keep the (prompt, trajectory) pair **only if `a_GPT == a_DRL`**. The DRL agent
   is a *quality filter*, not a direct teacher. Datasets from all scenarios are unioned.
4. **LoRA fine-tune LLaMA-2-13B**: r = 8, α = 16, dropout 0.05, INT8 (≈4× less GPU memory),
   30 epochs, Adam lr 3e-4, batch 128, 5 % val. `Observation:` tokens masked with −100.

Model: jobs `{ID, arrival, reqCom, QoS, Type}`, VMs `{VID, VCom, VCPU, VType, VSC, VEC}`,
FCFS per-VM queue, type mismatch **doubles** execution time, QoS = `T_exe / T_resp`, success
if QoS ≤ requirement. Reward `r = (1 + e^{−k·Cost}) · QoS`.

The genuinely interesting idea is **transferring decisions from many environments into one
reusable model**.

### Results — 11-VM test environment (5 High-CPU, 6 High-IO)

| Strategy | Avg resp. (s) | Success rate | Cost |
|---|---|---|---|
| Random | 0.422 | 31.6 % | 0.596 |
| Round-Robin | 0.290 | 47.0 % | 0.602 |
| Earliest | 0.282 | 50.0 % | 0.607 |
| **DQN** | **0.191** | **98.6 %** | 0.421 |
| GPT-4o (zero-shot) | 0.252 | 79.4 % | **0.400** |
| LLaMA-2 (untrained) | **9.064** | 24.5 % | 0.486 |
| LLaMA-2 (+1000) | 0.245 | 74.3 % | 0.437 |
| LLaMA-2 (+2000) | 0.364 | 43.3 % | 0.529 |
| LLaMA-2 (+4700) | 0.211 | 88.0 % | 0.439 |

**The DQN wins on all three metrics in the main test.** Raw LLaMA-2 is unusable (9 s, 24.5 %).
+2000 is *worse* than +1000 — non-monotonic and unexplained.

### Generalisation (8 / 20 / 40 VMs, no retraining)

- **DQN collapses**: response times **> 200 s** at 20 and 40 VMs; success rate plummets.
  Classic overfitting to the 11-VM training scenario.
- **Fine-tuned LLaMA-2 holds**: consistently low response times, highest success rate in all
  three, lowest cost (≈20 % below the closest naive method at 8 VMs). Degradation at 40 VMs,
  but not collapse.

### Limitations — this is where the gap is

- **The comparison is not controlled.** The DQN is *specialised* to one scenario; the LLM is
  trained on data from *many*. The generalisation advantage may come from data diversity and
  expert imitation, not from the language model. The paper never separates the two.
- **Evaluated in a VM simulator, not Kubernetes.** No filtering/scoring phases, no affinities,
  no Scheduling Framework, no real pods.
- **LLM inference latency is never measured**, and never counted as critical-path cost.
  Compare 15 ms (Decima), ~38 ms (DRS), 250 ms (Raith).
- Reported "cost" is a workload cost model, **not the total cost of serving an LLM**.
- Interpretability is claimed (natural-language justification per decision) but never
  evaluated — and **agreement with the DQN's action does not make the textual explanation
  true**.
- No decision-stability analysis, though our plan calls for one.

### What it gives us

- **Our Approach C already exists, in the opposite direction.** Our plan says "LLM decisions
  supervise a lighter student model"; Pei has DRL supervising the LLM. We must either justify
  our direction or adopt theirs.
- The exploitable result is **the trade-off, not the score**: DRL is better *in distribution*,
  the LLM generalises. Literally RQ3 — confirmed outside Kubernetes.
- **The controlled experiment they did not run, and we should**: small model, LLM and
  heuristic see the **same data**, have the **same action set**, and are evaluated on the
  **same unseen environments**. The question is: *does the LLM add anything beyond data
  diversity and expert imitation?*
- The DRL-as-filter curation trick is cheap and reusable if we do any distillation.
- Their future work: cloud-edge environments, few-shot adaptation.

---

## 7. Zhou et al. 2026 — SDQN / SDQN-n

*A Kubernetes custom scheduler based on reinforcement learning for compute-intensive pods*,
arXiv:2601.13579. Universiti Sains Malaysia + Xiamen Institute of Software Technology.
Reference [5] of our project plan — so we need a precise position on it.
Source: `2026_Zhou_K8s-RL-scheduler-compute-intensive.pdf`

### Innovation

Weak. DQN with 6 input features (CPU %, memory %, pod utilisation ratio, health status, node
uptime hours, running pod count), a single hidden layer 6 → 32 → 1 with ReLU, MSE loss, Adam
lr 0.001. Reward is a **hand-written points table** (base 100; −100 if unhealthy; +10 if CPU
in 40–70 %, −2 per point above 70 %; same for memory; +20 if pod ratio in [0.6, 0.9]; +5 if
uptime ≥ 24 h; +5 per additional node in the distribution).

**SDQN-n** (n = 2) changes one reward rule: placement outside the top 2 nodes → −50, forcing
consolidation onto 2 nodes so idle machines can be shut down ("green data centre"). LSTM and
Transformer variants (32 hidden units / d_model 32, 4 heads, 1 layer) are compared.

The one defensible idea: **consolidation is a legitimate alternative to spreading** and can
free nodes for shutdown or reassignment. But the effect of the consolidation *rule* must be
separated from the effect of the *neural network*, and the paper never does that.

### Results — 4 worker nodes, 50 no-op CPU-bound pods, 5 trials each

| Scheduler | Reported mean CPU utilisation | CV |
|---|---|---|
| Default | 30.87 % | 2.95 % |
| SDQN | 27.21 % | 4.67 % |
| **SDQN-n (n=2)** | **22.35 %** | 4.00 % |
| LSTM-based | 30.53 % | 5.35 % |
| Transformer-based | 30.15 % | 1.61 % |

### Verification — the headline number does not reconcile

Recomputing each table's mean from its own five trials:

```text
Default:      (29.97 + 31.82 + 30.95 + 29.71 + 31.91) / 5 = 30.872  ✓ matches 30.87
SDQN:         (25.21 + 27.69 + 26.39 + 27.93 + 28.84) / 5 = 27.212  ✓ matches 27.21
LSTM:         (31.97 + 32.87 + 28.43 + 29.73 + 29.67) / 5 = 30.534  ✓ matches 30.53
Transformer:  (29.25 + 30.01 + 30.48 + 30.47 + 30.52) / 5 = 30.146  ✓ matches 30.15
SDQN-n:       (25.21 + 22.57 + 26.39 + 22.01 + 23.84) / 5 = 24.004  ✗ paper states 22.35
```

**Every table reconciles except the one carrying the headline claim.** The real figure from
their own data is 24.00 %, not 22.35 % — so the claimed ">20 % reduction" vs default
(30.87 %) is actually **22.3 %**, not the ~28 % the stated number implies.

Two further internal problems in Table 10:

- SDQN-n **trial 1 is byte-identical to SDQN trial 1** — same distribution (13, 13, 21, 3),
  same 25.21 %. That distribution is spread over four nodes, which *contradicts* the n = 2
  consolidation rule the table is meant to demonstrate.
- SDQN-n trial 3 reports 26.39 %, the same value as SDQN trial 3, from a different pod
  distribution (21, 22, 3, 4 vs 16, 14, 19, 1).

Also, the narrative text for the default scheduler says "Test 1 … 29.27 %" while Table 8
trial 1 says 29.97 %.

Cite this paper's numbers only with the recomputation shown.

### Further limitations

- **The metric mechanically rewards consolidation.** It is "mean CPU % per node, including
  idle nodes". Their own example: `{20,20,20}` = 20 % vs `{10,25,20}` = 18.3 %. SDQN-n wins
  by concentrating, not by learning.
- Mean CPU utilisation demonstrates **neither** energy savings **nor** application latency.
- Workload is **no-op CPU-bound pods**. No application metric, no SLO, no makespan, no
  decision latency, no throughput. No affinities, no heterogeneous arrivals, no load change.
- 5 trials, CV 2.95–5.35 %, for claimed gaps of 10–20 %. No statistical test.
- **Sections 6.1 "Conclusions" and 6.2 "Future Work" are the same three bullets copied.**
- Under-exploited negative result: **LSTM (30.53 %) and Transformer (30.15 %) ≈ default
  (30.87 %)**. On a non-sequential state, sequence architectures buy nothing. Useful as a
  bound: model complexity does not automatically pay.

### Recommended use

Cite to situate the field ("recent work reports gains but under limited protocols"), not as a
credible baseline. Use it as **explicit motivation for our methodological contribution**.
Keep consolidation as an **independent baseline** — same state, same constraints, same
protocol — and measure, beyond CPU: work completed, response time, pending pods, SLO
violations, energy, and placement stability.

Its related-work table is a useful map of adjacent work (KubeAI, PPO-LRT, CSFRL, RLKube =
DDQN + PER plugin, NACS/TOPSIS, EIS).

---

## 8. Senjab et al. 2023 — Survey

*A survey of Kubernetes scheduling algorithms*, Journal of Cloud Computing (2023).
University of Sharjah + Ajman University. 124 studies screened → 67 → **47 included**.
Source: `2023_Senjab_Survey-K8s-scheduling.pdf`

### Taxonomy (4 sub-categories)

1. **Generic scheduling** — Santos et al. (network-aware, −80 % network latency), Stratus
   (batch on IaaS, −17–44 % cost), AlloX (min-cost bipartite matching over interchangeable
   CPU/GPU), Zhong et al. (−23–32 % cost), Kube-Knots.
2. **Multi-objective optimisation** — GA, PSO, ACO; KEIDS (energy + interference, Alibaba
   Trace V2018).
3. **AI-focused** — Optimus (+139 % JCT, +63 % makespan vs conventional), **Decima**,
   Gandiva-fair (200-GPU cluster), ProCon (−53.3 % completion, +23.0 % overall, −37.4 %
   makespan), DL2, SpeCon (−41.5 % job time, −24.7 % makespan), RLSK (federated multi-cluster
   DQN), MLFS (heuristic priorities → DRL, −53 % JCT), **KaiS** (edge-cloud, GNN + coordinated
   multi-agent actor-critic, +14.3 % throughput), Casquero et al. (multi-agent distributed
   scheduling), Zeus (**CPU utilisation 15 % → 60 %** without SLO violations), KubFBS (GPU
   sniffer + balance-aware).
4. **Autoscaling-enabled scheduling** — ML forecasting for autoscaling decisions.

The survey also stresses that **placement, dynamic resource allocation and autoscaling are
related but distinct problems** — worth keeping separate in our own framing.

### The two gaps they name — they are our two gaps

1. *"lack of real-world datasets for training and evaluation of AI-based scheduling
   algorithms. Most studies use synthetic or simulated datasets, which may not reflect the
   complexities of real-world workloads."*
2. *"the trade-off between accuracy and computational complexity."*

Plus: small clusters, varying objectives, no common protocol, results hard to compare.

### Limitations

- The taxonomy **mixes Kubernetes scheduling with AI-workload scheduling** in places.
- Table values are summarised without the experimental detail needed for rigorous comparison.
  **Consult the original papers before reusing any figure from it.**
- **Stops in 2023 and contains nothing on LLMs.**

### What it gives us

Skeleton for Phase 0 and the positioning paragraph. And since it stops in 2023, an up-to-date
review (2024–2026 + LLM) is publishable on its own.

---

## Adjacent work found online

Not in the folder. These reduce the novelty space and must be cited and differentiated.

- **[Cluster Workload Allocation: Semantic Soft Affinity Using Natural Language Processing](https://arxiv.org/abs/2601.09282)**
  — an LLM inside a **Kubernetes scheduler extender** interpreting textual placement
  preferences. Reports >95 % parsing accuracy on its test set and flags **synchronous LLM
  latency** as the problem. This is the closest published work to a naive Approach A; our
  differentiation must be the hybrid/off-critical-path design and the decision-cost
  characterisation.
- **[Towards Agentic OS: An LLM Agent Framework for Linux Schedulers](https://arxiv.org/abs/2509.01245)**
  — explicitly separates **reasoning from execution**, with verification before deployment.
  Directly relevant to the reasoning plane / scheduling plane split of RQ4.
- **[A Kubernetes Scheduler Plugin for Cluster-Wide Placement Optimisation](https://arxiv.org/abs/2608.06987)**
  — triggers on scheduling failure, on a period, or on a stable queue, and coordinates global
  plans. Very close to "invoke the expensive path only when it can change the outcome".
- **[Post-hoc estimators for learning to defer to an expert](https://proceedings.neurips.cc/paper_files/paper/2022/hash/bc8f76d9caadd48f77025b1c889d2e2d-Abstract.html)**
  — formalises the transferable idea: **learn when it is worth deferring a decision to a more
  expensive model**. This is the theoretical framing for our trigger mechanism.

---

## What the corpus actually establishes

1. Local heuristics produce fragmentation.
2. A global solver improves the hard cases, but its cost grows fast with cluster size.
3. The choice of *observations* is critical — CPU and memory do not describe all workloads.
4. RL can learn specialised policies, but generalisation must be tested explicitly, and often
   fails when it is.
5. Structured / graph representations help when cluster or workload size varies.
6. Consolidation, packing and spreading serve different objectives and cannot be compared on
   one metric.
7. Energy, cost, performance and fairness are in conflict.
8. The cost and stability of *adaptation* are themselves metrics.
9. Most headline results come from small clusters, single benchmarks, or synthetic workloads.

### What remains insufficiently studied

- controlled comparison across LLM / ML / RL / solver / heuristic;
- full cost of an LLM decision;
- behaviour after a load-regime change;
- never-before-seen applications;
- reproducibility across several clusters;
- the link between CPU utilisation, real energy, and user-visible performance;
- decision stability and oscillation;
- interaction between placement, autoscaling and migration;
- **the impact of HTTP/extender serialisation in the critical path** — which is exactly the
  boundary kaptain sits on.

---

## Synthesis: the four gaps

### A. Nobody measures decision cost comparably

15 ms (Decima, 50 KB GNN) / ~38 ms with most of it transport (DRS) / 250 ms (Raith HGT) /
1–20 s (Christensen CP-SAT) / **unknown** (Pei, LLM). No paper puts these on a common axis,
and the LLM number does not exist. This is the central axis of our project plan and it is
**empty**. Safest contribution available.

Corollary from DRS: **separate transport cost from inference cost.** Our HTTP extender must
instrument both, plus serialisation and the final bind.

### B. Gains vs default are never bounded

Christensen shows the default is already provably optimal in 19 % of hard cases. Everyone
else reports "+27 % vs kube-scheduler" without saying what fraction of the achievable gain
that is. An **offline CP-SAT oracle** over our scenarios gives us a denominator: report gains
as *% of achievable gain*. Cheap to implement, strong argumentatively.

### C. The ML/LLM trade-off exists only outside Kubernetes

Pei demonstrates it (DRL wins in-distribution, LLM generalises) in a VM simulator, and with
an uncontrolled comparison (specialised DQN vs multi-scenario LLM). Reproducing it fairly, in
a real cluster with filtering, scoring, affinities and the Scheduling Framework, is open.

### D. Cold start is the LLM's natural niche

Raith falls back to a heuristic for an unseen pod. DRS computes pod requirements from
"running history". Pei starts from numeric features. An LLM can read a manifest, an image
name, labels, annotations — **semantic** information none of these models exploit. Structural
advantage, not incremental. Best answer to RQ2.

### Bonus signal — PAX

Freezing a good configuration beats continuous reconfiguration (HPA settles and beats
Cilantro 4×; PAX freezes and beats both 5×). If this holds in our setup, the hybrid design is
not a compromise but the right design — a clean, defensible RQ4 result.

---

## Recommended research question

> Under a given compute budget, **when** does LLM-driven adaptation of a placement policy
> improve application performance over a small model and over classical optimisation —
> particularly after a change in load regime?

Preferable to "is the LLM better?", because it makes the conditions of success and the costs
measurable. The most defensible contribution is probably **not a new model**, but a
reproducible characterisation of when expensive reasoning actually pays, together with a
trigger mechanism and a light fallback.

The result must show simultaneously:

- when the model improves placement quality;
- when its cost cancels the gain;
- how it behaves after a load change;
- when to fall back to a heuristic;
- whether a small distilled model achieves the same trade-off at lower latency.

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

### Open

1. **Transport cost is only measurable from the scheduler side.** The extender cannot time
   its own RPC; that number comes from kube-scheduler's
   `scheduler_framework_extension_point_duration_seconds` and must be reported beside our
   handler duration. This asymmetry is the reason the plugin exists, not a defect to fix.
2. **No experiment runner.** No arrival generator, no `run.json` / `jobs.csv` /
   `decisions.csv` / `summary.csv`, so nothing aggregates the per-decision logs into a
   result yet. This is now the blocking item.
3. **No ML/RL/LLM policy.** They reach either path through the external decider, which has
   no service behind it yet.
4. **Whole `NodeList` serialised per pod** with `nodeCacheCapable: false`. Fine at 3 nodes;
   it becomes part of the measured cost at scale. Report serialisation as a function of node
   count, and consider `nodeCacheCapable: true` once behaviour is understood.
5. **Testbed.** kind remains one machine: wiring and decision-cost pilots only, main
   results on separate Linux VMs.

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

## Priorities

### Control experiment (no longer the headline)

**Information vs learning**: give a simple heuristic the same six metrics DRS feeds its DQN,
and compare on the same bench. Now a control in the evaluation section, justifying that the ML
arm is correctly implemented. See
[Contribution and anchor decision](#contribution-and-anchor-decision).

### First — make the bench able to produce a number

1. **Instrument latency** in `app.py`: parse, inference, serialise, total. Expose on a
   `/metrics` endpoint. This is gap A and it is roughly 15 lines.
2. **Make the fallback explicit**: log, count, expose. Otherwise crashes corrupt results
   invisibly.
3. **Split the arms**: one run per strategy, reset between runs. Retire the two-pod
   concurrent demo.
4. **Give the extender a K8s client** so strategies can compute allocatable − Σ requests.
   This unblocks every resource-aware baseline at once.
5. **Real workload generator**: the three DRS archetypes, an arrival process, a seed,
   terminating pods (so JCT and makespan exist).
6. **Post-run collector**: placement map, per-node requested utilisation, pending count,
   pending time, JCT, makespan → one CSV per run. Record machine, kernel, runtime and
   isolation mode in every run's metadata.
7. **Pick the testbed and freeze it.** See the platform table. If a bare-metal Linux box is
   available, move there before producing any reported number — it is the difference between
   "placement quality and decision cost" and "plus imbalance and energy" as the paper's scope.

### Next — build the contribution

1. **Port the candidate-scoring model** from `learning-to-schedule` to the extender's
   `N candidates → masked logits → argmax` shape. Rebuild the feature vector: the Linux 34
   features do not transfer (no `vruntime`, no remaining burst).
2. **Stand up the CP-SAT teacher** (Christensen's formulation) and generate labels.
3. **Behavioural cloning first**, closed-loop evaluation against kube-scheduler and DRS-style
   baselines.
4. **Then DAgger** — relabel states the policy itself induces. Contribution on its own if time
   runs short.
5. **Then RL with KL anchoring** on the application objective (makespan, SLO), teacher as leash.
6. **LLM cold-start signatures**, cached per workload type, off the critical path.
7. Add Round Robin, LeastAllocated, MostAllocated, Spreading — four corpus-named strategy
   classes on one protocol.
2. Train a lightweight model on the decisions of a heuristic or of the solver.
3. Add the offline CP-SAT oracle and start reporting **% of achievable gain**.
4. Use the LLM **off the critical path** to produce weights, a policy, or rules (Approach B).
5. Compare those policies on unseen clusters and workloads.
6. Add adaptation triggered by distribution drift or SLO degradation.

### Later

- GNN for a heterogeneous cluster or microservice dependencies (Raith / KaiS style);
- energy or performance prediction followed by placement selection (Raith's two-function
  split) — **unblocked by a bare-metal Linux testbed**, see platform table;
- global solver triggered only when a pod stays pending (Christensen's pattern);
- LLM → small model distillation (Approach C, note Pei runs it the other way);
- multi-objective optimisation with a Pareto front over latency, energy and cost.

---

## Open questions / to verify

- **Resolve the DRS latency discrepancy** against Table 3 of the published SPE version
  (38.2 ms / 35 ms transport vs 1.6 ms / 10 ms).
- Does PAX's "freeze the configuration" finding hold under **dynamic arrivals and sudden load
  shifts**? They admit an adversarial scenario would expose it. Testable and publishable.
- What is the **real per-decision inference latency** of a small LLM (7B/13B, quantised,
  local) on our hardware? Everything in RQ4 depends on this and nobody has published it.
- Is Zhou's SDQN-n gain reproducible under a **fair metric** (total cluster energy or makespan
  rather than mean-CPU-including-idle-nodes)? Likely not — worth one experiment. And note
  their headline mean does not reconcile with their own table (24.00 % vs 22.35 %).
- Can Raith's `merge` what-if be adapted from *power* to *placement quality* as our ML scoring
  function?
- Does the DRL-as-filter curation trick (Pei) transfer with the roles swapped (LLM supervises
  a student model, our Approach C)?
- How much of Pei's LLM generalisation advantage survives a **controlled** comparison where
  the small model sees the same multi-scenario data?
- Does `nodeCacheCapable: true` change measured extender cost enough to matter at our scale?
- On the target Linux machine: does `/sys/class/powercap/intel-rapl` (or the AMD equivalent)
  exist and is it readable from a container? And is Kepler reporting from real counters
  rather than its model-based estimator? Energy scope depends entirely on this.
- Does cpuset-pinned kind give per-node CPU numbers close enough to separate VMs to be worth
  the simpler setup? Worth one calibration experiment before committing the testbed.
- **Simulator decision**: build a light Kubernetes placement simulator, run DAgger on a real
  cluster, or do offline RL on collected traces? This gates the whole RL track and must be
  settled early.
- Does the CP-SAT teacher stay tractable at the label volume a policy needs? Christensen needs
  1–20 s per instance; label generation may need thousands.
- How much headroom actually exists above the packing-optimal teacher on an application metric?
  If little, RL refinement adds nothing and behavioural cloning is the whole story. Worth
  bounding early with the oracle.
- Papers cited in the project plan but **not yet in the folder**: KubeAI (IEEE Access 2026),
  Dong et al. FGCS 2026 (DRL-MLS, heterogeneous K8s ML training), Electronics 14(5):863 (2025,
  NN for web app scheduling). Obtain and read.
