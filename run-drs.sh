#!/usr/bin/env bash
# Run DRS experiments one after the other on the K3s master, unattended:
#   nohup bash run-drs.sh drsA-pilote drsA-even drsA-random drsA-cpu > run-drs.log 2>&1 &
# The decider is redeployed before each run, so every run starts from a fresh DQN. A run that
# fails is reported and the next one starts; a run without a single DRS decision stops the night.
set -uo pipefail

if [[ $# -eq 0 ]]; then
  echo 'Usage: bash run-drs.sh EXPERIMENT...' >&2
  exit 2
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

bash check-drs.sh || exit 1
failures=()
for experiment in "$@"; do
  echo "=== $experiment, $(date -u +%FT%TZ)"
  if ! bash deploy-policy.sh drs; then
    echo "Deploy failed before $experiment: stopping." >&2
    exit 1
  fi
  started=$(date +%s)
  (cd workloads && python3 kexp.py run "$experiment" --out results) || failures+=("$experiment")

  out=$(ls -td "workloads/results/$experiment"-*/ 2>/dev/null | head -1)
  if [[ -z $out || $(stat -c %Y "$out") -lt $started ]]; then
    echo "$experiment left no result directory." >&2
    continue
  fi
  if ! grep -q '"drs_decision"' "$out/decider.jsonl" 2>/dev/null; then
    echo "$experiment: no DRS decision in $out/decider.jsonl, every pod fell back: stopping." >&2
    exit 1
  fi
  echo "$experiment: $(grep -c '"drs_decision"' "$out/decider.jsonl") DRS decisions, results in $out"
done

echo "=== done, $(date -u +%FT%TZ)"
if [[ ${#failures[@]} -gt 0 ]]; then
  echo "Failed: ${failures[*]}" >&2
  exit 1
fi
