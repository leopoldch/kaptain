from .base import SchedulingStrategy
from .dummy_random import DummyRandom
from .largest_cpu_capacity import LargestCPUCapacity
from .least_allocated import LeastAllocated
from .least_used import LeastUsed

_REGISTRY = {s.name: s for s in (DummyRandom, LargestCPUCapacity, LeastAllocated, LeastUsed)}


def get_strategy(name: str) -> SchedulingStrategy:
    if name not in _REGISTRY:
        raise ValueError(f"unknown strategy {name!r}, known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()
