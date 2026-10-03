#!/usr/bin/env python3
import argparse
import copy
import csv
import json
import os
import random
import shlex
import statistics
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
KUBECTL = os.environ.get("KEXP_KUBECTL", "docker exec -i kaptain-k3s kubectl --server=https://10.50.0.1:6443")
ARRIVAL = "kaptain.io/arrival-s"
TASK_ID = "kaptain.io/task-id"
SAMPLE_EVERY_S = 5


# Arrival processes: the arrival time, in seconds, of every pod.

def fixed(a, count, rng):
    return [i * a["interval_s"] for i in range(count)]


def normal(a, count, rng):
    gaps = [max(0, rng.gauss(a["mean_s"], a["stddev_s"])) for _ in range(count - 1)]
    return [sum(gaps[:i]) for i in range(count)]


def poisson(a, count, rng):
    gaps = [rng.expovariate(1 / a["mean_s"]) for _ in range(count - 1)]
    return [sum(gaps[:i]) for i in range(count)]


def bursts(a, count, rng):
    steady = [i * a["steady_interval_s"] for i in range(a["steady_count"])]
    burst_times = [steady[-1] + b * a["burst_gap_s"] for b in range(1, a["bursts"] + 1)]
    return steady + [t for t in burst_times for _ in range(a["burst_size"])]


ARRIVALS = {"fixed": fixed, "normal": normal, "poisson": poisson, "bursts": bursts}


# Experiment -> pods.

