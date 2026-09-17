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
from datetime import datetime, timezone

from snapshot import parse_cpu, parse_memory

log = logging.getLogger("kaptain.telemetry")

METRICS_GROUP = "metrics.k8s.io"
METRICS_VERSION = "v1beta1"


def parse_node_metrics(items: list[dict]) -> dict[str, dict]:
    """Node usage from the metrics API payload, in millicores and bytes.

    `measured_at` comes from the payload's own `timestamp`, not from the moment we
    downloaded it. metrics-server serves a cached sample, so a fresh fetch of a stale
    measurement would otherwise look fresh, and a strategy would rank on old numbers while
    reporting an age of milliseconds.
    """
    usage = {}
    for item in items:
        name = (item.get("metadata") or {}).get("name")
        if not name:
            continue
        measured = item.get("usage") or {}
        usage[name] = {
            "millicpu": parse_cpu(measured.get("cpu", "0")),
            "memory_bytes": parse_memory(measured.get("memory", "0")),
            "measured_at": parse_timestamp(item.get("timestamp")),
        }
    return usage


def parse_timestamp(value: str | None) -> float | None:
    """RFC 3339 timestamp to epoch seconds, or None when the API did not send one."""
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


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
        """The node's measured usage, or None when it is missing or too old.

        The age is the age of the measurement itself, per node. A sample without a usable
        timestamp is refused: an unknown age cannot be checked against the limit.
        """
        with self._lock:
            entry = self._usage.get(node)
        if entry is None or entry.get("measured_at") is None:
            return None
        age = time.time() - entry["measured_at"]
        if age < 0:
            age = 0.0          # clock skew between us and the metrics source
        if age > self.max_age_seconds:
            return None
        return {"millicpu": entry["millicpu"], "memory_bytes": entry["memory_bytes"],
                "age_seconds": age}
