#!/usr/bin/env bash
# Install a DRS configuration (from make-config.py, or k8s/drs-config.template.json for a first
# try) as the kaptain-drs-config ConfigMap, read by the drs, drs-greedy and round-robin policies.
# Its monitors are rewritten from the labelled workers: their sorted names are DRS's action space.
# The decider reads it at start only: deploy the policy afterwards (run.sh does, before each run).
#
#   bash setup/apply-drs-config.sh CONFIG.json
set -euo pipefail
# shellcheck source=../lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/../lib.sh"

config=${1:?Usage: bash setup/apply-drs-config.sh CONFIG.json}
# A for loop, not while read: docker exec -i, in KUBECTL, would swallow the rest of the list.
mapfile -t nodes < <(workers)
(( ${#nodes[@]} )) || { echo "No node labelled $LABEL=true." >&2; exit 1; }
monitors=$(for node in "${nodes[@]}"; do printf '%s %s\n' "$node" "$(node_ip "$node")"; done)

filled=$(python3 - "$config" "$MONITOR_PORT" "$monitors" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
port = sys.argv[2]
config["monitors"] = {node: f"http://{ip}:{port}/state"
                      for node, ip in (line.split() for line in sys.argv[3].splitlines())}
print(json.dumps(config, indent=2))
PY
)
k -n kube-system create configmap kaptain-drs-config --from-file=config.json=/dev/stdin \
  --dry-run=client -o yaml <<<"$filled" | k apply -f -
if ! k -n kube-system get deployment kaptain-decider -o yaml | grep -q DRS_CONFIG; then
  k -n kube-system patch deployment kaptain-decider --patch "$(< "$repo_dir/k8s/decider-drs-patch.yaml")"
fi
python3 -c 'import json, sys; c = json.loads(sys.argv[1]); print("variant", c["variant"]); [print(n, u) for n, u in c["monitors"].items()]' "$filled"
