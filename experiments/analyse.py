"""Turn a directory of runs into one row per run, and into paired differences.

    python analyse.py runs/ --out summary.csv

Three things this refuses to do, because each would overstate what the data supports.

**The mean is Δsum/Δcount** between the scrape before and the scrape after. That is the mean
of the observations recorded in that window — exact, but only if the scheduler did not
restart (the collector aborts the run if a counter went backwards) and if nothing else used
that scheduler during the window. Both conditions are recorded, not assumed.

**The quantiles are estimated from buckets**, like `histogram_quantile`. The native scheduler
histograms start at 1 ms, so a quantile that lands in the first bucket means "somewhere below
1 ms" and nothing finer. Those are reported as `<1ms` rather than as a number, together with
the share of observations that fell in that first bucket.

**Paired differences are computed at the level of runs**, not of pods. Ten pairs give ten
differences; the confidence interval is over those.
"""

import argparse
import csv
import json
import math
import statistics
from pathlib import Path

import collect

PRIMARY = "scheduler_scheduling_algorithm_duration_seconds"
SECONDARY = (
    "scheduler_scheduling_attempt_duration_seconds",
    "scheduler_pod_scheduling_sli_duration_seconds",
)


def summarise_run(directory: Path) -> dict | None:
    """One row per run, or None when the run aborted or is incomplete."""
    record_path = directory / "run.json"
    if not record_path.exists():
        return None
    record = json.loads(record_path.read_text())
    if record.get("aborted"):
        return {"run_id": record["run_id"], "arm": record["arm"], "aborted": record["aborted"]}

    before_path, after_path = directory / "scheduler_metrics_before.txt", directory / "scheduler_metrics_after.txt"
    if not (before_path.exists() and after_path.exists()):
        return {"run_id": record["run_id"], "arm": record["arm"],
                "aborted": "scheduler metrics were not collected"}

    before, after = before_path.read_text(), after_path.read_text()
    row = {
        "run_id": record["run_id"],
        "arm": record["arm"],
        "scenario": record.get("scenario", ""),
        "strategy": record.get("strategy", ""),
        "telemetry_collector": record.get("telemetry_collector", ""),
        "kubernetes_version": record.get("kubernetes_version", ""),
        "feasible_nodes": len(record.get("feasible_nodes", [])),
        "pods": len(record.get("submissions", [])),
        "max_submission_lag_s": max((s["lag_s"] for s in record.get("submissions", [])), default=0.0),
        "aborted": "",
    }

    for metric in (PRIMARY, *SECONDARY):
        try:
            observed = collect.delta(collect.parse_histogram(before, metric),
                                     collect.parse_histogram(after, metric))
        except collect.CollectionError as failure:
            row["aborted"] = str(failure)
            return row
        prefix = _short(metric)
        row[f"{prefix}_count"] = observed.count
        row[f"{prefix}_mean_ms"] = _ms(observed.mean_s)
        row[f"{prefix}_unresolved_share"] = _round(collect.share_below_first_bucket(observed))
        for q in (0.5, 0.95, 0.99):
            row[f"{prefix}_p{int(q * 100)}_ms"] = _quantile_ms(observed, q)

    decisions = _decisions(directory)
    row["decisions"] = len(decisions)
    row["fallbacks"] = sum(1 for entry in decisions if entry.get("fallback"))
    row["ties"] = sum(1 for entry in decisions if entry.get("tie_count", 1) > 1)
    row["our_decision_mean_ms"] = _round(statistics.fmean(
        [entry["duration_ms"] for entry in decisions if "duration_ms" in entry]) if decisions else None)
    row["requests_age_ms_max"] = max((entry.get("requests_age_ms", 0.0) for entry in decisions), default=0.0)
    row["telemetry_age_ms_max"] = max((entry.get("telemetry_age_ms", 0.0) for entry in decisions), default=0.0)

    placements = _placements(directory)
    row["placed"] = sum(1 for placement in placements if placement.get("node"))
    row["nodes_used"] = len({placement["node"] for placement in placements if placement.get("node")})
    return row


def _decisions(directory: Path) -> list[dict]:
    path = directory / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip() and json.loads(line).get("event") == "decision"]


