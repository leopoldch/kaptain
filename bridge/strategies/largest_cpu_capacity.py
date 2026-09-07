from .base import SchedulingStrategy

MILLICORES_PER_CORE = 1000


def _millicores(cpu: str) -> int:
    return int(cpu[:-1]) if cpu.endswith("m") else int(float(cpu) * MILLICORES_PER_CORE)


def _allocatable_cpu(node: dict) -> int:
    return _millicores(node.get("status", {}).get("allocatable", {}).get("cpu", "0"))


class LargestCPUCapacity(SchedulingStrategy):
    name = "largest-cpu-capacity"

    def select(self, pod: dict, nodes: list[dict]) -> str:
        best = max(nodes, key=_allocatable_cpu)
        return best["metadata"]["name"]
