"""One placement decision: build the snapshot, run the strategy, validate it, fall back
explicitly, log it, count it.

A failing strategy must never silently become the default scheduler's choice. The Go plugin
does the same thing at the Score extension point; both label their results with
`integration`, so extender and plugin runs can be separated in the analysis.
"""

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field

import metrics
import snapshot as snap
from fallback import NAME as FALLBACK_NAME, fallback_scores

# Bounds what a strategy may return, mirroring maxRawScore in the Go plugin.
MAX_RAW_SCORE = snap.MAX_RAW_SCORE

REASON_STRATEGY_ERROR = "strategy_error"
REASON_INVALID_CHOICE = "invalid_choice"
REASON_INVALID_SCORE = "invalid_score"
REASON_NO_CANDIDATES = "no_candidates"

log = logging.getLogger("kaptain.decision")

counters: dict[str, int] = {}


def _count(key: str) -> None:
    counters[key] = counters.get(key, 0) + 1


@dataclass
class Decision:
    node: str
    scores: dict[str, float]
    normalized: dict[str, int]
    candidate_count: int
    tie_count: int
    requests_age_ms: float
    duration_ms: float
    snapshot_duration_ms: float = 0.0
    reserved: bool = False
    reserved_reason: str | None = None
    strategy_duration_ms: float = 0.0
    normalize_duration_ms: float = 0.0
    fallback_reason: str | None = None

    @property
    def fallback(self) -> bool:
        return self.fallback_reason is not None


