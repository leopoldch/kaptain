# Kaptain, the paper's approach without the LLM tags, learning online as DRS does.
#
# 1. Runtime estimates. A task is its workload label. For each task and node, the log-runtime
#    is a Gaussian (mean mu, variance v), updated by every pod that finishes. A pair never run
#    starts from the node where the task ran most, with more uncertainty.
# 2. Policy. Each node becomes a vector: its load, its size, the estimate for the incoming pod
#    and the work left on it. One network embeds every node, their mean g sums up the cluster,
#    and one network scores each node from its embedding and g, so any node count fits. The
#    critic values g. Trained with one-step A2C, sampling its nodes. A pod labelled
#    kaptain.io/phase=eval freezes it: it picks its best node and learns nothing (the estimates
#    still do).
# 3. Reward between two decisions: -(C + beta E) / 100 + lam (gamma Phi(s') - Phi(s)), where C
#    is unfinished pods x seconds and E the CPU-seconds used. Phi is minus the predicted time
#    left of the running pods, per 100, or with -llm a score an LLM gives the cluster state, in
#    the background: the decision never waits, its update waits for the score. The weights are
#    in the name: kaptain-b0.5-l1, kaptain-l4-llm, kaptain (both zero).
# Estimates and weights live in the decider's memory: a redeployment starts from scratch.

import json
import logging
import math
import os
import re
import ssl
import threading
import time
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from .base import SchedulingStrategy

log = logging.getLogger("kaptain.decider")

WORKLOAD = "kaptain.io/workload"
SIGMA2 = 0.1  # noise of one log-runtime, about 30%
MOVED_V = 1.0  # extra variance of an estimate taken from another node
UNKNOWN_S = 60.0  # runtime of a task never run anywhere
SCALE = 100.0  # task-seconds per unit of reward
ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
VARIANT = re.compile(r"kaptain(?:-b(\d+(?:\.\d+)?))?(?:-l(\d+(?:\.\d+)?)(-llm)?)?")  # -llm only with a lambda: it gives the Phi of the bonus
PHASE = "kaptain.io/phase"
MAX_WAITING = 100  # decisions whose update waits for the LLM; older ones are dropped
PROMPT = """You judge a Kubernetes cluster that runs batch tasks. From its state, score from 0 to
100 how well it is placed for every task to finish soon: 100 when nothing runs or waits, low
when tasks pile up on slow or busy nodes. Answer with JSON only: {"score": <number>}."""


def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


class Estimates:
    def __init__(self):
        self.known = {}  # (task, node) -> [mu, v, runs]

    def get(self, task, node):
        if (task, node) in self.known:
            return self.known[(task, node)]
        runs = [(n, other) for (t, other), (_, _, n) in self.known.items() if t == task]
        if not runs:
            return None
        mu, v, _ = self.known[(task, max(runs)[1])]
        return [mu, v + MOVED_V, 0]

    def update(self, task, node, runtime_s):
        y = math.log(max(runtime_s, 1.0))
        old = self.get(task, node)
        if old is None:
            self.known[(task, node)] = [y, SIGMA2, 1]
            return
        mu, v, n = old
        self.known[(task, node)] = [(mu * SIGMA2 + y * v) / (v + SIGMA2), v * SIGMA2 / (v + SIGMA2), n + 1]

    def seconds(self, task, node):
        found = self.get(task, node)
        return math.exp(found[0]) if found else UNKNOWN_S


class Cluster:
    """Every 2 s, lists the experiment pods: counts the unfinished ones (area += count x seconds),
    keeps the running ones, and feeds the runtime of each finished one to the estimates."""

    def __init__(self):
        self.estimates = Estimates()
        self.area = 0.0
        self.unfinished = 0
        self.running = []  # (task, node, started)
        self.seen = set()
        self.lock = threading.Lock()
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        last = time.time()
        while True:
            time.sleep(2)
            try:
                pods = self.poll()
            except Exception as error:  # the API missed once: the last state stands
                log.warning(json.dumps({"event": "kaptain_poll_failed", "error": str(error)}))
                pods = None
            now = time.time()
            with self.lock:
                if pods is not None:
                    self.read(pods)
                self.area += self.unfinished * (now - last)
            last = now

    def poll(self):
        url = (f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
               f"/api/v1/pods?labelSelector={WORKLOAD}")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {open(f'{ACCOUNT}/token').read()}"})
        context = ssl.create_default_context(cafile=f"{ACCOUNT}/ca.crt")
        with urllib.request.urlopen(request, context=context, timeout=5) as answer:
            return json.load(answer)["items"]

    def read(self, pods):
        self.unfinished, self.running = 0, []
        for pod in pods:
            task, node = pod["metadata"]["labels"][WORKLOAD], pod["spec"].get("nodeName")
            phase = pod["status"].get("phase")
            if phase not in ("Succeeded", "Failed"):
                self.unfinished += 1
            states = [s.get("state", {}) for s in pod["status"].get("containerStatuses", [])]
            if phase == "Running" and states and "running" in states[0]:
                self.running.append((task, node, stamp(states[0]["running"]["startedAt"])))
            uid = pod["metadata"]["uid"]
            if phase == "Succeeded" and uid not in self.seen and states and "terminated" in states[0]:
                self.seen.add(uid)
                done = states[0]["terminated"]
                self.estimates.update(task, node, stamp(done["finishedAt"]) - stamp(done["startedAt"]))

    def time_left(self, node, now):
        return sum(max(self.estimates.seconds(task, on) - (now - started), 0.0)
                   for task, on, started in self.running if node is None or on == node)


