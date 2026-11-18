from abc import ABC, abstractmethod


class SchedulingStrategy(ABC):
    name: str

    @abstractmethod
    def scores(self, snapshot: dict) -> dict[str, float]:
        """Raw score per node name, higher is better. A pure function of the snapshot."""

    def settle(self, phase: str, prior: str = "", sheets: dict | None = None) -> dict:
        """Between two runs, before the next one's pods (kexp): a policy that learns finishes
        learning from the run that ended. Nothing to do for the others. prior: what the next run
        gives the pairs never run, for the policies that estimate runtimes, on the nodes of sheets
        (each one's data sheet); the others refuse it."""
        if prior:
            raise ValueError(f"{self.name} has no runtime estimates: no prior {prior!r}")
        return {"settled": False}
