from .base import SchedulingStrategy


class LeastAllocated(SchedulingStrategy):
    """Prefers the node left with the most free CPU and memory once the pod is added.

    It needs per-node requested resources. The extender protocol does not carry them, so
    this strategy fails on a live cluster until the extender gets a Kubernetes API client;
    the failure is an explicit, logged fallback.
    """

    name = "least-allocated"
    requires = frozenset({"requested"})

    def scores(self, snapshot: dict) -> dict[str, float]:
        if not snapshot["nodes"]:
            raise ValueError("no candidate nodes")
        pod = snapshot["pod"]
        out = {}
        for node in snapshot["nodes"]:
            if node["allocatable_millicpu"] <= 0 or node["allocatable_memory_bytes"] <= 0:
                raise ValueError(f"node {node['name']!r} has no allocatable CPU or memory")
            if "requested" in node.get("missing", []):
                raise ValueError("per-node requested resources are not available")
            cpu = _free(node["allocatable_millicpu"],
                        node["requested_millicpu"] + pod["requested_millicpu"])
            memory = _free(node["allocatable_memory_bytes"],
                           node["requested_memory_bytes"] + pod["requested_memory_bytes"])
            out[node["name"]] = (cpu + memory) / 2
        return out


def _free(allocatable: int, requested: int) -> float:
    if requested >= allocatable:
        return 0.0
    return (allocatable - requested) / allocatable
