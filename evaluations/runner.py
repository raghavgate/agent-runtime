import hashlib
import json
import math
import re
from pathlib import Path
from uuid import uuid4
from pricing import estimate
from runtime import RunError
from tracing import utc_now
from evaluations.store import ReportStore

CASES_PATH = Path(__file__).with_name("cases.json")
PROFILES = {
    "quick": {"case_ids": ["acme-brief", "unknown-customer", "ambiguous-company"],
              "max_tool_calls": 4, "max_output_tokens": 700, "max_total_tokens": 12000},
    "full": {"case_ids": None, "max_tool_calls": 6, "max_output_tokens": 1200, "max_total_tokens": 20000},
}


def load_cases(cases_path=None, profile="full"):
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError("Unknown evaluation profile")
    cases = json.loads(Path(cases_path or CASES_PATH).read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError("Evaluation cases must be a nonempty list")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not isinstance(case.get("message"), str) or not case["message"].strip():
            raise ValueError("Each evaluation case needs a string ID and nonempty message")
        for pattern in case.get("answer_patterns", []) + case.get("forbidden_patterns", []):
            re.compile(pattern)
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Evaluation case IDs must be unique")
    if PROFILES[profile]["case_ids"] and cases_path is None:
        indexed = {case["id"]: case for case in cases}
        cases = [indexed[key] for key in PROFILES[profile]["case_ids"]]
    elif profile == "quick":
        cases = cases[:3]
    return cases


def grade(record, case):
    answer = record.answer.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "\u00a0": " "}))
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
    for index, expected in enumerate(case.get("expected_records", [])):
        observed = set()
        for event in record.events:
            if event["type"] == "tool_result" and event["tool_name"] == expected["tool_name"] and isinstance(event["result"], dict):
                for row in event["result"].get("data", []):
                    observed.add(str(row.get(expected["id_field"])))
        checks[f"retrieved_records_{index}"] = observed == {str(value) for value in expected["ids"]}
    tickets = set()
    for event in record.events:
        if event["type"] == "tool_result" and event["tool_name"] in ("support_get_open_tickets", "support_get_open_tickets_batch"):
            tickets.update(row["ticket_id"] for row in event["result"].get("data", []))
    if "ticket_ids" in case:
        checks["retrieved_ticket_ids"] = tickets == set(case["ticket_ids"])
    citations = set(re.findall(r"\[([a-zA-Z][\w-]*:[^\]\s]+)\]", record.answer))
    checks["required_citations"] = set(case.get("required_citations", [])) <= citations
    checks["citation_validity"] = citations <= {source["id"] for source in record.sources}
    for index, pattern in enumerate(case.get("answer_patterns", [])):
        checks[f"answer_fact_{index}"] = bool(re.search(pattern, answer, re.IGNORECASE | re.DOTALL))
    checks["no_forbidden_claims"] = not any(re.search(pattern, answer, re.IGNORECASE) for pattern in case.get("forbidden_patterns", []))
    return checks


def efficiency_notes(record):
    seen_calls, seen_data, notes = set(), set(), []
    for event in record.events:
        if event["type"] == "tool_call":
            key = (event["tool_name"], json.dumps(event["arguments"], sort_keys=True))
            if key in seen_calls:
                notes.append("The same tool and arguments were requested more than once.")
            seen_calls.add(key)
        if event["type"] == "tool_result" and isinstance(event["result"], dict):
            data = event["result"].get("data")
            if data:
                key = json.dumps(data, sort_keys=True)
                if key in seen_data:
                    notes.append("A retrieval returned data already available earlier in the run.")
                seen_data.add(key)
    return sorted(set(notes))


def result_row(record, case, iteration, rates=None):
    checks = grade(record, case)
    usage = record.usage if record.usage_available else None
    cost = record.estimated_cost_usd if record.estimated_cost_usd is not None else estimate(usage, rates)
    return {"case_id": case["id"], "iteration": iteration, "passed": all(checks.values()),
            "checks": checks, "run_id": record.run_id, "answer": record.answer,
            "error": record.error, "duration_ms": record.duration_ms, "tool_calls": record.tool_calls,
            "usage": usage, "estimated_cost_usd": cost, "efficiency_notes": efficiency_notes(record),
            "human_review": "pending"}


