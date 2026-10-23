# GitHub Actions

Open **Actions -> select a workflow -> Run workflow** on the default branch.

- **Cluster diagnostic** checks Docker, Kubernetes, and Kaptain pods.
- **Deploy policy** deploys the selected Python policy on the master.
- **Run experiment** runs one experiment from `workloads/experiments/`, optionally after
  deploying a policy, and publishes its report and raw files.
- **Run campaign** runs every experiment × policy × seed listed. Default: `drs-even`; Kaptain
  with beta in {0, 0.5, 2} and lambda in {0, 1, 4}, one at a time, the hand-written Phi or the
  LLM's (`-llm`, which uses the `OPENAI_API_KEY` secret); shortest-estimate and least-allocated;
  seeds 7 to 11. Each policy is deployed afresh for each experiment. The policies that learn
  (Kaptain, shortest-estimate) first train on `train_runs` other seeds (1000, 1001, ...), then
  every policy runs the evaluated seeds, Kaptain frozen: 8 × 10 + 5 = 85 runs. The summary has
  one report per run, then a table per experiment of the evaluated runs
  (`workloads/ktable.py`): mean ± 95% interval per policy, and the change against
  least-allocated paired by seed. A run that fails does not stop the others.

Stop active Kaptain workloads before deploying. Results are shown in the
Action summary and logs.
