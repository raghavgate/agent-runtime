"""Anthropic Messages API adapter. Runtime owns execution; this adapter owns wire format.

Implements the same protocol as openai_model.OpenAIModel: generate(), reset(), start_turn(),
export_state(), last_usage, provider and model_name. The runtime does not know which one it has.
"""
import json
from copy import deepcopy
from openai_model import DEFAULT_INSTRUCTIONS, ModelError
from schema import json_schema


class AnthropicModel:
    provider = "anthropic"

    def __init__(self, model_name, client=None, instructions=DEFAULT_INSTRUCTIONS, max_output_tokens=1200,
                 max_history_bytes=120000):
        if not model_name:
            raise ValueError("Set ANTHROPIC_MODEL or pass --model for Anthropic runs")
        self.model_name = model_name
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        self.max_output_tokens = max_output_tokens
        if isinstance(max_history_bytes, bool) or not isinstance(max_history_bytes, int) or max_history_bytes < 1:
            raise ValueError("max_history_bytes must be a positive integer")
        self.max_history_bytes = max_history_bytes
        self.owns_client = client is None
        if client is None:
            import os
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise ValueError("ANTHROPIC_API_KEY is not set in this terminal")
            from anthropic import Anthropic
            client = Anthropic(timeout=30.0, max_retries=0)
        self.client = client
        self.instructions = instructions
        self.reset()

    def close(self):
        if self.owns_client:
            self.client.close()

    def reset(self):
        self.history = []
        self.pending_call_id = None
        self.last_usage = None
        self.turn_ready = False

    def start_turn(self, user_message, state):
        """Restore an opaque, locally saved transcript and append one new user turn."""
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("history"), list):
            raise ModelError("Conversation state is incompatible; start a new conversation")
        if not all(isinstance(item, dict) and item.get("role") in ("user", "assistant") for item in state["history"]):
            raise ModelError("Conversation history is invalid")
        self.reset()
        self.history = deepcopy(state["history"])
        self.history.append({"role": "user", "content": user_message})
        self.turn_ready = True

    def export_state(self):
        if self.pending_call_id:
            raise ModelError("Cannot save a conversation with a pending tool call")
        return {"version": 1, "history": deepcopy(self.history)}

    @staticmethod
    def convert_tools(tools):
        converted = []
        for tool in tools or []:
            schema = json_schema(tool["inputSchema"])
            if schema.get("type") != "object":
                raise ModelError(f"Tool {tool['name']} must take an object input")
            converted.append({"name": tool["name"], "description": tool["description"], "input_schema": schema})
        return converted

    def generate(self, user_message, tool_result=None, tools=None):
        self.last_usage = None
        if not self.history:
            self.history.append({"role": "user", "content": user_message})
        elif self.pending_call_id:
            self.history.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": self.pending_call_id,
                "content": json.dumps(tool_result, ensure_ascii=False)}]})
            self.pending_call_id = None
        elif self.turn_ready:
            self.turn_ready = False
        else:
            raise ModelError("No pending tool call; start a new run before generating again")
        converted_tools = self.convert_tools(tools)
        context_bytes = len(json.dumps({"messages": self.history, "system": self.instructions,
                                       "tools": converted_tools}, ensure_ascii=False).encode("utf-8"))
        if context_bytes > self.max_history_bytes:
            raise ModelError("Conversation context limit reached. Start a new conversation.")
        request = {"model": self.model_name, "system": self.instructions, "messages": deepcopy(self.history),
                   "max_tokens": self.max_output_tokens}
        if converted_tools:
            # The runtime executes one tool at a time, so ask for at most one call per response.
            request["tools"] = converted_tools
            request["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        try:
            response = self.client.messages.create(**request)
        except Exception as error:
            # SDK errors may contain request payloads; keep them out of traces/UI.
            raise ModelError(f"Anthropic request failed ({type(error).__name__}); check credentials, model access and connection") from error
        if response.usage:
            self.last_usage = {"input_tokens": response.usage.input_tokens,
                               "output_tokens": response.usage.output_tokens}
        if response.stop_reason not in ("end_turn", "tool_use", "stop_sequence"):
            raise ModelError(f"Anthropic response stopped with {response.stop_reason}; no partial answer was accepted")
        blocks = [block.model_dump(exclude_none=True) for block in response.content]
        # Keep every block (including thinking) so the next request carries the full assistant turn.
        self.history.append({"role": "assistant", "content": blocks})
        calls = [block for block in blocks if block.get("type") == "tool_use"]
        if len(calls) > 1:
            raise ModelError("Sequential adapter received multiple tool calls")
        if calls:
            call = calls[0]
            if not isinstance(call.get("input"), dict):
                raise ModelError("Model tool arguments must be a JSON object")
            self.pending_call_id = call["id"]
            return {"type": "tool_call", "tool_name": call["name"], "arguments": call["input"]}
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        if not text.strip():
            raise ModelError("Model returned no final text (possibly a refusal)")
        return {"type": "final_answer", "content": text}
