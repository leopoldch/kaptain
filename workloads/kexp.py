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
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
KUBECTL = os.environ.get("KEXP_KUBECTL", "docker exec -i kaptain-k3s kubectl --server=https://10.50.0.1:6443")
ARRIVAL = "kaptain.io/arrival-s"
TASK_ID = "kaptain.io/task-id"
EXPERIMENT_LABEL = "kaptain.io/experiment"
OWNER_LABEL = "kaptain.io/run-owner"  # random per process: a run deletes only the namespace it created
HEARTBEAT = "kaptain.io/heartbeat"  # unix time, renewed while the run lives
HEARTBEAT_EVERY_S = 60
STALE_AFTER_S = 300  # silent this long: the run was killed, `kexp.py cleanup` may delete its namespace
EXPERIMENT_NODE = "kaptain.io/experiment-node"  # =true on the nodes experiments run on, master included
WATCH_PROBE = "kexp-watch-probe"  # a pod no scheduler takes, streamed once the watch is open
WATCH_ENDED = "ENDED"  # watch.csv: the stream stopped before the run did
SAMPLE_EVERY_S = 5
SAMPLE_TIMEOUT_S = 30  # a busy kubelet answers late, not never: the master's took over 5 s in bursts


# Arrival processes: the arrival time, in seconds, of every pod.

def fixed(a, count, rng):
    return [i * a["interval_s"] for i in range(count)]


def from_gaps(gaps):
    # sum(), not a running total: since Python 3.12 it compensates rounding, and any other
    # summation shifts seeded arrival times by a few ulps.
    return [sum(gaps[:i]) for i in range(len(gaps) + 1)]


def normal(a, count, rng):
    return from_gaps([max(0, rng.gauss(a["mean_s"], a["stddev_s"])) for _ in range(count - 1)])


def poisson(a, count, rng):
    return from_gaps([rng.expovariate(1 / a["mean_s"]) for _ in range(count - 1)])


def bursts(a, count, rng):
    steady = [i * a["steady_interval_s"] for i in range(a["steady_count"])]
    burst_times = [steady[-1] + b * a["burst_gap_s"] for b in range(1, a["bursts"] + 1)]
    return steady + [t for t in burst_times for _ in range(a["burst_size"])]


ARRIVALS = {"fixed": fixed, "normal": normal, "poisson": poisson, "bursts": bursts}


# Experiment -> pods.

def kubectl(*args, stdin=None, timeout_s=60):
    # Bounded twice: kubectl gives up on the API server, and we give up on docker exec.
    command = shlex.split(KUBECTL) + list(args) + [f"--request-timeout={timeout_s}s"]
    try:
        result = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout_s + 10)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: no answer after {timeout_s + 10} s") from None
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
    """The nodes experiments run on and measure: chosen by label, which the pods select too."""
    nodes = json.loads(kubectl("get", "nodes", "-l", f"{EXPERIMENT_NODE}=true", "-o", "json"))["items"]
    if not nodes:
        raise RuntimeError(f"no node labelled {EXPERIMENT_NODE}=true")
    return {n["metadata"]["name"]: cores(n["status"]["allocatable"]["cpu"]) for n in nodes}


def sample_node(node):
    try:
        raw = kubectl("get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary", timeout_s=SAMPLE_TIMEOUT_S)
        cpu = json.loads(raw)["node"]["cpu"]
        usage, kubelet_time = cpu["usageNanoCores"], cpu["time"]
        # Null until the kubelet has two readings to compute a rate from.
        if isinstance(usage, bool) or not isinstance(usage, (int, float)):
            raise ValueError(f"usageNanoCores is {usage!r}")
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        # Left out, never written as zero; the report counts the samples per node.
        print(f"sample of {node} missed: {type(error).__name__}: {error}", file=sys.stderr)
        return None

    # The kubelet refreshes every 10-15 s: its timestamp tells how old a value is.
    return [now(), node, usage / 1e9, kubelet_time]


