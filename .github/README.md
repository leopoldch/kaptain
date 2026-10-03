# GitHub Actions

Run workflows from **Actions -> select a workflow -> Run workflow**, using
the repository's default branch. Workflows start manually and share the
`kaptain-cluster` concurrency group to prevent overlapping cluster operations.

| Workflow | Purpose |
| --- | --- |
| Cluster diagnostic | Check Docker, the K3s API, node readiness, and Kaptain pods. |
| Deploy policy | Build and deploy the selected Python decision policy on the master. |

## Deploying a policy

Choose a strategy in **Deploy policy**. The workflow runs
[`deploy-policy.sh`](../deploy-policy.sh), builds the decider image with the
commit and run ID in its tag, and imports it into K3s. It briefly stops the
scheduler, updates the decider, aligns the scheduler's policy, and verifies
both components are Ready on `alex-master`. The scheduler keeps its image.

Active Kaptain workloads must be stopped before deployment. Failures after
updates begin restore the previous Kubernetes rollout revisions. If recovery
fails, the summary gives the revision numbers to restore with `rollout undo`.
Results appear in the Action summary; diagnostics appear in the logs.
The script targets the existing lab setup with one replica per component.
Run direct deployments one at a time; GitHub Actions serializes them automatically.

## Runner

`kaptain-vps-alex` runs as `test-kpt` on Alex's VPS, with Docker access and
the `kaptain-cluster` label. Its systemd service starts automatically.
To check it on the VPS:

```bash
cd /home/test-kpt/actions-runner
sudo ./svc.sh status
```

Use this runner for trusted workflows. Pull request checks should use
GitHub-hosted runners.
