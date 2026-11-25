#!/usr/bin/env bash
set -euo pipefail

if systemctl is-active --quiet kaptain-campaign; then
  echo 'A campaign is running: stop it first (systemctl stop kaptain-campaign).' >&2
  exit 1
fi

read_key() {
  [[ -f /vagrant/.env ]] || return 0
  # What > writes in Windows PowerShell 5.1: sed would find no key in it.
  if [[ $(head -c 2 /vagrant/.env | od -An -tx1) == ' ff fe' ]]; then
    echo '.env is in UTF-16: save it as UTF-8. Nothing started.' >&2
    exit 1
  fi
  # A .env written on Windows may start with a BOM and end its lines with \r.
  sed -e '1s/^\xEF\xBB\xBF//' -e 's/\r$//' /vagrant/.env |
    sed -n 's/^OPENAI_API_KEY=//p' | tail -n 1 | sed -e 's/^"//' -e 's/"$//'
}

check_key() {
  # printf is a builtin: the key is on no command line.
  local answer
  answer=$(printf 'header = "Authorization: Bearer %s"\n' "$1" |
    curl -s -m 30 -o /dev/null -w '%{http_code}' --config - https://api.openai.com/v1/models || true)
  if [[ $answer != 200 ]]; then
    echo "OpenAI answered '$answer' to the key in .env (000: not reached): nothing started." >&2
    exit 1
  fi
}

# kexp calls a run complete once its pods have finished, even if they all failed.
check_smoke() {
  PYTHONPATH=workloads python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

from kexp import read_decisions

run = Path(sys.argv[1])
phases = [pod["status"]["phase"] for pod in json.loads((run / "pods.json").read_text())["items"]]
decisions = read_decisions(run) or []
fallbacks = sum(1 for decision in decisions if decision["fallback"])
if not phases or set(phases) != {"Succeeded"} or not decisions or fallbacks:
    sys.exit(f"Smoke test failed, {run}: pods {phases}, {len(decisions)} decisions, {fallbacks} fallbacks")
PY
}

cd /opt/kaptain
export KEXP_KUBECTL='k3s kubectl' KAPTAIN_CTR='k3s ctr' KAPTAIN_MASTER=$MASTER_NAME
campaign=$(date -u +%Y%m%dt%H%M%Sz)
out=/vagrant/results/$campaign

env_file=/run/kaptain/env
install -d -m 700 /run/kaptain
rm -f "$env_file"

llm=$(tr ' ' '\n' <<< "$STRATEGIES" | { grep -E -- '-llm(-ef)?$' || true; } | xargs)
others=$(tr ' ' '\n' <<< "$STRATEGIES" | { grep -vE -- '-llm(-ef)?$' || true; } | xargs)
skipped=''
if [[ -n $llm ]]; then
  key=$(read_key)
  if [[ -n $key ]]; then
    check_key "$key"
    (umask 077; printf 'OPENAI_API_KEY=%s\n' "$key" > "$env_file")
    others=$STRATEGIES
  else
    skipped=$llm
  fi
  unset key
fi
if [[ -z $others ]]; then
  echo 'No policy left to run.' >&2
  exit 1
fi

mkdir -p "$out/campaign"
if [[ -n $skipped ]]; then
  echo "No OPENAI_API_KEY in .env, skipped: $skipped" | tee "$out/campaign/summary.md"
fi

{
  echo "commit: $(git rev-parse HEAD)"
  echo "started: $campaign"
  echo "host: ${HOST:-unknown}"
  echo "architecture: $(uname -m)"
  echo "kernel: $(uname -r)"
  k3s --version | head -n 1
  docker --version
  echo
  # Not cat: from a shared folder to a shared folder, it overwrites the start of the output.
  echo "$(</vagrant/vagrant.yaml)"
  echo
  k3s kubectl get nodes -o wide
} > "$out/environment.txt"

python3 workloads/kexp.py cleanup
bash deploy-policy.sh least-allocated
KEXP_RUN_ID=smoke python3 workloads/kexp.py run smoke --out "$out/smoke"
KEXP_RUN_ID=smoke python3 workloads/kexp.py run smoke-stress --out "$out/smoke"
check_smoke "$out/smoke/smoke-smoke"
check_smoke "$out/smoke/smoke-stress-smoke"
echo "Smoke test passed: see results/$campaign/smoke/"

systemd-run --unit=kaptain-campaign --collect --working-directory=/opt/kaptain \
  --property=EnvironmentFile=-"$env_file" \
  --setenv=KEXP_KUBECTL="$KEXP_KUBECTL" --setenv=KAPTAIN_CTR="$KAPTAIN_CTR" --setenv=KAPTAIN_MASTER="$KAPTAIN_MASTER" \
  --setenv=EXPERIMENTS="$EXPERIMENTS" --setenv=STRATEGIES="$others" --setenv=SEEDS="$SEEDS" \
  --setenv=TRAIN_RUNS="$TRAIN_RUNS" --setenv=REPLICATES="$REPLICATES" --setenv=VALIDATE_EVERY="$VALIDATE_EVERY" \
  --setenv=TRAIN_ON="${TRAIN_ON:-}" \
  --setenv=RUN="$campaign" --setenv=OUT="$out/campaign" --setenv=PLOTS=1 --setenv=HOME=/root \
  bash workloads/campaign.sh

echo "Campaign started in the background: results/$campaign/campaign/"
echo "Follow it: vagrant ssh $MASTER_NAME -c 'journalctl -fu kaptain-campaign'"
