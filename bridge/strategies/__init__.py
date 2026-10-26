from .base import SchedulingStrategy
from .dummy_random import DummyRandom
from .kaptain import VARIANT, EarliestFinish, Kaptain, ShortestEstimate
from .largest_cpu_capacity import LargestCPUCapacity
from .least_allocated import LeastAllocated
from .least_used import LeastUsed

_REGISTRY = {s.name: s for s in (DummyRandom, EarliestFinish, LargestCPUCapacity, LeastAllocated, LeastUsed,
                                    ShortestEstimate)}


def get_strategy(name: str) -> SchedulingStrategy:
    if VARIANT.fullmatch(name):  # kaptain, kaptain-b0.5, kaptain-l1-llm, ...: its weights are in the name
        return Kaptain(name)
    if name not in _REGISTRY:
        raise ValueError(f"unknown strategy {name!r}, known: {sorted(_REGISTRY)} and kaptain[-b<beta>][-l<lambda>[-llm]]")
    return _REGISTRY[name]()
