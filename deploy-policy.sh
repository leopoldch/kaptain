#!/usr/bin/env bash
# Deploy a Python policy on the existing K3s master.
set -euo pipefail

strategy=${1:-}
case "$strategy" in
  dummy-random|largest-cpu-capacity|least-allocated|least-used) ;;
  *) echo 'Usage: bash deploy-policy.sh POLICY' >&2; exit 2 ;;
esac

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
commit=$(git rev-parse HEAD)
if [[ -n $(git status --porcelain -- bridge/) ]]; then
  echo 'Commit the policy code before deploying.' >&2
  exit 1
fi
container=kaptain-k3s
run_id="deploy-${GITHUB_RUN_ID:-$(date -u +%s)}-${GITHUB_RUN_ATTEMPT:-1}"
image="kaptain-decider:$commit-$run_id"
changing=0
recovery='Not needed'

k() {
  docker exec "$container" kubectl --server=https://10.50.0.1:6443 \
    --request-timeout=20s -n kube-system "$@"
}
ready() {
  k rollout status "deployment/$1" --timeout=180s
}
stop_scheduler() {
  k scale deployment/kaptain-scheduler --replicas=0 || return 1
  k wait --for=delete pod -l app=kaptain-scheduler --timeout=180s
}
rollback() {
  echo 'Restoring the previous policy.'
  stop_scheduler || return 1
  k rollout undo deployment/kaptain-decider --to-revision="$decider_revision" || return 1
  ready kaptain-decider || return 1
  k rollout undo deployment/kaptain-scheduler --to-revision="$scheduler_revision" || return 1
  k scale deployment/kaptain-scheduler --replicas=1 || return 1
  ready kaptain-scheduler || return 1
}
cleanup_images() {
  local images old reference
  images=$(docker image ls kaptain-decider --format '{{.Repository}}:{{.Tag}}') || return 1
  for old in $images; do
    [[ $old == "$image" || $old == "$previous_image" || $old == *':<none>' ]] && continue
    docker image rm "$old" || return 1
  done
  # Untagged leftovers of earlier decider builds (failed or superseded): ours by label only.
  docker image prune -f --filter label=kaptain.io/component=decider >/dev/null || return 1
  images=$(docker exec "$container" ctr images list -q) || return 1
  for reference in $images; do
    old=${reference#docker.io/library/}
    [[ $old == kaptain-decider:* ]] || continue
    [[ $old == "$image" || $old == "$previous_image" ]] && continue
    docker exec "$container" ctr images rm "$reference" || return 1
  done
}
finish() {
  result=$?
  trap - EXIT INT TERM
  set +e
  status=Success
  if (( result != 0 )); then
    status=Failed
    if (( changing )); then
      if rollback; then
        recovery='Previous policy restored'
      else
        recovery="Failed: restore decider revision $decider_revision and scheduler revision $scheduler_revision"
      fi
    fi
  fi
  {
    echo "### Deployment: $status"
    echo
    echo "- Policy: $strategy"
    echo "- Commit: $commit"
    echo "- Image: $image"
    echo "- Rollback: $recovery"
  } | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Check the cluster before building or changing anything.
k get --raw=/readyz
active=$(k get pods --all-namespaces \
  --field-selector=status.phase!=Succeeded,status.phase!=Failed \
  -o 'jsonpath={range .items[?(@.spec.schedulerName=="kaptain-scheduler")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}')
if [[ -n "$active" ]]; then
  echo "Stop active Kaptain workloads before deploying: $active" >&2
  exit 1
fi
for name in kaptain-decider kaptain-scheduler; do
  ready "$name"
  [[ $(k get deployment "$name" -o 'jsonpath={.spec.replicas}') == 1 ]]
done
revision='{.metadata.annotations.deployment\.kubernetes\.io/revision}'
decider_revision=$(k get deployment kaptain-decider -o "jsonpath=$revision")
scheduler_revision=$(k get deployment kaptain-scheduler -o "jsonpath=$revision")
previous_image=$(k get deployment kaptain-decider \
  -o 'jsonpath={.spec.template.spec.containers[?(@.name=="decider")].image}')
previous_image=${previous_image#docker.io/library/}
[[ $decider_revision =~ ^[1-9][0-9]*$ && $scheduler_revision =~ ^[1-9][0-9]*$ ]]
echo "Previous revisions: decider=$decider_revision scheduler=$scheduler_revision"

# Build and check the image, then import it into K3s.
docker build --label "org.opencontainers.image.revision=$commit" --label kaptain.io/component=decider \
  -t "$image" bridge/
docker run --rm --network none -e "STRATEGY=$strategy" "$image" \
  uv run --no-sync python -c 'import app; print(app.healthz())'
docker save "$image" | docker exec -i "$container" ctr images import -

# Pause scheduling while the two components switch policies.
changing=1
stop_scheduler
k set image deployment/kaptain-decider decider="$image"
k set env deployment/kaptain-decider STRATEGY="$strategy"
ready kaptain-decider
k set env deployment/kaptain-scheduler \
  KAPTAIN_STRATEGY="$strategy" POLICY_VERSION="$commit" RUN_ID="$run_id"
k scale deployment/kaptain-scheduler --replicas=1
ready kaptain-scheduler

# Scheduler readiness also checks that the decider reports the same policy.
selector='app in (kaptain-decider,kaptain-scheduler)'
off_master=$(k get pods -l "$selector" --field-selector=spec.nodeName!=alex-master -o name)
[[ -z "$off_master" ]] || { echo "Kaptain pods are outside the master: $off_master" >&2; exit 1; }
k get pods -l "$selector" -o wide

# Keep the deployed image and its predecessor for rollback. Never prune on failure.
cleanup_images || echo 'Warning: image cleanup incomplete; deployment succeeded.' >&2
