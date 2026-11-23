"""Reproduces the live failure recorded in docs/BASELINE.md: the Acme brief retrieved the account but
deferred the ticket lookup. The harness must reject that answer, correct the model once, and only
accept a brief that actually retrieved the tickets."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from app import Application
from openai_model import OpenAIModel
from runtime import Runtime, RunError, IncompleteAnswerError, validate_completion_checks


class Item(SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


def call(name, arguments, call_id):
    return SimpleNamespace(output=[Item(type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id)],
                           output_text="", status="completed", usage=SimpleNamespace(input_tokens=100, output_tokens=10))


def text(content):
    return SimpleNamespace(output=[Item(type="message", role="assistant", content=[{"type": "output_text", "text": content}])],
                           output_text=content, status="completed", usage=SimpleNamespace(input_tokens=100, output_tokens=10))


SEARCH = call("crm_search_accounts", {"company_name": "Acme"}, "c1")
DEFERRED = text("Acme is owned by Maya Chen and renews 2026-11-15 [crm:accounts/17]. I can check open tickets if you want.")
TICKETS = call("support_get_open_tickets", {"organization_id": "org-acme"}, "c2")
COMPLETE = text("Acme (Maya Chen, renews 2026-11-15) [crm:accounts/17] has 2 open tickets: [support:tickets/134] HIGH, [support:tickets/171] LOW.")


class CompletionCheckTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        self.app.traces = None

    def runtime(self, **overrides):
        rt = self.app.runtime()
        rt.trace_store = None
        for key, value in overrides.items():
            setattr(rt, key, value)
        return rt

    def model(self, *responses):
        client = SimpleNamespace(responses=Mock())
        client.responses.create.side_effect = list(responses)
        return OpenAIModel("test-model", client=client), client

    def test_deferred_ticket_lookup_is_corrected_once(self):
        model, client = self.model(SEARCH, DEFERRED, TICKETS, COMPLETE)
        record = self.runtime().run_record(model, "Prepare an account brief for Acme")
        self.assertEqual(record.status, "completed")
        self.assertIn("2 open tickets", record.answer)
        events = [e["type"] for e in record.events]
        self.assertIn("completion_check_failed", events)
        self.assertIn("completion_check_passed", events)
        feedback = client.responses.create.call_args_list[2].kwargs["input"][-1]
        self.assertEqual(feedback["role"], "user")
        self.assertTrue(feedback["content"].startswith("Harness check:"))
        self.assertEqual(record.tool_calls, 2)

    def test_still_incomplete_after_retry_fails_closed(self):
        model, _ = self.model(SEARCH, DEFERRED, DEFERRED)
        with self.assertRaises(RunError) as raised:
            self.runtime().run_record(model, "Prepare an account brief for Acme")
        self.assertIn("brief_includes_open_tickets", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, IncompleteAnswerError)
        self.assertEqual(raised.exception.record.status, "failed")

    def test_warn_mode_accepts_and_flags(self):
        model, _ = self.model(SEARCH, DEFERRED, DEFERRED)
        record = self.runtime(completion_check_mode="warn").run_record(model, "Prepare an account brief for Acme")
        self.assertEqual(record.metadata["completion_unmet"], ["brief_includes_open_tickets"])

    def test_not_triggered_for_ambiguous_matches_or_other_tasks(self):
        northstar = call("crm_search_accounts", {"company_name": "Northstar"}, "c1")
        ask = text("Which account did you mean: Northstar Labs [crm:accounts/51] or Northstar Retail [crm:accounts/52]?")
        model, client = self.model(northstar, ask)
        record = self.runtime().run_record(model, "Prepare an account brief for Northstar")
        self.assertEqual(record.status, "completed")
        self.assertEqual(client.responses.create.call_count, 2)
        model, client = self.model(SEARCH, text("Maya Chen owns Acme [crm:accounts/17]."))
        self.runtime().run_record(model, "Who owns Acme?")
        self.assertEqual(client.responses.create.call_count, 2)

    def test_not_triggered_when_required_tool_is_not_configured(self):
        rt = self.runtime()
        self.assertEqual(rt.unmet_checks("account brief for Acme", [{"data": [{"support_customer_id": "x"}]}], set(), {"crm_search_accounts"}), [])

    def test_check_definitions_are_validated(self):
        for bad in ([{"id": "x"}], [{"id": "x", "require_any_tool": ["t"], "feedback": ""}],
                    [{"id": "x", "require_any_tool": ["t"], "feedback": "f", "when_message": "("}], "nope"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                validate_completion_checks(bad)
        with self.assertRaises(ValueError):
            Runtime(self.app.registry, completion_check_mode="maybe")

    def test_identical_call_reuses_result_and_loops_stop(self):
        executed = []
        rt = self.runtime()
        original = rt.execute
        rt.execute = lambda name, args: executed.append(name) or original(name, args)
        again = call("crm_search_accounts", {"company_name": "Acme"}, "c9")
        model, client = self.model(SEARCH, again, text("Maya Chen owns Acme [crm:accounts/17]."))
        record = rt.run_record(model, "Who owns Acme?")
        self.assertEqual(executed, ["crm_search_accounts"])
        cached = [e for e in record.events if e["type"] == "tool_result" and e.get("cached")]
        self.assertEqual(len(cached), 1)
        self.assertIn("Repeated call", json.loads(client.responses.create.call_args.kwargs["input"][-1]["output"])["notice"])
        model, _ = self.model(SEARCH, again, again, again)
        with self.assertRaises(RunError) as raised:
            rt.run_record(model, "Who owns Acme?")
        self.assertIn("repeated the same", str(raised.exception))

    def test_demo_suite_still_passes_with_checks_enabled(self):
        import tempfile
        from pathlib import Path
        from evaluations.runner import evaluate
        with tempfile.TemporaryDirectory() as directory:
            app = Application()
            app.traces.directory = Path(directory) / "runs"
            app.evaluation_dir = Path(directory) / "evaluations"
            self.assertEqual(evaluate(app)["summary"]["passed"], 10)


if __name__ == "__main__":
    unittest.main()
