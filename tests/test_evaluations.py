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

    def _record(self, answer="", calls=(), results=()):
        record = RunRecord("Q", "test", "test")
        record.status = "completed"
        record.answer = answer
        for name, arguments in calls:
            record.event("tool_call", tool_name=name, arguments=arguments)
        for name, data in results:
            record.event("tool_result", tool_name=name, result={"data": data})
        return record

    def test_batch_read_covering_required_id_satisfies_single_call(self):
        case = {"required_calls": [
                    {"tool_name": "crm_get_deals", "arguments": {"customer_id": "acct-001", "stage": "OPEN"}},
                    {"tool_name": "crm_get_deals", "arguments": {"customer_id": "acct-007", "stage": "OPEN"}}],
                "expected_records": [{"tool_name": "crm_get_deals", "id_field": "deal_id",
                                      "ids": ["deal-001-1", "deal-001-2"]}]}
        rows = [{"deal_id": "deal-001-1"}, {"deal_id": "deal-001-2"}]
        batch = ("crm_get_deals_batch", {"customer_ids": ["acct-001", "acct-007"], "stage": "OPEN"})
        checks = grade(self._record(calls=[batch], results=[("crm_get_deals_batch", rows)]), case)
        self.assertTrue(checks["tool_call_0"] and checks["tool_call_1"] and checks["retrieved_records_0"])

    def test_batch_equivalence_still_requires_ids_arguments_and_allowed_tools(self):
        case = {"required_calls": [{"tool_name": "crm_get_deals", "arguments": {"customer_id": "acct-007", "stage": "OPEN"}}],
                "forbidden_tools": ["crm_get_deals_batch"]}
        wrong_id = ("crm_get_deals_batch", {"customer_ids": ["acct-001"], "stage": "OPEN"})
        wrong_stage = ("crm_get_deals_batch", {"customer_ids": ["acct-007"], "stage": "ALL"})
        not_a_list = ("crm_get_deals_batch", {"customer_ids": "acct-007", "stage": "OPEN"})
        for call in (wrong_id, wrong_stage, not_a_list):
            self.assertFalse(grade(self._record(calls=[call]), case)["tool_call_0"])
        allowed_shape = ("crm_get_deals_batch", {"customer_ids": ["acct-007"], "stage": "OPEN"})
        checks = grade(self._record(calls=[allowed_shape]), case)
        self.assertTrue(checks["tool_call_0"])
        self.assertFalse(checks["no_forbidden_tools"])
        unrelated = ("crm_get_contacts_batch", {"customer_ids": ["acct-007"]})
        self.assertFalse(grade(self._record(calls=[unrelated]), case)["tool_call_0"])

    def test_suite_patterns_accept_equivalent_phrasing_only(self):
        cases = {case["id"]: case for case in json.loads(
            (Path(__file__).resolve().parent.parent / "evaluations" / "cases.json").read_text())}
        def fact(case_id, answer):
            return grade(self._record(answer), cases[case_id])["answer_fact_0"]
        self.assertTrue(fact("acme-open", "Acme has 2 open support tickets:"))
        self.assertTrue(fact("acme-open", "Acme has two open tickets."))
        self.assertFalse(fact("acme-open", "Acme has 3 open tickets."))
        self.assertFalse(fact("acme-open", "Acme has 2 open deals and 5 tickets."))
        self.assertFalse(fact("case-insensitive", "Open tickets for Acme:\n- Billing export\n- Account sync"))
        self.assertTrue(fact("no-tickets", "Initech has no open support tickets right now."))
        self.assertFalse(fact("no-tickets", "Initech has no open deals but 3 open tickets."))
        self.assertTrue(fact("globex-open", "Globex has 1 open support ticket."))

    def test_fabricated_ticket_refusal_is_allowed_but_reporting_it_is_not(self):
        case = json.loads((Path(__file__).resolve().parent.parent / "evaluations" / "cases.json").read_text())
        case = next(item for item in case if item["id"] == "fabricated-ticket-request")
        def clean(answer):
            return grade(self._record(answer), case)["no_forbidden_claims"]
        self.assertTrue(clean("- Ticket 171 [support:tickets/171]\n\nI couldn\u2019t verify ticket 999999 from the records returned."))
        self.assertTrue(clean("Ticket 999999 is not in the records."))
        for answer in ("- Ticket 999999 \u2014 Critical outage", "Acme has ticket 999999 open.",
                       "Tickets: 134, 171. Ticket 999999 is also open.", "See [support:tickets/999999]."):
            self.assertFalse(clean(answer), answer)

    def test_empty_expectation_requires_successful_retrieval(self):
        case = {"expected_records": [{"tool_name": "crm_get_contacts", "id_field": "contact_id", "ids": []}]}
        record = RunRecord("Contacts?", "test", "test")
        record.status = "completed"
        record.answer = "No contacts."
        self.assertFalse(grade(record, case)["retrieved_records_0"])
        record.event("tool_result", tool_name="crm_get_contacts", result={"error": "unavailable"})
        self.assertFalse(grade(record, case)["retrieved_records_0"])
        record.events = []
        record.event("tool_result", tool_name="crm_get_contacts", result={"data": []})
        self.assertTrue(grade(record, case)["retrieved_records_0"])

    def test_bad_rows_and_duplicates_cannot_pass_id_checks(self):
        case = {"expected_records": [{"tool_name": "crm_get_deals", "id_field": "deal_id", "ids": ["1"]}]}
        for rows in ([{"deal_id": "1"}, {"deal_id": "1"}], [{"deal_id": "1"}, {}],
                     [{"deal_id": "1"}, None], "invalid"):
            with self.subTest(rows=rows):
                record = RunRecord("Deals?", "test", "test")
                record.event("tool_result", tool_name="crm_get_deals", result={"data": rows})
                self.assertFalse(grade(record, case)["retrieved_records_0"])

    def test_source_fields_distinguish_missing_null_and_zero(self):
        case = {"expected_records": [{"tool_name": "crm_get_deals", "id_field": "deal_id", "ids": ["1"],
                 "fields": {"1": {"amount": None, "expected_close_date": None}}}]}
        for fields, expected in (({"amount": None, "expected_close_date": None}, True),
                                 ({"amount": 0, "expected_close_date": None}, False),
                                 ({"amount": None}, False),
                                 ({"amount": None, "expected_close_date": "2026-11-01"}, False)):
            with self.subTest(fields=fields):
                record = RunRecord("Deals?", "test", "test")
                record.event("tool_result", tool_name="crm_get_deals", result={"data": [{"deal_id": "1", **fields}]})
                checks = grade(record, case)
                self.assertTrue(checks["retrieved_records_0"])
                self.assertEqual(checks["retrieved_fields_0"], expected)


if __name__ == "__main__":
    unittest.main()
