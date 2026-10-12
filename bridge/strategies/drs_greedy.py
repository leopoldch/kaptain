# A one-step oracle for DRS's own objective: no learning, same inputs, same reward. It is the
# positive control of the reproduction (reproduction-drs, nuit du 11 octobre): if it does not
# balance the bench better than Random and Round Robin, an agent trained on that reward is unlikely to.
#
# Like DRS, it breaks the Kaptain rule of pure functions on purpose: it reads the DRS monitors.

import json
import logging
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from helpers import names, one_hot

from .base import SchedulingStrategy
from .drs import METRICS, load_config, normalise, reward

log = logging.getLogger("kaptain.decider")


def predicted(node_states, index, pod_vector):
    """The states DRS would see if the pod ran on node `index`: its six values added, capped at 100."""
    return [[min(value + extra, 100.0) for value, extra in zip(state, pod_vector)] if i == index else state
            for i, state in enumerate(node_states)]


class DRSGreedy(SchedulingStrategy):
    """Places each pod on the node that maximises DRS's reward one step ahead, from DRS's state:
    the monitors' six percentages per node plus the pod's measured vector (E14)."""

    name = "drs-greedy"

    def __init__(self):
        self.config = load_config()
        self.pool = ThreadPoolExecutor(max_workers=max(len(self.config["monitors"]), 1))

    def fetch(self, node):
        url = self.config["monitors"][node]
        with urllib.request.urlopen(url, timeout=self.config["monitor_timeout_s"]) as response:
            return json.load(response)

    def scores(self, snapshot: dict) -> dict[str, float]:
        candidates = names(snapshot)
        if not candidates:
            raise ValueError("no candidate nodes")
        monitors = self.config["monitors"]
        if not monitors:
            raise ValueError("drs-greedy reads the DRS monitors: set them in DRS_CONFIG")
        nodes = sorted(monitors)
        pod = snapshot["pod"]

        started = time.perf_counter()
        raws = list(self.pool.map(self.fetch, nodes))
        communication_ms = 1000 * (time.perf_counter() - started)
        node_states = [normalise(raw, self.config["caps"]) for raw in raws]
        workload = (pod.get("labels") or {}).get(self.config["workload_label"], "")
        pod_vector = list(self.config["pod_vectors"].get(workload, [0.0] * len(METRICS)))

        # Only nodes Kubernetes kept: unlike DRS, the oracle never picks a filtered-out node.
        rewards = {node: reward(predicted(node_states, i, pod_vector), self.config)[0]
                   for i, node in enumerate(nodes) if node in candidates}
        if not rewards:
            raise ValueError(f"no DRS monitor among the candidates {candidates}")
        chosen = max(sorted(rewards), key=rewards.__getitem__)  # ties: the first node by name

        log.info(json.dumps({
            "event": "drs_greedy_decision", "time": time.time(), "task_id": pod["task_id"],
            "workload": workload, "node": chosen, "predicted_rewards": rewards,
            "state": [value for state in node_states for value in state] + pod_vector,
            "communication_ms": round(communication_ms, 3),
        }))
        return one_hot(chosen, candidates)
