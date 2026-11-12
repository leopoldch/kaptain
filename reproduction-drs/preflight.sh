#!/usr/bin/env bash
# Checks before a run. Read-only, except --with-pods: a pod pinned to each worker checks the video.
#
#   bash preflight.sh ARM [--variant A|B] [--with-pods]
#   ARM: calibration | kube-scheduler | drs | drs-greedy | dummy-random | round-robin | least-used | ...
#   NO_MONITOR=1   check instead that the monitor DaemonSet is absent (calib-idle-no-monitor)
#   MIN_FREE_GB    free disk space required on each worker (default 5; 20 for drsdisk8g)
#
# Exit code 0 only if every check passes.
set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
# Every check is a short request: a server that hangs fails it instead of blocking.
k() { "${kubectl_command[@]}" --request-timeout=20s "$@"; }

arm=${1:?Usage: bash preflight.sh ARM [--variant A|B] [--with-pods]}
shift
variant=none
with_pods=0
while (( $# )); do
  case $1 in
    --variant) variant=$2; shift 2 ;;
    --with-pods) with_pods=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

failures=0
ok() { printf '  OK    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }
info() { printf '  info  %s\n' "$1"; }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else fail "$1"; fi; }

echo "== Cluster"
check "API ready" "k get --raw=/readyz"
mapfile -t nodes < <(workers)
if (( ${#nodes[@]} )); then ok "${#nodes[@]} workers labelled $LABEL=true: ${nodes[*]}"; else fail "no node labelled $LABEL=true"; fi
for node in "${nodes[@]}"; do
  check "$node Ready" "[[ \$(k get node $node -o 'jsonpath={.status.conditions[?(@.type==\"Ready\")].status}') == True ]]"
  info "$node: $(k get node "$node" -o 'jsonpath={.status.nodeInfo.architecture}, {.status.allocatable.cpu} CPU, {.status.addresses[?(@.type=="InternalIP")].address}')"
done
check "no kexp namespace left (kexp.py cleanup)" "[[ -z \$(k get ns -l kaptain.io/experiment -o name) ]]"
busy=$(k get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed \
  -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.spec.nodeName}{"\n"}{end}' \
  | awk -v workers="${nodes[*]}" 'BEGIN { split(workers, w, " "); for (i in w) bench[w[i]] = 1 }
                                  $1 !~ /^kube-system\// && ($2 in bench)')
if [[ -z $busy ]]; then ok "no pod outside kube-system on the workers"; else fail "workers busy: $busy"; fi

echo "== Kaptain"
for name in kaptain-scheduler kaptain-decider; do
  check "$name ready" "[[ \$(k -n kube-system get deploy $name -o 'jsonpath={.status.readyReplicas}') == 1 ]]"
done
scheduler=$(k -n kube-system get deploy kaptain-scheduler -o 'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="KAPTAIN_STRATEGY")].value}')
decider=$(k -n kube-system get deploy kaptain-decider -o 'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="STRATEGY")].value}')
info "deployed policy: scheduler=$scheduler decider=$decider"
case $arm in
  calibration) ok "calibration: pods pinned with nodeName, no scheduler decides" ;;
  kube-scheduler) ok "kube-scheduler arm: kexp runs with --scheduler default-scheduler" ;;
  *) check "deployed policy is $arm" "[[ $scheduler == $arm && $decider == $arm ]]"
     info "decider started at $(k -n kube-system get pods -l app=kaptain-decider -o 'jsonpath={.items[0].status.startTime}')" ;;
esac
if [[ $arm == drs || $arm == drs-greedy || $arm == round-robin ]]; then
  config=$(k -n kube-system get configmap kaptain-drs-config -o 'jsonpath={.data.config\.json}' 2>/dev/null)
  if python3 -c 'import json, sys
c = json.loads(sys.argv[1])
assert sorted(c["monitors"]) == sorted(sys.argv[2].split())
assert sys.argv[3] == "none" or c["variant"] == sys.argv[3]' "$config" "${nodes[*]}" "$variant" 2>/dev/null; then
    ok "kaptain-drs-config lists the workers (variant $variant)"
  else
    fail "kaptain-drs-config missing, of another variant, or not listing exactly the workers (setup/apply-drs-config.sh)"
  fi
  check "the decider mounts the DRS config" "k -n kube-system exec deploy/kaptain-decider -- test -f /etc/kaptain-drs/config.json"
