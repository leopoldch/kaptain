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


def row(arm, pair_id, mean=None, strategy="dummy-random", aborted="", scenario="A"):
    entry = {"run_id": f"{arm}-{pair_id}", "arm": arm, "pair_id": pair_id,
             "scenario": scenario, "strategy": strategy, "aborted": aborted}
    if mean is not None:
        entry["algorithm_mean_ms"] = mean
        entry["algorithm_unresolved_share"] = 0.1
    return entry


def test_pairing_refuses_arms_with_different_strategies():
    paired = analyse.pair([row("extender", "1", 3.0),
                           row("plugin", "1", 1.0, strategy="least-allocated")])
    assert len(paired) == 1 and "strategies differ" in paired[0]["aborted"]


def test_pairing_uses_the_pair_id_not_the_order():
    """A failed run must not shift every later pair onto the wrong counterpart."""
    rows = [
        row("extender", "1", 3.0),
        row("plugin", "1", aborted="only 1 node had room"),
        row("extender", "2", 3.4),
        row("plugin", "2", 1.2),
        row("extender", "3", 2.8),
        row("plugin", "3", 1.1),
    ]
    paired = analyse.pair(rows)
    by_id = {entry["pair_id"]: entry for entry in paired}

    assert by_id["1"]["aborted"].startswith("plugin:")
    # Pair 2 is still pair 2: extender 3.4 against plugin 1.2, not against 1.1.
    assert by_id["2"]["extender_mean_ms"] == 3.4 and by_id["2"]["plugin_mean_ms"] == 1.2
    assert by_id["3"]["difference_ms"] == 1.7


def test_a_missing_arm_is_an_unusable_pair():
    paired = analyse.pair([row("extender", "1", 3.0)])
    assert paired[0]["aborted"] == "the plugin arm of this pair is missing"


def test_a_missing_mean_never_becomes_zero():
    """Reading a missing mean as 0 would invent a difference the size of the other arm."""
    paired = analyse.pair([row("extender", "1", 3.0), row("plugin", "1")])
    assert paired[0]["aborted"] == "a mean is missing on one arm"
    assert "difference_ms" not in paired[0]


def test_paired_difference_and_interval():
    rows = []
    for index, (extender, plugin) in enumerate([(3.0, 1.0), (3.4, 1.2), (2.8, 1.1)]):
        rows.append(row("extender", str(index), extender))
        rows.append(row("plugin", str(index), plugin))

    paired = analyse.pair(rows)
    assert [p["difference_ms"] for p in paired] == [2.0, 2.2, 1.7]

    summary = analyse.interval([p["difference_ms"] for p in paired])
    assert summary["pairs"] == 3
    assert summary["low_ms"] < summary["mean_ms"] < summary["high_ms"]


def test_aborted_runs_are_reported_not_averaged(tmp_path: Path):
    directory = tmp_path / "0001-plugin"
    directory.mkdir()
    (directory / "run.json").write_text(json.dumps({
        "run_id": "0001", "arm": "plugin", "pair_id": "1", "scenario": "A",
        "aborted": "only 1 node had room"}))

    summary = analyse.summarise_run(directory)
    assert summary["aborted"] == "only 1 node had room"

    paired = analyse.pair([summary])
    assert len(paired) == 1 and paired[0]["aborted"].startswith("the extender arm")


MULTI_LABEL = """# TYPE scheduler_framework_extension_point_duration_seconds histogram
scheduler_framework_extension_point_duration_seconds_bucket{extension_point="Score",le="0.001"} 10
scheduler_framework_extension_point_duration_seconds_bucket{extension_point="Score",le="+Inf"} 20
scheduler_framework_extension_point_duration_seconds_sum{extension_point="Score"} 0.02
scheduler_framework_extension_point_duration_seconds_count{extension_point="Score"} 20
scheduler_framework_extension_point_duration_seconds_bucket{extension_point="PreScore",le="0.001"} 5
scheduler_framework_extension_point_duration_seconds_bucket{extension_point="PreScore",le="+Inf"} 20
scheduler_framework_extension_point_duration_seconds_sum{extension_point="PreScore"} 0.03
scheduler_framework_extension_point_duration_seconds_count{extension_point="PreScore"} 20
"""


def test_buckets_are_summed_across_label_sets_like_sum_and_count():
    """Overwriting instead of accumulating gave one label set's buckets against everyone's
    count, which quietly moved every quantile."""
    histogram = collect.parse_histogram(
        MULTI_LABEL, "scheduler_framework_extension_point_duration_seconds")

    assert histogram.count == 40
    assert histogram.buckets[0.001] == 15      # 10 + 5, not 5
    assert histogram.buckets[float("inf")] == 40


def test_selecting_one_label_set_keeps_its_own_buckets():
    histogram = collect.parse_histogram(
        MULTI_LABEL, "scheduler_framework_extension_point_duration_seconds",
        {"extension_point": "Score"})
    assert histogram.count == 20 and histogram.buckets[0.001] == 10


def test_a_restart_is_detected_even_when_counters_climbed_past():
    """A restart resets the counters; a busy scheduler then climbs back above the earlier
    values, so the delta looks perfectly sane. The process start time is what settles it."""
    before = "process_start_time_seconds 1000\n" + exposition(3)
    after = "process_start_time_seconds 2500\n" + exposition(5)

    assert collect.process_identity(before) == 1000
    assert not collect.same_process(before, after)
    # And the counters alone would have raised nothing:
    collect.delta(collect.parse_histogram(before, analyse.PRIMARY),
                  collect.parse_histogram(after, analyse.PRIMARY))


def test_same_process_requires_the_marker():
    same = "process_start_time_seconds 1000\n"
    assert collect.same_process(same, same)
    assert not collect.same_process("", same)


def test_quantity_parsing_for_capacity_checks():
    assert collect.quantity("2") == 2
    assert collect.quantity("500m") == 0.5
    assert collect.quantity("64Mi") == 64 * 1024 ** 2
    assert collect.quantity("123456789n") == pytest.approx(0.123456789)
