"""The decision snapshot, shared with the Go plugin.

Field names and the selection rules below are the contract between the two integrations:
`plugin/pkg/snapshot` implements the same thing in Go, and both read the fixtures in
`testdata/snapshots/`. A value that cannot be read is reported in `missing`, never as zero.
"""

MILLICORES_PER_CORE = 1000

# Kubernetes caps an extender score at MaxExtenderPriority and then multiplies it by
# MaxNodeScore/MaxExtenderPriority. The plugin applies the same quantisation, so both
# paths produce the same integers, the same winner and the same ties.
MAX_EXTENDER_PRIORITY = 10

# Mirrors scorePrecision and maxRawScore in the Go plugin.
SCORE_PRECISION = 1000
MAX_RAW_SCORE = 1e9

# A workload identity that survives pod recreation. The seeded draw keys on it: a pod UID
# changes on every recreation and would silently break replayability.
TASK_ID_KEY = "kaptain.io/task-id"

FEATURE_ALLOCATABLE = "allocatable"
FEATURE_REQUESTED = "requested"
FEATURE_TELEMETRY = "telemetry"

INTEGRATION = "extender"

_MEMORY_UNITS = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
    "k": 1000, "m": 1000**2,
}

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def parse_cpu(value: str) -> int:
    """Kubernetes CPU quantity to millicores."""
    text = str(value)
    if text.endswith("m"):
        return int(float(text[:-1]))
    return int(float(text) * MILLICORES_PER_CORE)


def parse_memory(value: str) -> int:
    """Kubernetes memory quantity to bytes."""
    text = str(value)
    for suffix, factor in _MEMORY_UNITS.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * factor)
    return int(float(text))


def task_id(pod: dict) -> str:
    """The kaptain.io/task-id label or annotation, else namespace/name, else the UID."""
    metadata = pod.get("metadata", {})
    for source in ("labels", "annotations"):
        value = (metadata.get(source) or {}).get(TASK_ID_KEY)
        if value:
            return value
    name = metadata.get("name", "")
    if name:
        return f"{metadata.get('namespace', '')}/{name}"
    return metadata.get("uid", "")


def scaled(value: float) -> int:
    """Raw score to the integer scale, clamped exactly like `scaled` in the Go plugin."""
    if value != value:  # NaN
        return 0
    product = value * SCORE_PRECISION
    product = min(product, MAX_RAW_SCORE * SCORE_PRECISION)
    product = max(product, -MAX_RAW_SCORE * SCORE_PRECISION)
    return int(_round_half_away(product))


def quantize_raw(score: float, lowest: float, highest: float) -> int:
    """Truncation onto [0, MAX_EXTENDER_PRIORITY], mirroring Go's QuantizeRaw."""
    if highest == lowest:
        return MAX_EXTENDER_PRIORITY
    return int((score - lowest) * MAX_EXTENDER_PRIORITY / (highest - lowest))


def _round_half_away(value: float) -> float:
    # Go's math.Round rounds half away from zero; Python's round() is banker's rounding.
    import math as _math

    return _math.floor(value + 0.5) if value >= 0 else _math.ceil(value - 0.5)


ALWAYS = "Always"


def requests_from_spec(spec: dict) -> tuple[int, int]:
    """Effective pod requests, in millicores and bytes.

    Port of `PodRequests` in k8s.io/kubernetes/pkg/api/v1/resource, which the Go plugin
    calls directly. Three rules, and none of them is "sum everything":

    - regular containers add up;
    - a restartable init container (a sidecar, restartPolicy: Always) also adds up, and
      counts towards every later init container;
    - an ordinary init container contributes a maximum, together with the sidecars declared
      before it, because it has finished before the regular containers start;
    - pod overhead is added last.

    testdata/pod-requests.json pins this against the Go implementation, case by case.
    """
    cpu = memory = 0
    for container in spec.get("containers") or []:
        container_cpu, container_memory = _container_requests(container)
        cpu += container_cpu
        memory += container_memory

    sidecar_cpu = sidecar_memory = 0
    init_cpu = init_memory = 0
    for container in spec.get("initContainers") or []:
        container_cpu, container_memory = _container_requests(container)
        if container.get("restartPolicy") == ALWAYS:
            cpu += container_cpu
            memory += container_memory
            sidecar_cpu += container_cpu
            sidecar_memory += container_memory
            current_cpu, current_memory = sidecar_cpu, sidecar_memory
        else:
            current_cpu = container_cpu + sidecar_cpu
            current_memory = container_memory + sidecar_memory
        init_cpu = max(init_cpu, current_cpu)
        init_memory = max(init_memory, current_memory)

    cpu, memory = max(cpu, init_cpu), max(memory, init_memory)

    overhead = spec.get("overhead") or {}
    cpu += parse_cpu(overhead.get("cpu", "0"))
    memory += parse_memory(overhead.get("memory", "0"))
    return cpu, memory


