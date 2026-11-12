#!/usr/bin/env bash
# Copy the authors' video (input.mp4) to /opt/drs on every worker, where the video pods mount it.
# No SSH to the workers: a pod pinned to each one mounts /opt/drs and the file goes through
# kubectl exec. Get the video first with setup/extract-video.py.
#
#   bash setup/install-video.sh [input.mp4]
set -euo pipefail
# shellcheck source=../lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/../lib.sh"

video=${1:-$drs_dir/input.mp4}
[[ -f $video ]] || { echo "$video not found: run setup/extract-video.py first." >&2; exit 1; }
[[ $(sha256sum "$video" | cut -d' ' -f1) == "$VIDEO_SHA256" ]] || { echo "$video: wrong sha256" >&2; exit 1; }
mapfile -t nodes < <(workers)
(( ${#nodes[@]} )) || { echo "No node labelled $LABEL=true." >&2; exit 1; }

setup_namespace
for i in "${!nodes[@]}"; do
  k -n drs-setup apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: video-$i}
spec:
  nodeName: ${nodes[$i]}
  restartPolicy: Never
  tolerations: [{operator: Exists}]
  containers:
    - name: c
      image: $BUSYBOX
      command: [sleep, "600"]
      volumeMounts: [{name: drs, mountPath: /opt/drs}]
  volumes:
    - name: drs
      hostPath: {path: /opt/drs, type: DirectoryOrCreate}
YAML
done
k -n drs-setup wait --for=condition=Ready pod --all --timeout=300s >/dev/null

failed=0
for i in "${!nodes[@]}"; do
  got=$(k -n drs-setup exec -i "video-$i" -- sh -c \
    'cat > /opt/drs/input.tmp && chmod 0644 /opt/drs/input.tmp && mv /opt/drs/input.tmp /opt/drs/input.mp4 && sha256sum /opt/drs/input.mp4' \
    < "$video" | cut -d' ' -f1)
  if [[ $got == "$VIDEO_SHA256" ]]; then echo "OK    ${nodes[$i]}"; else echo "FAIL  ${nodes[$i]}: $got"; failed=1; fi
done
exit "$failed"
