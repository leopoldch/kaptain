#!/usr/bin/env bash
# Run from the root of the repository by the Run campaign workflow and vagrant/experiments.sh.
set -uo pipefail

OUT=${OUT:-results}
summary=${GITHUB_STEP_SUMMARY:-$OUT/summary.md}
mkdir -p "$(dirname -- "$summary")"

read -ra experiments <<< "$EXPERIMENTS"
read -ra strategies <<< "$STRATEGIES"
read -ra seeds <<< "$SEEDS"
read -ra train_on <<< "${TRAIN_ON:-}"
for experiment in "${experiments[@]}" ${TRAIN_ON:-}; do
  [[ -f workloads/experiments/$experiment.toml && $experiment =~ ^[a-z0-9-]+$ ]] || { echo "unknown experiment: $experiment"; exit 1; }
done
for strategy in "${strategies[@]}"; do
  [[ $strategy =~ ^[a-z0-9][a-z0-9.-]*$ ]] || { echo "bad strategy: $strategy"; exit 1; }
done
for seed in "${seeds[@]}"; do
  [[ $seed =~ ^[0-9]+$ ]] || { echo "bad seed: $seed"; exit 1; }
done
[[ $TRAIN_RUNS =~ ^[0-9]+$ ]] || { echo "bad train_runs: $TRAIN_RUNS"; exit 1; }
[[ $REPLICATES =~ ^[1-9][0-9]*$ ]] || { echo "bad replicates: $REPLICATES"; exit 1; }
[[ $VALIDATE_EVERY =~ ^[0-9]+$ ]] || { echo "bad validate_every: $VALIDATE_EVERY"; exit 1; }

# A group is what one deployment is evaluated on: each experiment alone, trained on itself. With
# TRAIN_ON, every experiment at once: one deployment trains on TRAIN_ON's experiments in turn,
# then is evaluated on each, so one policy is measured under every load, some never trained on.
groups=("${experiments[@]}")
(( ${#train_on[@]} )) && groups=("${experiments[*]}")

failed=0
runs=()
for group in "${groups[@]}"; do
  read -ra evaluated <<< "$group"
  trained=("${evaluated[@]}")
  (( ${#train_on[@]} )) && trained=("${train_on[@]}")
  for strategy in "${strategies[@]}"; do
    # Kubernetes alone needs no deployment; only Kaptain's policies are trained several times.
    scheduler='' pin='' deploy=1 replicates=('')
    case $strategy in
      default-scheduler) scheduler=default-scheduler deploy=0 ;;
      pin-*) pin=${strategy#pin-} deploy=0 ;;
      kaptain*) replicates=($(seq 1 "$REPLICATES")) ;;
    esac
    for replicate in "${replicates[@]}"; do
      # Before the deployment: its own check refuses while pods of a killed run remain.
      python3 workloads/kexp.py cleanup
      if (( deploy )) && ! KAPTAIN_SEED=${replicate:-7} bash deploy-policy.sh "$strategy"; then
        runs+=("| $group | $strategy | ${replicate:-–} | all | all | deploy failed |")
        failed=1
        continue
      fi
      # Policies that learn train first, on other seeds; then every policy is evaluated,
      # Kaptain frozen. The others have nothing to learn. Kaptain's checkpoints along
      # the training run frozen on the validation seeds: whether it converges.
      plan=()
      if [[ $strategy == kaptain* || $strategy == shortest-estimate || $strategy == earliest-finish || $strategy == slowed-estimate* ]]; then
        for ((i = 1; i <= TRAIN_RUNS; i++)); do
          plan+=("${trained[(i - 1) % ${#trained[@]}]} train $((999 + i))")
          if [[ $strategy == kaptain* ]] && (( VALIDATE_EVERY && i % VALIDATE_EVERY == 0 )); then
            for experiment in "${evaluated[@]}"; do plan+=("$experiment valid 500 $i" "$experiment valid 501 $i"); done
          fi
        done
      fi
      for experiment in "${evaluated[@]}"; do
        for seed in "${seeds[@]}"; do plan+=("$experiment eval $seed"); done
      done
      for step in "${plan[@]}"; do
        read -r experiment phase seed checkpoint <<< "$step"
        python3 workloads/kexp.py cleanup
        # A namespace name takes no dot: kaptain-b0.5 becomes kaptain-b0-5 there.
        if KEXP_PHASE=$phase KEXP_SEED=$seed KEXP_REPLICATE=$replicate KEXP_SCHEDULER=$scheduler KEXP_PIN=$pin \
            KEXP_CHECKPOINT=$checkpoint \
            KEXP_RUN_ID=$RUN-${strategy//./-}${replicate:+-r$replicate}-$phase-s$seed${checkpoint:+-c$checkpoint} \
            python3 workloads/kexp.py run "$experiment" --out "$OUT"; then
          runs+=("| $experiment | $strategy | ${replicate:-–} | $phase${checkpoint:+ $checkpoint} | $seed | complete |")
        else
          runs+=("| $experiment | $strategy | ${replicate:-–} | $phase${checkpoint:+ $checkpoint} | $seed | failed |")
          failed=1
        fi
      done
    done
  done
done

{
  echo "# Campaign"
  echo
  echo '| Experiment | Policy | Training | Phase | Seed | Run |'
  echo '| --- | --- | --- | --- | --- | --- |'
  printf '%s\n' "${runs[@]}"
  echo
  [[ -d $OUT ]] && python3 workloads/ktable.py "$OUT" --reference least-allocated
} | tee -a "$summary"

if [[ -n ${PLOTS:-} ]]; then
  uv run workloads/kplot.py "$OUT" --out "$OUT/plots" || echo 'The plots failed; the runs are all there.' >&2
fi
exit "$failed"
