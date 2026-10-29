"""Run tasks, inspect traces, evaluate workflows, or start the local workspace."""
import argparse
import json
import sys
from app import Application
from evaluations.runner import compare_reports, evaluate, evaluate_saved
from runtime import RunError
from integrations.hubspot import HubSpotAPIError


def parser():
    root = argparse.ArgumentParser(description="System-agnostic agent harness prototype")
    root.add_argument("--config", help="Path to JSON configuration (paths resolve relative to it)")
    commands = root.add_subparsers(dest="command")
    run = commands.add_parser("run", help="Execute one task")
    run.add_argument("message")
    run.add_argument("--json", action="store_true", help="Print the complete run record")
    ev = commands.add_parser("eval", help="Run workflow cases (demo by default)")
    ev.add_argument("--cases", help="Alternate evaluation JSON file")
    ev.add_argument("--repeat", type=int, default=1)
    ev.add_argument("--profile", choices=["quick", "full"], default="quick")
    ev.add_argument("--split", choices=["development", "heldout"], default="development")
    ev.add_argument("--max-cost", type=float, default=0.05, help="Stop starting cases after this estimated USD threshold")
    ev.add_argument("--replay", help="Grade a JSON case-ID/run-ID manifest without making model requests")
    ev.add_argument("--compare", help="Previous evaluation report JSON")
    serve = commands.add_parser("serve", help="Open a loopback-only browser workspace")
    serve.add_argument("--port", type=int, default=8765)
    for command in (run, ev, serve):
        command.add_argument("--provider", choices=["demo", "openai"], default="demo")
        command.add_argument("--model", help="OpenAI model ID; alternatively set OPENAI_MODEL")
    trace = commands.add_parser("trace", help="Print a saved run")
    trace.add_argument("run_id")
    commands.add_parser("tools", help="List registered tools and schemas")
    commands.add_parser("validate-data", help="Validate local data and cross-system mappings without model requests")
    commands.add_parser("check-connections", help="Verify configured live integrations without model requests")
    importer = commands.add_parser("import-crm", help="Validate CSV/JSON accounts and create an isolated local workspace")
    importer.add_argument("source")
    importer.add_argument("--mapping", help="JSON mapping canonical fields to source columns")
    importer.add_argument("--output-dir")
    importer.add_argument("--dry-run", action="store_true", help="Validate without writing files or calling a model")
    return root


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "import-crm":
            from integrations.import_crm import import_crm
            print(json.dumps(import_crm(arguments.source, arguments.output_dir, arguments.mapping, arguments.dry_run), indent=2))
            return 0
        app = Application(arguments.config)
        if arguments.command in (None, "run"):
            message = getattr(arguments, "message", "Find open tickets for Acme")
            record = app.run(message, getattr(arguments, "provider", "demo"), getattr(arguments, "model", None))
            if getattr(arguments, "json", False):
                print(json.dumps(record.to_dict(), indent=2))
            else:
                print(record.answer)
                print(f"\nRun: {record.run_id} | {record.tool_calls} tools | {record.duration_ms} ms", file=sys.stderr)
        elif arguments.command == "tools":
            print(json.dumps(app.registry.list_tools(), indent=2))
        elif arguments.command == "validate-data":
            print(json.dumps(app.validate_data(), indent=2))
        elif arguments.command == "check-connections":
            print(json.dumps(app.check_connections(), indent=2))
        elif arguments.command == "trace":
            print(json.dumps(app.traces.get(arguments.run_id), indent=2))
        elif arguments.command == "eval":
            if arguments.replay:
                if arguments.repeat != 1:
                    raise ValueError("Replay supports one recorded run per case")
                report = evaluate_saved(app, arguments.replay, arguments.profile, arguments.cases, arguments.split)
            else:
                report = evaluate(app, arguments.provider, arguments.model, arguments.cases, arguments.repeat,
                                  arguments.profile, arguments.max_cost, arguments.split)
            if arguments.compare:
                from pathlib import Path
                report["comparison"] = compare_reports(report, json.loads(Path(arguments.compare).read_text()))
            print(json.dumps(report, indent=2))
            return 0 if report["summary"]["complete"] and report["summary"]["passed"] == report["summary"]["total"] else 1
        elif arguments.command == "serve":
            from server import serve
            serve(app, arguments.port, arguments.provider, arguments.model)
        return 0
    except RunError as error:
        print(f"Run failed: {error}. Trace: {error.record.run_id}", file=sys.stderr)
        return 1
    except (ValueError, OSError, HubSpotAPIError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
