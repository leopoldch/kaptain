# shellcheck shell=bash disable=SC2034
# Sourced by every script of the DRS reproduction. Settings come from cluster.env.
drs_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$drs_dir/.." && pwd)
if [[ ! -f $drs_dir/cluster.env ]]; then
  echo "Copy $drs_dir/cluster.env.example to cluster.env and fill it in." >&2
  exit 2
fi
# shellcheck source=cluster.env.example
source "$drs_dir/cluster.env"
: "${KUBECTL:?set KUBECTL in cluster.env}" "${DRS_NET_SERVER:?set DRS_NET_SERVER in cluster.env}"
: "${MONITOR_IFACES:?set MONITOR_IFACES in cluster.env}" "${MONITOR_DISKS:?set MONITOR_DISKS in cluster.env}"
RESULTS=${RESULTS:-$drs_dir/results}
[[ $RESULTS == /* ]] || RESULTS=$PWD/$RESULTS  # kexp.py runs from workloads/
# kexp.py reads the first two, ../deploy-policy.sh the first and the last two.
export KEXP_KUBECTL=$KUBECTL DRS_NET_SERVER KAPTAIN_CTR KAPTAIN_MASTER

LABEL=kaptain.io/experiment-node
MONITOR_PORT=9101
# The images of the DRS pods and of the monitor, as pinned in workloads/pods and k8s/drs-monitor.yaml.
FFMPEG=jrottenberg/ffmpeg@sha256:73b5e69d428ecd4b97c2a8085755ecbe2c56f244a38917ed710bb98597e65d8f
PYTHON=python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
BUSYBOX=busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
STRESS=ghcr.io/colinianking/stress-ng@sha256:cf90385beccc3de5c04839ff86b378c133fa02ec5cf36c0573a5ab5be652edf6
VIDEO_SHA256=079e25c4ef782df7f76bb0f0ef20631c7c5f5bbb433f3e78286a9e9ed95207c9

read -ra kubectl_command <<<"$KUBECTL"
# No --request-timeout here: it would cut kubectl exec while it copies the video.
k() { "${kubectl_command[@]}" "$@"; }

# The labelled workers, one per line.
workers() {
  k get nodes -l "$LABEL=true" -o 'jsonpath={range .items[*]}{.metadata.name}{"\n"}{end}'
}

# The first InternalIP: a dual-stack node lists two.
node_ip() {
  local ips
  ips=$(k get node "$1" -o 'jsonpath={.status.addresses[?(@.type=="InternalIP")].address}')
  echo "${ips%% *}"
}

# Experiment name -> DRS variant: A runs the authors' apps, B the stress-ng ones.
variant_of() {
  case $1 in
    drsA-*|calib-video|calib-net|calib-drsdisk*) echo A ;;
    drsB-*|calib-cpu|calib-memory|calib-disk) echo B ;;
    *) echo none ;;
  esac
}

# A throwaway namespace for setup pods, deleted on exit.
setup_namespace() {
  k wait --for=delete namespace/drs-setup --timeout=180s >/dev/null 2>&1 || true
  k create namespace drs-setup >/dev/null
  trap 'k delete namespace drs-setup --ignore-not-found --wait=true >/dev/null' EXIT
}
