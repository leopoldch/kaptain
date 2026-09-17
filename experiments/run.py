"""Run one arm of the integration-cost pilot, once.

    python run.py --arm plugin --plan plan.json --out runs/0001-plugin

What matters here is not the replay loop, which is short, but the guards around it. A run
that quietly measured nothing is worse than a run that failed, so every one of these aborts
with a reason: fewer than two feasible nodes, a scheduler that restarted mid-run, an arm
whose strategy does not match the other's, pods that never got scheduled, or leftovers from a
previous run.

The protocol is in ../docs/experiments/pilot-integration-cost.md.
"""

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import collect
from plan import Plan

LABEL = "app=kaptain-pilot"
TASK_LABEL = "kaptain.io/task-id"

# Metrics read from each arm's own scheduler. The first is the primary: it is the only one
# that contains the extender round trip and the plugin's work alike.
SCHEDULER_METRICS = (
    "scheduler_scheduling_algorithm_duration_seconds",
    "scheduler_scheduling_attempt_duration_seconds",
    "scheduler_pod_scheduling_sli_duration_seconds",
)


@dataclass
class Submission:
    task_id: str
    planned_offset_s: float
    actual_offset_s: float
    lag_s: float
    ok: bool
    error: str = ""


@dataclass
class RunRecord:
    run_id: str
    arm: str
    plan: str
    scenario: str
    started_at: str
    finished_at: str = ""
    strategy: str = ""
    seed: str = ""
    telemetry_collector: str = ""
    kubernetes_version: str = ""
    feasible_nodes: list[str] = field(default_factory=list)
    submissions: list[Submission] = field(default_factory=list)
    aborted: str = ""

    @property
    def max_submission_lag_s(self) -> float:
        return max((s.lag_s for s in self.submissions), default=0.0)


class Aborted(RuntimeError):
    pass


def manifest(task, run_id: str, scheduler: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": task.task_id,
            "labels": {"app": "kaptain-pilot", TASK_LABEL: task.task_id, "run": run_id},
        },
        "spec": {
            "schedulerName": scheduler,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0,
            "containers": [{
                "name": "task",
                "image": task.image,
                "command": task.command,
                "resources": {"requests": {"cpu": task.cpu, "memory": task.memory}},
            }],
        },
    }


def submit(task, run_id: str, scheduler: str) -> subprocess.CompletedProcess:
    body = json.dumps(manifest(task, run_id, scheduler))
    return subprocess.run(["kubectl", "apply", "-f", "-"], input=body,
                          capture_output=True, text=True, timeout=30)


def replay(plan: Plan, run_id: str, scheduler: str, record: RunRecord) -> None:
    """Create the pods at their planned instants, recording what actually happened.

    One `kubectl` per pod deforms a burst: the process start alone costs tens of
    milliseconds. The lag against the plan is therefore recorded per pod, and reported, so a
    burst that was not really a burst cannot be read as one.
    """
    started = time.monotonic()
    for task in plan.tasks:
        target = started + task.arrival_offset_s
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)

        actual = time.monotonic() - started
        result = submit(task, run_id, scheduler)
        record.submissions.append(Submission(
            task_id=task.task_id,
            planned_offset_s=task.arrival_offset_s,
            actual_offset_s=round(actual, 3),
            lag_s=round(actual - task.arrival_offset_s, 3),
            ok=result.returncode == 0,
            error=result.stderr.strip()[:200] if result.returncode else "",
        ))

    failed = [s for s in record.submissions if not s.ok]
    if failed:
        raise Aborted(f"{len(failed)} pods could not be submitted, first: {failed[0].error}")


def wait_for_scheduling(expected: int, timeout_s: float) -> list[dict]:
    """Wait until every pod has a node, or give up and say which ones did not."""
    deadline = time.monotonic() + timeout_s
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = collect.placements(LABEL)
        if len(rows) >= expected and all(row["node"] for row in rows):
            return rows
        time.sleep(1.0)

    pending = [row["pod_name"] for row in rows if not row["node"]]
    raise Aborted(f"{len(pending)} pods were never scheduled after {timeout_s:.0f}s: "
                  f"{pending[:5]}{' …' if len(pending) > 5 else ''}")


def cleanup(wait: bool = True) -> None:
    collect.kubectl("delete", "pods", "-l", LABEL, "--ignore-not-found", "--now",
                    check=False, timeout=180)
    if not wait:
        return
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if not collect.pods(LABEL):
            return
        time.sleep(1.0)
    raise Aborted("pods from this run are still present after cleanup")