def save_report(app, cases, results, provider, model_name, profile, rates=None, limits=None,
                repeat=1, stopped_reason=None, deployment_metadata=None, split="development"):
    metadata = deployment_metadata or {"dataset_hash": app.dataset_hash,
                                       "config_hash": app.config_hash, "code_hash": app.code_hash}
    planned = len(cases) * repeat
    passed = sum(row["passed"] for row in results)
    total = len(results)
    report = {
        "evaluation_id": uuid4().hex, "created_at": utc_now(), "provider": provider, "model": model_name,
        "kind": "live_model" if provider == "openai" else "deterministic_demo", "profile": profile, "split": split,
        **metadata, "suite_hash": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        "grader_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limits": limits or {}, "pricing": rates, "stopped_reason": stopped_reason,
        "summary": {"passed": passed, "total": total, "planned": planned, "complete": total == planned,
                    "pass_rate": passed / total if total else 0,
                    "mean_duration_ms": round(sum(row["duration_ms"] for row in results) / total, 2) if total else 0,
                    "total_tool_calls": sum(row["tool_calls"] for row in results),
                    "usage": {key: sum(row["usage"][key] for row in results) for key in ("input_tokens", "output_tokens")}
                    if results and all(row["usage"] is not None for row in results) else None,
                    "estimated_cost_usd": sum(row["estimated_cost_usd"] for row in results)
                    if results and all(row["estimated_cost_usd"] is not None for row in results) else None,
                    "cases_with_efficiency_notes": sum(bool(row["efficiency_notes"]) for row in results)},
        "limitations": "Rule-based checks of expected facts, calls, retrieved IDs and citation membership; not a semantic grounding judge. Demo results are not LLM accuracy. Human review is still required. Cost uses standard input rates without cache discounts; spending/token guards are checked between requests or cases, not billing caps.",
        "results": results,
    }
    store = ReportStore(app.evaluation_dir)
    baseline = store.baseline(report)
    report["baseline_id"] = baseline["evaluation_id"] if baseline else None
    report["comparison"] = compare_reports(report, baseline) if baseline else None
    store.save(report)
    if not baseline and report["summary"]["complete"]:
        if store.claim_baseline(report):
            report["baseline_id"] = report["evaluation_id"]
            store.save(report)
    return report


def workspace_cases_path(app, cases_path=None, split="development"):
    if split not in ("development", "heldout"):
        raise ValueError("Evaluation split must be development or heldout")
    if cases_path is not None:
        return cases_path
    key = "evaluation_heldout_cases" if split == "heldout" else "evaluation_cases"
    if app.config.get(key):
        return app.base / app.config[key]
    if split == "heldout":
        raise ValueError("This workspace has no held-out evaluation cases")
    return None


def evaluate(app, provider="demo", model_name=None, cases_path=None, repeat=1, profile="full", max_cost_usd=0.05, split="development"):
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 20:
        raise ValueError("repeat must be between 1 and 20")
    if not isinstance(max_cost_usd, (int, float)) or isinstance(max_cost_usd, bool) or not math.isfinite(max_cost_usd) or max_cost_usd <= 0:
        raise ValueError("Evaluation spend threshold must be a finite positive number")
    if provider == "demo" and app.config.get("demo_evaluation_supported") is False:
        raise ValueError("This workspace's contact/deal scenarios require a real model; use validate-data for offline fixture checks")
    cases_path = workspace_cases_path(app, cases_path, split)
    cases = load_cases(cases_path, profile)
    # Bundled gold answers belong only to the bundled dataset.
    if cases_path is None and app.dataset_hash != app.sample_dataset_hash():
        raise ValueError("Supply --cases with gold expectations for your imported data before evaluating it")
    limits = {key: value for key, value in PROFILES[profile].items() if key != "case_ids"}
    model = app.model(provider, model_name, max_output_tokens=limits["max_output_tokens"])
    resolved_model = model.model_name
    close = getattr(model, "close", None)
    if close:
        close()
    rates = app.rates(resolved_model) if provider == "openai" else None
    if provider == "openai" and not rates:
        raise ValueError("Configure model-specific pricing before a paid evaluation so the spend guard can operate")
    results, stopped_reason = [], None
    for iteration in range(repeat):
        for case in cases:
            if sum(row["estimated_cost_usd"] or 0 for row in results) >= max_cost_usd:
                stopped_reason = "Estimated spend threshold reached; remaining cases were not run."
                break
            try:
                record = app.run(case["message"], provider, resolved_model, purpose="evaluation", **limits)
            except RunError as error:
                record = error.record
            results.append(result_row(record, case, iteration + 1, rates))
            if provider == "openai" and not record.usage_available and record.status == "failed":
                stopped_reason = "Provider failed without usage information; stopped instead of repeating requests."
                break
        if stopped_reason:
            break
    limits["max_estimated_cost_usd"] = max_cost_usd
    return save_report(app, cases, results, provider, resolved_model, profile, rates, limits, repeat, stopped_reason, split=split)


