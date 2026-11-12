#!/usr/bin/env bash
# Several arms on one experiment, one run each, in the given order. drs may run several times in
# a row with the same agent (DRS_RUNS): the policy is deployed before its first run only, and the
# script stops if the decider pod changed, since the trained agent would then be lost.
#
#   bash run-arms.sh EXPERIMENT [ARM...]
#   default arms: drs-greedy dummy-random round-robin kube-scheduler drs
#   DRS_RUNS   consecutive drs runs with the same agent (default 1)
#   e.g. DRS_RUNS=3 bash run-arms.sh drsA-even
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

experiment=${1:?Usage: bash run-arms.sh EXPERIMENT [ARM...]}
shift
arms=("$@")
(( ${#arms[@]} )) || arms=(drs-greedy dummy-random round-robin kube-scheduler drs)
drs_runs=${DRS_RUNS:-1}

# Name and restart count of the decider pod: if either changes, the DRS agent is gone.
decider() {
  k -n kube-system get pods -l app=kaptain-decider \
    -o 'jsonpath={range .items[*]}{.metadata.name}:{.status.containerStatuses[0].restartCount} {end}'
}

for arm in "${arms[@]}"; do
  if [[ $arm != drs ]]; then
    echo "=== $(date -u +%FT%TZ): $arm on $experiment"
    bash "$drs_dir/run.sh" "$arm" "$experiment"
    continue
  fi
  for i in $(seq "$drs_runs"); do
    echo "=== $(date -u +%FT%TZ): drs on $experiment, run $i/$drs_runs"
    if (( i == 1 )); then
      bash "$drs_dir/run.sh" drs "$experiment"
      agent=$(decider)
    else
      [[ $(decider) == "$agent" ]] || { echo "decider changed ($agent -> $(decider)): agent lost, stopping." >&2; exit 1; }
      KEEP_AGENT=1 bash "$drs_dir/run.sh" drs "$experiment"
    fi
  done
  [[ $(decider) == "$agent" ]] || { echo "decider changed during the last drs run: leave that run out." >&2; exit 1; }
done
echo "=== $(date -u +%FT%TZ): done (${arms[*]})"
