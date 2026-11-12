#!/usr/bin/env bash
# Calibration: each app of a variant alone on each worker, three pods one after the other,
# pinned with nodeName. make-config.py turns the results into DRS's caps and pod vectors.
#
#   bash calibrate.sh A|B
#   APPS   override the apps (default: video net drsdisk for A, cpu memory disk for B);
#          drsdisk-8g instead of drsdisk for the authors' 8.2 GB disk task
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

case ${1:-} in
  A) apps=${APPS:-video net drsdisk} ;;
  B) apps=${APPS:-cpu memory disk} ;;
  *) echo 'Usage: bash calibrate.sh A|B' >&2; exit 2 ;;
esac
mapfile -t nodes < <(workers)
(( ${#nodes[@]} )) || { echo "No node labelled $LABEL=true." >&2; exit 1; }
for app in $apps; do
  for node in "${nodes[@]}"; do
    echo "=== $(date -u +%FT%TZ): calib-$app on $node"
    bash "$drs_dir/run.sh" calibration "calib-$app" --node "$node"
  done
done
echo "=== $(date -u +%FT%TZ): calibration done. Next: python3 make-config.py --variant $1 $RESULTS (--disk-8g after drsdisk-8g)"
