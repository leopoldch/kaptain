"""Per-node requested resources, read from the Kubernetes API.

The extender protocol only carries node capacities, so without this the resource-aware
strategies cannot run here and the comparison with the Go plugin is not fair: the plugin
reads the same information straight from the scheduler cache.

The refresh runs in the background. A decision never queries the API: it reads the last
snapshot and reports its age, so a stale value is visible rather than silent.
"""

import logging
import threading
import time

from snapshot import RequestsSource, requests_from_spec

# Pods in these phases hold no node resources any more.
TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})

log = logging.getLogger("kaptain.cluster")


def placed_uids(pods: list[dict]) -> set[str]:
    """UIDs of the pods the API reports as **placed**, that is with a node assigned.

    A reservation may only be released against an observed placement. Two weaker rules were
    wrong and both made a reserved node look free again too early: releasing by timestamp
    (a refresh that merely started after the decision), and releasing as soon as the pod
    exists in the API — a pod exists from the moment it is created, and sits Pending until
    the binding lands. Whatever is neither placed nor cancelled is held until the TTL in
    `get()`, which is the backstop for a pod that never gets placed at all.
    """
    return {pod["uid"] for pod in pods if pod.get("uid") and pod.get("node")}


def aggregate(pods: list[dict]) -> dict[str, dict]:
    """Sum the effective requests of the pods bound to each node.

    Takes pod specs in their Kubernetes shape so it can be tested without a cluster, and
    computes each pod's requests with `requests_from_spec`, the same rule as the scheduler.
    """
    totals: dict[str, dict] = {}
    for pod in pods:
        node = pod.get("node")
        if not node or pod.get("phase") in TERMINAL_PHASES:
            continue
        millicpu, memory = requests_from_spec(pod.get("spec", {}) or {})
        entry = totals.setdefault(node, {"millicpu": 0, "memory_bytes": 0, "pods": 0})
        entry["millicpu"] += millicpu
        entry["memory_bytes"] += memory
        entry["pods"] += 1
    return totals


class ApiRequestsSource(RequestsSource):
    """Background refresh of `aggregate` from the Kubernetes API, plus in-flight decisions.

    The refresh alone is not enough during a burst: between two refreshes the extender would
    keep seeing a node as free and overload it, while the plugin already sees its own
    reservations in the scheduler cache. `reserve` closes most of that gap by holding a
    decision until a refresh that started after it has completed, which is the extender's
    equivalent of the plugin's Reserve/Unreserve.

    It does not make the two views identical: the extender still learns about *other*
    schedulers' placements only at the next refresh. That residual difference is a property
    of the integration, to be reported (`requests_age_ms`) rather than hidden, and to be
    removed altogether by replaying identical snapshots when the goal is to compare decision
    cost alone.
    """

    def __init__(self, refresh_seconds: float = 2.0, max_age_seconds: float = 30.0,
                 inflight_ttl_seconds: float = 15.0):
        self.refresh_seconds = refresh_seconds
        self.max_age_seconds = max_age_seconds
        self.inflight_ttl_seconds = inflight_ttl_seconds
        self._lock = threading.Lock()
        self._totals: dict[str, dict] = {}
        self._inflight: dict[str, dict] = {}
        self._updated_at: float | None = None
        self._api = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from kubernetes import client, config

        config.load_incluster_config()
        self._api = client.CoreV1Api()
        self.refresh()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="requests-refresh")
        self._thread.start()

    def _loop(self) -> None:
        while True:
            time.sleep(self.refresh_seconds)
            try:
                self.refresh()
            except Exception as exc:
                log.warning("requests refresh failed: %s", exc)

    def refresh(self) -> None:
        listed = self._api.list_pod_for_all_namespaces(
            field_selector="status.phase!=Succeeded,status.phase!=Failed"
        )
        pods = [_as_dict(pod) for pod in listed.items]
        totals = aggregate(pods)
        placed = placed_uids(pods)
        with self._lock:
            self._totals = totals
            self._updated_at = time.monotonic()
            # Release a reservation only once that exact pod is observed on a node, where
            # `totals` now accounts for it. A Pending pod keeps its reservation.
            self._inflight = {
                uid: entry for uid, entry in self._inflight.items() if uid not in placed
            }

    def reserve(self, uid: str, node: str, millicpu: int, memory_bytes: int) -> None:
        """Hold a decision until a refresh that started after it has completed."""
        with self._lock:
            self._inflight[uid] = {
                "node": node,
                "millicpu": millicpu,
                "memory_bytes": memory_bytes,
                "at": time.monotonic(),
            }

    def get(self, node: str) -> dict | None:
        """The node's reserved requests, or None when the data is missing or too old."""
        with self._lock:
            if self._updated_at is None:
                return None
            now = time.monotonic()
            age = now - self._updated_at
            if age > self.max_age_seconds:
                return None

            entry = dict(self._totals.get(node, {"millicpu": 0, "memory_bytes": 0, "pods": 0}))
            for uid, held in list(self._inflight.items()):
                if now - held["at"] > self.inflight_ttl_seconds:
                    del self._inflight[uid]
                    continue
                if held["node"] == node:
                    entry["millicpu"] += held["millicpu"]
                    entry["memory_bytes"] += held["memory_bytes"]
                    entry["pods"] += 1
            return {**entry, "age_seconds": age}


def _as_dict(pod) -> dict:
    """Reduce an API pod object to the Kubernetes-shaped spec `aggregate` expects."""
    return {
        "uid": pod.metadata.uid,
        "node": pod.spec.node_name,
        "phase": pod.status.phase,
        "spec": {
            "containers": [_container(c) for c in pod.spec.containers or []],
            "initContainers": [_container(c) for c in pod.spec.init_containers or []],
            "overhead": pod.spec.overhead or {},
        },
    }


def _container(container) -> dict:
    requests = (container.resources.requests or {}) if container.resources else {}
    entry = {"resources": {"requests": dict(requests)}}
    if getattr(container, "restart_policy", None):
        entry["restartPolicy"] = container.restart_policy
    return entry
