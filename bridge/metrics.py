"""Prometheus metrics for the extender, mirroring the plugin's `kaptain_plugin_*` series.

Same names, same labels, same phases, so a run can compare the two integrations on the same
quantities. Labels stay low cardinality: no pod UID, no node name, no image. Those go to the
decision log.

What this cannot measure is the transport: the scheduler-side cost of calling us. That
number comes from kube-scheduler's own
`scheduler_framework_extension_point_duration_seconds` and must be reported next to ours,
never replaced by the handler duration.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

SUBSYSTEM = "kaptain_extender"

registry = CollectorRegistry()

# Identical to the Go plugin's ExponentialBuckets(0.0001, 2, 14), so the two histograms can
# be read side by side without converting anything.
_SECONDS = tuple(0.0001 * 2 ** power for power in range(14))

# The plugin uses different exponential ranges for the snapshot and normalise histograms;
# mirror them exactly so the two sides can be read side by side.
_SNAPSHOT = tuple(0.00001 * 2 ** power for power in range(14))
_SHORT = tuple(0.00001 * 2 ** power for power in range(12))
_NODES = (1, 2, 3, 5, 10, 25, 50, 100, 250, 500, 1000)
_AGES = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 300)

score_duration = Histogram(f"{SUBSYSTEM}_score_duration_seconds",
                           "Time spent producing the scores for one pod, fallback included.",
                           ["strategy", "integration", "status"], buckets=_SECONDS, registry=registry)
snapshot_duration = Histogram(f"{SUBSYSTEM}_snapshot_duration_seconds",
                              "Time spent building the decision snapshot.",
                              ["strategy", "status"], buckets=_SNAPSHOT, registry=registry)
decider_duration = Histogram(f"{SUBSYSTEM}_decider_duration_seconds",
                             "Time spent in the strategy.",
                             ["strategy", "decider", "status"], buckets=_SECONDS, registry=registry)
normalize_duration = Histogram(f"{SUBSYSTEM}_normalize_duration_seconds",
                               "Time spent quantising the scores.",
                               ["strategy"], buckets=_SHORT, registry=registry)
candidate_nodes = Histogram(f"{SUBSYSTEM}_candidate_nodes",
                            "Number of candidate nodes seen per decision.",
                            ["strategy"], buckets=_NODES, registry=registry)
decisions_total = Counter(f"{SUBSYSTEM}_decisions_total", "Decisions attempted, by outcome.",
                          ["strategy", "status"], registry=registry)
fallback_total = Counter(f"{SUBSYSTEM}_fallback_total",
                         "Decisions where the explicit fallback was used, by reason.",
                         ["strategy", "reason"], registry=registry)
invalid_decision_total = Counter(f"{SUBSYSTEM}_invalid_decision_total",
                                 "Invalid decisions: unknown node, invalid score.",
                                 ["strategy", "reason"], registry=registry)
cache_age_seconds = Histogram(f"{SUBSYSTEM}_cache_age_seconds",
                              "Age of the measurements used in a snapshot, by source.",
                              ["source"], buckets=_AGES, registry=registry)
missing_features_total = Counter(f"{SUBSYSTEM}_missing_features_total",
                                 "Features absent from a snapshot, by feature group.",
                                 ["feature_group"], registry=registry)


def render() -> bytes:
    return generate_latest(registry)
