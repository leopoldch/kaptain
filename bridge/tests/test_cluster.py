import os
import time
from types import SimpleNamespace

import cluster
from cluster import ApiRequestsSource, aggregate, placed_uids


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
        {"metadata": {"name": "w1"}, "timestamp": "2026-09-17T12:00:00Z",
         "usage": {"cpu": "250m", "memory": "1000Ki"}},
        {"metadata": {"name": "w2"}, "timestamp": "2026-09-17T12:00:00Z",
         "usage": {"cpu": "1500m", "memory": "2Gi"}},
        {"usage": {"cpu": "1"}},
    ])
    assert set(usage) == {"w1", "w2"}
    assert usage["w1"]["millicpu"] == 250
    assert usage["w1"]["memory_bytes"] == 1024000
    assert usage["w2"]["millicpu"] == 1500
    assert usage["w2"]["memory_bytes"] == 2147483648


def test_telemetry_reports_nothing_before_the_first_refresh():
    from telemetry import MetricsApiTelemetry

    assert MetricsApiTelemetry().get("w1") is None


def test_telemetry_age_comes_from_the_measurement_not_the_download(monkeypatch):
    """metrics-server serves a cached sample: a fresh fetch of an old measurement is old."""
    import telemetry as telemetry_module
    from telemetry import MetricsApiTelemetry

    source = MetricsApiTelemetry(max_age_seconds=5)
    monkeypatch.setattr(telemetry_module.time, "time", lambda: 1000.0)
    source._updated_at = 1000.0  # downloaded just now
    source._usage = {
        "fresh": {"millicpu": 100, "memory_bytes": 128, "measured_at": 998.0},
        "stale": {"millicpu": 100, "memory_bytes": 128, "measured_at": 900.0},
        "undated": {"millicpu": 100, "memory_bytes": 128, "measured_at": None},
    }

    assert source.get("fresh")["age_seconds"] == 2.0
    assert source.get("stale") is None
    assert source.get("undated") is None
    assert source.get("unknown") is None


def test_telemetry_timestamp_is_parsed_from_the_payload():
    from telemetry import parse_node_metrics

    usage = parse_node_metrics([
        {"metadata": {"name": "w1"}, "timestamp": "2026-09-17T12:00:00Z",
         "usage": {"cpu": "123456789n", "memory": "1000Ki"}},
        {"metadata": {"name": "w2"}, "usage": {"cpu": "1", "memory": "1Gi"}},
    ])
    assert usage["w1"]["measured_at"] == 1789646400.0
    assert usage["w2"]["measured_at"] is None


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
    assert measured["telemetry_age_seconds"] == 1.5
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


def test_quantities_match_the_apimachinery_reference():
    """testdata/quantities.json is generated by apimachinery; the metrics API reports CPU
    in nanocores, and Kubernetes rounds towards +infinity."""
    import json
    from pathlib import Path

    from snapshot import parse_cpu, parse_memory

    table = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "quantities.json").read_text())
    assert table["cases"]
    for case in table["cases"]:
        assert parse_cpu(case["text"]) == case["millicpu"], case["text"]
        assert parse_memory(case["text"]) == case["bytes"], case["text"]


def test_nanocores_from_the_metrics_api_are_accepted():
    from telemetry import parse_node_metrics

    usage = parse_node_metrics([
        {"metadata": {"name": "w1"}, "timestamp": "2026-09-17T12:00:00Z",
         "usage": {"cpu": "123456789n", "memory": "1000Ki"}},
    ])
    assert usage["w1"]["millicpu"] == 124
    assert usage["w1"]["memory_bytes"] == 1024000


