import json
import tempfile
import unittest
from pathlib import Path
from app import Application
from model import FakeModel, LoopModel, ErrorModel
from registry import ToolRegistry
from runtime import Runtime, RunError
from tool import Tool
from tracing import TraceStore


class ScriptedModel:
    provider = "scripted"
    model_name = "test-sequence"

    def __init__(self, responses):
        self.responses = responses

    def reset(self):
        self.position = 0
        self.received = []

    def generate(self, user_message, tool_result=None, tools=None):
        self.received.append(tool_result)
        response = self.responses[min(self.position, len(self.responses) - 1)]
        self.position += 1
        return response


def call(name="echo", **arguments):
    return {"type": "tool_call", "tool_name": name, "arguments": arguments}


def final(content="done"):
    return {"type": "final_answer", "content": content}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.executed = []
        self.registry.register(Tool("echo", "Echo text", {"text": "string"}, "string", self.echo))

    def echo(self, text):
        self.executed.append(text)
        return text

    def test_final_answer_at_exact_budget(self):
        model = ScriptedModel([call(text="hello"), final()])
        record = Runtime(self.registry, max_tool_calls=1).run_record(model, "hello")
        self.assertEqual(record.tool_calls, 1)
        self.assertEqual(record.answer, "done")
        self.assertEqual(model.received, [None, "hello"])

    def test_limit_prevents_extra_execution(self):
        with self.assertRaises(RunError) as raised:
            Runtime(self.registry, max_tool_calls=2).run(ScriptedModel([call(text="hello")]), "hi")
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(raised.exception.record.tool_calls, 2)

    def test_zero_budget_allows_final_only(self):
        self.assertEqual(Runtime(self.registry, max_tool_calls=0).run(ScriptedModel([final()]), "hi"), "done")
        with self.assertRaises(RunError):
            Runtime(self.registry, max_tool_calls=0).run(ScriptedModel([call(text="hi")]), "hi")
        self.assertEqual(self.executed, [])

    def test_invalid_arguments_return_to_model_without_execution(self):
        for arguments in ({"text": 5}, {"text": "hi", "extra": 1}, {}):
            with self.subTest(arguments=arguments):
                model = ScriptedModel([{"type": "tool_call", "tool_name": "echo", "arguments": arguments}, final()])
                Runtime(self.registry).run(model, "hi")
                self.assertEqual(model.received[1]["error"]["code"], "invalid_tool_call")
        self.assertEqual(self.executed, [])

    def test_invalid_attempts_consume_budget(self):
        with self.assertRaises(RunError):
            Runtime(self.registry, max_tool_calls=2).run(ScriptedModel([call(text=5)]), "hi")
        self.assertEqual(self.executed, [])

    def test_unknown_tool_can_recover(self):
        model = ScriptedModel([call("missing"), call(text="fixed"), final()])
        Runtime(self.registry).run(model, "hi")
        self.assertIn("Unknown tool", model.received[1]["error"]["message"])
        self.assertEqual(self.executed, ["fixed"])

    def test_write_tools_are_blocked(self):
        self.registry.register(Tool("write", "write", {}, "string", lambda: "changed", read_only=False))
        with self.assertRaises(RunError) as raised:
            Runtime(self.registry).run(ScriptedModel([call("write")]), "hi")
        self.assertEqual(raised.exception.record.error["type"], "PermissionError")

    def test_output_contract_failure_is_terminal(self):
        self.registry.register(Tool("bad", "bad", {}, "integer", lambda: "wrong"))
        with self.assertRaises(RunError) as raised:
            Runtime(self.registry).run(ScriptedModel([call("bad"), final()]), "hi")
        self.assertIn("ToolOutputError", str(raised.exception))

    def test_tool_programming_error_is_terminal(self):
        def broken():
            raise ValueError("private error contents")
        self.registry.register(Tool("broken", "broken", {}, "integer", broken))
        with self.assertRaises(RunError) as raised:
            Runtime(self.registry).run(ScriptedModel([call("broken")]), "hi")
        self.assertNotIn("private error", json.dumps(raised.exception.record.to_dict()))

    def test_malformed_responses_fail(self):
        for response in (None, {}, {"type": "error"}, final(""), call(text="hello") | {"arguments": []}):
            with self.subTest(response=response), self.assertRaises(RunError):
                Runtime(self.registry).run(ScriptedModel([response]), "hi")

    def test_failure_trace_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory)
            with self.assertRaises(RunError) as raised:
                Runtime(self.registry, trace_store=store).run(ErrorModel(), "hi")
            run_id = raised.exception.record.run_id
            saved = store.get(run_id)
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["events"][-1]["type"], "error")
            self.assertGreaterEqual(saved["duration_ms"], 0)
            with self.assertRaises(ValueError):
                store.get("../config")

    def test_usage_and_configured_cost(self):
        model = ScriptedModel([final()])
        model.last_usage = {"input_tokens": 100, "output_tokens": 50}
        record = Runtime(self.registry, input_rate=1, output_rate=2).run_record(model, "hi")
        self.assertEqual(record.usage["input_tokens"], 100)
        self.assertAlmostEqual(record.estimated_cost_usd, 0.0002)

    def test_failed_model_response_still_records_usage(self):
        class Incomplete:
            last_usage = None

            def generate(self, *args):
                self.last_usage = {"input_tokens": 100, "output_tokens": 50}
                raise ValueError("Incomplete response")

        with self.assertRaises(RunError) as raised:
            Runtime(self.registry).run(Incomplete(), "hi")
        self.assertTrue(raised.exception.record.usage_available)
        self.assertEqual(raised.exception.record.usage["input_tokens"], 100)

    def test_original_fake_model_still_works(self):
        from integrations.support import search_customer_tool, get_open_tickets_tool
        registry = ToolRegistry()
        registry.register(search_customer_tool)
        registry.register(get_open_tickets_tool)
        self.assertEqual(Runtime(registry).run(FakeModel(), "Find Acme"), "Acme has 2 open tickets")
        with self.assertRaises(RunError):
            Runtime(registry, max_tool_calls=2).run(LoopModel(), "loop")

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(self.registry.get("echo"))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()

    def test_search_ambiguity_and_case(self):
        result = self.app.registry.get("crm_search_accounts").invoke({"company_name": "northstar"})
        self.assertEqual(len(result["data"]), 2)
        result = self.app.registry.get("crm_search_accounts").invoke({"company_name": "aCmE"})
        self.assertEqual(result["data"][0]["customer_id"], 17)

    def test_no_tickets_distinct_from_unknown_organization(self):
        tool = self.app.registry.get("support_get_open_tickets")
        self.assertEqual(tool.invoke({"organization_id": "org-initech"})["data"], [])
        with self.assertRaises(ValueError):
            tool.invoke({"organization_id": "not-real"})

    def test_unknown_or_missing_citations_fail(self):
        for answer in ("Acme [crm:accounts/999]", "Acme has an owner"):
            model = ScriptedModel([call("crm_search_accounts", company_name="Acme"), final(answer)])
            with self.assertRaises(RunError):
                Runtime(self.app.registry, require_sources=True).run(model, "Acme")

    def test_source_state_does_not_leak_between_runs(self):
        runtime = Runtime(self.app.registry, require_sources=True)
        runtime.run(ScriptedModel([call("crm_search_accounts", company_name="Acme"), final("Acme [crm:accounts/17]")]), "Acme")
        with self.assertRaises(RunError):
            runtime.run(ScriptedModel([final("Acme [crm:accounts/17]")]), "Acme")

    def test_another_system_requires_no_runtime_change(self):
        registry = ToolRegistry()
        registry.register(Tool("inventory_stock", "Stock quantity", {"sku": "string"}, "integer", lambda sku: 12))
        answer = Runtime(registry).run(ScriptedModel([call("inventory_stock", sku="ABC"), final("12 units")]), "stock")
        self.assertEqual(answer, "12 units")


if __name__ == "__main__":
    unittest.main()