def sample_until(stop, node, telemetry, lock):
    # One thread per node: a slow kubelet delays neither a pod's arrival nor the other nodes'
    # samples. It is waited for, not skipped, since it is slow when its node is busiest.
    while not stop.wait(SAMPLE_EVERY_S):
        row = sample_node(node)
        if row:
            with lock:
                telemetry.writerow(row)


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
    # Checked at least once, so a submission that overran the deadline still sees finished pods.
    while not all_finished(namespace, expected):
        if time.monotonic() >= deadline:
            return False
        time.sleep(SAMPLE_EVERY_S)
    return True


def watch_pods(process, path, stop, opened):
    """Time, on the runner's clock, every change the API server streams for our pods."""
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "event", "pod", "node"])
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                break  # a line cut when the stream closes
            if event["type"] == "ERROR":
                # A Status, not a Pod, after which the server closes the watch.
                print(f"pod watch: {event['object'].get('message', '')}", file=sys.stderr)
                break
            pod = event["object"]
            if pod["metadata"]["name"] == WATCH_PROBE:
                opened.set()
                continue
            writer.writerow([time.time(), event["type"], pod["metadata"]["name"], pod["spec"].get("nodeName", "")])
        if not stop.is_set():
            # Not resumed: events replayed after a gap would carry the time they arrive, not when they happened.
            writer.writerow([time.time(), WATCH_ENDED, "", ""])


def confirm_watch(namespace, template, opened):
    """Wait until the watch streams a probe pod: from then on, every pod of ours is seen as it happens.

    Before that, a pod could reach the watch only in its initial list, already bound."""
    probe = copy.deepcopy(template)
    probe["metadata"] = {"name": WATCH_PROBE, "namespace": namespace}
    probe["spec"]["schedulerName"] = "kexp-none"  # no such scheduler: Pending, never run, deleted at once
    kubectl("create", "-f", "-", stdin=json.dumps(probe))
    try:
        if not opened.wait(30):
            raise RuntimeError("the pod watch streamed nothing within 30 s")
    finally:
        kubectl("-n", namespace, "delete", "pod", WATCH_PROBE, "--wait=false")


def run_pods(out, namespace, pods, nodes, timeout_s):
    deadline = time.monotonic() + timeout_s
    stop = threading.Event()
    opened = threading.Event()

    # Terminating docker exec leaves kubectl running in the K3s container: it lasts until the server
    # ends the watch, timeoutSeconds after it opened. Harmless, its namespace is gone and streams nothing.
    watch_url = f"/api/v1/namespaces/{namespace}/pods?watch=true&timeoutSeconds={timeout_s}"
    watch = subprocess.Popen(shlex.split(KUBECTL) + ["get", "--raw", watch_url],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    watcher = threading.Thread(target=watch_pods, args=(watch, out / "watch.csv", stop, opened))
    watcher.start()

    with (out / "telemetry.csv").open("w", newline="") as handle:
        telemetry = csv.writer(handle)
        telemetry.writerow(["time", "node", "cpu_cores", "kubelet_time"])
        lock = threading.Lock()
        samplers = [threading.Thread(target=sample_until, args=(stop, node, telemetry, lock)) for node in nodes]
        for sampler in samplers:
            sampler.start()
        try:
            confirm_watch(namespace, pods[0], opened)
            submit(pods)
            return wait(namespace, len(pods), deadline)
        finally:
            stop.set()
            for sampler in samplers:
                sampler.join()  # before the file closes
            watch.terminate()
            watcher.join()


def scheduler_env():
    deployment = json.loads(kubectl("-n", "kube-system", "get", "deploy", "kaptain-scheduler", "-o", "json"))
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", "") for e in container["env"]}


def running_images():
    """The images actually running, by digest: the deployment spec alone can be stale."""
    apps = ("kaptain-scheduler", "kaptain-decider")
    selector = f"app in ({','.join(apps)})"
    pods = json.loads(kubectl("-n", "kube-system", "get", "pods", "-l", selector, "-o", "json"))["items"]
    versions = {app: set() for app in apps}
    for pod in pods:
        # A pod of the previous rollout can still be terminating, and a new one not started yet.
        statuses = pod["status"].get("containerStatuses") or [{}]
        if pod["metadata"].get("deletionTimestamp") or not statuses[0].get("ready"):
            continue
        versions[pod["metadata"]["labels"]["app"]].add((statuses[0]["image"], statuses[0]["imageID"]))

    images = {}
    for app, found in versions.items():
        if len(found) != 1:
            # None: nothing to measure. Several: a rollout in progress, the run would mix policies.
            raise RuntimeError(f"{app}: {len(found)} versions ready, expected exactly one")
        image, digest = found.pop()
        images[app] = {"image": image, "digest": digest}
    return images


def create_namespace(namespace, owner):
    namespace_object = {"apiVersion": "v1", "kind": "Namespace",
                        "metadata": {"name": namespace, "labels": {EXPERIMENT_LABEL: "true", OWNER_LABEL: owner},
                                     "annotations": {HEARTBEAT: str(int(time.time()))}}}
    # Workloads need no network: deny everything. Not every node enforces it (see README).
    deny_all = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": "deny-all", "namespace": namespace},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}
    kubectl("create", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List",
                                                    "items": [namespace_object, deny_all]}))