fi

if [[ ${NO_MONITOR:-0} == 1 ]]; then
  echo "== DRS monitors: absent, for the overhead measurement"
  check "no drs-monitor DaemonSet" "! k -n kube-system get ds drs-monitor"
else
  echo "== DRS monitors (present in every arm)"
  check "drs-monitor ready on the ${#nodes[@]} workers" \
    "[[ \$(k -n kube-system get ds drs-monitor -o 'jsonpath={.status.desiredNumberScheduled} {.status.numberReady}') == '${#nodes[@]} ${#nodes[@]}' ]]"
  for node in "${nodes[@]}"; do
    url="http://$(node_ip "$node"):$MONITOR_PORT/state"
    check "$node monitor read from the decider ($url)" \
      "k -n kube-system exec deploy/kaptain-decider -- python -c \"import json, urllib.request; assert 'cpu' in json.load(urllib.request.urlopen('$url', timeout=2))\""
  done
fi

if [[ $variant == A ]]; then
  echo "== Network server (variant A)"
  host=${DRS_NET_SERVER%:*}
  port=${DRS_NET_SERVER##*:}
  check "$DRS_NET_SERVER reachable from the decider" \
    "k -n kube-system exec deploy/kaptain-decider -- python -c \"import socket; socket.create_connection(('$host', $port), timeout=3).close()\""
fi

echo "== Images on the workers"
images=("$PYTHON")  # the monitor
case $variant in
  A) images+=("$FFMPEG" "$BUSYBOX") ;;
  B) images+=("$STRESS") ;;
esac
for node in "${nodes[@]}"; do
  present=$(k get node "$node" -o 'jsonpath={range .status.images[*]}{.names}{"\n"}{end}')
  for image in "${images[@]}"; do
    if grep -q "${image#*@}" <<<"$present"; then ok "$node: ${image%%@*}"; else fail "$node: ${image%%@*} missing (setup/preload-images.sh)"; fi
  done
done

echo "== Resources"
for node in "${nodes[@]}"; do
  summary=$(k get --raw "/api/v1/nodes/$node/proxy/stats/summary")
  free_gb=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["node"]["fs"]["availableBytes"] // 10**9)' <<<"$summary")
  if (( free_gb >= ${MIN_FREE_GB:-5} )); then ok "$node: $free_gb GB free"; else fail "$node: $free_gb GB free (< ${MIN_FREE_GB:-5})"; fi
  skew=$(python3 -c 'import datetime, json, sys
stamp = json.load(sys.stdin)["node"]["cpu"]["time"].replace("Z", "+00:00")
print(int(abs(datetime.datetime.now(datetime.timezone.utc).timestamp() - datetime.datetime.fromisoformat(stamp).timestamp())))' <<<"$summary")
  if (( skew <= 20 )); then ok "$node: kubelet clock within ${skew} s of this host"; else fail "$node: kubelet clock ${skew} s away (NTP?)"; fi
done

if (( with_pods )); then
  echo "== Video on the workers (--with-pods)"
  setup_namespace
  for i in "${!nodes[@]}"; do
    k -n drs-setup run "video-$i" --restart=Never --image="$BUSYBOX" \
      --overrides="{\"spec\":{\"nodeName\":\"${nodes[$i]}\",\"tolerations\":[{\"operator\":\"Exists\"}],\"volumes\":[{\"name\":\"d\",\"hostPath\":{\"path\":\"/opt/drs\",\"type\":\"Directory\"}}],\"containers\":[{\"name\":\"c\",\"image\":\"$BUSYBOX\",\"command\":[\"sha256sum\",\"/drs/input.mp4\"],\"volumeMounts\":[{\"name\":\"d\",\"mountPath\":\"/drs\"}],\"securityContext\":{\"runAsUser\":65534}}]}}" >/dev/null
  done
  sleep 20
  for i in "${!nodes[@]}"; do
    if k -n drs-setup logs "video-$i" 2>/dev/null | grep -q "$VIDEO_SHA256"; then ok "${nodes[$i]}: input.mp4"; else fail "${nodes[$i]}: input.mp4 missing or different (setup/install-video.sh)"; fi
  done
fi

echo
if (( failures )); then echo "PREFLIGHT: $failures check(s) failed, do not run."; exit 1; fi
echo "PREFLIGHT: all checks passed."