class ShortestEstimate(SchedulingStrategy):
    """The paper's simple rule: the node with the shortest estimated runtime for the pod."""

    name = "shortest-estimate"

    def __init__(self):
        self.cluster = None  # started on the first decision: the health check runs without a cluster

    def scores(self, snapshot: dict) -> dict[str, float]:
        if self.cluster is None:
            self.cluster = Cluster()
        task = (snapshot["pod"].get("labels") or {}).get(WORKLOAD)
        with self.cluster.lock:
            known = {n["name"]: self.cluster.estimates.get(task, n["name"]) for n in snapshot["nodes"]}
        # A node where the task never ran is tried first, otherwise a copied estimate is never checked.
        return {name: -math.exp(e[0]) if e and e[2] else 0.0 for name, e in known.items()}


class LLM:
    """Scores cluster states in the background. Phi = (score - 100) / 10, from -10 to 0."""

    def __init__(self):
        self.key = os.environ["OPENAI_API_KEY"]  # missing: the deployment fails, not a decision
        self.model = os.environ.get("OPENAI_MODEL", "gpt-5.4-nano")
        self.pool = ThreadPoolExecutor(max_workers=4)

    def phi(self, text):
        return self.pool.submit(self.ask, text)

    def ask(self, text):
        body = {"model": self.model, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}]}
        request = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {self.key}",
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as answer:
            content = json.load(answer)["choices"][0]["message"]["content"]
        score = min(max(float(json.loads(content)["score"]), 0.0), 100.0)
        log.info(json.dumps({"event": "kaptain_llm", "score": score}))
        return (score - 100) / 10


def ready(phi):
    return not isinstance(phi, Future) or phi.done()


def value(phi):
    """Phi, or None when the LLM failed: that update goes without its bonus."""
    if not isinstance(phi, Future):
        return phi
    try:
        return phi.result()
    except Exception as error:
        log.warning(json.dumps({"event": "kaptain_llm_failed", "error": str(error)}))
        return None


