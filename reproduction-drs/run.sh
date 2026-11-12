#!/usr/bin/env bash
# One run of the DRS reproduction: deploy the policy (a fresh DRS agent each time), preflight,
# kexp run, network server log, cooldown. Stops at the first failure.
#
#   bash run.sh ARM EXPERIMENT [--node NODE]
#   ARM: calibration | kube-scheduler | drs | drs-greedy | dummy-random | round-robin
#        | least-used | least-allocated | largest-cpu-capacity
#   e.g. bash run.sh drs drsA-even
#        bash run.sh calibration calib-video --node worker-1
#
#   KEEP_AGENT=1   keep the deployed policy, so a drs agent goes on learning from its last run
#   COOLDOWN_S     pause after the run (default 60)
#   MIN_FREE_GB    passed to the preflight (default 5; 20 for drsdisk8g)
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

usage='Usage: bash run.sh ARM EXPERIMENT [--node NODE]'
arm=${1:?$usage}
experiment=${2:?$usage}
node=
if (( $# > 2 )); then
  [[ $3 == --node && -n ${4:-} ]] || { echo "$usage" >&2; exit 2; }
  node=$4
fi
[[ -f $repo_dir/workloads/experiments/$experiment.toml ]] || { echo "unknown experiment: $experiment" >&2; exit 2; }
[[ $arm != calibration || -n $node || $experiment == calib-idle-* ]] || { echo "calibration needs --node" >&2; exit 2; }

variant=$(variant_of "$experiment")
mkdir -p "$RESULTS"
stamp=$(date -u +%Y%m%dt%H%M%Sz)
# The run id names the namespace kexp-<run id>: lower case, 63 characters at most.
run_id=$(tr '[:upper:]' '[:lower:]' <<<"$arm-$stamp")
log="$RESULTS/log-$experiment-$run_id.txt"
exec > >(tee "$log") 2>&1
echo "== $stamp: $arm on $experiment${node:+ (node $node)}, variant $variant, commit $(git -C "$repo_dir" rev-parse --short HEAD)"

if [[ $arm != calibration && $arm != kube-scheduler && ${KEEP_AGENT:-0} != 1 ]]; then
  bash "$repo_dir/deploy-policy.sh" "$arm"
fi

[[ $experiment == calib-idle-no-monitor ]] && export NO_MONITOR=1
bash "$drs_dir/preflight.sh" "$arm" --variant "$variant"

options=()
[[ $arm == kube-scheduler ]] && options+=(--scheduler default-scheduler)
[[ -n $node ]] && options+=(--node "$node")
started=$(date -u +%FT%TZ)
status=0
(cd "$repo_dir/workloads" && KEXP_RUN_ID=$run_id python3 kexp.py run "$experiment" --out "$RESULTS" ${options[@]+"${options[@]}"}) || status=$?

out="$RESULTS/$experiment-$run_id"
if [[ -d $out ]]; then
  echo "$arm" > "$out/arm.txt"
  if [[ $variant == A ]] && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx drs-net-server; then
    docker logs --since "$started" drs-net-server > "$out/netserver.jsonl" 2>&1 || true
  fi
  cp "$log" "$out/log.txt" 2>/dev/null || true
fi

echo "== kexp exited with $status; cooldown ${COOLDOWN_S:-60} s"
sleep "${COOLDOWN_S:-60}"
exit "$status"
