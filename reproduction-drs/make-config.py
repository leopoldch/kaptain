#!/usr/bin/env python3
"""DRS configuration from the calibration runs (calibrate.sh): caps and pod vectors.

The paper sets each resource's maximum so that one microservice alone weighs "about 25 %". So,
for each metric, cap = 4 x the mean of the app that dominates it, over all its samples on all
workers while its pods ran alone. Memory stays in raw %, as in the authors' code. A pod vector
is the app's mean in % of the caps. The other settings come from the template.

In variant A the disk read is left at the template's cap: the 256 MiB drsdisk copy reads from the
page cache, so its read is noise. With the 8.2 GB task (calib-drsdisk-8g, --disk-8g) the file no
longer fits in the cache and the copy reads the disk, so its read is calibrated too.

Only the last run of each calibration experiment on each node counts, and only the pods that
succeeded.

Usage: python3 make-config.py --variant A [--disk-8g] [--template T] [-o OUT] RESULTS...
Standard library only.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

METRICS = ("cpu", "mem", "recv", "tran", "read", "write")
FACTOR = 4.0
APPS = {"A": ("video", "net", "drsdisk"), "B": ("cpu", "memory", "disk")}
DOMINANT = {
    "A": {"cpu": "video", "recv": "net", "tran": "net", "write": "drsdisk"},
    "B": {"cpu": "cpu", "read": "disk", "write": "disk"},  # no network app in variant B
}
# A sample at t covers ]t - 0.5 s, t], and the kubelet truncates startedAt and finishedAt to the
# second: a pod ran within [started, finished + 1[. Without this margin, a one-second pod loses
# its write peak, or every sample.
END_MARGIN_S = 1.5


def seconds(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def samples(path):
    """The monitor's sample lines, with or without the `kubectl logs --prefix` prefix."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        line = line[line.find("{"):] if "{" in line else ""
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "sample":
            rows.append(row)
    return rows


def profile(run, meta):
    """Per pod of a calibration run: its app, node and the samples of its node while it ran."""
    node = meta["node"]
    on_node = [row for row in samples(run / "monitor.jsonl") if row.get("node") == node]
    pods = json.loads((run / "pods.json").read_text())["items"] if (run / "pods.json").exists() else []
    rows = []
    for pod in pods:
        if pod.get("status", {}).get("phase") != "Succeeded":
            print(f"left out: {run.name}/{pod['metadata']['name']}, {pod.get('status', {}).get('phase')}",
                  file=sys.stderr)
            continue
        state = ((pod.get("status", {}).get("containerStatuses") or [{}])[0]).get("state", {})
        done = state.get("terminated", {})
        if not done.get("startedAt") or not done.get("finishedAt"):
            continue
        started, finished = seconds(done["startedAt"]), seconds(done["finishedAt"])
        inside = [row for row in on_node if started < row["t"] < finished + END_MARGIN_S]
        if inside:
            rows.append({"app": pod["metadata"]["labels"]["kaptain.io/workload"], "node": node,
                         "pod": pod["metadata"]["name"], "seconds": finished - started, "samples": inside})
    return rows


def mean(rows, metric):
    values = [sample[metric] for row in rows for sample in row["samples"]]
    return sum(values) / len(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("results", nargs="+", type=Path, help="directories holding calibration runs")
    parser.add_argument("--variant", required=True, choices=("A", "B"))
    parser.add_argument("--disk-8g", action="store_true", help="variant A: use calib-drsdisk-8g, not calib-drsdisk")
    parser.add_argument("--template", type=Path,
                        default=Path(__file__).resolve().parent.parent / "k8s" / "drs-config.template.json")
    parser.add_argument("-o", "--output", type=Path, help="default: drs-config-<variant>.json")
    args = parser.parse_args()

    disk = "drsdisk-8g" if args.disk_8g else "drsdisk"
    wanted = {f"calib-{app}" for app in APPS[args.variant]}
    if args.variant == "A":
        wanted = wanted - {"calib-drsdisk"} | {f"calib-{disk}"}
    # The last run of each experiment on each node: an earlier one was failed or redone.
    latest = {}
    for path in sorted({meta.parent for root in args.results for meta in root.rglob("meta.json")}):
        meta = json.loads((path / "meta.json").read_text())
        key = (meta["experiment"], meta.get("node"))
        if key[0] in wanted and key[1] and (key not in latest or meta["started_at"] > latest[key][1]["started_at"]):
            latest[key] = (path, meta)
    runs = sorted(latest.values(), key=lambda item: item[1]["started_at"])
    pods = [row for run, meta in runs for row in profile(run, meta)]
    missing = [app for app in APPS[args.variant] if not any(row["app"] == app for row in pods)]
    if missing:
        sys.exit(f"no successful calibration pod for {' '.join(missing)}: run calibrate.sh {args.variant} first")

    print(f"{len(pods)} pods from {len(runs)} runs. Raw means while each pod ran alone:")
    print(f"{'app':<9} {'node':<24} {'pods':>4} {'s':>7} " + " ".join(f"{m:>10}" for m in METRICS))
    for app, node in sorted({(row["app"], row["node"]) for row in pods}):
        part = [row for row in pods if row["app"] == app and row["node"] == node]
        duration = sum(row["seconds"] for row in part) / len(part)
        print(f"{app:<9} {node:<24} {len(part):>4} {duration:>7.1f} "
              + " ".join(f"{mean(part, m):>10.2f}" for m in METRICS))

    # Weighted by time: every sample counts the same, so a one-second pod weighs less than a long one.
    apps = sorted({row["app"] for row in pods})
    means = {app: {m: mean([row for row in pods if row["app"] == app], m) for m in METRICS} for app in apps}
    dominant = dict(DOMINANT[args.variant])
    if args.disk_8g and args.variant == "A":
        dominant["read"] = "drsdisk"

    config = json.loads(args.template.read_text())
    config.pop("_comment", None)
    config["variant"] = args.variant
    caps = dict(config["caps"])
    for metric, app in dominant.items():
        if app in means and means[app][metric] > 0:
            caps[metric] = round(FACTOR * means[app][metric], 3)
    caps["mem"] = 100.0
    config["caps"] = caps
    config["pod_vectors"] = {app: [round(min(100.0 * means[app][m] / caps[m], 100.0), 2) for m in METRICS]
                             for app in apps}
    config["_calibration"] = {"factor": FACTOR, "dominant": dominant, "pods": len(pods),
                              "raw_means": {app: {m: round(v, 3) for m, v in means[app].items()} for app in apps}}

    output = args.output or Path(f"drs-config-{args.variant}.json")
    output.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\ncaps: {json.dumps(caps)}")
    print(f"pod_vectors: {json.dumps(config['pod_vectors'])}")
    print(f"written: {output}. Next: bash setup/apply-drs-config.sh {output}")


if __name__ == "__main__":
    main()
