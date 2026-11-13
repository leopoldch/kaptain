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
#    advantages. A pod labelled kaptain.io/phase=eval, valid or cold freezes it: it picks its best
#    node and learns nothing (the estimates still do). With -ef, the network only corrects
#    earliest-finish: a node's logit is its earliest-finish score over TEMPERATURE_S plus the
#    network's, whose last layer starts at zero, so the untrained policy is earliest-finish and
#    the training learns how far to move from it, instead of starting from a random policy.
# 3. Time, not decisions, discounts: gamma^t with a half-life of HALF_LIFE_S. The reward of a
#    decision is -(C + beta E) / 100 + lam (gamma^dt Phi(s') - Phi(s)) until the next one, C the
#    unfinished pods x seconds and E the CPU-seconds used while a pod is unfinished, both
#    discounted to the decision: the idle cluster between two runs is not the policy's doing.
#    The last decision's runs until the run's last pod finished, and ends the episode. Phi is
#    minus the predicted time left of the running pods, per 100, or with -llm a score an LLM
#    gives the cluster state, in the background: the decision never waits, the update waits for
#    the score. With -placebo, Phi is drawn at random over the LLM's range, -10 to 0: a bonus as
#    large carrying no information, against which the LLM's is compared. The weights are in the
#    name: kaptain-b0.5-l1, kaptain-l4-llm, kaptain-l4-placebo, kaptain (both zero); -ef last.
#    E is what the experiment pods used: the CPU time (usr + sys) stress-ng reports for each,
#    spread evenly over its run.
# 4. Between two runs kexp settles the policy: the run that ended is learned from before the next
#    one starts, so a frozen run uses one model throughout; a valid or cold run starts from the
#    training's runtime estimates, and the training goes on from them afterwards. The same for
#    the estimates of shortest-estimate and earliest-finish, so that a cold run, an evaluation on
#    nodes the training never saw, meets them unknown every time.
# 5. With -pod, a decision is rewarded for the pod it placed, not for the whole cluster:
#    r = -(d + beta e + s) / 100, d the seconds from the decision to the end of its pod, e the
#    CPU-seconds the pod used, s the seconds it cost the pods already on its node: s = L - B, L
#    the time those pods still ran after the decision, B what a network expects them to run when
#    no pod joins them, learned on the nodes the decisions did not choose. Nothing is discounted
#    and nothing carried to the next decision: the advantage is r - V(s). Under 3, every decision
#    gets the cost of every unfinished pod, in which its own node weighs little. beta trades the
#    delay for the CPU: kaptain-pod, kaptain-b1-pod, kaptain-pod-ef; no -l. s is an estimate, not
#    a measure: B is a guess of what would have happened, and later pods on the node move L too.
#    -alone drops it (s = 0, no B), to see what it brings: kaptain-pod-alone. A pod that failed
#    weighs on its node until it stopped, and its own delay counts until the end of the run.
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

from helpers import FEATURE_REQUESTED

from .base import SchedulingStrategy

log = logging.getLogger("kaptain.decider")

WORKLOAD = "kaptain.io/workload"
SIGMA2 = 0.1  # noise of one log-runtime, about 30%
MOVED_V = 1.0  # extra variance of an estimate taken from another node
UNKNOWN_S = 60.0  # runtime of a task never run anywhere
SCALE = 100.0  # task-seconds per unit of reward
ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
VARIANT = re.compile(r"kaptain(?:-b(\d+(?:\.\d+)?))?(?:-l(\d+(?:\.\d+)?)(-llm|-placebo)?)?(?:(-pod)(-alone)?)?(-ef)?")  # -llm, -placebo only with a lambda: they give the Phi of the bonus
TEMPERATURE_S = 20.0  # of the earliest-finish prior (-ef): nodes this many seconds apart in finish time, e times as likely
FIXED_SLOWDOWN = 1.0  # slowed-estimate-fixed: a node fully booked runs a pod twice as long (learned on campaign 41251873008: 0.6-0.95 on the 1-core workers, 0.3 on the 8-core one)
SLOWDOWN_GRID = [i / 20 for i in range(61)]  # slowed-estimate-learned: the slowdowns tried, 0 to 3
MIN_LOADED = 20  # pods a node must have run before its slowdown is learned; the fixed one until then
MIN_TIMED_S = 10.0  # shorter pods do not teach the slowdown: timestamps to the second make a 2 s disk task 2 or 3 s
MAX_SLOWDOWN_SE = 0.5  # a slowdown is learned once its standard error, at the estimates' noise SIGMA2, is below this
PHASE = "kaptain.io/phase"
FROZEN = ("eval", "valid", "cold")  # phases that only use the policy: the evaluation, checkpoints along the training, and cold runs
# Of those, the phases whose runs each start from the runtime estimates the training left, and
# leave them as they were. A cold run is an evaluation on other nodes than the training's: what
# one run learns of a node never seen must not reach the next.
KEPT = ("valid", "cold")
HALF_LIFE_S = 240.0  # of the discount: about a disk task on the Pi, which a shorter one would let look cheap
GAE = 0.95  # generalized advantage estimation, per decision
EPOCHS = 8  # passes over an episode at each update
CLIP = 0.2  # PPO's bound on the change of a decision's probability
MAX_GRADIENT = 1.0  # norm
FLAT = 0.05  # probabilities this close together: the policy does not tell the nodes apart
LEFT_KEPT = 30000  # -pod: the nodes not chosen that B learns from, about 100 runs
LEFT_STEPS = 20  # -pod: B's gradient steps at each update
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


