import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4
from runtime import RunError
from tracing import utc_now

CASES_PATH = Path(__file__).with_name("cases.json")


def grade(record, case):
    calls = [event for event in record.events if event["type"] == "tool_call"]
    checks = {"completed": record.status == "completed"}
    for index, expected in enumerate(case.get("required_calls", [])):
        checks[f"tool_call_{index}"] = any(
            call["tool_name"] == expected["tool_name"] and all(
                str(call["arguments"].get(key, "")).casefold() == str(value).casefold()
                for key, value in expected["arguments"].items()
            ) for call in calls
        )
    checks["no_forbidden_tools"] = not any(call["tool_name"] in case.get("forbidden_tools", []) for call in calls)
    tickets = set()
    for event in record.events:
        if event["type"] == "tool_result" and event["tool_name"] == "support_get_open_tickets":
            tickets.update(row["ticket_id"] for row in event["result"].get("data", []))
    if "ticket_ids" in case:
        checks["retrieved_ticket_ids"] = tickets == set(case["ticket_ids"])
    citations = set(re.findall(r"\[([a-zA-Z][\w-]*:[^\]\s]+)\]", record.answer))
    checks["required_citations"] = set(case.get("required_citations", [])) <= citations
    checks["citation_validity"] = citations <= {source["id"] for source in record.sources}
    for index, pattern in enumerate(case.get("answer_patterns", [])):
        checks[f"answer_fact_{index}"] = bool(re.search(pattern, record.answer, re.IGNORECASE | re.DOTALL))
    checks["no_forbidden_claims"] = not any(
        re.search(pattern, record.answer, re.IGNORECASE) for pattern in case.get("forbidden_patterns", [])
    )
    return checks


def evaluate(app, provider="demo", model_name=None, cases_path=None, repeat=1):
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    path = Path(cases_path or CASES_PATH)
    raw = path.read_bytes()
    cases = json.loads(raw)
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Evaluation cases must be nonempty and have unique IDs")
    # Fail configuration before starting a suite, rather than recording N identical errors.
    model = app.model(provider, model_name)
    resolved_model = model.model_name
    close = getattr(model, "close", None)
    if close:
        close()
    results = []
    for iteration in range(repeat):
        for case in cases:
            try:
                record = app.run(case["message"], provider, model_name)
            except RunError as error:
                record = error.record
            checks = grade(record, case)
            results.append({"case_id": case["id"], "iteration": iteration + 1,
                            "passed": all(checks.values()), "checks": checks,
                            "run_id": record.run_id, "answer": record.answer, "error": record.error,
                            "duration_ms": record.duration_ms, "tool_calls": record.tool_calls,
                            "usage": record.usage if record.usage_available else None,
                            "estimated_cost_usd": record.estimated_cost_usd})
    passed = sum(row["passed"] for row in results)
    report = {
        "evaluation_id": uuid4().hex, "created_at": utc_now(), "provider": provider,
        "model": resolved_model,
        "kind": "live_model" if provider == "openai" else "deterministic_demo",
        "dataset_hash": app.dataset_hash, "suite_hash": hashlib.sha256(raw).hexdigest(),
        "config_hash": app.config_hash, "code_hash": app.code_hash,
        "summary": {"passed": passed, "total": len(results), "pass_rate": passed / len(results),
                    "mean_duration_ms": round(sum(row["duration_ms"] for row in results) / len(results), 2),
                    "total_tool_calls": sum(row["tool_calls"] for row in results),
                    "estimated_cost_usd": sum(row["estimated_cost_usd"] for row in results)
                    if all(row["estimated_cost_usd"] is not None for row in results) else None},
        "limitations": "Rule-based checks of expected facts, calls, retrieved IDs and citation membership; not a semantic grounding judge. Demo results are not LLM accuracy. Human review is still required.",
        "results": results,
    }
    app.evaluation_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = app.evaluation_dir / (report["evaluation_id"] + ".json")
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    destination.chmod(0o600)
    return report


def compare_reports(current, previous):
    if any(current[key] != previous[key] for key in ("dataset_hash", "suite_hash", "kind")):
        raise ValueError("Comparison requires matching dataset, suite, and evaluation kind")
    old = {}
    for row in previous["results"]:
        old.setdefault(row["case_id"], []).append(row["passed"])
    new = {}
    for row in current["results"]:
        new.setdefault(row["case_id"], []).append(row["passed"])
    if old.keys() != new.keys():
        raise ValueError("Reports contain different case IDs")
    deltas = {key: sum(new[key]) / len(new[key]) - sum(old[key]) / len(old[key]) for key in old}
    return {"pass_rate_delta": current["summary"]["pass_rate"] - previous["summary"]["pass_rate"],
            "regressions": [key for key, value in deltas.items() if value < 0],
            "improvements": [key for key, value in deltas.items() if value > 0]}
