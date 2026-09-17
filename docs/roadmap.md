# Roadmap and open questions

Ordered by what unblocks the most. The current blocking item is the experiment runner: both
integrations produce comparable per-decision logs and metrics, and nothing aggregates them
into a result yet.

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
