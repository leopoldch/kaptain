#!/usr/bin/env python3
"""One table per experiment: each policy's mean over its seeds, with a 95% interval, and its
change against a reference policy, paired by seed. A policy trained several times independently
(replicates) gets one value per training, its mean over the seeds, and the interval is over the
trainings: whether training reliably gives a good policy. Then the mean delay of each training
run, in order, to see how fast each policy learns, and of the frozen checkpoints run on the
validation seeds, to see whether it converges. Reads run folders as kexp.py writes them.

    python3 ktable.py runs/ --reference least-allocated"""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

from kexp import makespan, mean_imbalance, p95, pod_times, seconds, task_cpu

# Student t, two-sided 95%, by degrees of freedom; 1.96 beyond.
T95 = [12.71, 4.30, 3.18, 2.78, 2.57, 2.45, 2.36, 2.31, 2.26, 2.23, 2.20, 2.18, 2.16, 2.14, 2.13,
       2.12, 2.11, 2.10, 2.09, 2.09, 2.08, 2.07, 2.07, 2.06, 2.06, 2.06, 2.05, 2.05, 2.05, 2.04]
METRICS = ["mean delay (s)", "p95 delay (s)", "makespan (s)", "task CPU-s", "node CPU-s", "imbalance (pts)"]
# Against the reference: the makespan is set by the arrivals, the imbalance only explains.
COMPARED = ["mean delay (s)", "p95 delay (s)", "task CPU-s"]


def cpu_seconds(folder):
    """Kubelet CPU of the nodes, integrated over the run: the whole node, not only our pods."""
    samples = defaultdict(list)
    with (folder / "telemetry.csv").open() as handle:
        for row in csv.DictReader(handle):
            samples[row["node"]].append((seconds(row["time"]), float(row["cpu_cores"])))
    total = 0.0
    for points in samples.values():
        points.sort()
        total += sum((t1 - t0) * (c0 + c1) / 2 for (t0, c0), (t1, c1) in zip(points, points[1:]))
    return total


def metrics(folder, meta):
    """None when no pod succeeded: kexp calls a run complete once its pods finished, failed or not."""
    pods = json.loads((folder / "pods.json").read_text())["items"]
    succeeded = [pod_times(pod) for pod in pods if pod["status"]["phase"] == "Succeeded"]
    if not succeeded:
        return None
    delays = [row["completion"] for row in succeeded]
    # Per succeeded pod, only when every one of them reported it: a partial mean would be biased.
    cpu = task_cpu(folder) or {}
    names = [pod["metadata"]["name"] for pod in pods if pod["status"]["phase"] == "Succeeded"]
    per_task = statistics.fmean(cpu[n] for n in names) if all(n in cpu for n in names) else None
    return dict(zip(METRICS, [statistics.fmean(delays), p95(delays), makespan(succeeded, meta["pods"]), per_task,
                              cpu_seconds(folder), mean_imbalance(folder, meta["node_cores"])]))


def interval(values):
    """Mean and half-width of its 95% interval (None for a single value)."""
    if len(values) < 2:
        return statistics.fmean(values), None
    half = T95[min(len(values) - 1, len(T95)) - 1] * statistics.stdev(values) / len(values) ** 0.5
    return statistics.fmean(values), half


def cell(values, percent=False):
    values = [v for v in values if v is not None]  # a makespan is None when a pod failed
    if not values:
        return "–"
    mean, half = interval(values)
    if percent:
        return f"{mean:+.1f} %" + ("" if half is None else f" ± {half:.1f}")
    digits = 1 if abs(mean) < 100 else 0  # a task's CPU-seconds or an imbalance differ by tenths
    return f"{mean:.{digits}f}" + ("" if half is None else f" ± {half:.{digits}f}")


def change(value, reference):
    return None if value is None or not reference else 100 * (value / reference - 1)


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def name(strategy, replicate):
    return f"`{strategy}`" if replicate is None else f"`{strategy}` r{replicate}"


def learning(experiment, training):
    """Mean delay of each training run, by seed in the order they ran: whether a bonus makes it fall faster."""
    curves = {(strategy, replicate): seeds for (e, strategy), replicates in sorted(training.items())
              if e == experiment for replicate, seeds in sorted(replicates.items(), key=lambda kv: kv[0] or 0)}
    if not curves:
        return []
    order = sorted({seed for seeds in curves.values() for seed in seeds})
    lines = ["Training runs, by seed in the order they ran: mean delay (s), not a result.", "",
             "| Policy | " + " | ".join(str(seed) for seed in order) + " |",
             "|---|" + "---:|" * len(order)]
    for (strategy, replicate), seeds in curves.items():
        delays = [f"{seeds[s]['mean delay (s)']:.0f}" if s in seeds else "–" for s in order]
        lines.append(f"| {name(strategy, replicate)} | " + " | ".join(delays) + " |")
    return lines + [""]


