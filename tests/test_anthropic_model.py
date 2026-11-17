import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from anthropic_model import AnthropicModel
from openai_model import ModelError
from app import Application


class Block(SimpleNamespace):
    def model_dump(self, **kwargs):
        return {k: v for k, v in vars(self).items() if v is not None}


def response(blocks=None, stop="end_turn"):
    return SimpleNamespace(content=blocks or [], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


TOOLS = [{"name": "lookup", "description": "Lookup", "inputSchema": {"id": "integer"}}]


class AnthropicAdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = SimpleNamespace(messages=Mock())
        self.model = AnthropicModel("test-model", client=self.client)

    def test_roundtrip_pairs_tool_use_id_and_keeps_thinking(self):
        thinking = Block(type="thinking", thinking="plan", signature="sig")
        call = Block(type="tool_use", id="toolu_17", name="lookup", input={"id": 17})
        self.client.messages.create.side_effect = [response([thinking, call], "tool_use"),
                                                   response([Block(type="text", text="Found it")])]
        generated = self.model.generate("Find account", tools=TOOLS)
        self.assertEqual(generated, {"type": "tool_call", "tool_name": "lookup", "arguments": {"id": 17}})
        self.assertEqual(self.model.generate("Find account", {"data": [17]}, TOOLS), {"type": "final_answer", "content": "Found it"})
        request = self.client.messages.create.call_args.kwargs
        self.assertEqual(request["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True})
        self.assertEqual(request["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(request["messages"][1]["content"][0]["signature"], "sig")
        result = request["messages"][-1]["content"][0]
        self.assertEqual(result["tool_use_id"], "toolu_17")
        self.assertEqual(json.loads(result["content"]), {"data": [17]})
        self.assertEqual(self.model.last_usage, {"input_tokens": 10, "output_tokens": 5})

    def test_reset_removes_previous_run(self):
        self.client.messages.create.return_value = response([Block(type="text", text="Hello")])
        self.model.generate("first")
        self.model.reset()
        self.model.generate("second")
        self.assertEqual(self.client.messages.create.call_args.kwargs["messages"], [{"role": "user", "content": "second"}])

    def test_truncated_refused_empty_or_multiple_calls(self):
        call = Block(type="tool_use", id="t", name="lookup", input={})
        for result in (response([Block(type="text", text="partial")], "max_tokens"), response([], "refusal"),
                       response(), response([call, call], "tool_use"), response([Block(type="tool_use", id="t", name="x", input=[])], "tool_use")):
            self.model.reset()
            self.client.messages.create.return_value = result
            with self.subTest(stop=result.stop_reason), self.assertRaises(ModelError):
                self.model.generate("hi")

    def test_sdk_error_does_not_echo_secrets(self):
        self.client.messages.create.side_effect = RuntimeError("secret-value")
        with self.assertRaises(ModelError) as raised:
            self.model.generate("hi")
        self.assertNotIn("secret-value", str(raised.exception))

    def test_conversation_state_roundtrip(self):
        self.client.messages.create.return_value = response([Block(type="text", text="Hi")])
        self.model.generate("first")
        state = self.model.export_state()
        other = AnthropicModel("test-model", client=self.client)
        other.start_turn("second", state)
        other.generate("second")
        sent = self.client.messages.create.call_args.kwargs["messages"]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user"])
        with self.assertRaises(ModelError):
            other.start_turn("x", {"version": 1, "history": [{"type": "function_call"}]})

    def test_requires_model_and_key(self):
        with self.assertRaises(ValueError):
            AnthropicModel("", client=self.client)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            AnthropicModel("test-model")

    def test_real_sdk_with_mock_http_transport(self):
        try:  # newer SDKs ship their own httpx fork
            import httpx2 as httpx
        except ImportError:
            import httpx
        from anthropic import Anthropic
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                content, stop = [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"id": 17}}], "tool_use"
            else:
                content, stop = [{"type": "text", "text": "Found account"}], "end_turn"
            return httpx.Response(200, json={"id": "msg_1", "type": "message", "role": "assistant", "model": "test-model",
                                            "content": content, "stop_reason": stop, "stop_sequence": None,
                                            "usage": {"input_tokens": 12, "output_tokens": 8}})

        with Anthropic(api_key="test-key-not-real", http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
            adapter = AnthropicModel("test-model", client=client)
            self.assertEqual(adapter.generate("lookup", tools=TOOLS)["arguments"], {"id": 17})
            self.assertEqual(adapter.generate("lookup", {"data": [17]}, TOOLS)["content"], "Found account")
        self.assertEqual(requests[1]["messages"][-1]["content"][0]["tool_use_id"], "toolu_1")
        self.assertTrue(requests[0]["tool_choice"]["disable_parallel_tool_use"])


class ProviderSelectionTests(unittest.TestCase):
    def test_app_builds_either_live_adapter_with_shared_instructions(self):
        app = Application()
        app.config['workflow_instructions'] = 'Include open tickets in an account brief.'
        for provider, path in (("openai", "openai_model.OpenAIModel"), ("anthropic", "anthropic_model.AnthropicModel")):
            with self.subTest(provider=provider), patch(path) as adapter:
                app.model(provider, "test-model")
                self.assertIn('Include open tickets', adapter.call_args.kwargs['instructions'])

    def test_resolved_model_uses_provider_specific_variable(self):
        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "a-model", "OPENAI_MODEL": "o-model"}):
            self.assertEqual(Application.resolved_model("anthropic"), "a-model")
            self.assertEqual(Application.resolved_model("openai"), "o-model")
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            Application.resolved_model("anthropic")

    def test_paid_anthropic_eval_requires_pricing(self):
        from evaluations.runner import evaluate
        app = Application()
        with patch("anthropic_model.AnthropicModel") as adapter, self.assertRaises(ValueError) as raised:
            adapter.return_value = SimpleNamespace(model_name="unpriced-model")
            evaluate(app, provider="anthropic", model_name="unpriced-model", profile="quick")
        self.assertIn("pricing", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
