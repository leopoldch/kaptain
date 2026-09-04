from .base import SchedulingStrategy
from .dummy_random import DummyRandom
from .spread_cpu import SpreadCPU

_REGISTRY = {s.name: s for s in (DummyRandom, SpreadCPU)}


def get_strategy(name: str) -> SchedulingStrategy:
    return _REGISTRY[name]()
