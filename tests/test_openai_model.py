import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from openai_model import ModelError, OpenAIModel


class Item(SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


def response(items=None, text="", status="completed"):
    return SimpleNamespace(output=items or [], output_text=text, status=status,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = SimpleNamespace(responses=Mock())
        self.model = OpenAIModel("test-model", client=self.client)

    def test_roundtrip_pairs_call_id_and_keeps_reasoning(self):
        reasoning = Item(type="reasoning", id="reasoning_1", encrypted_content="opaque", summary=[])
        function = Item(type="function_call", name="lookup", arguments='{"id":17}', call_id="call_17")
        self.client.responses.create.side_effect = [response([reasoning, function]), response(text="Found it")]
        tools = [{"name": "lookup", "description": "Lookup", "inputSchema": {"id": "integer"}}]
        generated = self.model.generate("Find account", tools=tools)
        self.assertEqual(generated["arguments"], {"id": 17})
        self.assertEqual(self.model.generate("Find account", {"data": [17]}, tools)["type"], "final_answer")
        request = self.client.responses.create.call_args.kwargs
        self.assertFalse(request["store"])
        self.assertFalse(request["parallel_tool_calls"])
        self.assertEqual(request["input"][1]["encrypted_content"], "opaque")
        self.assertEqual(request["input"][-1]["call_id"], "call_17")
        self.assertEqual(json.loads(request["input"][-1]["output"]), {"data": [17]})
        self.assertEqual(self.model.last_usage, {"input_tokens": 10, "output_tokens": 5})

    def test_reset_removes_previous_run(self):
        self.client.responses.create.return_value = response(text="Hello")
        self.model.generate("first")
        self.model.reset()
        self.model.generate("second")
        self.assertEqual(self.client.responses.create.call_args.kwargs["input"], [{"role": "user", "content": "second"}])

    def test_invalid_json_arguments(self):
        for arguments in ("{broken", "[]", "null"):
            self.model.reset()
            self.client.responses.create.return_value = response([Item(type="function_call", name="lookup", arguments=arguments, call_id="c")])
            with self.subTest(arguments=arguments), self.assertRaises(ModelError):
                self.model.generate("hi")

    def test_incomplete_empty_or_multiple_calls(self):
        function = Item(type="function_call", name="lookup", arguments="{}", call_id="c")
        for result in (response(status="incomplete"), response(), response([function, function])):
            self.model.reset()
            self.client.responses.create.return_value = result
            with self.assertRaises(ModelError):
                self.model.generate("hi")

    def test_sdk_error_does_not_echo_secrets(self):
        self.client.responses.create.side_effect = RuntimeError("secret-value")
        with self.assertRaises(ModelError) as raised:
            self.model.generate("hi")
        self.assertNotIn("secret-value", str(raised.exception))

    def test_optional_properties_use_non_strict_mode(self):
        tools = [{"name": "lookup", "description": "lookup", "inputSchema": {
            "type": "object", "properties": {"id": {"type": "integer"}}, "required": [], "additionalProperties": False}}]
        self.assertFalse(self.model.convert_tools(tools)[0]["strict"])

    def test_nested_optional_properties_use_non_strict_mode(self):
        tools = [{"name": "lookup", "description": "lookup", "inputSchema": {
            "type": "object", "properties": {"filter": {"type": "object", "properties": {
                "name": {"type": "string"}}, "additionalProperties": False}},
            "required": ["filter"], "additionalProperties": False}}]
        self.assertFalse(self.model.convert_tools(tools)[0]["strict"])

    def test_real_sdk_with_mock_http_transport(self):
        import httpx
        from openai import OpenAI
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                output = [{"type": "function_call", "id": "fc_1", "call_id": "call_1",
                           "name": "lookup", "arguments": '{"id":17}', "status": "completed"}]
            else:
                output = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": "Found account", "annotations": []}]}]
            return httpx.Response(200, json={"id": "resp_1", "object": "response", "created_at": 1,
                                            "model": "test-model", "status": "completed", "output": output,
                                            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}})

        with OpenAI(api_key="test-key-not-real", http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
            adapter = OpenAIModel("test-model", client=client)
            self.assertEqual(adapter.generate("lookup")["arguments"], {"id": 17})
            self.assertEqual(adapter.generate("lookup", {"data": [17]})["content"], "Found account")
        self.assertEqual(requests[1]["input"][-1]["call_id"], "call_1")


if __name__ == "__main__":
    unittest.main()
