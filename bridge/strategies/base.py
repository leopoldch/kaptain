from abc import ABC, abstractmethod


class SchedulingStrategy(ABC):
    name: str
    # Snapshot feature groups the strategy cannot work without; the plugin falls back
    # rather than scoring on a snapshot that declares one missing.
    requires: frozenset[str] = frozenset()

    @abstractmethod
    def scores(self, snapshot: dict) -> dict[str, float]:
        """Raw score per node name, higher is better. A pure function of the snapshot."""