def evaluate_saved(app, manifest_path, profile="quick", cases_path=None, split="development"):
    """Grade existing traces without creating a model or making paid requests."""
    from tracing import RunRecord
    cases = load_cases(workspace_cases_path(app, cases_path, split), profile)
    manifest = json.loads(Path(manifest_path).read_text())
    if not isinstance(manifest, dict) or set(manifest) != {case["id"] for case in cases}:
        raise ValueError("Replay manifest must map each selected case ID to one saved run ID")
    records = []
    for case in cases:
        payload = app.traces.get(manifest[case["id"]])
        record = RunRecord(**payload)
        if record.user_message != case["message"]:
            raise ValueError("Saved run message does not match its evaluation case")
        records.append(record)
    first = records[0]
    identity = (first.provider, first.model, first.metadata.get("dataset_hash"),
                first.metadata.get("config_hash"), first.metadata.get("code_hash"))
    if any((record.provider, record.model, record.metadata.get("dataset_hash"),
            record.metadata.get("config_hash"), record.metadata.get("code_hash")) != identity for record in records):
        raise ValueError("Replay runs must share provider, model, dataset, configuration and code version")
    metadata = {key: first.metadata[key] for key in ("dataset_hash", "config_hash", "code_hash")}
    rates = app.rates(first.model) if first.provider == "openai" else None
    results = [result_row(record, case, 1, rates) for record, case in zip(records, cases)]
    deployed_limits = [event["max_tool_calls"] for record in records for event in record.events if event["type"] == "run_started"]
    report = save_report(app, cases, results, first.provider, first.model, profile, rates,
                         {"recorded_max_tool_calls": deployed_limits,
                          "execution": "replay; new quick-profile limits were not applied retroactively"},
                         deployment_metadata=metadata, split=split)
    report["replayed"] = True
    ReportStore(app.evaluation_dir).save(report)
    return report


def compare_reports(current, previous):
    if any(current[key] != previous[key] for key in ("dataset_hash", "suite_hash", "kind")):
        raise ValueError("Comparison requires matching dataset, suite, and evaluation kind")
    if current.get("split", "development") != previous.get("split", "development"):
        raise ValueError("Development and held-out reports must not be compared as the same suite")
    if current.get("grader_hash") != previous.get("grader_hash"):
        return {"comparable": False, "reason": "The grading rules changed; regrade saved traces before comparing scores."}
    old, new = {}, {}
    for row in previous["results"]:
        old.setdefault(row["case_id"], []).append(row["passed"])
    for row in current["results"]:
        new.setdefault(row["case_id"], []).append(row["passed"])
    if old.keys() != new.keys():
        return {"comparable": False, "reason": "One report has unrun cases; case success rates were not compared."}
    deltas = {key: sum(new[key]) / len(new[key]) - sum(old[key]) / len(old[key]) for key in old}
    return {"comparable": True,
            "pass_rate_delta": current["summary"]["pass_rate"] - previous["summary"]["pass_rate"],
            "mean_duration_ms_delta": current["summary"]["mean_duration_ms"] - previous["summary"]["mean_duration_ms"],
            "regressions": [key for key, value in deltas.items() if value < 0],
            "improvements": [key for key, value in deltas.items() if value > 0]}
