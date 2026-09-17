"""Explicit fallback, identical to the Go plugin's `FallbackName`.

It must never raise and must not depend on live telemetry: it runs precisely when
something else already went wrong. A fallback is a decision of the method under test, so
it is logged, counted, and kept in the results.

The second criterion matters: in the extender path per-node requested resources are
unknown, so every free ratio is 1 and the choice would otherwise collapse to name order.
"""

NAME = "least-allocated-requests"


def fallback_node(snapshot: dict) -> str:
    nodes = snapshot["nodes"]
    if not nodes:
        return ""
    ranked = sorted(nodes, key=lambda n: (-_free_ratio(n), -n["allocatable_millicpu"], n["name"]))
    return ranked[0]["name"]


def fallback_scores(snapshot: dict) -> dict[str, float]:
    chosen = fallback_node(snapshot)
    return {n["name"]: (1.0 if n["name"] == chosen else 0.0) for n in snapshot["nodes"]}


def _free_ratio(node: dict) -> float:
    allocatable = node["allocatable_millicpu"]
    if allocatable <= 0:
        return 0.0
    free = allocatable - node["requested_millicpu"]
    return free / allocatable if free > 0 else 0.0
