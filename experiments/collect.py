"""Collection: scheduler metrics, pod placements, decision logs.

Everything here reads the cluster through `kubectl`, with the permissions of the current
kubeconfig. That is fine for a pilot and it is stated in the run record, since a run made
with different permissions is not the same run.

The two integrations do not expose identical surfaces, and the collector absorbs the
difference rather than pretending it does not exist:

- metric prefixes differ (`kaptain_plugin_*` against `kaptain_extender_*`);
- the extender's own metrics live on its service, the plugin's on its scheduler;
- **the scheduler metrics that matter are on each arm's scheduler**, and for the extender arm
  that is the only place the HTTP round trip is visible at all.
"""

import json
import re
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass

NAMESPACE = "kube-system"

# Where each arm's numbers live.
ARMS = {
    "extender": {
        "scheduler_deploy": "ml-scheduler",
        "scheduler_port": 10259,
        "scheduler_scheme": "https",
        "own_deploy": "scheduler-extender",
        "own_port": 8888,
        "own_scheme": "http",
        "own_prefix": "kaptain_extender_",
        "scheduler_name": "ml-scheduler",
    },
    "plugin": {
        "scheduler_deploy": "kaptain-scheduler",
        "scheduler_port": 10259,
        "scheduler_scheme": "https",
        # The plugin's own metrics are served by the scheduler itself.
        "own_deploy": "kaptain-scheduler",
        "own_port": 10259,
        "own_scheme": "https",
        "own_prefix": "kaptain_plugin_",
        "scheduler_name": "kaptain-scheduler",
    },
}


class CollectionError(RuntimeError):
    pass


def kubectl(*args: str, check: bool = True, timeout: int = 60) -> str:
    if shutil.which("kubectl") is None:
        raise CollectionError("kubectl is not on PATH")
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise CollectionError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def port_forward(deployment: str, remote_port: int, namespace: str = NAMESPACE):
    """A port-forward that is always torn down, even when the run fails."""
    local = _free_port()
    process = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", f"deploy/{deployment}",
         f"{local}:{remote_port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise CollectionError(
                    f"port-forward to {deployment} exited: {process.stderr.read().strip()}")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", local)) == 0:
                    break
            time.sleep(0.2)
        else:
            raise CollectionError(f"port-forward to {deployment} never became ready")
        yield local
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def scrape(deployment: str, port: int, scheme: str) -> str:
    """The raw Prometheus exposition, saved verbatim so a run can be re-analysed later."""
    import urllib.request
    import ssl

    with port_forward(deployment, port) as local:
        url = f"{scheme}://127.0.0.1:{local}/metrics"
        context = ssl._create_unverified_context() if scheme == "https" else None
        with urllib.request.urlopen(url, timeout=30, context=context) as response:
            return response.read().decode()


# --- Prometheus text parsing -------------------------------------------------

@dataclass
class Histogram:
    count: float
    total: float           # the _sum series
    buckets: dict[float, float]

    @property
    def mean_s(self) -> float | None:
        return self.total / self.count if self.count else None


def parse_histogram(text: str, metric: str, labels: dict[str, str] | None = None) -> Histogram:
    """Read one histogram out of an exposition, summing over unmatched label sets."""
    wanted = labels or {}
    count = total = 0.0
    buckets: dict[float, float] = {}

    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(metric):
            continue
        name, _, rest = line.partition("{" if "{" in line else " ")
        if "{" in line:
            label_text, _, value_text = rest.partition("}")
            series_labels = dict(re.findall(r'(\w+)="([^"]*)"', label_text))
            value = float(value_text.strip())
        else:
            series_labels, value = {}, float(rest.strip())

        if any(series_labels.get(key) != want for key, want in wanted.items()):
            continue

        if name == f"{metric}_bucket":
            # Accumulate: several label sets contribute to the same bucket, exactly as they
            # do to _sum and _count. Overwriting here silently produced quantiles computed
            # from one label set against a count from all of them.
            le = float(series_labels["le"])
            buckets[le] = buckets.get(le, 0.0) + value
        elif name == f"{metric}_sum":
            total += value
        elif name == f"{metric}_count":
            count += value

    return Histogram(count=count, total=total, buckets=buckets)


