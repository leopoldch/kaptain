# DRS (Jian et al., Softw. Pract. Exper. 2024), ported from the authors' code
# (github.com/jolyonjian/DRS, commit c6054c9: drs-scheduler/dqn.py and myenv/K8sEnv.py).
#
# This is the one strategy that breaks two Kaptain rules on purpose, because DRS is defined by
# them: it keeps state (an online DQN) and it reads the cluster (the DRS monitors). Every
# departure from the paper or the code is numbered in reproduction-drs/ECARTS-DRS.md (Exx).

import json
import logging
import os
import statistics
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from helpers import names, one_hot

from .base import SchedulingStrategy

log = logging.getLogger("kaptain.decider")

METRICS = ("cpu", "mem", "recv", "tran", "read", "write")

DEFAULTS = {
    # node name -> URL of its DRS monitor. Its sorted order is the action space (E01).
    "monitors": {},
    "monitor_timeout_s": 1.0,
    # Raw value that maps to 100 %. These are the code's constants (CPU x4, network 40 KB/s,
    # disk 10240 KB/s); the calibration replaces them (E13).
    "caps": {"cpu": 25.0, "mem": 100.0, "recv": 40.0, "tran": 40.0, "read": 10240.0, "write": 10240.0},
    # Workload label -> its six normalised values, measured alone on a node (E14). An unknown
    # workload gets zeros, as /choose did for an unknown pod name.
    "pod_vectors": {},
    "workload_label": "kaptain.io/workload",
    # paper: alpha * AvgUtil - beta * Imbalance (eq. 14); code: -(sum of the six std devs) (E11).
    "reward": "paper",
    "alpha": 1.0,
    "beta": 10.0,
    "weights": {m: 1 / 6 for m in METRICS},
    "step_delay_s": 5.0,
    # The code's hyper-parameters (E10).
    "batch_size": 32,
    "lr": 0.01,
    "epsilon": 0.9,  # probability of the greedy action, as in the code
    "gamma": 0.9,
    "target_replace_iter": 100,
    "memory_capacity": 100,
    "hidden": 50,
    "seed": 7,
}


def load_config():
    path = os.getenv("DRS_CONFIG", "/etc/kaptain-drs/config.json")
    config = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as handle:
            for key, value in json.load(handle).items():
                if isinstance(DEFAULTS.get(key), dict):
                    if not isinstance(value, dict):
                        raise ValueError(f"{path}: {key} must be an object, not {value!r}")
                    # Key by key inside caps, weights and pod_vectors: a ConfigMap giving one cap
                    # must not drop the five others, which would fail every decision.
                    value = {**config[key], **value}
                config[key] = value
    return config


def normalise(raw, caps):
    """Six raw monitor values -> percentages capped at 100, as K8sEnv.getState did."""
    return [min(100.0 * raw[m] / caps[m], 100.0) if caps[m] > 0 else 0.0 for m in METRICS]


def reward(node_states, config):
    """node_states: one list of six percentages per node. Returns (reward, avg_util, imbalance)."""
    per_metric = list(zip(*node_states))  # six tuples, one value per node
    avg_util = sum(sum(column) for column in per_metric) / (len(node_states) * len(METRICS)) / 100
    if config["reward"] == "code":
        # K8sEnv.step: network and disk count rx+tx and read+write, all in percent.
        stds = [statistics.pstdev(column) for column in per_metric]
        return -sum(stds), avg_util, sum(stds) / 100
    imbalance = sum(config["weights"][m] * statistics.pstdev(column) / 100
                    for m, column in zip(METRICS, per_metric))
    return config["alpha"] * avg_util - config["beta"] * imbalance, avg_util, imbalance


