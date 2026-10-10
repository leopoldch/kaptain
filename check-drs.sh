#!/usr/bin/env bash
# Check, on the K3s master, what the drs policy needs besides the decider image, which
# deploy-policy.sh does not install: without it every pod falls back and the run measures nothing.
set -euo pipefail

container=kaptain-k3s
k() {
  docker exec "$container" kubectl --server=https://10.50.0.1:6443 \
    --request-timeout=20s -n kube-system "$@"
}

if ! config=$(k get configmap kaptain-drs-config -o 'jsonpath={.data.config\.json}') || [[ -z $config ]]; then
  echo 'No kaptain-drs-config ConfigMap: create it from k8s/drs-config.template.json.' >&2
  exit 1
fi
if ! k get deployment kaptain-decider -o yaml | grep -q DRS_CONFIG; then
  echo 'The decider does not mount the DRS config: apply k8s/decider-drs-patch.yaml.' >&2
  exit 1
fi

monitors=$(python3 -c 'import json, sys
for node, url in sorted(json.load(sys.stdin).get("monitors", {}).items()):
    print(node, url)' <<<"$config")
if [[ -z $monitors ]]; then
  echo 'kaptain-drs-config lists no monitors.' >&2
  exit 1
fi
failed=0
while read -r node url; do
  if curl -sf --max-time 2 "$url" >/dev/null; then
    echo "monitor $node: ok"
  else
    echo "monitor $node: no answer from $url (see k8s/drs-monitor.yaml)" >&2
    failed=1
  fi
done <<<"$monitors"
exit "$failed"
