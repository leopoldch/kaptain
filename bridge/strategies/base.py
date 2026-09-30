from abc import ABC, abstractmethod


class SchedulingStrategy(ABC):
    name: str

    @abstractmethod
    def scores(self, snapshot: dict) -> dict[str, float]:
        """Raw score per node name, higher is better. A pure function of the snapshot."""
