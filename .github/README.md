# GitHub Actions

Open **Actions -> select a workflow -> Run workflow** on the default branch.

- **Cluster diagnostic** checks Docker, Kubernetes, and Kaptain pods.
- **Deploy policy** deploys the selected Python policy on the master.
- **Run experiment** runs one experiment from `workloads/experiments/`, optionally after
  deploying a policy, and publishes its report and raw files.
- **Run campaign** runs every experiment × policy × seed listed. Default: `even-5s` and
  `even-10s`; Kaptain (delay only), earliest-finish, shortest-estimate, least-allocated and
  Kubernetes' own `default-scheduler`; seeds 7 to 11. Each policy is deployed afresh for each
  experiment. The policies that learn (Kaptain, shortest-estimate, earliest-finish) first train
  on `train_runs` other seeds (1000, 1001, ...), then every policy runs the evaluated seeds,
  Kaptain frozen. Each Kaptain policy does it `replicates` times, independent trainings
  (`KAPTAIN_SEED` 1, 2, ...); with `validate_every` set, every that many training runs it also
  runs frozen on the validation seeds 500 and 501, to see whether it converges. `pin-<node>` runs Kubernetes on that node only, to measure each
  task on each node (`calibration`, `calibration-load`). `nodes` runs a campaign on other machines
  than those labelled `kaptain.io/experiment-node=true`, for instance without `leopold-pve-worker-1`
  and with `alex-jetson`. The summary has one report per run, then a table per
  experiment of the evaluated runs (`workloads/ktable.py`): mean ± 95% interval per policy
  (over the trainings when there are several), and the change against least-allocated paired
  by seed. A run that fails does not stop the others.

Stop active Kaptain workloads before deploying. Results are shown in the
Action summary and logs.