def heartbeat_until(stop, owner):
    """Show `kexp.py cleanup` that the run is alive. A missed beat is harmless, only a long silence counts."""
    while not stop.wait(HEARTBEAT_EVERY_S):
        try:
            kubectl("annotate", "namespace", "-l", f"{OWNER_LABEL}={owner}",
                    f"{HEARTBEAT}={int(time.time())}", "--overwrite")
        except RuntimeError:
            pass  # the API did not answer this time (before the namespace exists, nothing matches: no error)


def delete_own_namespace(owner):
    # By the owner label, not by name: a run that reused the run id must not lose its namespace to us.
    # Matches nothing if this run was refused before creating one. Waits, so that no pod still
    # terminates when the next run starts.
    kubectl("delete", "namespace", "-l", f"{OWNER_LABEL}={owner}", "--wait=true", "--timeout=300s", timeout_s=330)


def delete_stale_namespaces():
    """Delete the namespaces of killed runs. A live run's is kept: the next run will refuse to start.

    One already being deleted is waited for, whatever its heartbeat: its run is over, and was cut
    while waiting for the deletion (a cancelled job)."""
    stale = []
    for namespace in json.loads(kubectl("get", "namespaces", "-l", EXPERIMENT_LABEL, "-o", "json"))["items"]:
        name = namespace["metadata"]["name"]
        beat = namespace["metadata"].get("annotations", {}).get(HEARTBEAT, "0")
        silent_s = time.time() - float(beat)
        if silent_s > STALE_AFTER_S or namespace["metadata"].get("deletionTimestamp"):
            stale.append(name)
        else:
            print(f"{name} kept: its run is alive (last heartbeat {silent_s:.0f} s ago)", file=sys.stderr)
    if stale:
        print(f"deleting {' '.join(stale)}: dead runs' leftovers", file=sys.stderr)
        kubectl("delete", "namespace", *stale, "--ignore-not-found", "--wait=true", "--timeout=300s", timeout_s=330)


def check_no_other_run():
    """Another run's namespace, or a killed run's: deleting either is for `kexp.py cleanup`, not for a run."""
    namespaces = kubectl("get", "namespaces", "-l", EXPERIMENT_LABEL, "-o", "name").split()
    if namespaces:
        raise RuntimeError(f"another run in progress, or a killed run's leftovers, which `kexp.py cleanup` "
                           f"deletes {STALE_AFTER_S} s after its last heartbeat: {' '.join(namespaces)}")


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
    """Never raises: a file that could not be collected is absent, and the report says missing."""
    try:
        (out / "pods.json").write_text(kubectl("-n", namespace, "get", "pods", "-o", "json"))
    except RuntimeError as error:
        print(f"pods not collected: {error}", file=sys.stderr)

    try:
        logs = kubectl("-n", "kube-system", "logs", "deploy/kaptain-scheduler", f"--since-time={started_at}")
    except RuntimeError as error:
        print(f"decisions not collected: {error}", file=sys.stderr)
        return
    ours = [line + "\n" for line in logs.splitlines() if f'"namespace":"{namespace}"' in line]
    (out / "decisions.jsonl").write_text("".join(ours))