def process_identity(text: str) -> float | None:
    """The exporter's process start time, which changes on every restart."""
    for line in text.splitlines():
        if line.startswith("process_start_time_seconds"):
            return float(line.rsplit(" ", 1)[1])
    return None


def same_process(before: str, after: str) -> bool:
    """Whether both scrapes came from the same process instance.

    Counters going backwards catch an obvious restart, but not one followed by enough
    activity to climb past the earlier values -- which is exactly what a busy scheduler does.
    The process start time settles it; the caller also compares the scheduler pod's identity
    and restart count.
    """
    first, second = process_identity(before), process_identity(after)
    if first is None or second is None:
        return False
    return first == second


def scheduler_identity(deployment: str, namespace: str = NAMESPACE) -> dict:
    """Pod UID and container restart count, to detect a restart the metrics could hide."""
    document = json.loads(kubectl("-n", namespace, "get", "pods", "-l", f"app={deployment}",
                                  "-o", "json"))
    items = document.get("items", [])
    if not items:
        raise CollectionError(f"no pod found for {deployment}")
    pod = items[0]
    restarts = sum(status.get("restartCount", 0)
                   for status in pod.get("status", {}).get("containerStatuses", []) or [])
    return {"pod_uid": pod["metadata"]["uid"], "restarts": restarts,
            "started_at": pod.get("status", {}).get("startTime", "")}


def delta(before: Histogram, after: Histogram) -> Histogram:
    """The observations recorded during the run.

    Valid only if the process did not restart in between. A restart resets the counters, and
    a restart plus enough traffic can leave them *above* their earlier values, so the caller
    must also compare process identities -- see same_process and scheduler_identity.
    """
    if after.count < before.count or after.total < before.total:
        raise CollectionError(
            "histogram went backwards: the scheduler restarted during the run, so this run "
            "cannot be used")
    return Histogram(
        count=after.count - before.count,
        total=after.total - before.total,
        buckets={le: after.buckets.get(le, 0.0) - before.buckets.get(le, 0.0)
                 for le in after.buckets},
    )


def quantile(histogram: Histogram, q: float) -> float | None:
    """Interpolate a quantile from bucket counts, as `histogram_quantile` does.

    Approximate by construction. With the native scheduler histograms starting at 1 ms, a
    quantile that falls in the first bucket is only "somewhere below 1 ms" — report the mean
    in that case and do not dress the estimate up as a measurement.
    """
    if not histogram.count:
        return None

    ordered = sorted(histogram.buckets.items())
    target = q * histogram.count
    previous_le, previous_count = 0.0, 0.0

    for le, cumulative in ordered:
        if cumulative >= target:
            if le == float("inf"):
                return previous_le
            span = cumulative - previous_count
            if span <= 0:
                return le
            return previous_le + (le - previous_le) * (target - previous_count) / span
        previous_le, previous_count = le, cumulative
    return previous_le


def first_bucket(histogram: Histogram) -> float:
    return min((le for le in histogram.buckets), default=0.0)


def share_below_first_bucket(histogram: Histogram) -> float | None:
    """How much of the run fell into the very first bucket, and is therefore unresolved."""
    if not histogram.count:
        return None
    floor = first_bucket(histogram)
    return histogram.buckets.get(floor, 0.0) / histogram.count


# --- Cluster state -----------------------------------------------------------

def pods(selector: str, namespace: str = "default") -> list[dict]:
    document = json.loads(kubectl("-n", namespace, "get", "pods", "-l", selector, "-o", "json"))
    return document.get("items", [])