def validating(experiment, validation):
    """Mean delay of each frozen checkpoint over the validation seeds: whether training converges."""
    curves = {(strategy, replicate): checkpoints for (e, strategy), replicates in sorted(validation.items())
              if e == experiment for replicate, checkpoints in sorted(replicates.items(), key=lambda kv: kv[0] or 0)}
    if not curves:
        return []
    order = sorted({c for checkpoints in curves.values() for c in checkpoints})
    lines = ["Frozen after that many training runs, on the validation seeds: mean delay (s).", "",
             "| Policy | " + " | ".join(str(c) for c in order) + " |",
             "|---|" + "---:|" * len(order)]
    for (strategy, replicate), checkpoints in curves.items():
        delays = [f"{statistics.fmean(checkpoints[c]):.1f}" if c in checkpoints else "–" for c in order]
        lines.append(f"| {name(strategy, replicate)} | " + " | ".join(delays) + " |")
    return lines + [""]


def row(replicates, base):
    """Cells of one policy: per metric, then per compared metric its change against the reference."""
    if len(replicates) == 1:  # one training, or none: the interval is over the seeds
        (seeds,) = replicates.values()
        shared = [s for s in seeds if s in base]
        return (str(len(seeds)), [cell([seeds[s][m] for s in seeds]) for m in METRICS],
                [cell([change(seeds[s][m], base[s][m]) for s in shared], percent=True) for m in COMPARED])
    # Several trainings: one value each, its mean over the seeds; the interval is over the trainings.
    values = {m: [mean(seeds[s][m] for s in seeds) for seeds in replicates.values()] for m in METRICS}
    changes = {m: [mean(change(seeds[s][m], base[s][m]) for s in seeds if s in base) for seeds in replicates.values()]
               for m in COMPARED}
    counts = {len(seeds) for seeds in replicates.values()}
    runs = f"{len(replicates)} × {counts.pop()}" if len(counts) == 1 else f"{len(replicates)} trainings"
    return runs, [cell(values[m]) for m in METRICS], [cell(changes[m], percent=True) for m in COMPARED]


def tables(paths, reference):
    runs = defaultdict(lambda: defaultdict(dict))  # (experiment, strategy) -> replicate -> seed -> metrics
    training = defaultdict(lambda: defaultdict(dict))  # the same, for the training runs
    validation = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))  # ... -> replicate -> checkpoint -> delays
    skipped = []
    for meta_path in sorted(p for path in paths for p in path.rglob("meta.json")):
        folder, meta = meta_path.parent, json.loads(meta_path.read_text())
        if meta["status"] != "complete":
            skipped.append(f"{folder.name} ({meta['status']})")
            continue
        values = metrics(folder, meta)
        if values is None:
            skipped.append(f"{folder.name} (no pod succeeded)")
            continue
        if meta.get("phase") == "valid":  # checkpoints along the training, not results either
            validation[(meta["experiment"], meta["strategy"])][meta.get("replicate")][meta.get("checkpoint")].append(
                values["mean delay (s)"])
            continue
        phase_runs = training if meta.get("phase") == "train" else runs  # training runs are not results
        phase_runs[(meta["experiment"], meta["strategy"])][meta.get("replicate")][meta.get("seed")] = values

    lines = []
    for experiment in sorted({e for e, _ in runs} | {e for e, _ in training} | {e for e, _ in validation}):
        # The reference by seed, averaged over its trainings if it has several.
        reference_runs = runs.get((experiment, reference), {})
        seeds = {s for by_seed in reference_runs.values() for s in by_seed}
        base = {s: {m: mean(by_seed[s][m] for by_seed in reference_runs.values() if s in by_seed) for m in METRICS}
                for s in seeds}
        lines += [f"## `{experiment}`", "",
                  "Mean over seeds ± 95% interval (over trainings for `r × seeds`); change against "
                  f"`{reference}` on the same seeds, negative is better.", "",
                  "| Policy | runs | " + " | ".join(METRICS) + " | "
                  + " | ".join(f"Δ {m.split(' (')[0]} %" for m in COMPARED) + " |",
                  "|---|---:|" + "---:|" * (len(METRICS) + len(COMPARED))]
        for (e, strategy), replicates in sorted(runs.items()):
            if e != experiment:
                continue
            count, cells, changes = row(replicates, base)
            if strategy == reference:
                changes = ["ref."] * len(COMPARED)
            lines.append(f"| `{strategy}` | {count} | " + " | ".join(cells + changes) + " |")
        lines.append("")
        lines += learning(experiment, training)
        lines += validating(experiment, validation)
    if skipped:
        lines += ["Left out: " + ", ".join(skipped) + ".", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(prog="ktable")
    parser.add_argument("runs", nargs="+", type=Path, help="run folders, or folders containing them")
    parser.add_argument("--reference", default="least-allocated")
    args = parser.parse_args()
    print(tables(args.runs, args.reference))


if __name__ == "__main__":
    main()