def kubectl(*args, stdin=None):
    command = shlex.split(KUBECTL) + list(args)
    result = subprocess.run(command, input=stdin, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {result.stderr.strip()}")
    return result.stdout


def load(name):
    return tomllib.loads((HERE / "experiments" / f"{name}.toml").read_text())


def arrival_times(experiment, rng):
    arrival = experiment["arrival"]
    return ARRIVALS[arrival["process"]](arrival, experiment.get("count"), rng)


def read_manifest(kind):
    # kubectl parses the YAML, so no YAML library is needed.
    yaml = (HERE / "pods" / f"{kind}.yaml").read_text()
    return json.loads(kubectl("create", "--dry-run=client", "-o", "json", "-f", "-", stdin=yaml))


def manifests(name, namespace="default"):
    experiment = load(name)
    rng = random.Random(experiment["seed"])
    times = arrival_times(experiment, rng)

    pattern = [kind for kind, weight in experiment["mix"].items() for _ in range(weight)]
    kinds = (pattern * len(times))[:len(times)]
    rng.shuffle(kinds)

    templates = {kind: read_manifest(kind) for kind in experiment["mix"]}
    cpu = experiment["resources"]["cpu_millis"]
    memory = experiment["resources"].get("memory_mib")

    pods = []
    for index, (arrival, kind) in enumerate(zip(times, kinds)):
        pod = copy.deepcopy(templates[kind])
        pod["metadata"]["name"] = f"{kind}-{index:03d}"
        pod["metadata"]["namespace"] = namespace
        pod["metadata"]["annotations"] = {
            ARRIVAL: f"{arrival:.3f}",
            # Seeded policies key on this; without it they use namespace/name, which changes every run.
            TASK_ID: f"{name}/{kind}-{index:03d}",
        }

        size = {"cpu": f"{rng.randint(*cpu)}m"}
        if memory:
            size["memory"] = f"{rng.randint(*memory)}Mi"
        for limit in pod["spec"]["containers"][0]["resources"].values():
            limit.update(size)  # requests = limits, as in DRS

        pods.append(pod)
    return experiment, pods


# Run.

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cores(quantity):
    if quantity.endswith("m"):
        return int(quantity[:-1]) / 1000
    return float(quantity)


def worker_nodes():
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    return {n["metadata"]["name"]: cores(n["status"]["allocatable"]["cpu"])
            for n in nodes if not n["spec"].get("taints")}


def sample(nodes, telemetry):
    for node in nodes:
        try:
            raw = kubectl("get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary")
            stats = json.loads(raw)["node"]
        except RuntimeError:
            continue  # a missed sample is left out, never written as zero

        # The kubelet refreshes every 10-15 s: its timestamp tells how old a value is.
        cpu = stats["cpu"].get("usageNanoCores")
        if cpu is not None:
            telemetry.writerow([now(), node, cpu / 1e9, stats["cpu"]["time"]])


def sample_until(stop, nodes, telemetry):
    # Its own thread: a slow kubelet must not delay a pod's arrival.
    while not stop.wait(SAMPLE_EVERY_S):
        sample(nodes, telemetry)


def all_finished(namespace, expected):
    phases = kubectl("-n", namespace, "get", "pods", "-o", "jsonpath={.items[*].status.phase}").split()
    return phases.count("Succeeded") + phases.count("Failed") == expected


def submit(pods):
    """Create each pod at its arrival time; return when each create request was sent and returned."""
    arrival = lambda pod: float(pod["metadata"]["annotations"][ARRIVAL])
    waiting = sorted(pods, key=arrival)
    start = time.monotonic()
    submitted = {}

    while waiting:
        elapsed = time.monotonic() - start
        due = [pod for pod in waiting if arrival(pod) <= elapsed]
        if due:
            sent = time.time()
            # Pods due together go in one request, so a burst stays a burst.
            kubectl("create", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": due}))
            returned = time.time()
            for pod in due:
                submitted[pod["metadata"]["name"]] = {"sent": sent, "returned": returned}
            waiting = waiting[len(due):]
        time.sleep(0.1)
    return submitted


def wait(namespace, expected, deadline):
    while time.monotonic() < deadline:
        if all_finished(namespace, expected):
            return True
        time.sleep(SAMPLE_EVERY_S)
    return False


def run_pods(out, namespace, pods, nodes, timeout_s):
    deadline = time.monotonic() + timeout_s
    stop = threading.Event()
    with (out / "telemetry.csv").open("w", newline="") as handle:
        telemetry = csv.writer(handle)
        telemetry.writerow(["time", "node", "cpu_cores", "kubelet_time"])
        sampler = threading.Thread(target=sample_until, args=(stop, nodes, telemetry))
        sampler.start()
        try:
            submitted = submit(pods)
            (out / "submissions.json").write_text(json.dumps(submitted, indent=1))
            return wait(namespace, len(pods), deadline)
        finally:
            stop.set()
            sampler.join()  # before the file closes


def scheduler_env():
    deployment = json.loads(kubectl("-n", "kube-system", "get", "deploy", "kaptain-scheduler", "-o", "json"))
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", "") for e in container["env"]}


def collect(out, namespace, started_at):
    (out / "pods.json").write_text(kubectl("-n", namespace, "get", "pods", "-o", "json"))

    logs = kubectl("-n", "kube-system", "logs", "deploy/kaptain-scheduler", f"--since-time={started_at}")
    ours = [line + "\n" for line in logs.splitlines() if f'"namespace":"{namespace}"' in line]
    (out / "decisions.jsonl").write_text("".join(ours))


def run(name, out_root):
    run_id = os.environ.get("KEXP_RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    namespace = f"kexp-{run_id}"
    experiment, pods = manifests(name, namespace)
    nodes = worker_nodes()
    env = scheduler_env()

    out = out_root / f"{name}-{run_id}"
    out.mkdir(parents=True)
    meta = {
        "experiment": name,
        "run_id": run_id,
        "source": experiment["source"],
        "pods": len(pods),
        "strategy": env["KAPTAIN_STRATEGY"],
        "policy_version": env["POLICY_VERSION"],
        "node_cores": nodes,
        "started_at": now(),
        "status": "aborted",
    }

    kubectl("create", "namespace", namespace)
    try:
        complete = run_pods(out, namespace, pods, nodes, experiment["timeout_s"])
        meta["status"] = "complete" if complete else "timeout"
    finally:
        try:
            (out / "meta.json").write_text(json.dumps(meta, indent=2))
            collect(out, namespace, meta["started_at"])
        finally:
            # Last, and whatever failed before: a leftover namespace would load the next run.
            kubectl("delete", "namespace", namespace, "--wait=false")
    return out, meta


# Report.

def seconds(stamp):
    return datetime.fromisoformat(stamp).timestamp()


def p95(values):
    return sorted(values)[round(0.95 * (len(values) - 1))]


def pod_times(pod):
    created = seconds(pod["metadata"]["creationTimestamp"])
    finished = seconds(pod["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"])
    return {
        "workload": pod["metadata"]["labels"]["kaptain.io/workload"],
        "node": pod["spec"]["nodeName"],
        "created": created,
        "finished": finished,
        "completion": finished - created,
    }


def placement_waits(out, log_lines):
    """Create request sent (runner) -> pod bound (plugin), in ms. Both run on the master: one clock."""
    path = out / "submissions.json"
    if not path.exists():
        return [], []
    submitted = json.loads(path.read_text())
    bindings = [line for line in log_lines if line["event"] == "binding"]
    waits = [1000 * (seconds(b["timestamp"]) - submitted[b["pod_name"]]["sent"]) for b in bindings]
    round_trips = [1000 * (s["returned"] - s["sent"]) for s in submitted.values()]
    return waits, round_trips


def table(title, rows, key):
    lines = [f"| {title} | pods | mean (s) | p95 (s) | max (s) |", "|---|---:|---:|---:|---:|"]
    for value in sorted({row[key] for row in rows}):
        times = [row["completion"] for row in rows if row[key] == value]
        lines.append(f"| {value} | {len(times)} | {statistics.fmean(times):.1f} | {p95(times):.0f} | {max(times):.0f} |")
    return lines


def utilization(out, node_cores):
    with (out / "telemetry.csv").open() as handle:
        samples = list(csv.DictReader(handle))

    lines, means = [], []
    for node, total in sorted(node_cores.items()):
        percent = [100 * float(s["cpu_cores"]) / total for s in samples if s["node"] == node]
        if not percent:
            lines.append(f"| {node} | missing | missing |")
            continue
        means.append(statistics.fmean(percent))
        lines.append(f"| {node} | {means[-1]:.1f} | {max(percent):.1f} |")

    header = ["| Node | mean CPU % | peak CPU % |", "|---|---:|---:|"]
    imbalance = statistics.pstdev(means) if len(means) > 1 else None
    return header + lines, imbalance


def report(out, meta):
    pods = json.loads((out / "pods.json").read_text())["items"]
    succeeded = [pod_times(pod) for pod in pods if pod["status"]["phase"] == "Succeeded"]
    failed = sum(1 for pod in pods if pod["status"]["phase"] == "Failed")
    unfinished = meta["pods"] - len(succeeded) - failed

    log_lines = [json.loads(line) for line in (out / "decisions.jsonl").read_text().splitlines()]
    decisions = [line for line in log_lines if line["event"] == "decision"]
    durations = [d["durations_ms"]["total"] for d in decisions] or [0]
    fallbacks = sum(1 for d in decisions if d["fallback"])

    makespan = "incomplete"
    if succeeded and not failed and not unfinished:
        first = min(row["created"] for row in succeeded)
        last = max(row["finished"] for row in succeeded)
        makespan = f"{last - first:.0f} s"
    waits, round_trips = placement_waits(out, log_lines)
    waits, round_trips = waits or [0], round_trips or [0]
    usage, imbalance = utilization(out, meta["node_cores"])

    return "\n".join([
        f"# `{meta['experiment']}` — run `{meta['run_id']}`",
        "",
        f"Status **{meta['status']}**, policy `{meta['strategy']}` (`{meta['policy_version'][:12]}`). "
        f"Source: {meta['source']}.",
        "",
        f"- Pods: {len(succeeded)} succeeded, {failed} failed, {unfinished} unfinished, of {meta['pods']}.",
        f"- Makespan, first pod created → last pod finished: **{makespan}**.",
        f"- Placement wait, create request sent → pod bound: median {statistics.median(waits):.0f} ms, "
        f"p95 {p95(waits):.0f} ms, max {max(waits):.0f} ms "
        f"(includes the create round trip, median {statistics.median(round_trips):.0f} ms).",
        f"- Decisions: {len(decisions)}, fallbacks: {fallbacks}, plugin time "
        f"median {statistics.median(durations):.2f} ms, p95 {p95(durations):.2f} ms.",
        "",
        "Completion time: pod created (API server) → container finished (kubelet), 1 s resolution.",
        "",
        *table("Workload", succeeded, "workload"),
        "",
        *table("Node", succeeded, "node"),
        "",
        "CPU utilization: kubelet usage / allocatable, sampled every few seconds.",
        "",
        *usage,
        "",
        f"Load imbalance (std. dev. of node mean CPU %): "
        f"**{'missing' if imbalance is None else f'{imbalance:.1f} points'}**.",
        "",
    ])


# Command line.

def list_experiments():
    for path in sorted((HERE / "experiments").glob("*.toml")):
        experiment = load(path.stem)
        count = len(arrival_times(experiment, random.Random(experiment["seed"])))
        print(f"{path.stem:<16} {count:>3} pods  {experiment['description']}")


def print_manifests(name):
    _, pods = manifests(name)
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": pods}, indent=1))


def run_and_report(name, out_root):
    out, meta = run(name, out_root)
    text = report(out, meta)
    (out / "report.md").write_text(text)
    print(text)

    if "GITHUB_STEP_SUMMARY" in os.environ:
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(text)
    return meta["status"] == "complete"


def main():
    parser = argparse.ArgumentParser(prog="kexp")
    parser.add_argument("command", choices=["list", "manifests", "run"])
    parser.add_argument("experiment", nargs="?")
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()

    if args.command == "list":
        return list_experiments()
    if args.command == "manifests":
        return print_manifests(args.experiment)
    if not run_and_report(args.experiment, args.out):
        sys.exit(1)


if __name__ == "__main__":
    main()
