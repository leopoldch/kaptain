"""The runtime estimates kept aside for valid and cold runs. Run with: cd bridge && uv run python -m unittest discover tests"""

import unittest

from strategies import kaptain as k
from test_slowdown import pod


class Quiet(k.Cluster):
    """No API here: the pods a test finishes are what the next poll lists."""

    def __init__(self):
        self.pods = []
        super().__init__()

    def loop(self):
        return

    def poll(self):
        return self.pods

    def finish(self, uid, node, seconds):
        self.pods.append(pod(uid, node, "cpu", 500, 0, 0, seconds))


def nodes(*names):
    return {"pod": {"labels": {k.WORKLOAD: "cpu"}}, "nodes": [{"name": name, "allocatable_millicpu": 1000} for name in names]}


class Keep(unittest.TestCase):
    def test_a_cold_run_leaves_the_training_estimates(self):
        cluster = Quiet()
        cluster.finish("1", "seen", 30)
        cluster.refresh()
        self.assertFalse(cluster.keep("train"))
        self.assertTrue(cluster.keep("cold"))
        cluster.finish("2", "new", 60)
        cluster.refresh()
        self.assertIn(("cpu", "new"), cluster.estimates.known)
        self.assertTrue(cluster.keep(""))  # after the run
        self.assertEqual(list(cluster.estimates.known), [("cpu", "seen")])
        self.assertTrue(cluster.keep("cold"))  # the next one starts from the same
        self.assertEqual(list(cluster.estimates.known), [("cpu", "seen")])

    def test_an_eval_run_goes_on_from_its_estimates(self):
        cluster = Quiet()
        cluster.finish("1", "seen", 30)
        cluster.refresh()
        self.assertFalse(cluster.keep("eval"))
        cluster.finish("2", "new", 60)
        cluster.refresh()
        self.assertFalse(cluster.keep(""))
        self.assertFalse(cluster.keep("eval"))
        self.assertIn(("cpu", "new"), cluster.estimates.known)

    def test_training_after_a_cold_run_goes_on_from_the_training(self):
        cluster = Quiet()
        cluster.finish("1", "seen", 30)
        cluster.refresh()
        cluster.keep("cold")
        cluster.finish("2", "new", 60)
        cluster.refresh()
        cluster.keep("")
        self.assertFalse(cluster.keep("train"))
        self.assertEqual(list(cluster.estimates.known), [("cpu", "seen")])


class Rules(unittest.TestCase):
    def test_every_cold_run_meets_the_new_node_unknown(self):
        for rule in (k.ShortestEstimate(), k.EarliestFinish()):
            rule.cluster = Quiet()
            rule.cluster.finish("1", "seen", 30)
            self.assertEqual(rule.settle("train"), {"settled": False})  # nothing kept: the API is not read
            for run in ("2", "3"):
                self.assertEqual(rule.settle("cold"), {"settled": False, "estimates_kept": True})
                scores = rule.scores(nodes("seen", "new"))
                self.assertEqual(scores["new"], 0.0, rule.name)  # never run there: tried first
                self.assertLess(scores["seen"], 0.0, rule.name)
                rule.cluster.finish(run, "new", 60)
                rule.settle("")
                self.assertEqual(list(rule.cluster.estimates.known), [("cpu", "seen")], rule.name)

    def test_without_a_decision_yet_nothing_is_kept_from_the_run(self):
        rule = k.ShortestEstimate()
        created = []
        original, k.Cluster = k.Cluster, lambda: created.append(Quiet()) or created[-1]
        try:
            self.assertEqual(rule.settle("cold"), {"settled": False, "estimates_kept": True})
        finally:
            k.Cluster = original
        rule.cluster.finish("1", "new", 60)
        rule.settle("")
        self.assertEqual(rule.cluster.estimates.known, {})


if __name__ == "__main__":
    unittest.main()
