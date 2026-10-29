# Kaptain, the paper's approach without the LLM tags, learning online as DRS does.
#
# 1. Runtime estimates. A task is its workload label. For each task and node, the log-runtime
#    is a Gaussian (mean mu, variance v), updated by every pod that finishes. A pair never run
#    starts from the node where the task ran most, with more uncertainty.
# 2. Policy. Each node becomes a vector: its load, its size, the estimate for the incoming pod
#    and the work left on it. One network embeds every node, their mean g sums up the cluster,
#    and one network scores each node from its embedding and g, so any node count fits. The
#    critic, with its own embedding, values g. It samples its nodes during a training run (an
#    episode) and learns once the run is over, in the background: PPO with generalized
#    advantages. A pod labelled kaptain.io/phase=eval or valid freezes it: it picks its best
#    node and learns nothing (the estimates still do).
# 3. Time, not decisions, discounts: gamma^t with a half-life of HALF_LIFE_S. The reward of a
#    decision is -(C + beta E) / 100 + lam (gamma^dt Phi(s') - Phi(s)) until the next one, C the
#    unfinished pods x seconds and E the CPU-seconds used while a pod is unfinished, both
#    discounted to the decision: the idle cluster between two runs is not the policy's doing.
#    The last decision's runs until the run's last pod finished, and ends the episode. Phi is
#    minus the predicted time left of the running pods, per 100, or with -llm a score an LLM
#    gives the cluster state, in the background: the decision never waits, the update waits for
#    the score. The weights are in the name: kaptain-b0.5-l1, kaptain-l4-llm, kaptain (both zero).
#    E is what the experiment pods used: the CPU time (usr + sys) stress-ng reports for each,
#    spread evenly over its run.
# 4. Between two runs kexp settles the policy: the run that ended is learned from before the next
#    one starts, so a frozen run uses one model throughout; a valid run starts from the
#    training's runtime estimates, and the training goes on from them afterwards.
# This is a new variant: Kaptain trained with one-step A2C, discounted per decision, until
# f53745f. Estimates and weights live in the decider's memory: a redeployment starts from
# scratch. Every update logs the weights (kaptain_checkpoint), and kexp collects them.
# KAPTAIN_SEED seeds the networks and the sampling: independent trainings of one policy.

import base64
import collections
import copy
import io
import json
import logging
import math
import os
import random
import re
import ssl
import statistics
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
FROZEN = ("eval", "valid")  # phases that only use the policy: the evaluation, and checkpoints along the training
HALF_LIFE_S = 240.0  # of the discount: about a disk task on the Pi, which a shorter one would let look cheap
GAE = 0.95  # generalized advantage estimation, per decision
EPOCHS = 8  # passes over an episode at each update
CLIP = 0.2  # PPO's bound on the change of a decision's probability
MAX_GRADIENT = 1.0  # norm
FLAT = 0.05  # probabilities this close together: the policy does not tell the nodes apart
# stress-ng --metrics-brief: "stressor bogo-ops real-time usr-time sys-time ..." (as workloads/kexp.py reads it).
STRESSOR_LINE = re.compile(r"\]\s+(?:cpu|vm|hdd)\s+\d+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)")
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


def millicores(text):
    return float(text[:-1]) if text.endswith("m") else float(text) * 1000


