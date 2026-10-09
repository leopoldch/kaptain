#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["matplotlib>=3.10"]
# ///
"""Plot one or more runs of kexp.py: makespan, pod completion, placement, latency, CPU per node.

Reads the run folders as kexp.py writes them, locally: the runner itself needs no matplotlib."""
import argparse
import bisect
import csv
import json
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from kexp import placement_waits, pod_times, seconds

# Fixed slots, so a strategy keeps its colour whatever runs are plotted together.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
STRATEGIES = ["dummy-random", "largest-cpu-capacity", "least-allocated", "least-used"]
LINESTYLES = ["-", "--", ":", "-."]  # repeated runs of one strategy share its colour
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
IMBALANCE_STEP_S = 5


def style():
    plt.rcParams.update({
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": MUTED, "axes.labelcolor": INK, "axes.titlesize": 11,
        "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
        "axes.grid": True, "axes.axisbelow": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "legend.frameon": False, "font.size": 9,
    })


def colour(name, known):
    if name not in known:
        known.append(name)
    return PALETTE[known.index(name) % len(PALETTE)]


# Reading a run.

def find_runs(paths):
    """Every run folder under the paths: a downloaded artifact nests it one level down."""
    folders = sorted({meta.parent for path in paths for meta in Path(path).rglob("meta.json")})
    if not folders:
        sys.exit(f"no run folder (with a meta.json) under {' '.join(map(str, paths))}")
    return [read_run(folder) for folder in folders]


def read_run(folder):
    meta = json.loads((folder / "meta.json").read_text())
    pods_path = folder / "pods.json"
    pods = json.loads(pods_path.read_text())["items"] if pods_path.exists() else []
    succeeded = [pod_times(pod) for pod in pods if pod["status"]["phase"] == "Succeeded"]
    waits, _ = placement_waits(folder)

    # As in the report: only when every pod succeeded.
    makespan = None
    if succeeded and len(succeeded) == meta["pods"]:
        makespan = max(p["finished"] for p in succeeded) - min(p["created"] for p in succeeded)

    decisions = []
    decisions_path = folder / "decisions.jsonl"
    if decisions_path.exists():
        lines = [json.loads(line) for line in decisions_path.read_text().splitlines()]
        decisions = [line for line in lines if line["event"] == "decision"]

    # Time zero is the first pod created; before any pod exists, the run's start.
    start = min((seconds(p["metadata"]["creationTimestamp"]) for p in pods), default=seconds(meta["started_at"]))
    return {"meta": meta, "makespan": makespan, "pods": succeeded, "waits": waits, "start": start,
            "plugin_ms": [d["durations_ms"]["total"] for d in decisions],
            "fallbacks": sum(1 for d in decisions if d["fallback"]),
            "cpu": usage_series(folder, meta["node_cores"], start, "cpu_cores", "kubelet_time"),
            # Runs before memory was sampled have neither the column nor the allocatable memory.
            "memory": usage_series(folder, meta.get("node_memory_bytes", {}), start, "memory_bytes", "memory_time")}


def usage_series(folder, allocatable, start, column, time_column):
    """% of allocatable per node, at the kubelet's own time, each reading once.

    The sampler asks every 5 s but the kubelet refreshes every 10-15 s: between two
    refreshes it returns the same reading, which would draw as a flat step."""
    series = {node: {} for node in allocatable}
    path = folder / "telemetry.csv"
    if path.exists():
        with path.open() as handle:
            for row in csv.DictReader(handle):
                if row["node"] in series and row.get(column):
                    at = seconds(row[time_column]) - start
                    series[row["node"]][at] = 100 * float(row[column]) / allocatable[row["node"]]
    return {node: sorted(points.items()) for node, points in series.items()}


def imbalance_series(cpu, left_out):
    """Std. dev. of CPU % across nodes at each instant, as DRS's Imbalance_t but CPU only.

    Each node holds its last reading until the next. Only where every node has one: from
    the latest first reading to the earliest last one."""
    series = {node: points for node, points in cpu.items() if points and node not in left_out}
    if len(series) < 2:
        return []
    times = {node: [t for t, _ in points] for node, points in series.items()}
    first = max(t[0] for t in times.values())
    last = min(t[-1] for t in times.values())
    imbalance = []
    t = first
    while t <= last:
        values = [series[node][bisect.bisect_right(times[node], t) - 1][1] for node in series]
        imbalance.append((t, statistics.pstdev(values)))
        t += IMBALANCE_STEP_S
    return imbalance


def label(runs):
    """The strategy alone when it tells the runs apart; otherwise the run id too."""
    strategies = [run["meta"]["strategy"] for run in runs]
    experiments = {run["meta"]["experiment"] for run in runs}
    for run in runs:
        meta = run["meta"]
        text = meta["strategy"]
        if strategies.count(meta["strategy"]) > 1:
            text += f" · {meta['run_id']}"
        if len(experiments) > 1:
            text = f"{meta['experiment']} · {text}"
        run["label"] = text


