"""Tests for the parts that need no cluster: parsing, quantiles, the plan, the guards."""

import json
from pathlib import Path

import pytest

import analyse
import collect
from plan import Plan, steady_then_bursts

# A scheduler exposition, trimmed to what the analysis reads.
EXPOSITION = """# HELP scheduler_scheduling_algorithm_duration_seconds Scheduling algorithm latency in seconds
# TYPE scheduler_scheduling_algorithm_duration_seconds histogram
scheduler_scheduling_algorithm_duration_seconds_bucket{le="0.001"} 40
scheduler_scheduling_algorithm_duration_seconds_bucket{le="0.002"} 80
scheduler_scheduling_algorithm_duration_seconds_bucket{le="0.004"} 95
scheduler_scheduling_algorithm_duration_seconds_bucket{le="+Inf"} 100
scheduler_scheduling_algorithm_duration_seconds_sum 0.15
scheduler_scheduling_algorithm_duration_seconds_count 100
"""


def exposition(scale: int) -> str:
    """The same histogram, scaled, to build a before/after pair."""
    out = []
    for line in EXPOSITION.splitlines():
        if line.startswith("#"):
            out.append(line)
        elif "_sum" in line:
            name, value = line.rsplit(" ", 1)
            out.append(f"{name} {float(value) * scale}")
        else:
            name, value = line.rsplit(" ", 1)
            out.append(f"{name} {int(float(value) * scale)}")
    return "\n".join(out) + "\n"


def test_histogram_is_parsed():
    histogram = collect.parse_histogram(EXPOSITION, analyse.PRIMARY)
    assert histogram.count == 100
    assert histogram.total == pytest.approx(0.15)
    assert histogram.buckets[0.001] == 40
    assert histogram.mean_s == pytest.approx(0.0015)


def test_delta_is_what_happened_during_the_run():
    before = collect.parse_histogram(exposition(1), analyse.PRIMARY)
    after = collect.parse_histogram(exposition(3), analyse.PRIMARY)
    observed = collect.delta(before, after)

    assert observed.count == 200
    assert observed.total == pytest.approx(0.30)
    assert observed.mean_s == pytest.approx(0.0015)


def test_a_restarted_scheduler_invalidates_the_run():
    """Counters reset on restart, so the delta goes backwards. That is not a small number,
    it is an unusable run, and it must say so rather than produce one."""
    before = collect.parse_histogram(exposition(3), analyse.PRIMARY)
    after = collect.parse_histogram(exposition(1), analyse.PRIMARY)
    with pytest.raises(collect.CollectionError):
        collect.delta(before, after)


def test_quantiles_are_interpolated_from_buckets():
    histogram = collect.parse_histogram(EXPOSITION, analyse.PRIMARY)
    median = collect.quantile(histogram, 0.5)
    # 50th observation sits in the 1-2 ms bucket.
    assert 0.001 < median <= 0.002


def test_a_quantile_inside_the_first_bucket_is_reported_as_unresolved():
    """The native histograms start at 1 ms. Below that, a quantile is not a measurement."""
    fast = EXPOSITION.replace('le="0.001"} 40', 'le="0.001"} 95')
    histogram = collect.parse_histogram(fast, analyse.PRIMARY)

    assert collect.share_below_first_bucket(histogram) == 0.95
    assert analyse._quantile_ms(histogram, 0.5) == "<1.0ms"


def test_plan_replays_identically(tmp_path: Path):
    plan = steady_then_bursts(steady_count=5, bursts=2, burst_size=3)
    path = tmp_path / "plan.json"
    plan.write(path)
    reread = Plan.read(path)

    assert [task.task_id for task in reread.tasks] == [task.task_id for task in plan.tasks]
    assert [task.arrival_offset_s for task in reread.tasks] == [task.arrival_offset_s for task in plan.tasks]
    assert len({task.task_id for task in plan.tasks}) == len(plan.tasks)


def test_bursts_arrive_together():
    plan = steady_then_bursts(steady_count=2, steady_interval_s=1, bursts=1, burst_size=4)
    burst = [task.arrival_offset_s for task in plan.tasks if "burst" in task.task_id]
    assert len(set(burst)) == 1


def test_pairing_refuses_arms_with_different_strategies():
    rows = [
        {"run_id": "a", "arm": "extender", "scenario": "A", "strategy": "dummy-random",
         "algorithm_mean_ms": 3.0, "algorithm_unresolved_share": 0.1, "aborted": ""},
        {"run_id": "b", "arm": "plugin", "scenario": "A", "strategy": "least-allocated",
         "algorithm_mean_ms": 1.0, "algorithm_unresolved_share": 0.1, "aborted": ""},
    ]
    paired = analyse.pair(rows)
    assert len(paired) == 1 and "strategies differ" in paired[0]["aborted"]


def test_paired_difference_and_interval():
    rows = []
    for index, (extender, plugin) in enumerate([(3.0, 1.0), (3.4, 1.2), (2.8, 1.1)]):
        rows.append({"run_id": f"e{index}", "arm": "extender", "scenario": "A",
                     "strategy": "dummy-random", "algorithm_mean_ms": extender,
                     "algorithm_unresolved_share": 0.0, "aborted": ""})
        rows.append({"run_id": f"p{index}", "arm": "plugin", "scenario": "A",
                     "strategy": "dummy-random", "algorithm_mean_ms": plugin,
                     "algorithm_unresolved_share": 0.0, "aborted": ""})

    paired = analyse.pair(rows)
    assert [p["difference_ms"] for p in paired] == [2.0, 2.2, 1.7]

    summary = analyse.interval([p["difference_ms"] for p in paired])
    assert summary["pairs"] == 3
    assert summary["low_ms"] < summary["mean_ms"] < summary["high_ms"]


def test_aborted_runs_are_reported_not_averaged(tmp_path: Path):
    directory = tmp_path / "0001-plugin"
    directory.mkdir()
    (directory / "run.json").write_text(json.dumps({
        "run_id": "0001", "arm": "plugin", "aborted": "only 1 feasible node"}))

    row = analyse.summarise_run(directory)
    assert row["aborted"] == "only 1 feasible node"
    assert analyse.pair([row]) == []
