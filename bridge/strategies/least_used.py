from snapshot import FEATURE_ALLOCATABLE, FEATURE_TELEMETRY

from .base import SchedulingStrategy


class LeastUsed(SchedulingStrategy):
    """Ranks on measured free CPU and memory rather than on requests: the protocol's BT arm."""

    name = "least-used"
    requires = frozenset({FEATURE_ALLOCATABLE, FEATURE_TELEMETRY})

    def scores(self, snapshot: dict) -> dict[str, float]:
        if not snapshot["nodes"]:
            raise ValueError("no candidate nodes")
        pod = snapshot["pod"]
        out = {}
        for node in snapshot["nodes"]:
            if node["allocatable_millicpu"] <= 0 or node["allocatable_memory_bytes"] <= 0:
                raise ValueError(f"node {node['name']!r} has no allocatable CPU or memory")
            if FEATURE_TELEMETRY in node.get("missing", []):
                raise ValueError(f"node {node['name']!r} has no measured usage")
            cpu = _free(node["allocatable_millicpu"],
                        node["used_millicpu"] + pod["requested_millicpu"])
            memory = _free(node["allocatable_memory_bytes"],
                           node["used_memory_bytes"] + pod["requested_memory_bytes"])
            out[node["name"]] = (cpu + memory) / 2
        return out


def _free(allocatable: int, used: int) -> float:
    if used >= allocatable:
        return 0.0
    return (allocatable - used) / allocatable
