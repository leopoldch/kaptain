"""Kaptain's -pod reward. Run with: cd bridge && uv run python -m unittest discover tests"""

import unittest

import torch

from strategies import get_strategy
from strategies import kaptain as k
from test_slowdown import Quiet, pod


def decision(name, time, node, nodes=("a", "b")):
    return {"pod": name, "time": time, "node": node, "nodes": list(nodes), "action": nodes.index(node),
            "rows": torch.zeros(len(nodes), 14), "unfinished": 0, "namespace": "run", "prior": None,
            "analytic": 0.0, "log_probability": -0.7}


class PlacedCosts(unittest.TestCase):
    def test_own_delay_and_time_left_on_each_node(self):
        episode = [decision("p0", 0, "a"), decision("p1", 10, "a"), decision("p2", 20, "b")]
        costs = k.placed_costs(episode, {"p0": 30, "p1": 15, "p2": 50}, end=60)
        self.assertEqual(costs[0], (30, [0, 0]))  # nothing placed before the first
        self.assertEqual(costs[1], (5, [20, 0]))  # p0 still runs 20 s on a
        self.assertEqual(costs[2], (30, [10, 0]))  # p0 has 10 s left on a, p1 is over

    def test_a_pod_that_did_not_succeed_counts_until_the_end_of_the_run(self):
        episode = [decision("p0", 0, "a"), decision("p1", 10, "a")]
        self.assertEqual(k.placed_costs(episode, {"p1": 15}, end=60), [(60, [0, 0]), (5, [50, 0])])


class Variant(unittest.TestCase):
    def test_names(self):
        self.assertTrue(get_strategy("kaptain-pod").pod)
        both = get_strategy("kaptain-pod-ef")
        self.assertTrue(both.pod and both.residual)
        self.assertFalse(get_strategy("kaptain-ef").pod)
        for name in ("kaptain-b0.5-pod", "kaptain-l4-pod"):
            with self.assertRaises(ValueError):
                get_strategy(name)


class Learning(unittest.TestCase):
    def test_the_cluster_keeps_when_each_pod_finished(self):
        cluster = Quiet()
        done = pod("u", "w", "cpu", 500, 0, 0, 20)
        done["metadata"].update(namespace="run", name="p0")
        cluster.read([done])
        self.assertEqual([(namespace, name) for namespace, name, _ in cluster.ended], [("run", "p0")])

    def test_one_update_rewards_each_decision_for_its_pod(self):
        policy = get_strategy("kaptain-pod")
        episode = [decision("p0", 0, "a"), decision("p1", 10, "a"), decision("p2", 20, "b")]
        with self.assertLogs("kaptain.decider", "INFO") as logs:
            policy.learn(episode, [], [], {"p0": 30, "p1": 15, "p2": 50}, 60)
        self.assertEqual(policy.episodes, 1)
        self.assertEqual(len(policy.others), 3)  # one node not chosen per decision
        steps = [line for line in logs.output if "kaptain_step" in line]
        self.assertEqual(len(steps), 3)
        self.assertIn('"delay_task_s": 5', steps[1])
        self.assertIn('"discount": 0.0', steps[1])


if __name__ == "__main__":
    unittest.main()