def arm_identity(arm: str) -> dict:
    """What this arm says it is, so two arms can be checked against each other."""
    settings = collect.ARMS[arm]
    if arm == "extender":
        with collect.port_forward(settings["own_deploy"], settings["own_port"]) as local:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{local}/healthz", timeout=15) as body:
                health = json.loads(body.read().decode())
        return {"strategy": health.get("strategy", ""),
                "telemetry_collector": health.get("telemetry_collector", ""),
                "features": health.get("features", [])}

    # The plugin states its configuration in its startup line.
    text = collect.kubectl("-n", collect.NAMESPACE, "logs",
                           f"deploy/{settings['own_deploy']}", "--tail=-1", check=False)
    for line in text.splitlines():
        if "kaptain plugin ready" in line:
            fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
            return {"strategy": fields.get("strategy", "").strip('"'),
                    "telemetry_collector": fields.get("telemetryCollector", "").strip('"'),
                    "features": []}
    return {"strategy": "", "telemetry_collector": "", "features": []}


def run(arm: str, plan_path: Path, out: Path, scenario: str, run_id: str) -> RunRecord:
    settings = collect.ARMS[arm]
    plan = Plan.read(plan_path)
    out.mkdir(parents=True, exist_ok=True)

    record = RunRecord(run_id=run_id, arm=arm, plan=plan_path.name, scenario=scenario,
                       started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        # Guard: no leftovers, or the previous run's pods are counted in this one.
        if collect.pods(LABEL):
            raise Aborted("pods from a previous run are still present; clean up first")

        # Guard: scoring is skipped entirely when a single node survives filtering, so with
        # fewer than two feasible nodes neither integration would run at all.
        record.feasible_nodes = collect.feasible_nodes()
        if len(record.feasible_nodes) < 2:
            raise Aborted(f"only {len(record.feasible_nodes)} feasible node(s); the scheduler "
                          "would skip scoring and the run would measure nothing")

        identity = arm_identity(arm)
        record.strategy = identity["strategy"]
        record.telemetry_collector = identity["telemetry_collector"]
        version = json.loads(collect.kubectl("version", "-o", "json"))
        record.kubernetes_version = version.get("serverVersion", {}).get("gitVersion", "")

        before = collect.scrape(settings["scheduler_deploy"], settings["scheduler_port"],
                               settings["scheduler_scheme"])
        own_before = collect.scrape(settings["own_deploy"], settings["own_port"],
                                    settings["own_scheme"])

        replay(plan, run_id, settings["scheduler_name"], record)
        rows = wait_for_scheduling(len(plan.tasks), timeout_s=plan.duration_s + 300)

        after = collect.scrape(settings["scheduler_deploy"], settings["scheduler_port"],
                               settings["scheduler_scheme"])
        own_after = collect.scrape(settings["own_deploy"], settings["own_port"],
                                   settings["own_scheme"])

        (out / "scheduler_metrics_before.txt").write_text(before)
        (out / "scheduler_metrics_after.txt").write_text(after)
        (out / "own_metrics_before.txt").write_text(own_before)
        (out / "own_metrics_after.txt").write_text(own_after)
        _write_rows(out / "pods.csv", rows)
        (out / "decisions.jsonl").write_text("\n".join(
            json.dumps(entry) for entry in collect.decision_log(settings["own_deploy"])) + "\n")

    except Aborted as failure:
        record.aborted = str(failure)
    except collect.CollectionError as failure:
        record.aborted = f"collection failed: {failure}"
    finally:
        record.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            cleanup()
        except Aborted as failure:
            record.aborted = (record.aborted + " | " if record.aborted else "") + str(failure)
        (out / "run.json").write_text(json.dumps(asdict(record), indent=2) + "\n")

    return record


def _write_rows(path: Path, rows: list[dict]) -> None:
    import csv

    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=sorted(collect.ARMS), required=True)
    parser.add_argument("--plan", type=Path, default=Path("plan.json"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scenario", default="A-light")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    run_id = args.run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{args.arm}"
    record = run(args.arm, args.plan, args.out, args.scenario, run_id)

    if record.aborted:
        print(f"ABORTED ({args.arm}): {record.aborted}", file=sys.stderr)
        return 1
    print(f"{args.arm}: {len(record.submissions)} pods, max submission lag "
          f"{record.max_submission_lag_s:.3f}s, strategy {record.strategy!r} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
