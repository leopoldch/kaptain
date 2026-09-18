"""The submission plan: generated once, replayed by every run.

This is the "dataset" of the integration-cost pilot. It is deliberately synthetic and tiny:
the question is what a decision costs, not how well it places, so the workload only has to be
identical across arms and to keep at least two nodes feasible at all times.

A plan is a list of tasks, each with a stable `task_id`. Both integrations key their seeded
draw on that id, so the same plan places the same pods on the same nodes on both arms, and
any difference left is the integration.
"""

import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Task:
    task_id: str
    arrival_offset_s: float
    image: str
    cpu: str
    memory: str
    command: list[str]


@dataclass
class Plan:
    name: str
    seed: int
    tasks: list[Task]

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(
            {"name": self.name, "seed": self.seed, "tasks": [asdict(t) for t in self.tasks]},
            indent=2,
        ) + "\n")

    @staticmethod
    def read(path: Path) -> "Plan":
        document = json.loads(Path(path).read_text())
        return Plan(
            name=document["name"],
            seed=document["seed"],
            tasks=[Task(**task) for task in document["tasks"]],
        )

    @property
    def duration_s(self) -> float:
        return max((task.arrival_offset_s for task in self.tasks), default=0.0)


# Small pods on purpose: several must fit on every worker, so that filtering never leaves a
# single feasible node. With one candidate, kube-scheduler skips scoring entirely and neither
# integration runs -- the run would measure nothing and say nothing about it.
DEFAULT_IMAGE = "busybox:1.36"
DEFAULT_CPU = "10m"
DEFAULT_MEMORY = "16Mi"
DEFAULT_LIFETIME_S = 60


def steady_then_bursts(
    name: str = "pilot",
    seed: int = 7,
    steady_count: int = 30,
    steady_interval_s: float = 1.0,
    bursts: int = 3,
    burst_size: int = 10,
    burst_gap_s: float = 10.0,
    lifetime_s: int = DEFAULT_LIFETIME_S,
) -> Plan:
    """A steady rate, then identical bursts.

    The steady phase measures the decision cost with no queueing; the bursts are where
    queueing, reservations and the freshness difference between the two paths show up.
    """
    tasks: list[Task] = []
    offset = 0.0

    for index in range(steady_count):
        tasks.append(_task(f"{name}-steady-{index:03d}", offset, lifetime_s))
        offset += steady_interval_s

    for burst in range(bursts):
        offset += burst_gap_s
        for index in range(burst_size):
            # Identical arrival instant: the burst is meant to arrive at once.
            tasks.append(_task(f"{name}-burst{burst}-{index:03d}", offset, lifetime_s))

    return Plan(name=name, seed=seed, tasks=tasks)


def _task(task_id: str, offset: float, lifetime_s: int) -> Task:
    return Task(
        task_id=task_id,
        arrival_offset_s=round(offset, 3),
        image=DEFAULT_IMAGE,
        cpu=DEFAULT_CPU,
        memory=DEFAULT_MEMORY,
        command=["sleep", str(lifetime_s)],
    )


def background_load(nodes: int, cpu: str = "500m", memory: str = "256Mi") -> list[dict]:
    """A fixed stress-ng pod per worker, for scenario B.

    On kind these workers share one machine, so this is host-wide contention rather than
    per-node load. It is a control, not a heterogeneity: stress-ng's own documentation says
    it is not a precise benchmark suite.
    """
    return [
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"kaptain-load-{index}",
                "labels": {"app": "kaptain-load"},
            },
            "spec": {
                "containers": [{
                    "name": "stress",
                    "image": "polinux/stress-ng:latest",
                    "args": ["--cpu", "1", "--cpu-load", "60", "--timeout", "0"],
                    "resources": {"requests": {"cpu": cpu, "memory": memory}},
                }],
            },
        }
        for index in range(nodes)
    ]


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "plan.json")
    plan = steady_then_bursts()
    plan.write(target)
    print(f"{len(plan.tasks)} tasks over {plan.duration_s:.0f}s -> {target}")
    random.seed(plan.seed)  # reserved: future plans may randomise sizes