def limit_millicpu(pod):
    return millicores(pod["spec"]["containers"][0].get("resources", {}).get("limits", {}).get("cpu", "0"))


def occupancy_spread(points):
    """The sum of squares of each point's occupancy around its task's mean: what tells the slowdown
    apart from the task's own runtime. Occupancies that do not vary within a task fit every alpha
    as well, and the tie would keep 0."""
    by_task = collections.defaultdict(list)
    for task, occ, _ in points:
        by_task[task].append(occ)
    return sum(sum((occ - sum(occs) / len(occs)) ** 2 for occ in occs) for occs in by_task.values())


def fit_slowdown(points):
    """The alpha of the grid that best fits log(runtime x CPU) = b_task + log(1 + alpha occ), b_task
    each task's mean, over (task, occ, log(runtime x CPU)) points. Runtime x CPU, because a pod's
    runtime goes as one over its CPU limit: bigger pods load the node more and finish sooner, which
    would otherwise read as a node that load does not slow. None while the occupancy varies too
    little within the tasks to pin alpha down: its standard error is about sqrt(SIGMA2 / spread),
    spread from occupancy_spread. With earliest-finish's placements of campaign 41251873008, the
    1-core workers reach it after 3 or 4 runs, the 8-core one, busy by a smaller fraction, after 7;
    a node that never runs pods side by side keeps the fixed alpha."""
    if occupancy_spread(points) < SIGMA2 / MAX_SLOWDOWN_SE ** 2:
        return None
    best = None
    for alpha in SLOWDOWN_GRID:
        sums = collections.defaultdict(lambda: [0, 0.0, 0.0])  # task -> count, sum, sum of squares
        for task, occ, y in points:
            r = y - math.log1p(alpha * occ)
            total = sums[task]
            total[0], total[1], total[2] = total[0] + 1, total[1] + r, total[2] + r * r
        error = sum(squares - total ** 2 / count for count, total, squares in sums.values())
        if best is None or error < best[0] - 1e-9:  # a tie keeps the smaller slowdown
            best = (error, alpha)
    return best[1]


