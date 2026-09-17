import cluster
from cluster import ApiRequestsSource, aggregate


def pod(node, phase="Running", cpu="500m", memory="256Mi", init=None):
    spec = {"containers": [{"resources": {"requests": {"cpu": cpu, "memory": memory}}}]}
    if init:
        spec["initContainers"] = [{"resources": {"requests": {"cpu": init[0], "memory": init[1]}}}]
    return {"node": node, "phase": phase, "spec": spec}


def test_requests_are_summed_per_node():
    totals = aggregate([pod("a"), pod("a", cpu="1", memory="1Gi"), pod("b")])
    assert totals["a"] == {"millicpu": 1500, "memory_bytes": 1342177280, "pods": 2}
    assert totals["b"]["pods"] == 1


def test_terminal_and_unscheduled_pods_hold_nothing():
    totals = aggregate([pod("a", phase="Succeeded"), pod("a", phase="Failed"), pod(None)])
    assert totals == {}


def test_init_containers_count_as_a_maximum_not_a_sum():
    totals = aggregate([pod("a", cpu="500m", memory="256Mi", init=("2", "1Gi"))])
    assert totals["a"]["millicpu"] == 2000
    assert totals["a"]["memory_bytes"] == 1073741824


def test_source_reports_nothing_before_the_first_refresh():
    assert ApiRequestsSource().get("a") is None


def test_source_reports_age_and_zero_for_an_empty_node(monkeypatch):
    source = ApiRequestsSource()
    monkeypatch.setattr(cluster.time, "monotonic", lambda: 100.0)
    source._totals = {"a": {"millicpu": 250, "memory_bytes": 128, "pods": 1}}
    source._updated_at = 99.0

    assert source.get("a") == {"millicpu": 250, "memory_bytes": 128, "pods": 1, "age_seconds": 1.0}
    assert source.get("unknown")["millicpu"] == 0


def test_stale_data_is_refused_rather_than_used(monkeypatch):
    source = ApiRequestsSource(max_age_seconds=5)
    monkeypatch.setattr(cluster.time, "monotonic", lambda: 100.0)
    source._totals = {"a": {"millicpu": 250, "memory_bytes": 128, "pods": 1}}
    source._updated_at = 80.0

    assert source.get("a") is None


def test_pod_requests_match_the_shared_table():
    """The Go plugin walks this same table with the upstream helper."""
    import json
    from pathlib import Path

    from snapshot import pod_requests

    table = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "pod-requests.json").read_text())
    assert table["cases"]
    for case in table["cases"]:
        expected = (case["expected"]["millicpu"], case["expected"]["memory_bytes"])
        assert pod_requests(case["pod"]) == expected, case["name"]


def test_aggregate_uses_the_effective_requests():
    sidecar = {
        "node": "a",
        "phase": "Running",
        "spec": {
            "containers": [{"resources": {"requests": {"cpu": "500m", "memory": "256Mi"}}}],
            "initContainers": [
                {"restartPolicy": "Always", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}
            ],
        },
    }
    assert aggregate([sidecar])["a"]["millicpu"] == 600


def test_node_metrics_are_parsed_into_millicores_and_bytes():
    from telemetry import parse_node_metrics

    usage = parse_node_metrics([
        {"metadata": {"name": "w1"}, "usage": {"cpu": "250m", "memory": "1000Ki"}},
        {"metadata": {"name": "w2"}, "usage": {"cpu": "1500m", "memory": "2Gi"}},
        {"usage": {"cpu": "1"}},
    ])
    assert usage == {
        "w1": {"millicpu": 250, "memory_bytes": 1024000},
        "w2": {"millicpu": 1500, "memory_bytes": 2147483648},
    }


def test_telemetry_reports_nothing_before_the_first_refresh():
    from telemetry import MetricsApiTelemetry

    assert MetricsApiTelemetry().get("w1") is None


def test_stale_telemetry_is_refused(monkeypatch):
    import telemetry as telemetry_module
    from telemetry import MetricsApiTelemetry

    source = MetricsApiTelemetry(max_age_seconds=5)
    monkeypatch.setattr(telemetry_module.time, "monotonic", lambda: 100.0)
    source._usage = {"w1": {"millicpu": 100, "memory_bytes": 128}}
    source._updated_at = 80.0
    assert source.get("w1") is None

    source._updated_at = 98.0
    assert source.get("w1")["age_seconds"] == 2.0
    assert source.get("unknown") is None


def test_snapshot_marks_telemetry_present_only_when_measured():
    from snapshot import FEATURE_TELEMETRY, build

    class Telemetry:
        def get(self, node):
            if node == "w1":
                return {"millicpu": 900, "memory_bytes": 4096, "age_seconds": 1.5}
            return None

    nodes = [
        {"metadata": {"name": "w1"}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}},
        {"metadata": {"name": "w2"}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}},
    ]
    snapshot = build({"metadata": {}}, nodes, "r", "p", "s", 0, telemetry=Telemetry())

    measured, absent = snapshot["nodes"]
    assert measured["used_millicpu"] == 900
    assert FEATURE_TELEMETRY not in measured["missing"]
    assert measured["metrics_age_seconds"] == 1.5
    assert FEATURE_TELEMETRY in absent["missing"]
    assert absent["used_millicpu"] == 0


def test_least_used_needs_telemetry_and_ranks_on_it():
    from decision import Decider
    from strategies import get_strategy, missing_requirements

    assert missing_requirements(get_strategy("least-used")) == frozenset({"telemetry"})

    class Telemetry:
        def get(self, node):
            return {"millicpu": 3800 if node == "w1" else 200,
                    "memory_bytes": 2**30, "age_seconds": 0.5}

    nodes = [
        {"metadata": {"name": "w1"}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}},
        {"metadata": {"name": "w2"}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}},
    ]
    pod = {"metadata": {"name": "p", "namespace": "default", "uid": "u"},
           "spec": {"containers": [{"resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}}

    decision = Decider(get_strategy("least-used"), telemetry=Telemetry()).decide(pod, nodes)
    assert not decision.fallback, decision.fallback_reason
    assert decision.node == "w2"

    without = Decider(get_strategy("least-used")).decide(pod, nodes)
    assert without.fallback_reason.startswith("strategy_error")
