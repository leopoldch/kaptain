from helpers import names, one_hot, pick

from .base import SchedulingStrategy


class DummyRandom(SchedulingStrategy):
    """Control arm: a seeded draw, reproducible across runs."""

    name = "dummy-random"

    def scores(self, snapshot: dict) -> dict[str, float]:
        candidates = names(snapshot)
        if not candidates:
            raise ValueError("no candidate nodes")
        chosen = pick(candidates, snapshot["seed"], snapshot["pod"]["task_id"])
        return one_hot(chosen, candidates)
