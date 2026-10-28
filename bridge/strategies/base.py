from abc import ABC, abstractmethod


class SchedulingStrategy(ABC):
    name: str

    @abstractmethod
    def scores(self, snapshot: dict) -> dict[str, float]:
        """Raw score per node name, higher is better. A pure function of the snapshot."""

    def settle(self, phase: str) -> dict:
        """Between two runs, before the next one's pods (kexp): a policy that learns finishes
        learning from the run that ended. Nothing to do for the others."""
        return {"settled": False}
