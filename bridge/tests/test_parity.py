"""Parity with the Go plugin.

Both integrations read `testdata/snapshots/*.json` and must select the same node. The Go
side runs the same fixtures in `plugin/pkg/strategies/strategies_test.go`; if a strategy
changes on one side only, one of the two suites fails.
"""

import json
from pathlib import Path

import pytest

from decision import normalize
from snapshot import best, names
from strategies import get_strategy

FIXTURES = sorted((Path(__file__).resolve().parents[2] / "testdata" / "snapshots").glob("*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_fixtures_exist():
    assert FIXTURES, "no shared snapshot fixtures found"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_expected_node_is_selected(path):
    fixture = load(path)
    snapshot = fixture["snapshot"]
    for strategy_name, expected in fixture["expected"].items():
        scores = get_strategy(strategy_name).scores(snapshot)
        assert best(names(snapshot), scores) == expected, f"{strategy_name} on {path.stem}"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_normalised_scores_match_the_fixture(path):
    """The winner after normalisation is what decides the placement, so pin every score."""
    fixture = load(path)
    snapshot = fixture["snapshot"]
    for strategy_name, wanted in fixture["expected_normalized"].items():
        normalized = normalize(get_strategy(strategy_name).scores(snapshot))
        assert normalized == wanted, f"{strategy_name} on {path.stem}"
        intended = fixture["expected"][strategy_name]
        assert normalized[intended] == max(normalized.values()) == 10


def test_quantisation_table_matches_the_plugin():
    """The Go plugin walks this same table; a divergence breaks one of the two suites."""
    table = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "quantisation.json").read_text())
    assert table["cases"]
    for case in table["cases"]:
        normalized = normalize(case["raw"])
        assert normalized == case["expected"], case["raw"]
        winner = max(sorted(normalized), key=lambda name: normalized[name])
        assert winner == case["winner"], case["raw"]


def test_seeded_draw_matches_the_go_rule():
    # The Go implementation of Pick(["a","b","c"], 7, "pod-1") must give the same node.
    from snapshot import pick

    assert pick(["c", "b", "a"], 7, "pod-1") == pick(["a", "b", "c"], 7, "pod-1")


def test_seeded_draw_survives_pod_recreation():
    from snapshot import pick, task_id

    recreated = {"metadata": {"uid": "uid-2", "namespace": "default", "name": "job-1"}}
    original = {"metadata": {"uid": "uid-1", "namespace": "default", "name": "job-1"}}
    assert task_id(original) == task_id(recreated) == "default/job-1"
    assert pick(["a", "b", "c"], 7, task_id(original)) == pick(["a", "b", "c"], 7, task_id(recreated))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_replay_endpoint_reproduces_the_fixture(path, monkeypatch):
    """`/replay` and `kaptain-replay` must agree on a recorded snapshot: that pairing is
    what isolates decision cost from the two paths' different views of the cluster."""
    from fastapi.testclient import TestClient

    import app as extender

    fixture = load(path)
    for strategy_name, wanted in fixture["expected_normalized"].items():
        monkeypatch.setattr(extender, "strategy", get_strategy(strategy_name))
        response = TestClient(extender.app).post("/replay", json=fixture["snapshot"])
        assert response.status_code == 200
        body = response.json()
        assert body["integration"] == "extender"
        assert body["mode"] == "replay"
        assert body["scale"] == 10
        assert body["normalized"] == wanted
        assert body["intended_node"] == fixture["expected"][strategy_name]
