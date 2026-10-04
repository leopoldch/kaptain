# Can the Alibaba 2018 trace teach a scheduler?

Alibaba cluster-trace-v2018: 8 days, 4,034 identical machines (96 cores), 14.3 M batch tasks,
1.35 B instances. A **decision** = one instance (pod) placed on one machine at one time.

**Short answer: no for placement, yes for workloads.** The trace cannot tell that a
placement was bad, so offline RL or imitation of the scheduler is not possible. It is a good
source of realistic jobs to replay in Kaptain, and it lets us predict what a job will consume.

![summary](results/figures/summary.png)

## What the trace has, and what it lacks

| before the decision | at the decision | after the decision |
|---|---|---|
| requested CPU / memory, number of instances, DAG (in task names) | machine CPU, memory, network, disk every ~10 s (gaps cover 9.7 % of the time) | duration (1 s precision, median 9 s), status, real CPU / memory used |

Missing, and no analysis can rebuild it: the **candidate machines**, the **scheduler scores**,
the **input size** of each instance, the cause of failures. With 1 s precision, 53 % of the
durations are off by at least 10 %.

![](results/figures/explore_duration_precision.png)

There was a real choice to make: at the same moment, the 10 % calmest and 10 % busiest
machines differ by 26 CPU points. PCA needs 4 components out of 5 for 99 % of the machine
state; NMF shows that 87 % of machines follow the same day/night cycle and finds two
anomalies (end of trace, idle machines), which are excluded.

| | |
|---|---|
| ![](results/figures/explore_decision_margin.png) | ![](results/figures/explore_pca_variance.png) |

![](results/figures/explore_nmf_profiles.png)

## Placement: the machine barely matters

We compare **twins**: copies of the same task started in the same 5 minutes on different
machines (11.5 M instances). Only the machine changes.

| | |
|---|---|
| ![](results/figures/twins_information_budget.png) | ![](results/figures/twins_load_effect.png) |

- The task explains **95 %** of the duration variance, the machine **0.28 %**.
- A machine above 60 % CPU looks 42 % slower when tasks are mixed, but only **5 % slower**
  between twins: long tasks simply land more often on busy machines.
- The effect is real but small: about 1 s for a 10 s instance.
- Load hurts the worst cases more than the median: P99 gap +86 % on calm machines, +149 % above 60 % CPU.

![](results/figures/twins_quantiles.png)

**A model cannot flag a bad decision.** Bad = 1.5x slower than the twins' median and at least
2 s. Gradient boosting on the machine state reaches AUC 0.58; a placebo that sees the
machine state **1 h later** reaches 0.57. The model learns which machines are always busy,
not what happened at the decision. A temporal split (days 0-5 → 5.5-7.75) gives the same picture.

| | |
|---|---|
| ![](results/figures/placement_roc.png) | ![](results/figures/placement_temporal_validation.png) |

## The closest we get to "this decision was bad"

| | |
|---|---|
| ![](results/figures/bad_twin_group_example.png) | ![](results/figures/bad_regret.png) |

- On a loaded machine, a pod is slower than its calm twins in 60 % of cases and faster in
  18 %: net 15.6 % of time lost.
- **Slow machines stay slow** (correlation 0.95 between days 0-4 and 4-8). They host
  heavy online services (correlation 0.52 with reserved online CPU, only 0.13 with neighbour CPI).
- Flagging the 10 % most suspicious pods finds 12 truly slow pods per 100, against 6 at random.

| | |
|---|---|
| ![](results/figures/bad_machine_reputation.png) | ![](results/figures/bad_reputation_vs_neighbours.png) |

![](results/figures/bad_detection.png)

So the only placement lesson is **which machines to avoid**, not how to place each pod.

## Workloads: what the trace is good for

A job is a DAG of tasks; a task has N identical instances (like a Kubernetes Job with
parallelism N). 76 % of dependent tasks start before their last parent ends (pipelining).

| | |
|---|---|
| ![](results/figures/workload_job_dag.png) | ![](results/figures/workload_job_composition.png) |

| | |
|---|---|
| ![](results/figures/workload_job_timeline.png) | ![](results/figures/workload_delay_categories.png) |

**Six job families** (205,936 jobs, 14 features in 3 equally weighted blocks: requested,
behavior, DAG structure; PCA then KMeans k = 6, stability 0.99). KMeans beats GMM and
HDBSCAN on stability and separation. A classifier recognizes the family **before launch**
with 94 % accuracy, except the straggler-prone family (15 %), which only shows at run time.

| | |
|---|---|
| ![](results/figures/jobs_family_map.png) | ![](results/figures/jobs_recognition.png) |

![](results/figures/jobs_method_comparison.png)

LDA topics (job = document, task = word) give readable task mixes but match the families
only moderately (NMI 0.39): they see composition, not behavior.

| | |
|---|---|
| ![](results/figures/topics_words.png) | ![](results/figures/topics_vs_clusters.png) |

**Predicting consumption works.** A typical pod uses about 14 % of the memory it requests
(about 7x over-reservation). Gradient boosting on before-launch features predicts, on later
days, memory used / requested (R² 0.37), CPU (0.58) and duration (0.66), and still 0.37 /
0.55 / 0.48 on job profiles never seen in training.

| | |
|---|---|
| ![](results/figures/prediction_reservation_simple.png) | ![](results/figures/prediction_usage.png) |

PCA, an autoencoder, TF-IDF on task mixes and LLM text embeddings (all-MiniLM-L6-v2) do
**not** beat the raw features.

![](results/figures/embeddings_prediction.png)

Relaunching stragglers would save at most 6.5 % of task time (upper bound, 9 extra copies
per 100 instances).

![](results/figures/prediction_straggler_gain.png)

## Limits

- All machines are identical: nothing can be said about heterogeneous clusters, where
  placement matters most.
- Samples: twins and models on 1 % of jobs (fixed hash), families on 5 %.
- 85 % of jobs share their before-launch profile with another job, so random splits are
  optimistic; we use a temporal split.
- The "bad decision" threshold is a choice; other thresholds change the base rate, not the verdict.

## What to do with it in Kaptain

Replay the trace as a **workload generator** (arrivals, task sizes, DAGs, day/night cycle),
use the families to build scenarios, use the consumption predictor to right-size requests,
and learn placement in Kaptain itself, where candidates, scores and outcomes are logged.

## Reproduce

`data/` is not committed. Download the trace from
[alibaba/clusterdata](https://github.com/alibaba/clusterdata/tree/master/cluster-trace-v2018)
into `data/raw/`: `machine_meta.csv`, `machine_usage.csv`, `container_meta.csv`, `batch_task.csv`,
`batch_instance.csv` (unpacked) and `container_usage.tar.gz` (kept packed, 176 GB unpacked).

Then, from this folder:

```bash
uv run scripts/prepare.py             # CSV -> Parquet, skips what exists
uv run scripts/explore.py             # duration precision, machine state (PCA, NMF)
uv run scripts/workload.py            # task clusters, DAG delays, job composition
uv run scripts/twins.py               # does the machine change the duration?
uv run scripts/placement_model.py     # can a model flag a bad decision?
uv run scripts/bad_decisions.py       # regret, machine reputation, detection
uv run scripts/jobs.py                # job families, recognition before launch
uv run scripts/prediction.py          # over-reservation, stragglers, real usage
uv run scripts/embeddings.py          # PCA, autoencoder, LLM embeddings vs raw features
uv run scripts/topics.py              # LDA topics (~10 min)
uv run scripts/summary.py             # summary figure
```