class Cluster:
    """Every 2 s, lists the experiment pods: counts the unfinished ones (a sample: time, seconds
    since the last, count), keeps the running ones, and feeds the runtime of each finished one
    to the estimates. With cpu, reads in the background the CPU time each finished pod reports."""

    def __init__(self, cpu=False):
        self.estimates = Estimates()
        self.samples = collections.deque(maxlen=20000)  # 11 h: longer than any run
        self.unfinished = 0
        self.running = []  # (task, node, started)
        self.seen = set()
        self.cpu = ThreadPoolExecutor(max_workers=2) if cpu else None
        self.reading = []  # the CPU reads not finished yet
        self.used = collections.deque(maxlen=20000)  # (namespace, started, finished, CPU-seconds, measured)
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
                self.samples.append((now, now - last, self.unfinished))
            last = now

    def refresh(self):
        """Reads the pods now, not at the next tick: a run's last pods count before the next run."""
        pods = self.poll()
        with self.lock:
            self.read(pods)

    def get(self, path):
        url = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}{path}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {open(f'{ACCOUNT}/token').read()}"})
        context = ssl.create_default_context(cafile=f"{ACCOUNT}/ca.crt")
        with urllib.request.urlopen(request, context=context, timeout=5) as answer:
            return answer.read()

    def poll(self):
        return json.loads(self.get(f"/api/v1/pods?labelSelector={WORKLOAD}"))["items"]

    def used_cpu(self, namespace, name, started, finished, limit):
        """What the pod used, from its log; without the line, its limit for its whole run."""
        try:
            found = STRESSOR_LINE.search(self.get(f"/api/v1/namespaces/{namespace}/pods/{name}/log").decode())
        except Exception as error:
            log.warning(json.dumps({"event": "kaptain_log_failed", "pod": name, "error": str(error)}))
            found = None
        seconds = float(found[1]) + float(found[2]) if found else limit / 1000 * (finished - started)
        with self.lock:
            self.used.append((namespace, started, finished, seconds, found is not None))

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
                if self.cpu:
                    limit = millicores(pod["spec"]["containers"][0].get("resources", {}).get("limits", {}).get("cpu", "0"))
                    self.reading = [f for f in self.reading if not f.done()]
                    self.reading.append(self.cpu.submit(
                        self.used_cpu, pod["metadata"]["namespace"], pod["metadata"]["name"],
                        stamp(done["startedAt"]), stamp(done["finishedAt"]), limit))

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


class EarliestFinish(SchedulingStrategy):
    """Shortest estimate, seeing the load: the pod's estimated runtime plus the work left on the
    node shared over its cores. The baseline Kaptain must beat when contention matters."""

    name = "earliest-finish"

    def __init__(self):
        self.cluster = None

    def scores(self, snapshot: dict) -> dict[str, float]:
        if self.cluster is None:
            self.cluster = Cluster()
        task, now = (snapshot["pod"].get("labels") or {}).get(WORKLOAD), time.time()
        scores = {}
        with self.cluster.lock:
            for n in snapshot["nodes"]:
                estimate = self.cluster.estimates.get(task, n["name"])
                if not (estimate and estimate[2]):
                    scores[n["name"]] = 0.0  # never run there: tried first, as by shortest-estimate
                    continue
                queue = self.cluster.time_left(n["name"], now) / (n["allocatable_millicpu"] / 1000)
                scores[n["name"]] = -(math.exp(estimate[0]) + queue)
        return scores


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


def value(phi):
    """Phi, or None when the LLM failed: that transition goes without its bonus."""
    if not isinstance(phi, Future):
        return phi
    try:
        return phi.result()
    except Exception as error:
        log.warning(json.dumps({"event": "kaptain_llm_failed", "error": str(error)}))
        return None


def discounted(samples, start, end, kappa):
    """Integral over [start, end] of what the samples count, discounted to start at rate kappa."""
    total = 0.0
    for t, dt, count in samples:
        a, b = max(t - dt, start), min(t, end)
        if count and b > a:
            total += count * (math.exp(-kappa * (a - start)) - math.exp(-kappa * (b - start))) / kappa
    return total


