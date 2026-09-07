from abc import ABC, abstractmethod


class SchedulingStrategy(ABC):
    name: str

    @abstractmethod
    def select(self, pod: dict, nodes: list[dict]) -> str:
        """Return the name of the node to schedule `pod` on, given all candidates."""