def placements(selector: str, namespace: str = "default") -> list[dict]:
    """One row per pod: task id, node, phase, and the coarse Kubernetes timestamps.

    Those timestamps have second resolution, which is why they never carry the latency
    result. They serve to verify placement and to detect pods that never got scheduled.
    """
    rows = []
    for pod in pods(selector, namespace):
        metadata, status = pod["metadata"], pod.get("status", {})
        scheduled = next((condition for condition in status.get("conditions", [])
                          if condition.get("type") == "PodScheduled"), {})
        rows.append({
            "task_id": metadata.get("labels", {}).get("kaptain.io/task-id", ""),
            "pod_name": metadata["name"],
            "pod_uid": metadata["uid"],
            "node": pod.get("spec", {}).get("nodeName", ""),
            "phase": status.get("phase", ""),
            "created_at": metadata.get("creationTimestamp", ""),
            "scheduled_at": scheduled.get("lastTransitionTime", ""),
            "scheduled_status": scheduled.get("status", ""),
        })
    return rows


def decision_log(deployment: str, since_time: str, pod_uids: set[str],
                 namespace: str = NAMESPACE) -> list[dict]:
    """The decision lines belonging to **this run**, and to no other.

    Two filters, because neither is sufficient. `--since-time` bounds the window but the
    deployment keeps serving other pods -- a leftover, a system pod, the previous run's tail.
    The pod UID set is the authoritative filter: a line counts only if it decided about a pod
    this run created. Without it, the fallback counters and our internal means would silently
    include earlier runs.
    """
    text = kubectl("-n", namespace, "logs", f"deploy/{deployment}",
                   f"--since-time={since_time}", "--tail=-1", check=False)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") not in {"decision", "binding"}:
            continue
        if entry.get("pod_uid") not in pod_uids:
            continue
        lines.append(entry)
    return lines


_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
             "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def quantity(text: str) -> float:
    """A Kubernetes quantity as a float. Good enough to compare capacities."""
    text = str(text).strip()
    if not text:
        return 0.0
    for suffix in ("Ki", "Mi", "Gi", "Ti"):
        if text.endswith(suffix):
            return float(text[:-2]) * _SUFFIXES[suffix]
    if text[-1] in _SUFFIXES:
        return float(text[:-1]) * _SUFFIXES[text[-1]]
    return float(text)


def free_capacity() -> dict[str, dict[str, float]]:
    """Allocatable minus the requests already bound, per schedulable node."""
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    pods = json.loads(kubectl("get", "pods", "--all-namespaces", "-o", "json"))["items"]

    free = {}
    for node in nodes:
        if node["metadata"]["name"] not in set(feasible_nodes()):
            continue
        allocatable = node["status"].get("allocatable", {})
        free[node["metadata"]["name"]] = {
            "cpu": quantity(allocatable.get("cpu", "0")),
            "memory": quantity(allocatable.get("memory", "0")),
        }

    for pod in pods:
        node = pod.get("spec", {}).get("nodeName")
        if node not in free or pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        for container in pod["spec"].get("containers", []) or []:
            requests = (container.get("resources", {}) or {}).get("requests", {}) or {}
            free[node]["cpu"] -= quantity(requests.get("cpu", "0"))
            free[node]["memory"] -= quantity(requests.get("memory", "0"))
    return free


def nodes_that_fit(cpu: str, memory: str) -> list[str]:
    """Nodes with room for one pod of that size, right now.

    Readiness is not feasibility. When filtering leaves a single node, kube-scheduler returns
    it without scoring at all, so neither integration runs and the run measures nothing.
    """
    needed_cpu, needed_memory = quantity(cpu), quantity(memory)
    return sorted(name for name, left in free_capacity().items()
                  if left["cpu"] >= needed_cpu and left["memory"] >= needed_memory)


def feasible_nodes() -> list[str]:
    document = json.loads(kubectl("get", "nodes", "-o", "json"))
    ready = []
    for node in document["items"]:
        conditions = {c["type"]: c["status"] for c in node["status"].get("conditions", [])}
        unschedulable = node.get("spec", {}).get("unschedulable", False)
        taints = node.get("spec", {}).get("taints", []) or []
        control_plane = any(t.get("key", "").startswith("node-role.kubernetes.io/control-plane")
                            for t in taints)
        if conditions.get("Ready") == "True" and not unschedulable and not control_plane:
            ready.append(node["metadata"]["name"])
    return ready
