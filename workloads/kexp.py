#!/usr/bin/env python3
import argparse
import copy
import csv
import json
import os
import random
import re
import shlex
import signal
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
EXPERIMENT_LABEL = "kaptain.io/experiment"
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
    arrival = lambda pod: float(pod["metadata"]["annotations"][ARRIVAL])
    waiting = sorted(pods, key=arrival)
    start = time.monotonic()

    while waiting:
        elapsed = time.monotonic() - start
        due = [pod for pod in waiting if arrival(pod) <= elapsed]
        if due:
            # Pods due together go in one request, so a burst stays a burst.
            kubectl("create", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": due}))
            waiting = waiting[len(due):]
        time.sleep(0.1)


def wait(namespace, expected, deadline):
    while time.monotonic() < deadline:
        if all_finished(namespace, expected):
            return True
        time.sleep(SAMPLE_EVERY_S)
    return False


def watch_pods(process, path):
    """Time, on the runner's clock, every change the API server streams for our pods."""
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "event", "pod", "node"])
        for line in process.stdout:
            event = json.loads(line)
            pod = event["object"]
            writer.writerow([time.time(), event["type"], pod["metadata"]["name"], pod["spec"].get("nodeName", "")])


def run_pods(out, namespace, pods, nodes, timeout_s):
    deadline = time.monotonic() + timeout_s
    stop = threading.Event()

    # The server ends the watch after timeoutSeconds, so it cannot outlive the run inside the K3s container.
    watch_url = f"/api/v1/namespaces/{namespace}/pods?watch=true&timeoutSeconds={timeout_s}"
    watch = subprocess.Popen(shlex.split(KUBECTL) + ["get", "--raw", watch_url],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    watcher = threading.Thread(target=watch_pods, args=(watch, out / "watch.csv"))
    watcher.start()
    time.sleep(2)  # let the watch open before the first pod

    with (out / "telemetry.csv").open("w", newline="") as handle:
        telemetry = csv.writer(handle)
        telemetry.writerow(["time", "node", "cpu_cores", "kubelet_time"])
        sampler = threading.Thread(target=sample_until, args=(stop, nodes, telemetry))
        sampler.start()
        try:
            submit(pods)
            return wait(namespace, len(pods), deadline)
        finally:
            stop.set()
            sampler.join()  # before the file closes
            watch.terminate()
            watcher.join()


def scheduler_env():
    deployment = json.loads(kubectl("-n", "kube-system", "get", "deploy", "kaptain-scheduler", "-o", "json"))
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", "") for e in container["env"]}


def running_images():
    """The images actually running, by digest: the deployment spec alone can be stale."""
    selector = "app in (kaptain-scheduler,kaptain-decider)"
    pods = json.loads(kubectl("-n", "kube-system", "get", "pods", "-l", selector, "-o", "json"))["items"]
    images = {}
    for pod in pods:
        status = pod["status"]["containerStatuses"][0]
        images[pod["metadata"]["labels"]["app"]] = {"image": status["image"], "digest": status["imageID"]}
    return images


def create_namespace(namespace):
    namespace_object = {"apiVersion": "v1", "kind": "Namespace",
                        "metadata": {"name": namespace, "labels": {EXPERIMENT_LABEL: "true"}}}
    # Workloads need no network: deny everything. Not every node enforces it (see README).
    deny_all = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": "deny-all", "namespace": namespace},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}
    kubectl("create", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List",
                                                    "items": [namespace_object, deny_all]}))


def delete_experiment_namespaces():
    # Waits, so that the next run never starts while previous pods are still terminating.
    kubectl("delete", "namespace", "-l", EXPERIMENT_LABEL, "--wait=true", "--timeout=300s")


