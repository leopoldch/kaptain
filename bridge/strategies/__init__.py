from .base import SchedulingStrategy
from .dummy_random import DummyRandom
from .largest_cpu_capacity import LargestCPUCapacity

_REGISTRY = {s.name: s for s in (DummyRandom, LargestCPUCapacity)}


def get_strategy(name: str) -> SchedulingStrategy:
    return _REGISTRY[name]()
