"""Run tasks, inspect traces, evaluate workflows, or start the local workspace."""
import argparse
import json
import sys
from app import Application
from evaluations.runner import compare_reports, evaluate
from runtime import RunError


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
    ev.add_argument("--compare", help="Previous evaluation report JSON")
    serve = commands.add_parser("serve", help="Open a loopback-only browser workspace")
    serve.add_argument("--port", type=int, default=8765)
    for command in (run, ev, serve):
        command.add_argument("--provider", choices=["demo", "openai"], default="demo")
        command.add_argument("--model", help="OpenAI model ID; alternatively set OPENAI_MODEL")
    trace = commands.add_parser("trace", help="Print a saved run")
    trace.add_argument("run_id")
    commands.add_parser("tools", help="List registered tools and schemas")
    return root


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
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
        elif arguments.command == "trace":
            print(json.dumps(app.traces.get(arguments.run_id), indent=2))
        elif arguments.command == "eval":
            report = evaluate(app, arguments.provider, arguments.model, arguments.cases, arguments.repeat)
            if arguments.compare:
                from pathlib import Path
                report["comparison"] = compare_reports(report, json.loads(Path(arguments.compare).read_text()))
            print(json.dumps(report, indent=2))
            return 0 if report["summary"]["passed"] == report["summary"]["total"] else 1
        elif arguments.command == "serve":
            from server import serve
            serve(app, arguments.port, arguments.provider, arguments.model)
        return 0
    except RunError as error:
        print(f"Run failed: {error}. Trace: {error.record.run_id}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