def test_reservation_is_skipped_when_the_placement_is_a_guess():
    from decision import Decider
    from strategies import get_strategy

    class Recorder:
        def __init__(self):
            self.reserved = []

        def get(self, node):
            return {"millicpu": 0, "memory_bytes": 0, "pods": 0, "age_seconds": 0.5}

        def reserve(self, uid, node, millicpu, memory_bytes):
            self.reserved.append((uid, node))

    pod = {"metadata": {"name": "p", "namespace": "default", "uid": "u"},
           "spec": {"containers": [{"resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}}
    tied = [
        {"metadata": {"name": "w1"}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}},
        {"metadata": {"name": "w2"}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}},
    ]
    recorder = Recorder()
    decision = Decider(get_strategy("largest-cpu-capacity"), requests=recorder).decide(pod, tied)

    assert decision.tie_count == 2
    assert not decision.reserved and decision.reserved_reason == "tie"
    assert recorder.reserved == []

    distinct = [
        {"metadata": {"name": "small"}, "status": {"allocatable": {"cpu": "2", "memory": "8Gi"}}},
        {"metadata": {"name": "big"}, "status": {"allocatable": {"cpu": "8", "memory": "8Gi"}}},
    ]
    recorder = Recorder()
    decision = Decider(get_strategy("largest-cpu-capacity"), requests=recorder).decide(pod, distinct)

    assert decision.tie_count == 1 and decision.reserved
    assert recorder.reserved == [("u", "big")]


def api_pod(uid, node, cpu="500m", memory="256Mi", phase="Running"):
    """An object shaped like the API client's pod, for refresh() to consume."""
    container = SimpleNamespace(
        resources=SimpleNamespace(requests={"cpu": cpu, "memory": memory}),
        restart_policy=None,
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(uid=uid),
        spec=SimpleNamespace(node_name=node, containers=[container], init_containers=None, overhead=None),
        status=SimpleNamespace(phase=phase),
    )


class FakeApi:
    def __init__(self, pods):
        self.pods = pods

    def list_pod_for_all_namespaces(self, field_selector=None):
        return SimpleNamespace(items=self.pods)


def test_only_a_placed_pod_releases_its_reservation():
    """A pod exists in the API from creation and sits Pending until its binding lands."""
    assert placed_uids([{"uid": "pending", "node": None}]) == set()
    assert placed_uids([{"uid": "bound", "node": "w1"}]) == {"bound"}
    assert placed_uids([{"node": "w1"}]) == set()


def test_refresh_keeps_the_reservation_of_a_still_pending_pod():
    source = ApiRequestsSource()
    source._api = FakeApi([api_pod("pending-uid", None)])
    source.reserve("pending-uid", "w1", 500, 1024)

    source.refresh()

    # The pod is in the API but has no node yet, so it is in no node's totals: dropping the
    # reservation here would make w1 look free again.
    assert source.get("w1")["millicpu"] == 500
    assert "pending-uid" in source._inflight


def test_refresh_releases_the_reservation_once_the_pod_is_placed():
    source = ApiRequestsSource()
    source._api = FakeApi([api_pod("pending-uid", None)])
    source.reserve("pending-uid", "w1", 500, 1024)
    source.refresh()

    source._api = FakeApi([api_pod("pending-uid", "w1")])
    source.refresh()

    # Now the pod is counted in the totals, once, and not twice.
    assert source.get("w1")["millicpu"] == 500
    assert source._inflight == {}


def test_refresh_releases_a_reservation_when_the_pod_lands_elsewhere():
    source = ApiRequestsSource()
    source._api = FakeApi([api_pod("pod-uid", None)])
    source.reserve("pod-uid", "w1", 500, 1024)
    source.refresh()

    source._api = FakeApi([api_pod("pod-uid", "w2")])
    source.refresh()

    assert source.get("w1")["millicpu"] == 0
    assert source.get("w2")["millicpu"] == 500


def test_open_source_selects_the_collector():
    import pytest as _pytest

    from telemetry import DISABLED, KUBELET, METRICS_API, MetricsApiTelemetry, open_source

    assert open_source("") is None
    assert open_source(DISABLED) is None

    # Reserved, and refused explicitly rather than silently falling back to metrics-server.
    with _pytest.raises(NotImplementedError):
        open_source(KUBELET)

    with _pytest.raises(ValueError):
        open_source("prometheus")

    assert MetricsApiTelemetry.name == METRICS_API


def test_the_two_ages_are_reported_separately():
    """A mixed age cannot say which source is stale, so requests and telemetry keep
    their own field, all the way into the decision log."""
    from decision import Decider
    from snapshot import build
    from strategies import get_strategy

    class Requests:
        def get(self, node):
            return {"millicpu": 100, "memory_bytes": 128, "pods": 1, "age_seconds": 12.0}

    class Telemetry:
        name = "metrics-api"

        def get(self, node):
            return {"millicpu": 900, "memory_bytes": 4096, "age_seconds": 0.5}

    nodes = [{"metadata": {"name": "w1"}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}}]
    snapshot = build({"metadata": {}}, nodes, "r", "p", "s", 0, Requests(), Telemetry())

    node = snapshot["nodes"][0]
    assert node["requests_age_seconds"] == 12.0
    assert node["telemetry_age_seconds"] == 0.5

    pod = {"metadata": {"name": "p", "namespace": "default", "uid": "u"},
           "spec": {"containers": [{"resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}}
    decision = Decider(get_strategy("least-used"), requests=Requests(), telemetry=Telemetry()).decide(pod, nodes)

    assert decision.requests_age_ms == 12000.0
    assert decision.telemetry_age_ms == 500.0


def test_durations_accept_both_unit_styles():
    """The Go plugin and the extender must read the same manifest values the same way."""
    import pytest as _pytest

    from config import duration, parse_duration

    assert parse_duration("2") == parse_duration("2s") == 2.0
    assert parse_duration("500ms") == 0.5
    assert parse_duration("1m30s") == 90.0

    for bad in ("", "2x", "abc", "2 s"):
        with _pytest.raises(ValueError):
            parse_duration(bad)

    monkey = "KAPTAIN_TEST_DURATION"
    os.environ[monkey] = "1500ms"
    assert duration(monkey, "2s") == 1.5
    os.environ[monkey] = "nonsense"
    with _pytest.raises(RuntimeError):
        duration(monkey, "2s")
    os.environ[monkey] = "0"
    with _pytest.raises(RuntimeError):
        duration(monkey, "2s")
    del os.environ[monkey]