def run(name, run_id, namespace, owner, out_root):
    experiment, pods = manifests(name, namespace)
    nodes = worker_nodes()
    env = scheduler_env()
    images = running_images()  # before any output: a run without clear provenance is not started
    check_no_other_run()
    check_idle(nodes)

    out = out_root / f"{name}-{run_id}"
    out.mkdir(parents=True)
    meta = {
        "experiment": name,
        "run_id": run_id,
        "source": experiment["source"],
        "pods": len(pods),
        "strategy": env["KAPTAIN_STRATEGY"],
        "policy_version": env["POLICY_VERSION"],
        "images": images,
        "node_cores": nodes,
        "started_at": now(),
        "status": "aborted",
    }

    created = False
    try:
        create_namespace(namespace, owner)
        created = True
        complete = run_pods(out, namespace, pods, nodes, experiment["timeout_s"])
        meta["status"] = "complete" if complete else "timeout"
    except Exception as error:
        # The run stays aborted, but what it measured is still collected and reported.
        traceback.print_exc()
        meta["error"] = f"{type(error).__name__}: {error}"
    except (KeyboardInterrupt, SystemExit):
        meta["error"] = "cancelled"  # SIGINT or SIGTERM: collected, not reported
        raise
    finally:
        (out / "meta.json").write_text(json.dumps(meta, indent=2))
        if created:  # otherwise a namespace of that name, if any, is another run's
            collect(out, namespace, meta["started_at"])
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
    """Pod first seen by the watch -> first seen with a node, in ms, both on the runner's clock.

    Also tells whether the watch stopped before the run did."""
    created, bound = {}, {}
    ended = False
    path = out / "watch.csv"
    if not path.exists():
        return [], ended
    with path.open() as handle:
        for row in csv.DictReader(handle):
            ended = ended or row["event"] == WATCH_ENDED
            if row["event"] == "ADDED" and not row["node"]:
                created.setdefault(row["pod"], float(row["time"]))
            if row["node"] and row["pod"] in created:
                bound.setdefault(row["pod"], float(row["time"]))
    return [1000 * (bound[pod] - created[pod]) for pod in bound], ended


def table(title, rows, key):
    lines = [f"| {title} | pods | mean (s) | p95 (s) | max (s) |", "|---|---:|---:|---:|---:|"]
    for value in sorted({row[key] for row in rows}):
        times = [row["completion"] for row in rows if row[key] == value]
        lines.append(f"| {value} | {len(times)} | {statistics.fmean(times):.1f} | {p95(times):.0f} | {max(times):.0f} |")
    return lines


def utilization(out, node_cores):
    samples = []
    path = out / "telemetry.csv"
    if path.exists():
        with path.open() as handle:
            samples = list(csv.DictReader(handle))

    lines, means = [], []
    for node, total in sorted(node_cores.items()):
        percent = [100 * float(s["cpu_cores"]) / total for s in samples if s["node"] == node]
        if not percent:
            lines.append(f"| {node} | 0 | missing | missing |")
            continue
        means.append(statistics.fmean(percent))
        lines.append(f"| {node} | {len(percent)} | {means[-1]:.1f} | {max(percent):.1f} |")

    header = ["| Node | samples | mean CPU % | peak CPU % |", "|---|---:|---:|---:|"]
    imbalance = statistics.pstdev(means) if len(means) > 1 else None
    return header + lines, imbalance


