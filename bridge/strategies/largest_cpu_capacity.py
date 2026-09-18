from .base import SchedulingStrategy


class LargestCPUCapacity(SchedulingStrategy):
    """Ranks on static allocatable CPU: it does not spread and ignores what is running."""

    name = "largest-cpu-capacity"

    def scores(self, snapshot: dict) -> dict[str, float]:
        if not snapshot["nodes"]:
            raise ValueError("no candidate nodes")
        return {n["name"]: float(n["allocatable_millicpu"]) for n in snapshot["nodes"]}
