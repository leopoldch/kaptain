#!/usr/bin/env python3
"""One table per experiment: each policy's mean over its seeds, with a 95% interval, and its
change against a reference policy, paired by seed. Then the mean delay of each training run, in
order, to see how fast each policy learns. Reads run folders as kexp.py writes them.

    python3 ktable.py runs/ --reference least-allocated"""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

from kexp import makespan, p95, pod_times, seconds

# Student t, two-sided 95%, by degrees of freedom; 1.96 beyond.
T95 = [12.71, 4.30, 3.18, 2.78, 2.57, 2.45, 2.36, 2.31, 2.26, 2.23, 2.20, 2.18, 2.16, 2.14, 2.13,
       2.12, 2.11, 2.10, 2.09, 2.09, 2.08, 2.07, 2.07, 2.06, 2.06, 2.06, 2.05, 2.05, 2.05, 2.04]
METRICS = ["mean delay (s)", "p95 delay (s)", "makespan (s)", "node CPU-s"]


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
    return dict(zip(METRICS, [statistics.fmean(delays), p95(delays), makespan(succeeded, meta["pods"]),
                              cpu_seconds(folder)]))


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
    return f"{mean:.0f}" + ("" if half is None else f" ± {half:.0f}")


def change(value, reference):
    return None if value is None or not reference else 100 * (value / reference - 1)


def learning(experiment, training):
    """Mean delay of each training run, by seed in the order they ran: whether a bonus makes it fall faster."""
    curves = {strategy: seeds for (e, strategy), seeds in sorted(training.items()) if e == experiment}
    if not curves:
        return []
    order = sorted({seed for seeds in curves.values() for seed in seeds})
    lines = ["Training runs, by seed in the order they ran: mean delay (s), not a result.", "",
             "| Policy | " + " | ".join(str(seed) for seed in order) + " |",
             "|---|" + "---:|" * len(order)]
    for strategy, seeds in curves.items():
        delays = [f"{seeds[s]['mean delay (s)']:.0f}" if s in seeds else "–" for s in order]
        lines.append(f"| `{strategy}` | " + " | ".join(delays) + " |")
    return lines + [""]


def tables(paths, reference):
    runs = defaultdict(dict)  # (experiment, strategy) -> seed -> metrics
    training = defaultdict(dict)  # the same, for the training runs
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
        phase_runs = training if meta.get("phase") == "train" else runs  # training runs are not results
        phase_runs[(meta["experiment"], meta["strategy"])][meta.get("seed")] = values

    lines = []
    for experiment in sorted({e for e, _ in runs} | {e for e, _ in training}):
        base = runs.get((experiment, reference), {})
        lines += [f"## `{experiment}`", "",
                  "Mean over seeds ± 95% interval; change against "
                  f"`{reference}` on the same seeds, negative is better.", "",
                  "| Policy | runs | " + " | ".join(METRICS) + " | " + " | ".join(f"Δ {m.split(' (')[0]} %" for m in METRICS[:3]) + " |",
                  "|---|---:|" + "---:|" * (2 * len(METRICS) - 1)]
        for (e, strategy), seeds in sorted(runs.items()):
            if e != experiment:
                continue
            shared = [seed for seed in seeds if seed in base]
            row = [cell([seeds[s][m] for s in seeds]) for m in METRICS]
            row += ["ref." if strategy == reference else
                    cell([change(seeds[s][m], base[s][m]) for s in shared], percent=True) for m in METRICS[:3]]
            lines.append(f"| `{strategy}` | {len(seeds)} | " + " | ".join(row) + " |")
        lines.append("")
        lines += learning(experiment, training)
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
