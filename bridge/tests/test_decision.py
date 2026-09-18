import pytest

from decision import (MAX_RAW_SCORE, REASON_INVALID_SCORE, REASON_STRATEGY_ERROR,
                      Decider, normalize)
from fallback import fallback_node


def node(name: str, cpu: str) -> dict:
    return {
        "metadata": {"name": name},
        "status": {
            "allocatable": {"cpu": cpu, "memory": "8Gi"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


@pytest.fixture
def nodes() -> list[dict]:
    return [node("w1", "4"), node("w2", "8"), node("w3", "8")]


@pytest.fixture
def pod() -> dict:
    return {
        "metadata": {"name": "p", "namespace": "default", "uid": "u1"},
        "spec": {"containers": [{"resources": {"requests": {"cpu": "500m", "memory": "256Mi"}}}]},
    }


class Working:
    name = "working"

    def scores(self, snapshot):
        return {n["name"]: 1.0 if n["name"] == "w1" else 0.0 for n in snapshot["nodes"]}


class Raising:
    name = "raising"

    def scores(self, snapshot):
        raise ValueError("nope")


class Lying:
    name = "lying"

    def scores(self, snapshot):
        return {"not-a-candidate": 1.0}


class NotANumber:
    name = "not-a-number"

    def scores(self, snapshot):
        return {n["name"]: float("nan") for n in snapshot["nodes"]}


def test_valid_choice_is_kept(pod, nodes):
    decision = Decider(Working()).decide(pod, nodes)
    assert decision.node == "w1"
    assert not decision.fallback


def test_strategy_error_falls_back_explicitly(pod, nodes):
    decision = Decider(Raising()).decide(pod, nodes)
    assert decision.fallback_reason == f"{REASON_STRATEGY_ERROR}:ValueError"
    assert decision.node in {"w2", "w3"}


def test_choice_outside_candidates_falls_back(pod, nodes):
    decision = Decider(Lying()).decide(pod, nodes)
    assert decision.fallback_reason == REASON_INVALID_SCORE


def test_score_that_is_not_a_number_falls_back(pod, nodes):
    decision = Decider(NotANumber()).decide(pod, nodes)
    assert decision.fallback_reason == REASON_INVALID_SCORE


def test_fallback_prefers_capacity_when_requests_are_unknown(nodes):
    from snapshot import build

    # The extender protocol carries no per-node requests, so every free ratio is 1 and the
    # second criterion decides: most allocatable CPU, then name.
    snapshot = build({"metadata": {}}, nodes, "r", "p", "s", 0)
    assert fallback_node(snapshot) == "w2"


def test_fallback_survives_missing_capacity():
    from snapshot import build

    snapshot = build({"metadata": {}}, [{"metadata": {"name": "x"}, "status": {}}], "r", "p", "s", 0)
    assert fallback_node(snapshot) == "x"


class Partial:
    name = "partial"

    def scores(self, snapshot):
        # "w1" missing: read as zero it could outrank a legitimately negative score.
        return {"w2": -5.0, "w3": -7.0}


class Absurd:
    name = "absurd"

    def scores(self, snapshot):
        return {n["name"]: MAX_RAW_SCORE * 10 for n in snapshot["nodes"]}


class Negative:
    name = "negative"

    def scores(self, snapshot):
        return {"w1": -1.0, "w2": 0.0, "w3": -2.0}


def test_partial_score_map_falls_back(pod, nodes):
    decision = Decider(Partial()).decide(pod, nodes)
    assert decision.fallback_reason == REASON_INVALID_SCORE


def test_absurd_score_falls_back(pod, nodes):
    decision = Decider(Absurd()).decide(pod, nodes)
    assert decision.fallback_reason == REASON_INVALID_SCORE


def test_complete_negative_scores_are_accepted(pod, nodes):
    decision = Decider(Negative()).decide(pod, nodes)
    assert not decision.fallback
    assert decision.node == "w2"
    assert decision.normalized["w3"] == 0


def test_duration_covers_normalisation(pod, nodes):
    decision = Decider(Negative()).decide(pod, nodes)
    assert decision.normalize_duration_ms > 0
    assert decision.duration_ms >= decision.strategy_duration_ms + decision.normalize_duration_ms


def test_tie_count_is_recorded(pod, nodes):
    decision = Decider(Working()).decide(pod, nodes)
    assert decision.tie_count == 1
    scores = {"a": 1.0, "b": 1.0}
    assert set(normalize(scores).values()) == {10}


def test_resource_aware_strategy_is_refused_by_the_extender():
    from strategies import get_strategy, missing_requirements

    assert missing_requirements(get_strategy("least-allocated")) == frozenset({"requested"})
    assert missing_requirements(get_strategy("dummy-random")) == frozenset()


class FakeRequests:
    """A requests source, as bridge/cluster.py provides on a real cluster."""

    def __init__(self, totals):
        self.totals = totals

    def get(self, node):
        return self.totals.get(node)


def test_resource_aware_strategy_works_once_requests_are_available(pod, nodes):
    from strategies import get_strategy

    totals = {
        "w1": {"millicpu": 3800, "memory_bytes": 2**33, "pods": 9, "age_seconds": 1.0},
        "w2": {"millicpu": 500, "memory_bytes": 2**30, "pods": 1, "age_seconds": 1.0},
        "w3": {"millicpu": 7800, "memory_bytes": 2**33, "pods": 20, "age_seconds": 1.0},
    }
    decision = Decider(get_strategy("least-allocated"), requests=FakeRequests(totals)).decide(pod, nodes)

    assert not decision.fallback, decision.fallback_reason
    assert decision.node == "w2"


def test_resource_aware_strategy_falls_back_when_requests_are_missing(pod, nodes):
    from strategies import get_strategy

    decision = Decider(get_strategy("least-allocated")).decide(pod, nodes)
    assert decision.fallback_reason == f"{REASON_STRATEGY_ERROR}:ValueError"
