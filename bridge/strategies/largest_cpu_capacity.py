from helpers import FEATURE_ALLOCATABLE

from .base import SchedulingStrategy


class LargestCPUCapacity(SchedulingStrategy):
    """Ranks on static allocatable CPU: it does not spread and ignores what is running."""

    name = "largest-cpu-capacity"
    requires = frozenset({FEATURE_ALLOCATABLE})

    def scores(self, snapshot: dict) -> dict[str, float]:
        if not snapshot["nodes"]:
            raise ValueError("no candidate nodes")
        out = {}
        for node in snapshot["nodes"]:
            # A node whose allocatable could not be read reports it missing; ranking it at
            # zero would put it last on a value nobody measured.
            if FEATURE_ALLOCATABLE in node.get("missing", []):
                raise ValueError(f"node {node['name']!r} has no allocatable capacity")
            out[node["name"]] = float(node["allocatable_millicpu"])
        return out