class Load:
    """How much a node's occupancy slows a pod: runtime = alone x (1 + alpha occ), occ the CPU
    requested on the node, the pod included, over its cores. alpha is FIXED_SLOWDOWN, or learned
    for each node from the pods that finished there, each with the occupancy when it was bound,
    the one its decision saw: fitted on the mean over its run instead, alpha came out too large.
    The estimates get the runtime alone, divided by (1 + alpha occ): multiplying an estimate
    learned under load by the load would count it twice. Every runtime is kept with its
    occupancy, and a node whose alpha changes has its estimates computed again: the estimates
    forget nothing, so the runtimes divided by an alpha since revised would stay wrong."""

    def __init__(self, learn):
        self.learn = learn
        self.cores = {}  # node -> cores, from the snapshots
        # node -> millicores requested by pods outside the experiment, which the snapshot counts
        # and the pod list does not: the least a snapshot showed while the pod list had no
        # experiment pod on the node, as at the first decision of a run. A busy node's snapshot
        # also counts experiment pods, which record() adds again from the pod list. The pod list
        # lags the snapshot by up to 2 s: it can miss a pod just bound, which the least forgets,
        # but a node it shows idle has not lost a pod since. 0 until the node is seen idle.
        self.system = {}
        self.points = collections.defaultdict(lambda: collections.deque(maxlen=1000))  # node -> (task, occ, log(runtime x CPU))
        self.history = collections.defaultdict(list)  # node -> (task, runtime, occ) of each pod finished there, in order
        self.alphas = {}

    def see(self, nodes, busy):
        """Reads a snapshot's nodes, busy the nodes where the pod list has an unfinished
        experiment pod."""
        for n in nodes:
            self.cores[n["name"]] = n["allocatable_millicpu"] / 1000
            if n["name"] not in busy:
                self.system[n["name"]] = min(self.system.get(n["name"], math.inf), n["requested_millicpu"])

    def alpha(self, node):
        return self.alphas.get(node, FIXED_SLOWDOWN)

    def alone(self, node, runtime, occ):
        return runtime / (1 + self.alpha(node) * occ)

    def record(self, task, node, runtime, millicpu, others):
        """Keeps a pod that ran runtime seconds on node, others the millicores the node's other
        experiment pods requested when it was bound, and learns the node's slowdown from it.
        Returns its occupancy, and whether the node's alpha changed."""
        if node not in self.cores:  # finished before any decision saw its node: taken as it ran
            self.history[node].append((task, runtime, 0.0))
            return 0.0, False
        occ = (self.system.get(node, 0.0) + millicpu + others) / 1000 / self.cores[node]
        self.history[node].append((task, runtime, occ))
        if not (self.learn and runtime >= MIN_TIMED_S and millicpu > 0):  # no limit: no work to scale by
            return occ, False
        self.points[node].append((task, occ, math.log(runtime * millicpu / 1000)))
        if len(self.points[node]) < MIN_LOADED:
            return occ, False
        alpha = fit_slowdown(self.points[node])
        if alpha is None or alpha == self.alpha(node):
            if alpha is not None:
                self.alphas[node] = alpha
            return occ, False
        log.info(json.dumps({"event": "kaptain_slowdown", "node": node, "alpha": alpha, "pods": len(self.points[node])}))
        self.alphas[node] = alpha
        return occ, True


def bound_at(pod):
    """When the pod was bound to its node, from which the snapshot counts its requests; its start
    without the condition, None when it has neither."""
    for condition in pod["status"].get("conditions") or []:
        if condition.get("type") == "PodScheduled" and condition.get("status") == "True" and condition.get("lastTransitionTime"):
            return stamp(condition["lastTransitionTime"])
    state = (pod["status"].get("containerStatuses") or [{}])[0].get("state", {})
    for key in ("running", "terminated"):
        if state.get(key, {}).get("startedAt"):
            return stamp(state[key]["startedAt"])
    return None


