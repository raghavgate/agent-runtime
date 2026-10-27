import copy
import json
import tempfile
import unittest
from pathlib import Path
from app import Application
from evaluations.runner import evaluate, grade, compare_reports
from tracing import RunRecord


class EvaluationTests(unittest.TestCase):
    def test_demo_suite_and_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Application()
            app.traces.directory = Path(directory) / "runs"
            app.evaluation_dir = Path(directory) / "evaluations"
            report = evaluate(app)
            self.assertEqual(report["summary"]["passed"], 10)
            self.assertEqual(report["kind"], "deterministic_demo")
            self.assertIsNone(report["summary"]["estimated_cost_usd"])
            self.assertEqual(len(list(app.traces.directory.glob("*.json"))), 10)
            self.assertEqual(compare_reports(report, report)["regressions"], [])
            other = copy.deepcopy(report)
            other["dataset_hash"] = "different"
            with self.assertRaises(ValueError):
                compare_reports(report, other)

    def test_grader_rejects_plausible_wrong_answer(self):
        record = RunRecord("Find tickets", "scripted", "wrong")
        record.status = "completed"
        record.answer = "Acme has 3 open tickets [support:tickets/652]"
        checks = grade(record, {"ticket_ids": [134, 171], "required_citations": ["support:tickets/134"],
                                "answer_patterns": [r"\b2 open tickets"], "forbidden_patterns": ["652"]})
        self.assertFalse(checks["retrieved_ticket_ids"])
        self.assertFalse(checks["citation_validity"])
        self.assertFalse(checks["answer_fact_0"])
        self.assertFalse(checks["no_forbidden_claims"])

    def test_repeat_preserves_case_results(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Application()
            app.traces.directory = Path(directory) / "runs"
            app.evaluation_dir = Path(directory) / "evaluations"
            report = evaluate(app, repeat=2)
            self.assertEqual(report["summary"]["total"], 20)
            self.assertEqual({row["iteration"] for row in report["results"]}, {1, 2})


if __name__ == "__main__":
    unittest.main()
