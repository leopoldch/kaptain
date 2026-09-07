import random

from .base import SchedulingStrategy


class DummyRandom(SchedulingStrategy):
    name = "dummy-random"

    def select(self, pod: dict, nodes: list[dict]) -> str:
        return random.choice(nodes)["metadata"]["name"]