class Cluster:
    """Every 2 s, lists the experiment pods: counts the unfinished ones (a sample: time, seconds
    since the last, count), keeps the running ones, and feeds the runtime of each finished one
    to the estimates. With cpu, reads in the background the CPU time each finished pod reports.
    With a Load, the estimates are of a pod alone on its node (Load)."""

    def __init__(self, cpu=False, load=None):
        self.load = load
        self.estimates = Estimates()
        self.samples = collections.deque(maxlen=20000)  # 11 h: longer than any run
        self.unfinished = 0
        self.running = []  # (task, node, started)
        self.seen = set()  # the finished pods learned from
        self.failed = set()  # those whose learning raised: tried again at each read, logged once
        self.busy = set()  # nodes where the last read found an unfinished experiment pod
        self.cpu = ThreadPoolExecutor(max_workers=2) if cpu else None
        self.reading = []  # the CPU reads not finished yet
        self.used = collections.deque(maxlen=20000)  # (namespace, started, finished, CPU-seconds, measured, pod)
        self.ended = collections.deque(maxlen=20000)  # (namespace, pod, finished, succeeded) of every pod that stopped
        self.kept = None  # the training's runtime estimates, while the runs of a KEPT phase use a copy
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
                    try:
                        self.read(pods)
                    except Exception as error:  # one odd pod must not stop the polling, and the learning, for good
                        log.error(json.dumps({"event": "kaptain_read_failed", "error": repr(error)}))
                self.samples.append((now, now - last, self.unfinished))
            last = now

    def refresh(self):
        """Reads the pods now, not at the next tick: a run's last pods count before the next run."""
        pods = self.poll()
        with self.lock:
            self.read(pods)

    def keep(self, phase):
        """Between two runs, the pods of the last one read (refresh). A run of a KEPT phase starts
        from the training's runtime estimates and leaves them as they were: measuring must not
        teach them. Any other run goes on from them. True while the training's are kept aside."""
        with self.lock:
            if self.kept is not None:
                self.estimates.known = copy.deepcopy(self.kept)
            if phase in KEPT:
                if self.kept is None:
                    self.kept = copy.deepcopy(self.estimates.known)
            elif phase:
                self.kept = None
            return self.kept is not None

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
            self.used.append((namespace, started, finished, seconds, found is not None, name))

    def read(self, pods):
        self.unfinished, self.running = 0, []
        ran = self.intervals(pods) if self.load else {}
        self.busy = {node for node, spans in ran.items() if any(end == math.inf for _, (_, end, _) in spans)}
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
                try:
                    self.finished(task, node, pod, states[0]["terminated"], ran)
                except Exception as error:  # not seen: the next read tries it again, and the other pods go on
                    if uid not in self.failed:
                        log.error(json.dumps({"event": "kaptain_learn_failed", "pod": pod["metadata"].get("name"), "error": repr(error)}))
                    self.failed.add(uid)
                    continue
                self.seen.add(uid)
                self.failed.discard(uid)
            elif phase == "Failed" and uid not in self.seen and states and states[0].get("terminated", {}).get("finishedAt"):
                # No runtime to learn from, but -pod needs to know when it left its node.
                self.ended.append((pod["metadata"].get("namespace"), pod["metadata"].get("name"),
                                   stamp(states[0]["terminated"]["finishedAt"]), False))
                self.seen.add(uid)

    def finished(self, task, node, pod, done, ran):
        """Learns from a pod that finished. Everything that can raise comes before the first
        change, so a pod that fails is not counted twice when tried again."""
        started, ended = stamp(done["startedAt"]), stamp(done["finishedAt"])
        limit = limit_millicpu(pod)
        if self.load:
            self.learn_alone(task, node, pod, ended - started, ran)
        else:
            self.estimates.update(task, node, ended - started)
        self.ended.append((pod["metadata"].get("namespace"), pod["metadata"].get("name"), ended, True))
        if self.cpu:
            self.reading = [f for f in self.reading if not f.done()]
            self.reading.append(self.cpu.submit(
                self.used_cpu, pod["metadata"]["namespace"], pod["metadata"]["name"], started, ended, limit))

    def learn_alone(self, task, node, pod, runtime, ran):
        """With a Load: the runtime alone into the estimates, all of the node's again when its
        slowdown changed."""
        runtime = max(runtime, 1.0)  # the timestamps are to the second
        uid, bound = pod["metadata"]["uid"], bound_at(pod)
        others = 0 if bound is None else sum(cpu for other, (s, e, cpu) in ran.get(node, []) if other != uid and s <= bound < e)
        occ, changed = self.load.record(task, node, runtime, limit_millicpu(pod), others)
        if not changed:
            self.estimates.update(task, node, self.load.alone(node, runtime, occ))
            return
        for key in [key for key in self.estimates.known if key[1] == node]:
            del self.estimates.known[key]
        for task_then, runtime_then, occ_then in self.load.history[node]:
            self.estimates.update(task_then, node, self.load.alone(node, runtime_then, occ_then))

    @staticmethod
    def intervals(pods):
        """node -> (uid, (bound, finished or inf, millicpu)) of every pod bound to a node: what the
        snapshot's requests count."""
        out = collections.defaultdict(list)
        for pod in pods:
            node, bound = pod["spec"].get("nodeName"), bound_at(pod)
            if node is None or bound is None:
                continue
            state = (pod["status"].get("containerStatuses") or [{}])[0].get("state", {})
            finished = state.get("terminated", {}).get("finishedAt")
            out[node].append((pod["metadata"]["uid"], (bound, stamp(finished) if finished else math.inf, limit_millicpu(pod))))
        return out

    def time_left(self, node, now):
        return sum(max(self.estimates.seconds(task, on) - (now - started), 0.0)
                   for task, on, started in self.running if node is None or on == node)