# Figures.

def plot_makespan(runs, colours, out):
    fig, ax = plt.subplots(figsize=(7, 0.5 * len(runs) + 1.2))
    rows = list(reversed(runs))  # first run on top
    for y, run in enumerate(rows):
        if run["makespan"] is None:
            ax.text(0, y, f"  not every pod succeeded ({run['meta']['status']})", va="center", color=MUTED)
            continue
        ax.barh(y, run["makespan"], height=0.6, color=colours[run["meta"]["strategy"]])
        ax.text(run["makespan"], y, f"  {run['makespan']:.0f} s", va="center", color=INK)
    ax.set_yticks(range(len(rows)), [run["label"] for run in rows])
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_xlabel("makespan (s): first pod created → last pod finished")
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.15)
    save(fig, out / "makespan.png")


def jitter(y, count):
    return [y + 0.3 * ((i * 0.618) % 1 - 0.5) for i in range(count)]


def plot_distribution(runs, colours, key, counted, xlabel, path):
    """One row per run: every value as a dot, the box only to read the median and quartiles."""
    fig, ax = plt.subplots(figsize=(7, 0.5 * len(runs) + 1.4))
    rows = list(reversed(runs))
    for y, run in enumerate(rows):
        values = run[key]
        if not values:
            ax.text(0.01, y, "  missing", va="center", color=MUTED, transform=ax.get_yaxis_transform())
            continue
        ax.boxplot(values, positions=[y], orientation="horizontal", widths=0.5, showfliers=False,
                   medianprops={"color": INK, "linewidth": 1.5}, boxprops={"color": MUTED},
                   whiskerprops={"color": MUTED}, capprops={"color": MUTED})
        ax.scatter(values, jitter(y, len(values)), s=10, color=colours[run["meta"]["strategy"]], alpha=0.7,
                   linewidths=0, zorder=3)
    ax.set_yticks(range(len(rows)), [f"{run['label']}\n{counted(run)}" for run in rows])
    present = [run[key] for run in runs if run[key]]
    if present and max(map(max, present)) > 20 * min(map(min, present)):
        ax.set_xscale("log")
    ax.set_xlabel(xlabel)
    ax.grid(axis="y", visible=False)
    save(fig, path)


def plot_completion(runs, node_colours, out):
    """Every pod, by workload: how long it took, and on which node."""
    workloads = sorted({pod["workload"] for run in runs for pod in run["pods"]})
    if not workloads:
        return
    fig, axes = plt.subplots(1, len(workloads), figsize=(3.2 * len(workloads) + 1.5, 0.55 * len(runs) + 1.6),
                             sharey=True, squeeze=False)
    rows = list(reversed(runs))
    for ax, workload in zip(axes[0], workloads):
        for y, run in enumerate(rows):
            pods = [pod for pod in run["pods"] if pod["workload"] == workload]
            ax.scatter([pod["completion"] for pod in pods], jitter(y, len(pods)), s=14, linewidths=0, alpha=0.8,
                       color=[node_colours[pod["node"]] for pod in pods])
        ax.set_title(workload, loc="left")
        ax.set_xlim(left=0)
        ax.grid(axis="y", visible=False)
    axes[0, 0].set_yticks(range(len(rows)), [run["label"] for run in rows])
    axes[0, 0].set_ylim(-0.6, len(rows) - 0.4)
    fig.supxlabel("completion time (s): pod created → container finished, succeeded pods", fontsize=9)
    node_legend(fig, node_colours, marker="o")
    save(fig, out / "completion.png", legend=True)


def plot_pods_per_node(runs, node_colours, out):
    fig, ax = plt.subplots(figsize=(7, 0.5 * len(runs) + 1.6))
    rows = list(reversed(runs))
    for y, run in enumerate(rows):
        left = 0
        for node in node_colours:
            count = sum(1 for pod in run["pods"] if pod["node"] == node)
            if not count:
                continue
            ax.barh(y, count, left=left, height=0.6, color=node_colours[node], edgecolor="white", linewidth=1.5)
            if count >= 3:  # a narrower segment has no room for its number
                ax.text(left + count / 2, y, str(count), ha="center", va="center", color="white", fontsize=8)
            left += count
    ax.set_yticks(range(len(rows)), [f"{run['label']}\n{len(run['pods'])}/{run['meta']['pods']} succeeded"
                                     for run in rows])
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_xlabel("succeeded pods, by the node they ran on")
    ax.grid(axis="y", visible=False)
    node_legend(fig, node_colours, marker="s")
    save(fig, out / "pods-per-node.png", legend=True)


