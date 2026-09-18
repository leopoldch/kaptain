from abc import ABC, abstractmethod


class SchedulingStrategy(ABC):
    name: str
    # Snapshot feature groups the strategy cannot work without. The extender refuses to
    # start when it cannot provide them, instead of producing a run of pure fallbacks.
    requires: frozenset[str] = frozenset()

    @abstractmethod
    def scores(self, snapshot: dict) -> dict[str, float]:
        """Return a raw score per node name, higher is better.

        A strategy is a pure function of the snapshot: no cluster call, no state, no
        training. The Go plugin implements the same rules in `plugin/pkg/strategies`.
        """
