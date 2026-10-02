# Kaptain cluster runner

The `kaptain-vps-alex` runner is installed on Alex's VPS for the
`leopoldch/kaptain` repository. It runs as `test-kpt`, a member of the
`docker` group, in `/home/test-kpt/actions-runner`.

Labels: `self-hosted`, `linux`, `x64`, `kaptain-cluster`.

The October 2, 2026 installation uses the official GitHub runner 2.337.0,
with SHA-256 verification of the archive. Automatic runner updates are
enabled.

## Service

The systemd service starts automatically when the VPS boots:

```bash
cd /home/test-kpt/actions-runner
sudo ./svc.sh status
sudo ./svc.sh stop
sudo ./svc.sh start
```

The runner connects to GitHub from the VPS. Workflows do not need the SSH
credentials used during installation.

## Diagnostics from GitHub

Publish `cluster-diagnostic.yml` on the default branch, `mainline`, to make
it available for manual execution in GitHub.
Then select Actions -> Cluster diagnostic -> Run workflow, using `mainline`.

It checks Docker, the K3s API, and node readiness, and lists Kaptain
deployments and pods. Results appear in the GitHub Actions summary.
It does not launch workloads or modify deployments.

The diagnostic commands passed on the VPS under `test-kpt`. The GitHub API
confirmed that the runner was `online`. The workflow is prepared locally;
no GitHub Actions run has been launched yet.

Kubernetes access:

```bash
docker exec kaptain-k3s kubectl --server=https://10.50.0.1:6443 get nodes -o wide
```

At installation, three nodes were Ready: `alex-master`,
`leopold-raspberrypi`, and `alex-jetson`. `leopold-vps` was absent.

## Guidelines for future workflows

- Use the `kaptain-cluster` label and the same `kaptain-cluster` concurrency
  group for deployments and experiments. Do not cancel an experiment in progress.
- The repository is public, and the runner can access Docker on the VPS.
  Reserve it for trusted manual workflows on `mainline`. Pull request checks
  should use GitHub-hosted runners. Avoid `pull_request_target` on this runner.
- Keep SSH passwords, registration tokens, and the runner's `.credentials*`
  files out of the repository and workflow artifacts.
- Future deployments should check out the requested commit into the Actions
  workspace and use images identified by that commit.

References:
[adding a runner](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners),
[systemd service](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application).
