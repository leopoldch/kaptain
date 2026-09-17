"""Measured node usage, read from the Kubernetes metrics API.

Requested resources say what pods asked for, not what is being used. The BT arm of the
protocol needs the measured value, and so does any comparison claiming that a model beats
a heuristic *given the same observations*.

Same contract as the Go plugin's `pkg/telemetry`: a background refresh, a decision that
never calls the API, and an age carried with every sample so a stale value is visible.
"""

import logging
import threading
import time

from snapshot import parse_cpu, parse_memory

log = logging.getLogger("kaptain.telemetry")

METRICS_GROUP = "metrics.k8s.io"
METRICS_VERSION = "v1beta1"


def parse_node_metrics(items: list[dict]) -> dict[str, dict]:
    """Node usage from the metrics API payload, in millicores and bytes."""
    usage = {}
    for item in items:
        name = (item.get("metadata") or {}).get("name")
        if not name:
            continue
        measured = item.get("usage") or {}
        usage[name] = {
            "millicpu": parse_cpu(measured.get("cpu", "0")),
            "memory_bytes": parse_memory(measured.get("memory", "0")),
        }
    return usage


class MetricsApiTelemetry:
    def __init__(self, refresh_seconds: float = 2.0, max_age_seconds: float = 30.0):
        self.refresh_seconds = refresh_seconds
        self.max_age_seconds = max_age_seconds
        self._lock = threading.Lock()
        self._usage: dict[str, dict] = {}
        self._updated_at: float | None = None
        self._api = None

    def start(self) -> None:
        from kubernetes import client, config

        config.load_incluster_config()
        self._api = client.CustomObjectsApi()
        try:
            self.refresh()
        except Exception as exc:
            log.error("telemetry: first refresh failed, starting without a sample: %s", exc)
        threading.Thread(target=self._loop, daemon=True, name="telemetry-refresh").start()

    def _loop(self) -> None:
        while True:
            time.sleep(self.refresh_seconds)
            try:
                self.refresh()
            except Exception as exc:
                log.warning("telemetry refresh failed: %s", exc)

    def refresh(self) -> None:
        listed = self._api.list_cluster_custom_object(METRICS_GROUP, METRICS_VERSION, "nodes")
        usage = parse_node_metrics(listed.get("items", []))
        with self._lock:
            self._usage = usage
            self._updated_at = time.monotonic()

    def get(self, node: str) -> dict | None:
        """The node's measured usage, or None when it is missing or too old."""
        with self._lock:
            if self._updated_at is None:
                return None
            age = time.monotonic() - self._updated_at
            if age > self.max_age_seconds:
                return None
            entry = self._usage.get(node)
            if entry is None:
                return None
            return {**entry, "age_seconds": age}
