"""Checks that protect the experiment: no answer leaks into the input, scoring is right."""

import json
import unittest

import measure
import report
import tag


class TaggingTests(unittest.TestCase):
    def setUp(self):
        self.cases = tag.load(tag.HERE / "cases.json")
        self.refs = tag.load(tag.HERE / "references.json")
        self.images = tag.load(tag.HERE / "images.json")

    def test_input_is_only_what_the_scheduler_has(self):
        node = tag.load(tag.HERE / "node.json")
        for case in self.cases:
            for level in tag.LEVELS:
                task = tag.model_input(case, level, self.images, node)
                self.assertEqual(task["pod"], case["pod"])
                self.assertNotIn("metadata", task["pod"])
                self.assertEqual("images" in task, level != "pod")
                self.assertEqual("node" in task, level == "pod_image_node")
                text = json.dumps(task)
                for leak in [case["id"], case["source"], self.refs[case["id"]]["why"], "kaptain", "_about"]:
                    self.assertNotIn(leak, text)

    def test_every_case_has_a_valid_reference(self):
        self.assertEqual({c["id"] for c in self.cases}, {k for k in self.refs if not k.startswith("_")})
        for case in self.cases:
            tag.validate({"work_types": self.refs[case["id"]]["work_types"], "duration_seconds": None,
                          "evidence": [], "missing_information": []})

    def test_invalid_answers_are_rejected(self):
        for tags in [["cpu", "unknown"], ["wait", "disk"], ["cpu", "cpu"], [], ["gpu"]]:
            with self.assertRaises(ValueError):
                tag.validate({"work_types": tags, "duration_seconds": 5, "evidence": [], "missing_information": []})
        for seconds in [-1, "10", True]:
            with self.assertRaises(ValueError):
                tag.validate({"work_types": ["cpu"], "duration_seconds": seconds, "evidence": [], "missing_information": []})

    def test_duration_error_is_a_symmetric_factor(self):
        self.assertEqual(report.factor(20, 10), 2)
        self.assertEqual(report.factor(5, 10), 2)
        self.assertIsNone(report.factor(None, 10))
        self.assertEqual(report.factor(0, 10), float("inf"))

    def test_duration_boundaries(self):
        self.assertEqual([measure.duration_class(s) for s in [9.99, 10, 59.99, 60]], ["short", "medium", "medium", "long"])

    def test_docker_translation_uses_pod_limits(self):
        case = next(c for c in self.cases if c["id"] == "c02")
        command = measure.docker_command(case["pod"], "x")
        self.assertEqual(command[command.index("--cpus") + 1], "0.5")
        self.assertEqual(command[command.index("--memory") + 1], str(256 * 2**20))
        self.assertEqual(command[command.index("--user") + 1], "65534:65534")

    def test_docker_timestamps_keep_nanoseconds(self):
        a = measure.nanoseconds("2026-10-09T20:00:00.000000001Z")
        b = measure.nanoseconds("2026-10-09T20:00:01.5Z")
        self.assertEqual(b - a, 1_499_999_999)


if __name__ == "__main__":
    unittest.main()