def plot_imbalance(runs, colours, left_out, out):
    fig, ax = plt.subplots(figsize=(9, 3.4))
    seen = {}
    for run in runs:
        strategy = run["meta"]["strategy"]
        series = imbalance_series(run["cpu"], left_out)
        if not series:
            continue
        mean = statistics.fmean(value for _, value in series)
        style_index = seen.setdefault(strategy, -1) + 1
        seen[strategy] = style_index
        ax.plot(*zip(*series), color=colours[strategy], linewidth=1.5,
                linestyle=LINESTYLES[style_index % len(LINESTYLES)], label=f"{run['label']} (mean {mean:.1f})")
    nodes = sorted({node for run in runs for node in run["cpu"]} - set(left_out))
    ax.set_title(f"nodes: {', '.join(nodes)}", loc="left", fontsize=8, color=MUTED)
    ax.set_xlabel("seconds since the first pod was created (kubelet time)")
    ax.set_ylabel("std. dev. of CPU % across nodes")
    ax.set_ylim(bottom=0)
    if seen:
        ax.legend(loc="upper left", bbox_to_anchor=(0, -0.2), ncol=2)  # below: the lines fill the plot
    else:
        ax.text(0.5, 0.5, "fewer than two nodes with CPU samples", transform=ax.transAxes, ha="center", color=MUTED)
    save(fig, out / "imbalance.png")


def plot_usage(runs, node_colours, key, what, path):
    if not any(points for run in runs for points in run[key].values()):
        print(f"{path}: skipped, no run has {what} samples")
        return
    fig, axes = plt.subplots(len(runs), 1, figsize=(9, 2.3 * len(runs) + 0.6), sharex=True, sharey=True,
                             squeeze=False)
    for ax, run in zip(axes[:, 0], runs):
        if not any(run[key].values()):
            ax.text(0.5, 0.5, f"no {what} sample", transform=ax.transAxes, ha="center", color=MUTED)
        for node, points in run[key].items():
            if points:
                ax.plot(*zip(*points), color=node_colours[node], linewidth=1.5)
        if run["makespan"] is not None:
            ax.axvline(run["makespan"], color=MUTED, linestyle="--", linewidth=1)
            ax.text(run["makespan"], 1, " last pod finished", transform=ax.get_xaxis_transform(),
                    va="top", color=MUTED)
        ax.set_title(run["label"], loc="left")
        ax.set_ylabel(f"{what} % of allocatable")
    axes[-1, 0].set_xlabel("seconds since the first pod was created (kubelet time)")
    axes[0, 0].set_ylim(bottom=0)
    node_legend(fig, node_colours)
    save(fig, path, legend=True)


def node_legend(fig, node_colours, marker=None):
    if marker:
        handles = [plt.Line2D([], [], color=c, marker=marker, linestyle="", markersize=6)
                   for c in node_colours.values()]
    else:
        handles = [plt.Line2D([], [], color=c, linewidth=2) for c in node_colours.values()]
    fig.legend(handles, list(node_colours), loc="upper center", ncol=(len(node_colours) + 1) // 2,
               bbox_to_anchor=(0.5, 1.0))


def save(fig, path, legend=False):
    # A figure-wide legend sits above the axes: keep two lines of it clear.
    top = 1 - 0.4 / fig.get_figheight() if legend else 1
    fig.tight_layout(rect=(0, 0, 1, top))
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(path)


def main():
    parser = argparse.ArgumentParser(prog="kplot", description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path, help="run folders, or folders containing them")
    parser.add_argument("--out", type=Path, default=Path("results/plots"))
    parser.add_argument("--imbalance-without", action="append", default=[], metavar="NODE",
                        help="leave a node out of the imbalance, e.g. alex-master and its control plane")
    args = parser.parse_args()

    runs = find_runs(args.runs)
    runs.sort(key=lambda run: (run["meta"]["experiment"], run["meta"]["strategy"], run["meta"]["run_id"]))
    label(runs)
    known = list(STRATEGIES)
    colours = {run["meta"]["strategy"]: colour(run["meta"]["strategy"], known) for run in runs}
    nodes = sorted({node for run in runs for node in run["meta"]["node_cores"]})
    node_colours = {node: PALETTE[i % len(PALETTE)] for i, node in enumerate(nodes)}

    style()
    args.out.mkdir(parents=True, exist_ok=True)
    plot_makespan(runs, colours, args.out)
    plot_completion(runs, node_colours, args.out)
    plot_pods_per_node(runs, node_colours, args.out)
    plot_distribution(runs, colours, "waits", lambda run: f"{len(run['waits'])}/{run['meta']['pods']} pods",
                      "placement wait (ms): pod seen → seen bound, API watch", args.out / "placement-wait.png")
    plot_distribution(runs, colours, "plugin_ms", lambda run: f"{len(run['plugin_ms'])} decisions, "
                      f"{run['fallbacks']} fallbacks", "plugin time per decision (ms): snapshot + decider call",
                      args.out / "plugin-time.png")
    plot_imbalance(runs, colours, args.imbalance_without, args.out)
    plot_usage(runs, node_colours, "cpu", "CPU", args.out / "cpu.png")
    plot_usage(runs, node_colours, "memory", "memory", args.out / "memory.png")


if __name__ == "__main__":
    main()