class Kaptain(SchedulingStrategy):
    def __init__(self, name="kaptain", half_life_s=HALF_LIFE_S, lr=3e-3, hidden=16, entropy=0.01, seed=7):
        import torch  # here, so that the other strategies never load it
        from torch import nn

        weights = VARIANT.fullmatch(name)
        self.name = name
        self.beta = float(weights[1] or 0)  # weight of the CPU-seconds
        self.lam = float(weights[2] or 0)  # weight of the Phi bonus
        if weights[3] and not self.lam:
            raise ValueError(f"{name}: -llm scores the bonus, which lambda = 0 turns off")
        self.llm = LLM() if weights[3] else None
        self.seed = int(os.environ.get("KAPTAIN_SEED") or seed)
        torch.manual_seed(self.seed)
        torch.set_num_threads(1)  # the master is a shared 4 vCPU VPS
        self.torch, self.kappa, self.entropy = torch, math.log(2) / half_life_s, entropy
        self.random = random.Random(self.seed)  # breaks the ties of a frozen policy

        def embedding():
            return nn.Sequential(nn.Linear(14, hidden), nn.ReLU(), nn.Linear(hidden, hidden), nn.ReLU())

        # The actor and the critic each embed the nodes: the critic's errors no longer reshape
        # what the actor sees of them.
        self.net = nn.ModuleDict({"embed": embedding(), "critic_embed": embedding(),
                                  "score": nn.Sequential(nn.Linear(2 * hidden + 1, hidden), nn.ReLU(), nn.Linear(hidden, 1)),
                                  "critic": nn.Sequential(nn.Linear(hidden + 1, hidden), nn.ReLU(), nn.Linear(hidden, 1))})
        # The learner trains its own copy in the background, then hands its weights over.
        self.trained = copy.deepcopy(self.net)
        self.optimizer = torch.optim.Adam(self.trained.parameters(), lr=lr)
        self.learner = ThreadPoolExecutor(max_workers=1)  # one episode at a time, in order
        self.episodes = 0
        with torch.no_grad():  # the first pass is slow; not on a decision
            self.logits(self.net, torch.zeros(2, 14), 0)
        self.cluster = None  # started on the first decision: the health check runs without a cluster
        self.episode = []  # the decisions of the training run under way
        self.update = None  # the update under way, if any
        self.failure = None  # the first update that failed: every settle refuses from then on
        self.kept = None  # the training's runtime estimates, while valid runs use a copy
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

    def summary(self, h, unfinished):
        return self.torch.cat([h.mean(0), self.torch.tensor([math.log1p(unfinished)])])

    def logits(self, net, rows, unfinished):
        h = net["embed"](rows)
        return net["score"](self.torch.cat([h, self.summary(h, unfinished).expand(len(h), -1)], 1)).squeeze(1)

    def worth(self, net, rows, unfinished):
        return net["critic"](self.summary(net["critic_embed"](rows), unfinished))[0]

    def scores(self, snapshot: dict) -> dict[str, float]:
        with self.lock:
            if self.cluster is None:
                self.cluster = Cluster(cpu=self.beta > 0)
            torch, now, pod = self.torch, time.time(), snapshot["pod"]
            labels = pod.get("labels") or {}
            frozen = labels.get(PHASE) in FROZEN
            # Without settle (a run kexp did not start): kexp gives every run its own namespace.
            if self.episode and (frozen or self.episode[0]["namespace"] != pod["namespace"]):
                self.close(now)
            with self.cluster.lock:
                state = {"rows": torch.tensor([self.node_vector(n, pod, labels.get(WORKLOAD), now)
                                               for n in snapshot["nodes"]]),
                         "unfinished": self.cluster.unfinished, "time": now, "namespace": pod["namespace"], "phi": 0.0}
                if self.lam and not frozen:
                    state["phi"] = (self.llm.phi(self.describe(snapshot, now)) if self.llm
                                    else -self.cluster.time_left(None, now) / SCALE)
            with torch.no_grad():
                logits = self.logits(self.net, state["rows"], state["unfinished"])
            probabilities = torch.softmax(logits, 0)
            if frozen:  # its best node; among equals, any: never the first one listed
                best = float(logits.max())
                action = self.random.choice([i for i, x in enumerate(logits.tolist()) if best - x < 1e-6])
            else:
                action = int(torch.distributions.Categorical(logits=logits).sample())
                state.update(action=action, log_probability=float(torch.log(probabilities[action])))
                self.episode.append(state)
            chosen = snapshot["nodes"][action]["name"]
            spread = float(probabilities.max() - probabilities.min())
            # What the policy thought of every node, also when frozen: an argmax hides how close the others were.
            log.info(json.dumps({"event": "kaptain_decision", "strategy": self.name, "seed": self.seed,
                                 "namespace": pod["namespace"], "pod": pod.get("name"), "frozen": frozen,
                                 "chosen": chosen, "flat": spread < FLAT, "episodes": self.episodes,
                                 "probabilities": {n["name"]: round(p, 4) for n, p in
                                                   zip(snapshot["nodes"], probabilities.tolist())}}))
        return {n["name"]: 1.0 if n["name"] == chosen else 0.0 for n in snapshot["nodes"]}

    def settle(self, phase):
        """Between two runs (kexp), never on a decision: learns from the training run that ended
        and waits for it, so the next run uses one model throughout. A valid run starts from the
        training's runtime estimates and leaves them as they were: measuring must not teach the
        training. Any other run goes on from them."""
        if self.cluster is None:
            return {"settled": True, "episodes": self.episodes}
        self.cluster.refresh()
        with self.lock:
            if self.episode:
                self.close(time.time())
            update = self.update
        if update:
            update.result()  # raises what the update raised
        if self.failure:  # also one that ran without settle: no run goes on from a model not learned
            raise RuntimeError(f"an update failed: {self.failure}")
        with self.cluster.lock:
            if self.kept is not None:
                self.cluster.estimates.known = copy.deepcopy(self.kept)
            if phase == "valid":
                if self.kept is None:
                    self.kept = copy.deepcopy(self.cluster.estimates.known)
            elif phase:
                self.kept = None
        return {"settled": True, "episodes": self.episodes, "estimates_kept": self.kept is not None}

    def close(self, now):
        """Ends the training run: its last pod has finished. Learns from it in the background,
        once the CPU of its pods is read."""
        episode, self.episode = self.episode, []
        with self.cluster.lock:
            samples = [s for s in self.cluster.samples if s[0] > episode[0]["time"]]
            reading = list(self.cluster.reading)
        self.update = self.learner.submit(self.learn_safely, episode, samples, reading, now)

    def learn_safely(self, episode, samples, reading, end):
        try:
            for read in reading:
                read.result()
            with self.cluster.lock:
                used = [u for u in self.cluster.used if u[0] == episode[0]["namespace"]]
            self.learn(episode, samples, used, end)
        except Exception as error:  # an executor keeps exceptions to itself: settle raises them
            self.failure = self.failure or f"{type(error).__name__}: {error}"
            log.exception(json.dumps({"event": "kaptain_learn_failed", "error": str(error)}))
            raise

    def transitions(self, episode, samples, used, end):
        """Per decision: the cost until the next one, discounted to it, and the discount to the next.
        The last one runs until the last pod finished, then the episode ends: its whole tail counts,
        discounted as any other cost, and nothing follows."""
        # The CPU each pod used, as a constant rate over its run: (finished, seconds, rate).
        rates = [(finished, finished - started, seconds / max(finished - started, 1.0))
                 for _, started, finished, seconds, _ in used]
        out = []
        for k, state in enumerate(episode):
            last = k == len(episode) - 1
            following = None if last else episode[k + 1]
            start, stop = state["time"], end if last else following["time"]
            delay = discounted(samples, start, stop, self.kappa)  # pods x seconds
            cpu = discounted(rates, start, stop, self.kappa) if self.beta else 0.0  # CPU-seconds of the pods
            discount = 0.0 if last else math.exp(-self.kappa * (stop - start))
            phi, following_phi = value(state["phi"]), 0.0 if last else value(following["phi"])  # Phi is zero once over
            bonus = 0.0 if phi is None or following_phi is None else self.lam * (discount * following_phi - phi)
            out.append({"reward": -(delay + self.beta * cpu) / SCALE + bonus, "discount": discount,
                        "delay": delay, "cpu": cpu, "bonus": bonus, "phi": phi})
        return out

    def learn(self, episode, samples, used, end):
        """PPO on one episode: generalized advantages, normalized; clipped policy steps and a Huber
        critic, a few epochs over the episode, the actor's and the critic's gradients clipped apart."""
        torch, net, started = self.torch, self.trained, time.perf_counter()
        steps = self.transitions(episode, samples, used, end)
        parts = {"actor": [*net["embed"].parameters(), *net["score"].parameters()],
                 "critic": [*net["critic_embed"].parameters(), *net["critic"].parameters()]}
        with torch.no_grad():
            values = [float(self.worth(net, s["rows"], s["unfinished"])) for s in episode] + [0.0]
        advantages, running = [0.0] * len(steps), 0.0
        for k in reversed(range(len(steps))):
            step = steps[k]
            delta = step["reward"] + step["discount"] * values[k + 1] - values[k]
            running = delta + step["discount"] * GAE * running
            advantages[k] = running
        returns = torch.tensor([a + v for a, v in zip(advantages, values)])
        advantage = torch.tensor(advantages)
        if len(advantage) > 1:
            advantage = (advantage - advantage.mean()) / (advantage.std() + 1e-8)
        actions = torch.tensor([s["action"] for s in episode])
        old = torch.tensor([s["log_probability"] for s in episode])

        norms, clipped = {part: [] for part in parts}, 0.0
        for _ in range(EPOCHS):
            dists = [torch.distributions.Categorical(logits=self.logits(net, s["rows"], s["unfinished"])) for s in episode]
            new = torch.stack([d.log_prob(a) for d, a in zip(dists, actions)])
            entropy = torch.stack([d.entropy() for d in dists]).mean()
            ratio = torch.exp(new - old)
            policy_loss = -torch.min(ratio * advantage, ratio.clamp(1 - CLIP, 1 + CLIP) * advantage).mean()
            predicted = torch.stack([self.worth(net, s["rows"], s["unfinished"]) for s in episode])
            value_loss = torch.nn.functional.smooth_l1_loss(predicted, returns)
            loss = policy_loss + value_loss - self.entropy * entropy
            self.optimizer.zero_grad()
            loss.backward()
            for part, parameters in parts.items():  # a large critic gradient no longer shrinks the actor's
                norms[part].append(float(torch.nn.utils.clip_grad_norm_(parameters, MAX_GRADIENT)))
            self.optimizer.step()
            clipped = float(((ratio - 1).abs() > CLIP).float().mean())

        with torch.no_grad():
            rows = torch.cat([s["rows"] for s in episode])
            # Units that no node of the episode turns on: a ReLU that never fires stops learning.
            silent = {part: float((net[part](rows) <= 0).all(0).float().mean()) for part in ("embed", "critic_embed")}
            spreads = [float(p.max() - p.min()) for p in
                       (torch.softmax(self.logits(net, s["rows"], s["unfinished"]), 0) for s in episode)]
        with self.lock:
            self.net.load_state_dict(net.state_dict())
            self.episodes += 1
        for state, step, a, v in zip(episode, steps, advantages, values):
            log.info(json.dumps({"event": "kaptain_step", "strategy": self.name, "namespace": state["namespace"],
                                 "reward": round(step["reward"], 4), "delay_task_s": round(step["delay"], 1),
                                 "cpu_s": round(step["cpu"], 1), "phi": step["phi"], "bonus": round(step["bonus"], 4),
                                 "discount": round(step["discount"], 4), "value": round(v, 4),
                                 "advantage": round(a, 4)}))
        log.info(json.dumps({"event": "kaptain_update", "strategy": self.name, "seed": self.seed,
                             "namespace": episode[0]["namespace"], "episode": self.episodes, "decisions": len(episode),
                             "return": round(float(returns[0]), 3), "last_reward": round(steps[-1]["reward"], 3),
                             "policy_loss": round(policy_loss.item(), 4), "value_loss": round(value_loss.item(), 4),
                             "entropy": round(entropy.item(), 4), "clipped": round(clipped, 3),
                             "gradient_norm": {part: [round(x, 3) for x in n] for part, n in norms.items()},
                             "silent_units": silent, "cpu_unmeasured": sum(1 for u in used if not u[4]),
                             "spread": round(statistics.median(spreads), 4),
                             "flat": round(sum(s < FLAT for s in spreads) / len(spreads), 3),
                             "seconds": round(time.perf_counter() - started, 2)}))
        # The weights, in the log that kexp collects: a redeployment loses them otherwise.
        buffer = io.BytesIO()
        torch.save(net.state_dict(), buffer)
        log.info(json.dumps({"event": "kaptain_checkpoint", "strategy": self.name, "seed": self.seed,
                             "episode": self.episodes, "namespace": episode[0]["namespace"],
                             "state_dict": base64.b64encode(buffer.getvalue()).decode()}))
