# Helpers used by policies scoring a scheduler snapshot.

FEATURE_ALLOCATABLE = "allocatable"
FEATURE_REQUESTED = "requested"
FEATURE_TELEMETRY = "telemetry"

INTEGRATION = "decider"

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def names(snapshot: dict) -> list[str]:
    return sorted(n["name"] for n in snapshot["nodes"])


def one_hot(chosen: str, candidates: list[str]) -> dict[str, float]:
    # Policies that pick one node: it alone scores, so the plugin binds it.
    return {name: (1.0 if name == chosen else 0.0) for name in candidates}


def pick(candidates: list[str], seed: int, key: str) -> str:
    # FNV-1a makes a seeded draw stable across runs.
    if not candidates:
        return ""
    ordered = sorted(candidates)
    digest = _FNV_OFFSET
    for byte in f"{seed}:{key}".encode():
        digest = ((digest ^ byte) * _FNV_PRIME) & _FNV_MASK
    return ordered[digest % len(ordered)]
