"""Provider- and system-independent execution loop."""
import json
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


MAX_IDENTICAL_REPEATS = 2


class IncompleteAnswerError(ValueError):
    pass


def validate_completion_checks(checks):
    """Completion checks are declarative workspace rules; the runtime stays domain-agnostic."""
    if not isinstance(checks, list):
        raise ValueError("completion_checks must be a list")
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"]:
            raise ValueError("Each completion check needs an id")
        tools = check.get("require_any_tool")
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
            raise ValueError(f"Completion check {check['id']} needs require_any_tool")
        if not isinstance(check.get("feedback"), str) or not check["feedback"].strip():
            raise ValueError(f"Completion check {check['id']} needs feedback text")
        for key in ("when_message", "when_single_value"):
            if key in check and not isinstance(check[key], str):
                raise ValueError(f"Completion check {check['id']}: {key} must be a string")
        if "when_message" in check:
            re.compile(check["when_message"])
    return checks


def _field_values(results, field):
    values = set()
    for result in results:
        data = result.get("data") if isinstance(result, dict) else None
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        for row in rows:
            if isinstance(row, dict) and row.get(field) not in (None, ""):
                values.add(json.dumps(row[field], sort_keys=True))
    return values


class Runtime:
    def __init__(self, registry, max_tool_calls=10, trace_store=None, require_sources=False,
                 input_rate=None, output_rate=None, metadata=None, max_total_tokens=None,
                 completion_checks=None, max_completion_retries=1, completion_check_mode="fail"):
        if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int) or max_tool_calls < 0:
            raise ValueError("max_tool_calls must be a nonnegative integer")
        self.registry = registry
        self.max_tool_calls = max_tool_calls
        self.trace_store = trace_store
        self.require_sources = require_sources
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.metadata = metadata or {}
        if max_total_tokens is not None and (isinstance(max_total_tokens, bool) or not isinstance(max_total_tokens, int) or max_total_tokens < 1):
            raise ValueError("max_total_tokens must be a positive integer")
        self.max_total_tokens = max_total_tokens
        self.completion_checks = validate_completion_checks(completion_checks or [])
        if isinstance(max_completion_retries, bool) or not isinstance(max_completion_retries, int) or not 0 <= max_completion_retries <= 3:
            raise ValueError("max_completion_retries must be an integer from 0 to 3")
        self.max_completion_retries = max_completion_retries
        if completion_check_mode not in ("fail", "warn"):
            raise ValueError("completion_check_mode must be 'fail' or 'warn'")
        self.completion_check_mode = completion_check_mode

    def unmet_checks(self, user_message, results, called_tools, available_tools):
        """Return checks whose conditions hold for this turn but whose required tool was never called."""
        unmet = []
        for check in self.completion_checks:
            required = set(check["require_any_tool"])
            if not required & available_tools or required & called_tools:
                continue
            if "when_message" in check and not re.search(check["when_message"], user_message, re.IGNORECASE):
                continue
            if "when_single_value" in check and len(_field_values(results, check["when_single_value"])) != 1:
                continue
            unmet.append(check)
        return unmet

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

    def run_record(self, model, user_message, model_state=None, prior_sources=None):
        record = RunRecord(user_message, getattr(model, "provider", "test"),
                           getattr(model, "model_name", type(model).__name__))
        record.metadata = dict(self.metadata)
        started = monotonic()
        try:
            if not isinstance(user_message, str) or not user_message.strip():
                raise ValueError("User message must be a nonempty string")
            if model_state is not None:
                start_turn = getattr(model, "start_turn", None)
                if not start_turn:
                    raise ValueError("This model adapter does not support conversations")
                start_turn(user_message, model_state)
            else:
                reset = getattr(model, "reset", None)
                if reset:
                    reset()
            tool_result = None
            sources = {source["id"]: source for source in (prior_sources or [])}
            record.sources = list(sources.values())
            retrieved_this_turn = False
            results_this_turn, called_tools = [], set()
            completion_retries = 0
            call_cache, repeats = {}, {}
            tools = self.registry.list_tools()
            available_tools = {tool["name"] for tool in tools}
            record.event("run_started", max_tool_calls=self.max_tool_calls,
                         max_total_tokens=self.max_total_tokens, tools=tools)
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
                    if self.require_sources and retrieved_this_turn and not citations:
                        raise ValueError("Answer must cite at least one retrieved source")
                    unmet = self.unmet_checks(user_message, results_this_turn, called_tools, available_tools)
                    if unmet:
                        ids = [check["id"] for check in unmet]
                        can_continue = (completion_retries < self.max_completion_retries
                                        and hasattr(model, "export_state") and hasattr(model, "start_turn")
                                        and (self.max_total_tokens is None or sum(record.usage.values()) < self.max_total_tokens))
                        if can_continue:
                            # Reject the incomplete answer and give the model one precise correction.
                            completion_retries += 1
                            feedback = "Harness check: your answer is incomplete. " + " ".join(check["feedback"] for check in unmet)
                            record.event("completion_check_failed", checks=ids, rejected_answer=content, retry=completion_retries)
                            model.start_turn(feedback, model.export_state())
                            tool_result = None
                            continue
                        record.event("completion_check_unmet", checks=ids)
                        if self.completion_check_mode == "fail":
                            raise IncompleteAnswerError("Answer incomplete: " + ", ".join(ids))
                        record.metadata["completion_unmet"] = ids
                    elif completion_retries:
                        record.event("completion_check_passed", retries=completion_retries)
                    record.answer = content
                    record.sources = list(sources.values())
                    record.status = "completed"
                    return record
                if response_type != "tool_call":
                    raise ValueError(f"Unexpected model response type: {response_type}")
                if self.max_total_tokens is not None and sum(record.usage.values()) >= self.max_total_tokens:
                    raise ValueError("Run token threshold reached; no further requests were made")
                if record.tool_calls >= self.max_tool_calls:
                    raise ValueError(f"Model tool call limit: {record.tool_calls} tool calls reached")
                name = response.get("tool_name")
                arguments = response.get("arguments")
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise ValueError("Tool call requires a tool_name string and arguments object")
                record.tool_calls += 1
                called_tools.add(name)
                tool_started = monotonic()
                record.event("tool_call", tool_name=name, arguments=arguments)
                key = (name, json.dumps(arguments, sort_keys=True, default=str))
                if key in call_cache:
                    # Identical read-only call in the same turn: reuse the result instead of hitting the system again.
                    repeats[key] = repeats.get(key, 0) + 1
                    if repeats[key] >= MAX_IDENTICAL_REPEATS:
                        raise ValueError(f"Model repeated the same {name} call {repeats[key] + 1} times; stopped to avoid a loop")
                    tool_result = dict(call_cache[key]) if isinstance(call_cache[key], dict) else call_cache[key]
                    if isinstance(tool_result, dict):
                        tool_result["notice"] = "Repeated call: this is the result already returned earlier in this turn. Use it instead of calling again."
                    record.event("tool_result", tool_name=name, result=tool_result, cached=True,
                                 duration_ms=round((monotonic() - tool_started) * 1000, 2))
                    continue
                try:
                    tool_result = self.execute(name, arguments)
                except PermissionError:
                    raise
                except ToolCallError as error:
                    tool_result = {"error": {"code": "invalid_tool_call", "message": str(error)}}
                except Exception as error:
                    raise ToolExecutionError(f"Tool {name} failed ({type(error).__name__})") from error
                if not (isinstance(tool_result, dict) and "error" in tool_result):
                    call_cache[key] = tool_result
                if isinstance(tool_result, dict):
                    results_this_turn.append(tool_result)
                    retrieved_this_turn = retrieved_this_turn or bool(tool_result.get("sources"))
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