def report(out, meta):
    pods_path = out / "pods.json"
    pods = json.loads(pods_path.read_text())["items"] if pods_path.exists() else []
    succeeded = [pod_times(pod) for pod in pods if pod["status"]["phase"] == "Succeeded"]
    failed = sum(1 for pod in pods if pod["status"]["phase"] == "Failed")
    unfinished = meta["pods"] - len(succeeded) - failed
    pod_counts = f"{len(succeeded)} succeeded, {failed} failed, {unfinished} unfinished"
    if not pods_path.exists():
        pod_counts = "missing"

    decisions_path = out / "decisions.jsonl"
    log_lines = []
    if decisions_path.exists():
        log_lines = [json.loads(line) for line in decisions_path.read_text().splitlines()]
    decisions = [line for line in log_lines if line["event"] == "decision"]
    durations = [d["durations_ms"]["total"] for d in decisions]
    plugin_time = "missing"
    if durations:
        plugin_time = f"median {statistics.median(durations):.2f} ms, p95 {p95(durations):.2f} ms"
    fallbacks = sum(1 for d in decisions if d["fallback"])
    decision_counts = f"{len(decisions)}, fallbacks: {fallbacks}"
    if not decisions_path.exists():
        decision_counts = "missing"

    makespan = "missing" if not pods_path.exists() else "incomplete"
    if succeeded and not failed and not unfinished:
        first = min(row["created"] for row in succeeded)
        last = max(row["finished"] for row in succeeded)
        makespan = f"{last - first:.0f} s"
    waits, watch_ended = placement_waits(out)
    coverage = f"{len(waits)}/{meta['pods']} pods"
    if watch_ended:
        coverage += ", the watch ended early"
    placement = f"incomplete ({coverage})" if waits else f"missing ({coverage})"
    if waits and len(waits) == meta["pods"]:
        placement = (f"median {statistics.median(waits):.0f} ms, p95 {p95(waits):.0f} ms, "
                     f"max {max(waits):.0f} ms ({coverage})")
    scheduler = meta["images"]["kaptain-scheduler"]
    decider = meta["images"]["kaptain-decider"]
    warning = "" if built_from_commit(scheduler["image"]) else " — ⚠ not built from a recorded commit"
    usage, imbalance = utilization(out, meta["node_cores"])
    error = f" Error: `{meta['error']}`." if "error" in meta else ""

    return "\n".join([
        f"# `{meta['experiment']}` — run `{meta['run_id']}`",
        "",
        f"Status **{meta['status']}**, policy `{meta['strategy']}` (`{meta['policy_version'][:12]}`). "
        f"Source: {meta['source']}.{error}",
        "",
        f"- Scheduler `{scheduler['image']}` (digest `{short(scheduler['digest'])}`){warning}; "
        f"decider `{decider['image']}` (digest `{short(decider['digest'])}`).",
        f"- Pods: {pod_counts}, of {meta['pods']}.",
        f"- Makespan, first pod created → last pod finished: **{makespan}**.",
        f"- Placement wait, pod created → bound (API watch): {placement}.",
        f"- Decisions: {decision_counts}, plugin time {plugin_time}.",
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
    run_id = os.environ.get("KEXP_RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    namespace = f"kexp-{run_id}"
    owner = uuid.uuid4().hex
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # a cancelled job still cleans up
    stop = threading.Event()
    heart = threading.Thread(target=heartbeat_until, args=(stop, owner), daemon=True)
    heart.start()
    try:
        out, meta = run(name, run_id, namespace, owner, out_root)
        text = report(out, meta)
        (out / "report.md").write_text(text)
        print(text)

        if "GITHUB_STEP_SUMMARY" in os.environ:
            Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(text)
    finally:
        stop.set()  # not joined: a beat in flight must not delay the deletion of a cancelled job
        # Last, after the report and whatever failed before: a leftover namespace would load the next run.
        delete_own_namespace(owner)
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
        return delete_stale_namespaces()
    if not run_and_report(args.experiment, args.out):
        sys.exit(1)


if __name__ == "__main__":
    main()