class Kaptain(SchedulingStrategy):
    def __init__(self, name="kaptain", gamma=0.95, lr=1e-2, hidden=16, entropy=0.01, seed=7):
        import torch  # here, so that the other strategies never load it
        from torch import nn

        weights = VARIANT.fullmatch(name)
        self.name = name
        self.beta = float(weights[1] or 0)  # weight of the CPU-seconds
        self.lam = float(weights[2] or 0)  # weight of the Phi bonus
        if weights[3] and not self.lam:
            raise ValueError(f"{name}: -llm scores the bonus, which lambda = 0 turns off")
        self.llm = LLM() if weights[3] else None
        torch.manual_seed(seed)
        torch.set_num_threads(1)  # the master is a shared 4 vCPU VPS
        self.torch, self.gamma, self.entropy = torch, gamma, entropy
        self.embed = nn.Sequential(nn.Linear(14, hidden), nn.ReLU(), nn.Linear(hidden, hidden), nn.ReLU())
        self.score = nn.Sequential(nn.Linear(2 * hidden + 1, hidden), nn.ReLU(), nn.Linear(hidden, 1))
        self.critic = nn.Sequential(nn.Linear(hidden + 1, hidden), nn.ReLU(), nn.Linear(hidden, 1))
        networks = [*self.embed.parameters(), *self.score.parameters(), *self.critic.parameters()]
        self.optimizer = torch.optim.Adam(networks, lr=lr)
        # One pass without a step: torch's first backward takes ~50 ms, the plugin's whole timeout.
        h, g = self.summary(torch.zeros(2, 14), 0)
        torch.distributions.Categorical(logits=self.logits(h, g)).entropy().backward()
        self.optimizer.zero_grad()
        self.cluster = None  # started on the first decision: the health check runs without a cluster
        self.last = None  # the previous decision's state, until the next one completes it
        self.waiting = []  # (state, next state) pairs not learned from yet
        self.lock = threading.Lock()  # FastAPI may call scores() from two threads at once

    def node_vector(self, n, pod, task, now):
        cpu, memory = n["allocatable_millicpu"], n["allocatable_memory_bytes"]
        estimate = self.cluster.estimates.get(task, n["name"])
        mu, v, runs = estimate if estimate else (0.0, 0.0, 0)
        return [n["requested_millicpu"] / cpu, n["requested_memory_bytes"] / memory,
                n["used_millicpu"] / cpu, n["used_memory_bytes"] / memory,
                math.log(cpu / 1000), math.log(memory / 2**30),
                1.0 if estimate else 0.0, mu, v, math.log1p(runs),
                math.log1p(self.cluster.time_left(n["name"], now)),
                pod["requested_millicpu"] / 1000, pod["requested_memory_bytes"] / 2**30,
                math.log1p(sum(1 for _, on, _ in self.cluster.running if on == n["name"]))]

    def describe(self, snapshot, now):
        """The cluster state as the LLM reads it."""
        lines = [f"Unfinished tasks: {self.cluster.unfinished}"]
        for n in snapshot["nodes"]:
            running = [f"{task} ({max(self.cluster.estimates.seconds(task, on) - (now - started), 0):.0f} s left)"
                       for task, on, started in self.cluster.running if on == n["name"]]
            lines.append(f"{n['name']}: {n['allocatable_millicpu'] / 1000:g} cores, "
                         f"CPU {100 * n['used_millicpu'] / n['allocatable_millicpu']:.0f}% used, "
                         f"memory {100 * n['used_memory_bytes'] / n['allocatable_memory_bytes']:.0f}% used; "
                         f"running: {', '.join(running) or 'nothing'}")
        return "\n".join(lines)

    def summary(self, rows, unfinished):
        h = self.embed(rows)
        return h, self.torch.cat([h.mean(0), self.torch.tensor([math.log1p(unfinished)])])

    def logits(self, h, g):
        return self.score(self.torch.cat([h, g.expand(len(h), -1)], 1)).squeeze(1)

    def scores(self, snapshot: dict) -> dict[str, float]:
        with self.lock:
            if self.cluster is None:
                self.cluster = Cluster()
            torch, now, pod = self.torch, time.time(), snapshot["pod"]
            labels = pod.get("labels") or {}
            frozen = labels.get(PHASE) == "eval"
            with self.cluster.lock:
                state = {"rows": torch.tensor([self.node_vector(n, pod, labels.get(WORKLOAD), now)
                                               for n in snapshot["nodes"]]),
                         "unfinished": self.cluster.unfinished, "area": self.cluster.area, "time": now,
                         "namespace": pod["namespace"],
                         "cpu_used": sum(n["used_millicpu"] for n in snapshot["nodes"]) / 1000, "phi": 0.0}
                if self.lam and not frozen:
                    state["phi"] = (self.llm.phi(self.describe(snapshot, now)) if self.llm
                                    else -self.cluster.time_left(None, now) / SCALE)
            if frozen:
                self.last = None
            else:
                if self.last:
                    self.waiting.append((self.last, state))
                self.learn_ready()
                self.last = state

            with torch.no_grad():
                logits = self.logits(*self.summary(state["rows"], state["unfinished"]))
            action = int(logits.argmax()) if frozen else int(torch.distributions.Categorical(logits=logits).sample())
            state["action"] = action
            chosen = snapshot["nodes"][action]["name"]
        return {n["name"]: 1.0 if n["name"] == chosen else 0.0 for n in snapshot["nodes"]}

    def learn_ready(self):
        """Learns from the waiting decisions in order, up to the first whose Phi is not there yet."""
        if len(self.waiting) > MAX_WAITING:
            log.warning(json.dumps({"event": "kaptain_dropped", "decisions": len(self.waiting) - MAX_WAITING}))
            del self.waiting[:-MAX_WAITING]
        while self.waiting and ready(self.waiting[0][0]["phi"]) and ready(self.waiting[0][1]["phi"]):
            self.learn(*self.waiting.pop(0))

    def learn(self, last, state):
        """One A2C step on one decision: advantage = r + gamma V(s') - V(s)."""
        torch = self.torch
        elapsed = state["time"] - last["time"]
        done = state["namespace"] != last["namespace"]  # kexp gives every run its own namespace
        delay = state["area"] - last["area"]
        cpu = (last["cpu_used"] + state["cpu_used"]) / 2 * elapsed
        phi, following_phi = value(last["phi"]), 0.0 if done else value(state["phi"])  # Phi is zero once the run is over
        bonus = 0.0 if phi is None or following_phi is None else self.lam * (self.gamma * following_phi - phi)
        reward = -(delay + self.beta * cpu) / SCALE + bonus

        # Recomputed with the current weights: other updates may have run since that decision.
        h, g = self.summary(last["rows"], last["unfinished"])
        dist = torch.distributions.Categorical(logits=self.logits(h, g))
        with torch.no_grad():
            following = 0.0 if done else self.critic(self.summary(state["rows"], state["unfinished"])[1])[0]
        advantage = reward + self.gamma * following - self.critic(g)[0]
        action = torch.tensor(last["action"])
        loss = -dist.log_prob(action) * advantage.detach() + advantage.pow(2) - self.entropy * dist.entropy()
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        log.info(json.dumps({"event": "kaptain_step", "strategy": self.name, "reward": round(reward, 4),
                             "delay_task_s": round(delay, 1), "cpu_s": round(cpu, 1), "bonus": round(bonus, 4),
                             "advantage": round(float(advantage), 4), "done": done}))