@dataclass
class Decider:
    strategy: object
    requests: object | None = None
    telemetry: object | None = None
    run_id: str = field(default_factory=lambda: os.getenv("RUN_ID", "unset"))
    policy_version: str = field(default_factory=lambda: os.getenv("POLICY_VERSION", "unset"))
    seed: int = field(default_factory=lambda: int(os.getenv("KAPTAIN_SEED", "0")))

    def decide(self, pod: dict, nodes: list[dict]) -> Decision:
        started = time.perf_counter()
        snapshot_started = time.perf_counter()
        current = snap.build(pod, nodes, self.run_id, self.policy_version,
                             self.strategy.name, self.seed, self.requests, self.telemetry)
        snapshot_ms = (time.perf_counter() - snapshot_started) * 1000
        candidates = snap.names(current)
        name = self.strategy.name

        metrics.snapshot_duration.labels(name, "ok").observe(snapshot_ms / 1000)
        metrics.candidate_nodes.labels(name).observe(len(candidates))
        for node in current["nodes"]:
            for absent in node.get("missing", []):
                metrics.missing_features_total.labels(absent).inc()
            if node.get("metrics_age_seconds"):
                metrics.cache_age_seconds.labels(snap.FEATURE_TELEMETRY).observe(node["metrics_age_seconds"])

        strategy_started = time.perf_counter()
        reason = None
        try:
            scores = self.strategy.scores(current)
        except Exception as exc:
            scores, reason = None, f"{REASON_STRATEGY_ERROR}:{type(exc).__name__}"
            log.warning("strategy %s failed: %s", self.strategy.name, exc)
        strategy_ms = (time.perf_counter() - strategy_started) * 1000

        if reason is None and not _valid(current, scores):
            reason = REASON_INVALID_SCORE
            metrics.invalid_decision_total.labels(name, REASON_INVALID_SCORE).inc()
        metrics.decider_duration.labels(name, "local", "error" if reason else "ok").observe(strategy_ms / 1000)

        if reason is not None:
            scores = fallback_scores(current)

        node = snap.best(candidates, scores)
        if not node:
            reason = reason or REASON_INVALID_CHOICE
            _count(f"invalid_decision_total:{REASON_INVALID_CHOICE}")
            metrics.invalid_decision_total.labels(name, REASON_INVALID_CHOICE).inc()
            node = candidates[0]

        normalize_started = time.perf_counter()
        normalized = normalize(scores)
        normalize_ms = (time.perf_counter() - normalize_started) * 1000
        top = max(normalized.values())

        age_ms = max((n.get("metrics_age_seconds", 0.0) for n in current["nodes"]), default=0.0) * 1000

        decision = Decision(
            node=node,
            scores=scores,
            normalized=normalized,
            candidate_count=len(candidates),
            tie_count=sum(1 for score in normalized.values() if score == top),
            requests_age_ms=age_ms,
            snapshot_duration_ms=snapshot_ms,
            duration_ms=(time.perf_counter() - started) * 1000,
            strategy_duration_ms=strategy_ms,
            normalize_duration_ms=normalize_ms,
            fallback_reason=reason,
        )
        self._reserve(current, decision)

        self._record(current, decision)
        return decision

    def _reserve(self, current: dict, decision: Decision) -> None:
        """Hold the pod's requests against the chosen node, but only when we know it.

        The extender scores; the scheduler places. When several nodes come out with the same
        normalised score, Kubernetes picks among them at random, so the node we preferred is
        not necessarily the node the pod lands on. Reserving it anyway would charge a load to
        a node that never received the pod, and the next decisions would run on a fiction.
        In that case we reserve nothing and say so: the next refresh will show the truth.
        """
        reserve = getattr(self.requests, "reserve", None)
        if reserve is None:
            return
        if decision.tie_count > 1:
            decision.reserved_reason = "tie"
            _count("reservation_skipped_total:tie")
            return
        reserve(current["pod"]["uid"], decision.node,
                current["pod"]["requested_millicpu"],
                current["pod"]["requested_memory_bytes"])
        decision.reserved = True

    def _record(self, current: dict, decision: Decision) -> None:
        _count("decisions_total")
        name = current["strategy"]
        status = "fallback" if decision.fallback else "ok"
        metrics.decisions_total.labels(name, status).inc()
        metrics.normalize_duration.labels(name).observe(decision.normalize_duration_ms / 1000)
        metrics.score_duration.labels(name, snap.INTEGRATION, status).observe(decision.duration_ms / 1000)
        if decision.fallback:
            _count(f"fallback_total:{decision.fallback_reason}")
            metrics.fallback_total.labels(name, decision.fallback_reason.split(":")[0]).inc()
        log.info(json.dumps({
            "run_id": current["run_id"],
            "integration": snap.INTEGRATION,
            "strategy": current["strategy"],
            "policy_version": current["policy_version"],
            "pod_uid": current["pod"]["uid"],
            "namespace": current["pod"]["namespace"],
            "pod_name": current["pod"]["name"],
            "candidate_count": decision.candidate_count,
            "requests_age_ms": round(decision.requests_age_ms, 3),
            "intended_node": decision.node,
            "tie_count": decision.tie_count,
            "score": round(decision.scores.get(decision.node, 0.0), 3),
            "fallback": decision.fallback,
            "fallback_reason": decision.fallback_reason,
            "fallback_strategy": FALLBACK_NAME if decision.fallback else None,
            "reserved": decision.reserved,
            "reserved_skipped_reason": decision.reserved_reason,
            "duration_ms": round(decision.duration_ms, 3),
            "durations_ms": {
                "snapshot": round(decision.snapshot_duration_ms, 3),
                "strategy": round(decision.strategy_duration_ms, 3),
                "normalize": round(decision.normalize_duration_ms, 3),
                "total": round(decision.duration_ms, 3),
            },
            "event": "decision",
        }))


def _valid(current: dict, scores: dict[str, float] | None) -> bool:
    """Reject anything that would make the ranking meaningless.

    Completeness matters: a missing candidate would be read as zero and could outrank a
    node that was legitimately given a negative score. Same rule as `validScores` in the
    Go plugin.
    """
    if not scores or len(scores) != len(current["nodes"]):
        return False
    for node in current["nodes"]:
        value = scores.get(node["name"])
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        if math.isnan(value) or math.isinf(value) or abs(value) > MAX_RAW_SCORE:
            return False
    return True


def normalize(scores: dict[str, float]) -> dict[str, int]:
    """Quantise the raw scores onto the extender scale, keeping the ordering.

    Identical arithmetic to `QuantizeRaw` in the Go plugin, applied to the same raw values,
    so both paths produce the same scores, the same winner and the same ties. Kubernetes
    then multiplies our score by MaxNodeScore/MaxExtenderPriority, which is what the plugin
    does itself.
    """
    lowest, highest = min(scores.values()), max(scores.values())
    return {
        name: snap.quantize_raw(value, lowest, highest) for name, value in scores.items()
    }
