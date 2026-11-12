#!/usr/bin/env bash
# Pull the images of the DRS pods and of the monitor on every worker, so that no run measures a
# pull (ffmpeg weighs several hundred MB). A pod pinned to each worker pulls the image; it counts
# as present once the node lists it in .status.images.
#
#   bash setup/preload-images.sh [IMAGE@sha256:...]...    (default: all the DRS images)
set -euo pipefail
# shellcheck source=../lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/../lib.sh"

images=("$@")
(( ${#images[@]} )) || images=("$FFMPEG" "$PYTHON" "$BUSYBOX" "$STRESS")
mapfile -t nodes < <(workers)
(( ${#nodes[@]} )) || { echo "No node labelled $LABEL=true." >&2; exit 1; }

setup_namespace
failed=0
for image in "${images[@]}"; do
  digest=${image#*@}
  [[ $digest == sha256:* ]] || { echo "$image: pin it by digest" >&2; exit 2; }
  for i in "${!nodes[@]}"; do
    # The command may not exist in the image: the pod can fail, the pull is what matters.
    k -n drs-setup run "pull-$i" --image="$image" --restart=Never --image-pull-policy=IfNotPresent \
      --overrides="{\"spec\":{\"nodeName\":\"${nodes[$i]}\",\"tolerations\":[{\"operator\":\"Exists\"}]}}" \
      --command -- sleep 1 >/dev/null
  done
  for node in "${nodes[@]}"; do
    present=0
    for _ in $(seq 180); do  # 15 min
      if k get node "$node" -o 'jsonpath={range .status.images[*]}{.names}{"\n"}{end}' | grep -q "$digest"; then
        present=1
        break
      fi
      sleep 5
    done
    if (( present )); then echo "OK    $node: ${image%%@*}"; else echo "FAIL  $node: ${image%%@*} missing after 15 min"; failed=1; fi
  done
  k -n drs-setup delete pod --all --wait=true >/dev/null
done
exit "$failed"