def _placements(directory: Path) -> list[dict]:
    path = directory / "pods.csv"
    if not path.exists() or not path.read_text().strip():
        return []
    with path.open() as handle:
        return list(csv.DictReader(handle))


def _short(metric: str) -> str:
    return metric.replace("scheduler_", "").replace("_duration_seconds", "").replace("scheduling_", "")


def _ms(value: float | None) -> float | None:
    return None if value is None else round(value * 1000, 3)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _quantile_ms(histogram: collect.Histogram, q: float) -> str | float | None:
    """A quantile, or a statement that the buckets cannot resolve it."""
    value = collect.quantile(histogram, q)
    if value is None:
        return None
    floor = collect.first_bucket(histogram)
    if value <= floor:
        return f"<{_ms(floor)}ms"
    return _ms(value)


def pair(rows: list[dict]) -> list[dict]:
    """Match runs by scenario and order, one arm against the other."""
    usable = [row for row in rows if not row.get("aborted")]
    arms = {}
    for row in usable:
        arms.setdefault((row["scenario"], row["arm"]), []).append(row)

    pairs = []
    scenarios = {scenario for scenario, _ in arms}
    for scenario in sorted(scenarios):
        extender = arms.get((scenario, "extender"), [])
        plugin = arms.get((scenario, "plugin"), [])
        for index, (left, right) in enumerate(zip(extender, plugin)):
            if left["strategy"] != right["strategy"]:
                pairs.append({"scenario": scenario, "pair": index,
                              "aborted": f"strategies differ: {left['strategy']!r} vs {right['strategy']!r}"})
                continue
            prefix = _short(PRIMARY)
            pairs.append({
                "scenario": scenario,
                "pair": index,
                "strategy": left["strategy"],
                "extender_mean_ms": left[f"{prefix}_mean_ms"],
                "plugin_mean_ms": right[f"{prefix}_mean_ms"],
                "difference_ms": _round((left[f"{prefix}_mean_ms"] or 0) - (right[f"{prefix}_mean_ms"] or 0)),
                "extender_unresolved_share": left[f"{prefix}_unresolved_share"],
                "plugin_unresolved_share": right[f"{prefix}_unresolved_share"],
                "aborted": "",
            })
    return pairs


def interval(differences: list[float], confidence: float = 0.95) -> dict:
    """Mean paired difference with a normal interval, over runs.

    With ten pairs this is indicative, not a strong claim; report the individual differences
    beside it.
    """
    usable = [value for value in differences if value is not None]
    if len(usable) < 2:
        return {"pairs": len(usable), "mean_ms": usable[0] if usable else None,
                "low_ms": None, "high_ms": None}
    mean = statistics.fmean(usable)
    error = statistics.stdev(usable) / math.sqrt(len(usable))
    z = 1.96 if confidence == 0.95 else 2.576
    return {"pairs": len(usable), "mean_ms": round(mean, 3),
            "low_ms": round(mean - z * error, 3), "high_ms": round(mean + z * error, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, help="directory holding one subdirectory per run")
    parser.add_argument("--out", type=Path, default=Path("summary.csv"))
    args = parser.parse_args()

    rows = [row for row in (summarise_run(directory)
                            for directory in sorted(args.runs.iterdir()) if directory.is_dir())
            if row is not None]
    if not rows:
        print("no runs found")
        return 1

    fields = sorted({key for row in rows for key in row})
    with args.out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    aborted = [row for row in rows if row.get("aborted")]
    pairs = pair(rows)
    summary = interval([p.get("difference_ms") for p in pairs if not p.get("aborted")])

    print(f"{len(rows)} runs, {len(aborted)} aborted -> {args.out}")
    for row in aborted:
        print(f"  aborted {row['run_id']}: {row['aborted']}")
    print(f"{summary['pairs']} usable pairs")
    if summary["mean_ms"] is not None:
        print(f"  extender - plugin, {_short(PRIMARY)} mean: "
              f"{summary['mean_ms']} ms [{summary['low_ms']}, {summary['high_ms']}] (95%)")
    unresolved = [p["extender_unresolved_share"] for p in pairs if p.get("extender_unresolved_share")]
    if unresolved and max(unresolved) > 0.5:
        print("  note: over half the observations fall in the first 1 ms bucket; "
              "the mean is usable, the quantiles are not")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
