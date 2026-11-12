#!/usr/bin/env bash
# Start the DRS monitor (monitor/drs_monitor.py) on every worker, as the DaemonSet of
# k8s/drs-monitor.yaml, measuring the interface and disk named in cluster.env. It runs in every
# arm, not only under drs: the same load on the nodes, and the same six metrics for every policy.
#
#   bash setup/apply-monitors.sh            # apply, and wait for one ready monitor per worker
#   bash setup/apply-monitors.sh --remove   # delete it, for calib-idle-no-monitor only
set -euo pipefail
# shellcheck source=../lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/../lib.sh"

if [[ ${1:-} == --remove ]]; then
  k -n kube-system delete daemonset drs-monitor --ignore-not-found --wait=true
  exit 0
fi

k -n kube-system create configmap drs-monitor-script --from-file=drs_monitor.py=/dev/stdin \
  --dry-run=client -o yaml < "$repo_dir/monitor/drs_monitor.py" | k apply -f -
sed -e "s|{ name: DRS_IFACES, value: \"[^\"]*\" }|{ name: DRS_IFACES, value: \"$MONITOR_IFACES\" }|" \
    -e "s|{ name: DRS_DISK, value: \"[^\"]*\" }|{ name: DRS_DISK, value: \"$MONITOR_DISKS\" }|" \
    "$repo_dir/k8s/drs-monitor.yaml" | k apply -f -
# A new script in the ConfigMap is only read by new pods.
k -n kube-system rollout restart daemonset drs-monitor >/dev/null
k -n kube-system rollout status daemonset drs-monitor --timeout=300s

expected=$(workers | wc -l | tr -d ' ')
ready=$(k -n kube-system get daemonset drs-monitor -o 'jsonpath={.status.numberReady}')
[[ $ready == "$expected" ]] || { echo "$ready monitors ready, $expected workers." >&2; exit 1; }
k -n kube-system logs -l app=drs-monitor --prefix --tail=-1 | grep monitor_start || true