def check_idle(workers):
    """Refuse to measure on top of other work: it would load the nodes and sway resource-aware policies."""
    busy = []
    for pod in json.loads(kubectl("get", "pods", "--all-namespaces", "-o", "json"))["items"]:
        if pod["metadata"]["namespace"] == "kube-system" or pod["status"]["phase"] in ("Succeeded", "Failed"):
            continue
        if pod["spec"].get("nodeName") in workers or pod["spec"].get("schedulerName") == "kaptain-scheduler":
            busy.append(f"{pod['metadata']['namespace']}/{pod['metadata']['name']}")
    if busy:
        raise RuntimeError(f"cluster not idle, stop these first: {' '.join(busy)}")


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
        "images": running_images(),
        "node_cores": nodes,
        "started_at": now(),
        "status": "aborted",
    }

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # a cancelled job still cleans up
    delete_experiment_namespaces()  # left over by a run that was killed
    check_idle(nodes)
    try:
        create_namespace(namespace)
        complete = run_pods(out, namespace, pods, nodes, experiment["timeout_s"])
        meta["status"] = "complete" if complete else "timeout"
    finally:
        try:
            (out / "meta.json").write_text(json.dumps(meta, indent=2))
            collect(out, namespace, meta["started_at"])
        finally:
            # Last, and whatever failed before: a leftover namespace would load the next run.
            delete_experiment_namespaces()
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


def short(digest):
    return digest.split(":")[-1][:12]


def built_from_commit(image):
    """Deployed images are tagged <commit>-...; anything else cannot be traced back to the code."""
    return re.match(r"[0-9a-f]{40}", image.rsplit(":", 1)[-1]) is not None


def placement_waits(out):
    """Pod first seen by the watch -> first seen with a node, in ms, both on the runner's clock."""
    created, bound = {}, {}
    path = out / "watch.csv"
    if not path.exists():
        return []
    with path.open() as handle:
        for row in csv.DictReader(handle):
            if row["event"] == "ADDED" and not row["node"]:
                created.setdefault(row["pod"], float(row["time"]))
            if row["node"] and row["pod"] in created:
                bound.setdefault(row["pod"], float(row["time"]))
    return [1000 * (bound[pod] - created[pod]) for pod in bound]


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
    durations = [d["durations_ms"]["total"] for d in decisions]
    plugin_time = "missing"
    if durations:
        plugin_time = f"median {statistics.median(durations):.2f} ms, p95 {p95(durations):.2f} ms"
    fallbacks = sum(1 for d in decisions if d["fallback"])

    makespan = "incomplete"
    if succeeded and not failed and not unfinished:
        first = min(row["created"] for row in succeeded)
        last = max(row["finished"] for row in succeeded)
        makespan = f"{last - first:.0f} s"
    waits = placement_waits(out)
    coverage = f"{len(waits)}/{meta['pods']} pods"
    placement = f"incomplete ({coverage})" if waits else f"missing ({coverage})"
    if waits and len(waits) == meta["pods"]:
        placement = (f"median {statistics.median(waits):.0f} ms, p95 {p95(waits):.0f} ms, "
                     f"max {max(waits):.0f} ms ({coverage})")
    scheduler = meta["images"]["kaptain-scheduler"]
    decider = meta["images"]["kaptain-decider"]
    warning = "" if built_from_commit(scheduler["image"]) else " — ⚠ not built from a recorded commit"
    usage, imbalance = utilization(out, meta["node_cores"])

    return "\n".join([
        f"# `{meta['experiment']}` — run `{meta['run_id']}`",
        "",
        f"Status **{meta['status']}**, policy `{meta['strategy']}` (`{meta['policy_version'][:12]}`). "
        f"Source: {meta['source']}.",
        "",
        f"- Scheduler `{scheduler['image']}` (digest `{short(scheduler['digest'])}`){warning}; "
        f"decider `{decider['image']}` (digest `{short(decider['digest'])}`).",
        f"- Pods: {len(succeeded)} succeeded, {failed} failed, {unfinished} unfinished, of {meta['pods']}.",
        f"- Makespan, first pod created → last pod finished: **{makespan}**.",
        f"- Placement wait, pod created → bound (API watch): {placement}.",
        f"- Decisions: {len(decisions)}, fallbacks: {fallbacks}, plugin time {plugin_time}.",
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
    parser.add_argument("command", choices=["list", "manifests", "run", "cleanup"])
    parser.add_argument("experiment", nargs="?")
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()

    if args.command == "list":
        return list_experiments()
    if args.command == "manifests":
        return print_manifests(args.experiment)
    if args.command == "cleanup":
        return delete_experiment_namespaces()
    if not run_and_report(args.experiment, args.out):
        sys.exit(1)


if __name__ == "__main__":
    main()