class Agent:
    """dqn.py's DQN, unchanged except that learn() is called (E09) and seeded (E15)."""

    def __init__(self, n_states, n_actions, config):
        import numpy as np
        import torch
        from torch import nn

        torch.manual_seed(config["seed"])
        torch.set_num_threads(1)  # the master is a shared 4 vCPU VPS
        self.np, self.torch = np, torch
        # One generator per thread that draws: choose() runs on the decision thread, learn() on the
        # step threads, and a shared one would hand out its numbers in whatever order they ran.
        # The states still come from live monitors, so a run is never replayed exactly (E15).
        self.choose_rng = np.random.RandomState(config["seed"])
        self.learn_rng = np.random.RandomState(config["seed"] + 1)
        self.config = config
        self.n_states, self.n_actions = n_states, n_actions

        def net():
            hidden = nn.Linear(n_states, config["hidden"])
            hidden.weight.data.normal_(0, 0.1)
            out = nn.Linear(config["hidden"], n_actions)
            out.weight.data.normal_(0, 0.1)
            return nn.Sequential(hidden, nn.ReLU(), out)

        self.eval_net, self.target_net = net(), net()
        self.optimizer = torch.optim.Adam(self.eval_net.parameters(), lr=config["lr"])
        self.loss_func = nn.MSELoss()
        self.memory = np.zeros((config["memory_capacity"], n_states * 2 + 2))
        self.memory_counter = 0
        self.learn_step_counter = 0

    def choose(self, state):
        x = self.torch.unsqueeze(self.torch.FloatTensor(state), 0)
        with self.torch.no_grad():
            q = self.eval_net(x)[0].tolist()
        if self.choose_rng.uniform() < self.config["epsilon"]:
            return int(max(range(len(q)), key=q.__getitem__)), True, q
        return int(self.choose_rng.randint(0, self.n_actions)), False, q

    def store(self, s, a, r, s_):
        index = self.memory_counter % self.config["memory_capacity"]
        self.memory[index, :] = self.np.hstack((s, [a, r], s_))
        self.memory_counter += 1

    def learn(self):
        torch, config = self.torch, self.config
        if self.learn_step_counter % config["target_replace_iter"] == 0:
            self.target_net.load_state_dict(self.eval_net.state_dict())
        self.learn_step_counter += 1

        n = self.n_states
        sample = self.memory[self.learn_rng.choice(config["memory_capacity"], config["batch_size"]), :]
        b_s = torch.FloatTensor(sample[:, :n])
        b_a = torch.LongTensor(sample[:, n:n + 1].astype(int))
        b_r = torch.FloatTensor(sample[:, n + 1:n + 2])
        b_s_ = torch.FloatTensor(sample[:, -n:])

        q_eval = self.eval_net(b_s).gather(1, b_a)
        q_next = self.target_net(b_s_).detach()
        q_target = b_r + config["gamma"] * q_next.max(1)[0].view(config["batch_size"], 1)
        loss = self.loss_func(q_eval, q_target)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        return float(loss)