def settle_estimates(strategy, phase):
    """Between two runs, for a rule that only learns runtime estimates: nothing to wait for, but
    the estimates are kept aside for the runs of a KEPT phase, as Kaptain's are (Cluster.keep)."""
    if phase not in KEPT and (strategy.cluster is None or strategy.cluster.kept is None):
        return {"settled": False}  # nothing kept aside, nothing to keep: no read of the API that could fail the run
    if strategy.cluster is None:  # no decision yet: what is kept is then empty
        strategy.cluster = Cluster()
    strategy.cluster.refresh()
    return {"settled": False, "estimates_kept": strategy.cluster.keep(phase)}


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

    def settle(self, phase):
        return settle_estimates(self, phase)


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
        with self.cluster.lock:
            finish = finish_scores(self.cluster, task, snapshot["nodes"], now)
        return {n["name"]: score for n, score in zip(snapshot["nodes"], finish)}

    def settle(self, phase):
        return settle_estimates(self, phase)


class SlowedEstimate(SchedulingStrategy):
    """Shortest estimate, slowed by the load, with no work left counted: a node's score is minus
    the pod's estimated runtime alone there, multiplied by (1 + alpha occ), occ the CPU requested
    on the node, the pod included, over its cores. The pods run side by side within their limits:
    a busy node slows the new one, it does not queue it behind the others, as earliest-finish has
    it. A bounded penalty, in proportion to the node's speed. The snapshot's requests count a pod
    as soon as it is bound, the pod list up to 2 s later."""

    learn = False

    def __init__(self):
        self.cluster = None

    def scores(self, snapshot: dict) -> dict[str, float]:
        if self.cluster is None:
            self.cluster = Cluster(load=Load(self.learn))
        pod, task = snapshot["pod"], (snapshot["pod"].get("labels") or {}).get(WORKLOAD)
        for n in snapshot["nodes"]:
            if FEATURE_REQUESTED in n.get("missing", []) or n["allocatable_millicpu"] <= 0:
                raise ValueError(f"node {n['name']!r} has no requested or allocatable CPU")
        out = {}
        with self.cluster.lock:
            self.cluster.load.see(snapshot["nodes"], self.cluster.busy)
            for n in snapshot["nodes"]:
                estimate = self.cluster.estimates.get(task, n["name"])
                if not (estimate and estimate[2]):
                    out[n["name"]] = 0.0  # never run there: tried first, as by shortest-estimate
                    continue
                occ = (n["requested_millicpu"] + pod["requested_millicpu"]) / n["allocatable_millicpu"]
                out[n["name"]] = -math.exp(estimate[0]) * (1 + self.cluster.load.alpha(n["name"]) * occ)
        return out


class FixedSlowedEstimate(SlowedEstimate):
    """alpha = FIXED_SLOWDOWN on every node."""

    name = "slowed-estimate-fixed"


class LearnedSlowedEstimate(SlowedEstimate):
    """alpha learned for each node, FIXED_SLOWDOWN until it ran MIN_LOADED pods."""

    name = "slowed-estimate-learned"
    learn = True


def finish_scores(cluster, task, nodes, now):
    """Earliest-finish's score of each node, under the cluster's lock: minus the pod's estimated
    runtime there plus the work left on it shared over its cores, 0 where the task never ran."""
    scores = []
    for n in nodes:
        estimate = cluster.estimates.get(task, n["name"])
        if not (estimate and estimate[2]):
            scores.append(0.0)  # never run there: tried first, as by shortest-estimate
            continue
        queue = cluster.time_left(n["name"], now) / (n["allocatable_millicpu"] / 1000)
        scores.append(-(math.exp(estimate[0]) + queue))
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


def placed_costs(episode, ended, end):
    """-pod. Per decision: the seconds from it to the end of its pod, and for each of its nodes the
    seconds the pods placed there before it still ran after it. ended: pod -> (when it stopped,
    whether it succeeded). A pod is on its node until it stopped, until end, the end of the run,
    when it never did; its own delay counts until end unless it succeeded."""
    stopped = [ended.get(state["pod"], (end, False)) for state in episode]
    costs = []
    for k, state in enumerate(episode):
        left = [sum(stopped[j][0] - state["time"] for j in range(k)
                    if episode[j]["node"] == node and stopped[j][0] > state["time"]) for node in state["nodes"]]
        costs.append(((stopped[k][0] if stopped[k][1] else end) - state["time"], left))
    return costs


