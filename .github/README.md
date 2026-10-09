# GitHub Actions

Open **Actions -> select a workflow -> Run workflow** on the default branch.

- **Cluster diagnostic** checks Docker, Kubernetes, and Kaptain pods.
- **Deploy policy** deploys the selected Python policy on the master.
- **Run experiment** runs one experiment from `workloads/experiments/`, optionally after
  deploying a policy, and publishes its report and raw files.

Stop active Kaptain workloads before deploying. Results are shown in the
Action summary and logs.
