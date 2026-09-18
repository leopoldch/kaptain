from .base import SchedulingStrategy
from .dummy_random import DummyRandom
from .largest_cpu_capacity import LargestCPUCapacity
from .least_allocated import LeastAllocated
from .least_used import LeastUsed

_REGISTRY = {s.name: s for s in (DummyRandom, LargestCPUCapacity, LeastAllocated, LeastUsed)}


# What the extender protocol carries on its own. "requested" needs the Kubernetes API,
# which `bridge/cluster.py` provides when KAPTAIN_REQUESTS_SOURCE=api; the Go plugin reads
# it straight from the scheduler cache.
PROTOCOL_FEATURES = frozenset({"allocatable"})


def missing_requirements(strategy: SchedulingStrategy,
                         available: frozenset[str] = PROTOCOL_FEATURES) -> frozenset[str]:
    return frozenset(strategy.requires) - available


def get_strategy(name: str) -> SchedulingStrategy:
    if name not in _REGISTRY:
        raise ValueError(f"unknown strategy {name!r}, known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()