class Kaptain(SchedulingStrategy):
    def __init__(self, name="kaptain", half_life_s=HALF_LIFE_S, lr=3e-3, hidden=16, entropy=0.01, seed=7):
        import torch  # here, so that the other strategies never load it
        from torch import nn

        weights = VARIANT.fullmatch(name)
        self.name = name
        self.beta = float(weights[1] or 0)  # weight of the CPU-seconds
        self.lam = float(weights[2] or 0)  # weight of the Phi bonus
        if weights[3] and not self.lam:
            raise ValueError(f"{name}: {weights[3]} gives the bonus its Phi, which lambda = 0 turns off")
        self.llm = LLM() if weights[3] == "-llm" else None
        self.seed = int(os.environ.get("KAPTAIN_SEED") or seed)
        torch.manual_seed(self.seed)
        torch.set_num_threads(1)  # the master is a shared 4 vCPU VPS
        self.torch, self.kappa, self.entropy = torch, math.log(2) / half_life_s, entropy
        self.random = random.Random(self.seed)  # breaks the ties of a frozen policy
        # Its own generator: drawing Phi leaves the policy's sampling as it is without it.
        self.placebo = random.Random(self.seed + 1) if weights[3] == "-placebo" else None
        self.pod = bool(weights[4])  # -pod: each decision rewarded for the pod it placed
        self.alone = bool(weights[5])  # -alone: without s, what the pod cost the pods on its node
        if self.pod and self.lam:
            raise ValueError(f"{name}: -pod rewards a decision for its own pod, without -l")
        self.residual = bool(weights[6])  # -ef: the network corrects earliest-finish instead of scoring alone

        def embedding():
            return nn.Sequential(nn.Linear(14, hidden), nn.ReLU(), nn.Linear(hidden, hidden), nn.ReLU())

        # The actor and the critic each embed the nodes: the critic's errors no longer reshape
        # what the actor sees of them.
        self.net = nn.ModuleDict({"embed": embedding(), "critic_embed": embedding(),
                                  "score": nn.Sequential(nn.Linear(2 * hidden + 1, hidden), nn.ReLU(), nn.Linear(hidden, 1)),
                                  "critic": nn.Sequential(nn.Linear(hidden + 1, hidden), nn.ReLU(), nn.Linear(hidden, 1))})
        if self.residual:  # a correction of zero: untrained, the policy is earliest-finish
            nn.init.zeros_(self.net["score"][-1].weight)
            nn.init.zeros_(self.net["score"][-1].bias)
        if self.pod and not self.alone:  # B: from a node's vector, the time left of the pods on it, per 100 s
            self.net["left"] = nn.Sequential(nn.Linear(14, hidden), nn.ReLU(), nn.Linear(hidden, hidden), nn.ReLU(), nn.Linear(hidden, 1))
            self.others = collections.deque(maxlen=LEFT_KEPT)  # (vector, time left / 100) of the nodes not chosen
        # The learner trains its own copy in the background, then hands its weights over.
        self.trained = copy.deepcopy(self.net)
        self.optimizer = torch.optim.Adam(self.trained.parameters(), lr=lr)
        self.left_optimizer = torch.optim.Adam(self.trained["left"].parameters(), lr=lr) if "left" in self.net else None
        self.learner = ThreadPoolExecutor(max_workers=1)  # one episode at a time, in order
        self.episodes = 0
        with torch.no_grad():  # the first pass is slow; not on a decision
            self.logits(self.net, torch.zeros(2, 14), 0)
        self.cluster = None  # started on the first decision: the health check runs without a cluster
        self.episode = []  # the decisions of the training run under way
        self.update = None  # the update under way, if any
        self.failure = None  # the first update that failed: every settle refuses from then on
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

    def logits(self, net, rows, unfinished, prior=None):
        """Each node's logit; with -ef, the earliest-finish prior of the decision plus the network's correction."""
        h = net["embed"](rows)
        out = net["score"](self.torch.cat([h, self.summary(h, unfinished).expand(len(h), -1)], 1)).squeeze(1)
        return out if prior is None else prior + out

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
                         "unfinished": self.cluster.unfinished, "time": now, "namespace": pod["namespace"], "phi": 0.0,
                         "analytic": -self.cluster.time_left(None, now) / SCALE}
                # Kept with the decision: the update scores it again with the prior it had then.
                state["prior"] = (torch.tensor(finish_scores(self.cluster, labels.get(WORKLOAD), snapshot["nodes"], now))
                                  / TEMPERATURE_S if self.residual else None)
                # Logged with every decision, so that any Phi can be scored offline on real states.
                description = self.describe(snapshot, now)
                if self.lam and not frozen:
                    state["phi"] = (self.llm.phi(description) if self.llm
                                    else self.placebo.uniform(-10.0, 0.0) if self.placebo else state["analytic"])
            with torch.no_grad():
                logits = self.logits(self.net, state["rows"], state["unfinished"], state["prior"])
            probabilities = torch.softmax(logits, 0)
            if frozen:  # its best node; among equals, any: never the first one listed
                best = float(logits.max())
                action = self.random.choice([i for i, x in enumerate(logits.tolist()) if best - x < 1e-6])
            else:
                action = int(torch.distributions.Categorical(logits=logits).sample())
                state.update(action=action, log_probability=float(torch.log(probabilities[action])))
                self.episode.append(state)
            chosen = snapshot["nodes"][action]["name"]
            if not frozen:  # -pod finds the pod again when it has finished, and those placed before it on each node
                state.update(pod=pod.get("name"), node=chosen, nodes=[n["name"] for n in snapshot["nodes"]])
            spread = float(probabilities.max() - probabilities.min())
            # What the policy thought of every node, also when frozen: an argmax hides how close the others were.
            log.info(json.dumps({"event": "kaptain_decision", "strategy": self.name, "seed": self.seed,
                                 "namespace": pod["namespace"], "pod": pod.get("name"), "frozen": frozen,
                                 "chosen": chosen, "flat": spread < FLAT, "episodes": self.episodes,
                                 "state": description, "phi_analytic": round(state["analytic"], 4),
                                 # Where earliest-finish alone would go: how far the correction moved it.
                                 **({"prior_choice": snapshot["nodes"][int(state["prior"].argmax())]["name"]}
                                    if self.residual else {}),
                                 "probabilities": {n["name"]: round(p, 4) for n, p in
                                                   zip(snapshot["nodes"], probabilities.tolist())}}))
        return {n["name"]: 1.0 if n["name"] == chosen else 0.0 for n in snapshot["nodes"]}

    def settle(self, phase):
        """Between two runs (kexp), never on a decision: learns from the training run that ended
        and waits for it, so the next run uses one model throughout. A valid or cold run starts
        from the training's runtime estimates and leaves them as they were (Cluster.keep)."""
        if self.cluster is None:
            if phase not in KEPT:
                return {"settled": True, "episodes": self.episodes}
            with self.lock:  # no decision yet: what is kept is then empty, and stays so run after run
                self.cluster = Cluster(cpu=self.beta > 0)
        self.cluster.refresh()
        with self.lock:
            if self.episode:
                self.close(time.time())
            update = self.update
        if update:
            update.result()  # raises what the update raised
        if self.failure:  # also one that ran without settle: no run goes on from a model not learned
            raise RuntimeError(f"an update failed: {self.failure}")
        return {"settled": True, "episodes": self.episodes, "estimates_kept": self.cluster.keep(phase)}

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
                ended = {pod: (stopped, succeeded) for namespace, pod, stopped, succeeded in self.cluster.ended
                         if namespace == episode[0]["namespace"]}
            self.learn(episode, samples, used, ended, end)
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
                 for _, started, finished, seconds, _, _ in used]
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
                        "delay": delay, "cpu": cpu, "bonus": bonus, "phi": phi, "analytic": state["analytic"]})
        return out

    def placed(self, episode, used, ended, end):
        """-pod. Per decision: r = -(d + beta e + s) / 100, ending there (discount 0). d: its pod's
        seconds; e: its pod's CPU-seconds, 0 for a pod without a reading (one that did not succeed:
        its d already runs to the end of the run). s = L - B: the seconds the pods already on its
        node still ran, minus what B expects of them when no pod joins; 0 with -alone. B is read,
        and its error measured on this run's nodes not chosen, before it learns from them: a run
        is never scored by a B that has already seen it."""
        torch, net = self.torch, self.trained
        costs = placed_costs(episode, ended, end)
        cpu = {pod: seconds for _, _, _, seconds, _, pod in used}
        unfinished = sum(1 for state in episode if not ended.get(state["pod"], (end, False))[1])
        unread = sum(1 for state in episode if state["pod"] not in cpu) if self.beta else 0
        if unfinished or unread:
            log.warning(json.dumps({"event": "kaptain_pods_unfinished", "namespace": episode[0]["namespace"],
                                    "pods": unfinished, "without_cpu": unread}))
        expected, self.left_error = [0.0] * len(episode), None
        if not self.alone:
            with torch.no_grad():
                expected = (net["left"](torch.stack([state["rows"][state["action"]] for state in episode])).squeeze(1) * SCALE).tolist()
            seen = [(state["rows"][i], seconds / SCALE) for state, (_, left) in zip(episode, costs)
                    for i, seconds in enumerate(left) if i != state["action"]]
            if seen:  # none when every decision had a single node
                with torch.no_grad():
                    guess = net["left"](torch.stack([row for row, _ in seen])).squeeze(1)
                self.left_error = float((guess - torch.tensor([t for _, t in seen])).abs().mean()) * SCALE
                self.others.extend(seen)
            if self.others:
                rows, target = torch.stack([row for row, _ in self.others]), torch.tensor([t for _, t in self.others])
                for _ in range(LEFT_STEPS):
                    loss = torch.nn.functional.smooth_l1_loss(net["left"](rows).squeeze(1), target)
                    self.left_optimizer.zero_grad()
                    loss.backward()
                    self.left_optimizer.step()
        out = []
        for state, (delay, left), b in zip(episode, costs, expected):
            slowed, e = 0.0 if self.alone else left[state["action"]] - b, cpu.get(state["pod"], 0.0)
            out.append({"reward": -(delay + self.beta * e + slowed) / SCALE, "discount": 0.0, "delay": delay,
                        "slowed": slowed, "cpu": e, "bonus": 0.0, "phi": 0.0, "analytic": state["analytic"]})
        return out

    def learn(self, episode, samples, used, ended, end):
        """PPO on one episode: generalized advantages, normalized; clipped policy steps and a Huber
        critic, a few epochs over the episode, the actor's and the critic's gradients clipped apart.
        With -pod every discount is 0: the advantage of a decision is its reward minus the critic's."""
        torch, net, started = self.torch, self.trained, time.perf_counter()
        steps = self.placed(episode, used, ended, end) if self.pod else self.transitions(episode, samples, used, end)
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
            dists = [torch.distributions.Categorical(logits=self.logits(net, s["rows"], s["unfinished"], s["prior"]))
                     for s in episode]
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
                       (torch.softmax(self.logits(net, s["rows"], s["unfinished"], s["prior"]), 0) for s in episode)]
        with self.lock:
            self.net.load_state_dict(net.state_dict())
            self.episodes += 1
        for state, step, a, v in zip(episode, steps, advantages, values):
            log.info(json.dumps({"event": "kaptain_step", "strategy": self.name, "namespace": state["namespace"],
                                 "reward": round(step["reward"], 4), "delay_task_s": round(step["delay"], 1),
                                 "cpu_s": round(step["cpu"], 1), "phi": step["phi"], "phi_analytic": round(step["analytic"], 4),
                                 **({"slowed_s": round(step["slowed"], 1)} if self.pod else {}),
                                 "bonus": round(step["bonus"], 4),
                                 "discount": round(step["discount"], 4), "value": round(v, 4),
                                 "advantage": round(a, 4)}))
        log.info(json.dumps({"event": "kaptain_update", "strategy": self.name, "seed": self.seed,
                             "namespace": episode[0]["namespace"], "episode": self.episodes, "decisions": len(episode),
                             "return": round(float(returns[0]), 3), "last_reward": round(steps[-1]["reward"], 3),
                             # The run's cost without the bonus, each part discounted to its decision: return is the critic's target.
                             "cost": round(sum(s["bonus"] - s["reward"] for s in steps), 3),
                             # -pod: cost is the objective, not a time. What was measured (the pods' delays) and
                             # what was estimated (s, and B's mean error in seconds before this run taught it), apart.
                             **({"delay_s": round(sum(s["delay"] for s in steps), 1),
                                 "slowed_s": round(sum(s["slowed"] for s in steps), 1),
                                 "left_error_s": None if self.left_error is None else round(self.left_error, 1)}
                                if self.pod else {}),
                             "bonus": round(sum(s["bonus"] for s in steps), 3),
                             "llm_failed": sum(1 for s in steps if s["phi"] is None),
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
