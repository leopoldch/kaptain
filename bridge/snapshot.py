# The rules the decider and the Go plugin must both implement. Pinned by
# testdata/snapshots/ and testdata/quantisation.json, read by both test suites.

MAX_EXTENDER_PRIORITY = 10

FEATURE_ALLOCATABLE = "allocatable"
FEATURE_REQUESTED = "requested"
FEATURE_TELEMETRY = "telemetry"

INTEGRATION = "decider"

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def names(snapshot: dict) -> list[str]:
    return sorted(n["name"] for n in snapshot["nodes"])


def best(candidates: list[str], scores: dict[str, float]) -> str:
    scored = [name for name in sorted(candidates) if name in scores]
    if not scored:
        return ""
    return min(scored, key=lambda name: (-scores[name], name))


def normalize(scores: dict[str, float]) -> dict[str, int]:
    lowest, highest = min(scores.values()), max(scores.values())
    if highest == lowest:
        return {name: MAX_EXTENDER_PRIORITY for name in scores}
    span = highest - lowest
    return {
        name: int((value - lowest) * MAX_EXTENDER_PRIORITY / span)
        for name, value in scores.items()
    }


def pick(candidates: list[str], seed: int, key: str) -> str:
    # FNV-1a, so the draw is identical to the Go implementation.
    if not candidates:
        return ""
    ordered = sorted(candidates)
    digest = _FNV_OFFSET
    for byte in f"{seed}:{key}".encode():
        digest = ((digest ^ byte) * _FNV_PRIME) & _FNV_MASK
    return ordered[digest % len(ordered)]