class DRS(SchedulingStrategy):
    """Online DQN over six node metrics; picks one node and gives it the only non-zero score."""

    name = "drs"

    def __init__(self):
        # Nothing here may reach the network: deploy-policy.sh checks the image with
        # `import app` in a container without network, and without the ConfigMap.
        self.config = load_config()
        self.lock = threading.Lock()
        self.agent = None
        # Pod UID -> node, so a retried pod keeps its action (E16). Not the task id, which is the
        # same in every run of an experiment: a second run would replay the first one's choices.
        self.actions = {}
        monitors = self.config["monitors"]
        # Separate pools: steps stuck on a silent monitor must not delay the next decision past
        # the plugin's 2 s, whose fallback would count against DRS.
        self.deciding = ThreadPoolExecutor(max_workers=max(len(monitors), 1))
        self.stepping = ThreadPoolExecutor(max_workers=max(len(monitors), 1))
        if monitors:
            # Built now, not on the first pod: importing torch takes seconds, and the plugin
            # gives the decider 2 s before falling back.
            self.agent = Agent(len(METRICS) * (len(monitors) + 1), len(monitors), self.config)

    def nodes(self):
        monitors = self.config["monitors"]
        if not monitors:
            raise ValueError("DRS has no monitors: set them in DRS_CONFIG")
        return sorted(monitors)

    def fetch(self, node):
        url = self.config["monitors"][node]
        with urllib.request.urlopen(url, timeout=self.config["monitor_timeout_s"]) as response:
            return json.load(response)

    def observe(self, nodes, pool):
        """One normalised six-vector per node, queried in parallel. Raises if a monitor is down."""
        raws = list(pool.map(self.fetch, nodes))
        return [normalise(raw, self.config["caps"]) for raw in raws], raws

    def scores(self, snapshot: dict) -> dict[str, float]:
        candidates = names(snapshot)
        if not candidates:
            raise ValueError("no candidate nodes")
        pod = snapshot["pod"]
        task = pod["task_id"]

        with self.lock:
            cached = self.actions.get(pod["uid"])
        if cached is not None:
            log.info(json.dumps({"event": "drs_cached", "task_id": task, "node": cached}))
            return self.one_hot(cached, candidates, task)

        nodes = self.nodes()
        fetch_started = time.perf_counter()
        node_states, raws = self.observe(nodes, self.deciding)
        communication_ms = 1000 * (time.perf_counter() - fetch_started)

        workload = (pod.get("labels") or {}).get(self.config["workload_label"], "")
        pod_vector = list(self.config["pod_vectors"].get(workload, [0.0] * len(METRICS)))
        state = [value for node in node_states for value in node] + pod_vector

        infer_started = time.perf_counter()
        with self.lock:
            if self.agent is None:
                self.agent = Agent(len(state), len(nodes), self.config)
            if self.agent.n_states != len(state):
                raise ValueError(f"state of {len(state)} values for an agent built for {self.agent.n_states}")
            action, greedy, q = self.agent.choose(state)
            self.actions[pod["uid"]] = nodes[action]
        decision_ms = 1000 * (time.perf_counter() - infer_started)
        chosen = nodes[action]

        log.info(json.dumps({
            "event": "drs_decision", "time": time.time(), "task_id": task, "workload": workload,
            "action": action, "node": chosen, "greedy": greedy, "q": q, "state": state,
            "raw": dict(zip(nodes, raws)), "communication_ms": round(communication_ms, 3),
            "decision_ms": round(decision_ms, 3), "candidates": candidates,
        }))
        # The transition is measured whether or not Kubernetes can honour the action: DRS's own
        # environment kept stepping too (E07).
        threading.Thread(target=self.step, args=(task, nodes, state, action, pod_vector), daemon=True).start()
        return self.one_hot(chosen, candidates, task)

    def one_hot(self, chosen, candidates, task):
        if chosen not in candidates:
            # DRS's Filter would leave the pod pending; a Kaptain score cannot, so the plugin's
            # explicit fallback takes over and is counted (E07).
            raise ValueError(f"DRS chose {chosen!r} for {task!r}, which Kubernetes filtered out")
        return one_hot(chosen, candidates)

    def step(self, task, nodes, state, action, pod_vector):
        """K8sEnv.step + makeStep: wait, observe, reward, store, then learn (E09, E12)."""
        time.sleep(self.config["step_delay_s"])
        try:
            node_states, raws = self.observe(nodes, self.stepping)
        except Exception as error:  # a lost transition is logged, never invented
            log.info(json.dumps({"event": "drs_step_error", "task_id": task, "error": repr(error)}))
            return
        next_state = [value for node in node_states for value in node] + pod_vector
        value, avg_util, imbalance = reward(node_states, self.config)

        loss = None
        with self.lock:
            # list(state): the code stored the very list it then overwrote, so s == s' (E12).
            self.agent.store(list(state), action, value, next_state)
            if self.agent.memory_counter > self.config["memory_capacity"]:
                loss = self.agent.learn()
            counter, learn_steps = self.agent.memory_counter, self.agent.learn_step_counter

        log.info(json.dumps({
            "event": "drs_transition", "time": time.time(), "task_id": task, "action": action,
            "reward": value, "avg_util": avg_util, "imbalance": imbalance, "next_state": next_state,
            "raw": dict(zip(nodes, raws)), "memory_counter": counter, "learn_steps": learn_steps,
            "loss": loss,
        }))
