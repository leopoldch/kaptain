import re

from helpers import names, one_hot

from .base import SchedulingStrategy
from .drs import load_config

_INDEX = re.compile(r"(\d+)$")


class RoundRobin(SchedulingStrategy):
    """The Round Robin arm of Jian et al. 2024 (DRS, section 5.3), kept a pure function: the
    n-th pod of a run, numbered by kexp in arrival order, goes to node n mod k of the bench.

    The bench is fixed, DRS's action space (the monitors of DRS_CONFIG, sorted, E01): a node
    Kubernetes filtered out is skipped for the next one, so the cycle does not shift."""

    name = "round-robin"

    def __init__(self):
        self.bench = sorted(load_config()["monitors"])

    def scores(self, snapshot: dict) -> dict[str, float]:
        candidates = names(snapshot)
        if not candidates:
            raise ValueError("no candidate nodes")
        if not self.bench:
            raise ValueError("round-robin cycles over the DRS monitors' nodes: set them in DRS_CONFIG")
        match = _INDEX.search(snapshot["pod"]["task_id"])
        if match is None:
            raise ValueError("round-robin needs a task id ending in the pod's arrival index")
        index = int(match.group(1))
        for step in range(len(self.bench)):
            chosen = self.bench[(index + step) % len(self.bench)]
            if chosen in candidates:
                return one_hot(chosen, candidates)
        raise ValueError(f"no node of the bench {self.bench} is a candidate")