def _container_requests(container: dict) -> tuple[int, int]:
    requests = (container.get("resources", {}) or {}).get("requests", {}) or {}
    return parse_cpu(requests.get("cpu", "0")), parse_memory(requests.get("memory", "0"))


def pod_requests(pod: dict) -> tuple[int, int]:
    return requests_from_spec(pod.get("spec", {}) or {})


def build(pod: dict, nodes: list[dict], run_id: str, policy_version: str,
          strategy: str, seed: int, requests: "RequestsSource | None" = None,
          telemetry: object | None = None) -> dict:
    """Build a snapshot from what the extender protocol actually carries.

    The extender receives a NodeList: capacities and conditions, but neither live usage nor
    the requests of the pods already bound. Those gaps are declared in `missing`, which is
    why the resource-aware strategies fall back until the extender gets an API client.
    """
    millicpu, memory = pod_requests(pod)
    metadata = pod.get("metadata", {})
    return {
        "run_id": run_id,
        "policy_version": policy_version,
        "integration": INTEGRATION,
        "strategy": strategy,
        "seed": seed,
        "pod": {
            "uid": metadata.get("uid", ""),
            "task_id": task_id(pod),
            "namespace": metadata.get("namespace", ""),
            "name": metadata.get("name", ""),
            "requested_millicpu": millicpu,
            "requested_memory_bytes": memory,
            "labels": metadata.get("labels", {}),
        },
        "nodes": [_node(n, requests, telemetry) for n in nodes],
    }


def _node(node: dict, requests: "RequestsSource | None", telemetry: object | None = None) -> dict:
    allocatable = node.get("status", {}).get("allocatable", {}) or {}
    name = node["metadata"]["name"]
    missing = [FEATURE_TELEMETRY]
    if not allocatable:
        missing.insert(0, FEATURE_ALLOCATABLE)

    reserved = requests.get(name) if requests is not None else None
    if reserved is None:
        missing.insert(0, FEATURE_REQUESTED)
        reserved = {"millicpu": 0, "memory_bytes": 0, "pods": 0, "age_seconds": 0.0}

    measured = telemetry.get(name) if telemetry is not None else None
    if measured is None:
        measured = {"millicpu": 0, "memory_bytes": 0, "age_seconds": 0.0}
    else:
        missing = [feature for feature in missing if feature != FEATURE_TELEMETRY]

    entry = {
        "name": name,
        "allocatable_millicpu": parse_cpu(allocatable.get("cpu", "0")),
        "allocatable_memory_bytes": parse_memory(allocatable.get("memory", "0")),
        "requested_millicpu": reserved["millicpu"],
        "requested_memory_bytes": reserved["memory_bytes"],
        "used_millicpu": measured["millicpu"],
        "used_memory_bytes": measured["memory_bytes"],
        "pods": reserved["pods"],
        "ready": _ready(node),
        "missing": missing,
    }
    age = max(reserved["age_seconds"], measured["age_seconds"])
    if age:
        entry["metrics_age_seconds"] = round(age, 3)
    return entry


def _ready(node: dict) -> bool:
    for condition in node.get("status", {}).get("conditions", []) or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def names(snapshot: dict) -> list[str]:
    return sorted(n["name"] for n in snapshot["nodes"])


def has(snapshot: dict, name: str) -> bool:
    return any(n["name"] == name for n in snapshot["nodes"])


def best(candidates: list[str], scores: dict[str, float]) -> str:
    """Highest score wins, ties broken by name, so both integrations agree."""
    scored = [name for name in sorted(candidates) if name in scores]
    if not scored:
        return ""
    return min(scored, key=lambda name: (-scores[name], name))


class RequestsSource:
    """What `bridge/cluster.py` provides: requested resources already bound per node."""

    def get(self, node: str) -> dict | None:  # pragma: no cover - interface
        raise NotImplementedError


def pick(candidates: list[str], seed: int, key: str) -> str:
    """Seeded draw. FNV-1a keeps the rule identical to the Go implementation."""
    if not candidates:
        return ""
    ordered = sorted(candidates)
    digest = _FNV_OFFSET
    for byte in f"{seed}:{key}".encode():
        digest = ((digest ^ byte) * _FNV_PRIME) & _FNV_MASK
    return ordered[digest % len(ordered)]
