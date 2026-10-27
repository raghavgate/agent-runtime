"""Provider- and system-independent execution loop."""
import re
from time import monotonic
from tracing import RunRecord
from tool import ToolCallError


class RunError(ValueError):
    def __init__(self, message, record):
        super().__init__(message)
        self.record = record


class ToolExecutionError(ValueError):
    pass


class Runtime:
    def __init__(self, registry, max_tool_calls=10, trace_store=None, require_sources=False,
                 input_rate=None, output_rate=None, metadata=None):
        if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int) or max_tool_calls < 0:
            raise ValueError("max_tool_calls must be a nonnegative integer")
        self.registry = registry
        self.max_tool_calls = max_tool_calls
        self.trace_store = trace_store
        self.require_sources = require_sources
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.metadata = metadata or {}

    def execute(self, tool_name, arguments):
        try:
            selected_tool = self.registry.get(tool_name)
        except KeyError as error:
            raise ToolCallError(f"Unknown tool: {tool_name}") from error
        if not selected_tool.read_only:
            raise PermissionError("This prototype only executes read-only tools")
        return selected_tool.invoke(arguments)

    def run(self, model, user_message):
        """Compatibility entry point: return just the final answer."""
        return self.run_record(model, user_message).answer

    def run_record(self, model, user_message):
        record = RunRecord(user_message, getattr(model, "provider", "test"),
                           getattr(model, "model_name", type(model).__name__))
        record.metadata = dict(self.metadata)
        started = monotonic()
        try:
            if not isinstance(user_message, str) or not user_message.strip():
                raise ValueError("User message must be a nonempty string")
            reset = getattr(model, "reset", None)
            if reset:
                reset()
            tool_result = None
            sources = {}
            tools = self.registry.list_tools()
            record.event("run_started", max_tool_calls=self.max_tool_calls, tools=tools)
            while True:
                model_started = monotonic()
                try:
                    response = model.generate(user_message, tool_result, tools)
                finally:
                    # Incomplete/refused responses can still consume billable tokens.
                    usage = getattr(model, "last_usage", None)
                    if usage:
                        record.usage_available = True
                        for key in record.usage:
                            record.usage[key] += usage.get(key, 0)
                if not isinstance(response, dict):
                    raise ValueError("Model response must be a dictionary")
                record.event("model_response", response=response,
                             duration_ms=round((monotonic() - model_started) * 1000, 2))
                response_type = response.get("type")
                if response_type == "final_answer":
                    content = response.get("content")
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError("Final answer must contain nonempty text")
                    citations = set(re.findall(r"\[([a-zA-Z][\w-]*:[^\]\s]+)\]", content))
                    if citations - sources.keys():
                        raise ValueError("Answer cites sources that were not retrieved")
                    if self.require_sources and sources and not citations:
                        raise ValueError("Answer must cite at least one retrieved source")
                    record.answer = content
                    record.sources = list(sources.values())
                    record.status = "completed"
                    return record
                if response_type != "tool_call":
                    raise ValueError(f"Unexpected model response type: {response_type}")
                if record.tool_calls >= self.max_tool_calls:
                    raise ValueError(f"Model tool call limit: {record.tool_calls} tool calls reached")
                name = response.get("tool_name")
                arguments = response.get("arguments")
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise ValueError("Tool call requires a tool_name string and arguments object")
                record.tool_calls += 1
                tool_started = monotonic()
                record.event("tool_call", tool_name=name, arguments=arguments)
                try:
                    tool_result = self.execute(name, arguments)
                except PermissionError:
                    raise
                except ToolCallError as error:
                    tool_result = {"error": {"code": "invalid_tool_call", "message": str(error)}}
                except Exception as error:
                    raise ToolExecutionError(f"Tool {name} failed ({type(error).__name__})") from error
                if isinstance(tool_result, dict):
                    for source in tool_result.get("sources", []):
                        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
                            raise ValueError("Tool returned an invalid source reference")
                        sources[source["id"]] = source
                record.sources = list(sources.values())
                record.event("tool_result", tool_name=name, result=tool_result,
                             duration_ms=round((monotonic() - tool_started) * 1000, 2))
        except Exception as error:
            record.status = "failed"
            record.error = {"type": type(error).__name__, "message": str(error)}
            record.event("error", error_type=record.error["type"], message=record.error["message"])
            raise RunError(str(error), record) from error
        finally:
            record.duration_ms = round((monotonic() - started) * 1000, 2)
            if record.usage_available and self.input_rate is not None and self.output_rate is not None:
                record.estimated_cost_usd = (
                    record.usage["input_tokens"] * self.input_rate +
                    record.usage["output_tokens"] * self.output_rate
                ) / 1_000_000
            if self.trace_store:
                self.trace_store.save(record)
