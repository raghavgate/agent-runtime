"""Local single-user workspace. Loopback only; not a production HTTP server."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from evaluations.runner import evaluate, load_cases, workspace_cases_path
from runtime import RunError
from evaluations.store import ReportStore
from conversations import ConversationBusy, ConversationMismatch

WEB_ROOT = Path(__file__).with_name("web")


def make_handler(app, port, provider="demo", model_name=None):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not put task text or trace contents in HTTP logs.

        def send_content(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, payload):
            self.send_content(status, json.dumps(payload).encode())

        def host_allowed(self):
            if self.headers.get("Host") not in allowed_hosts:
                self.send_json(403, {"error": "Use the local workspace address"})
                return False
            return True

        def do_GET(self):
            if not self.host_allowed():
                return
            path = urlsplit(self.path).path
            try:
                assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                          "/style.css": ("style.css", "text/css")}
                if path in assets:
                    name, kind = assets[path]
                    self.send_content(200, (WEB_ROOT / name).read_bytes(), kind + "; charset=utf-8")
                elif path == "/api/status":
                    splits = ["development"] + (["heldout"] if app.config.get("evaluation_heldout_cases") else [])
                    counts = {split: {profile: len(load_cases(workspace_cases_path(app, split=split), profile))
                                      for profile in ("quick", "full")} for split in splits}
                    ready = app.dataset_hash == app.sample_dataset_hash() or bool(app.config.get("evaluation_cases"))
                    block_reason = None
                    if provider == "demo" and app.config.get("demo_evaluation_supported") is False:
                        ready = False
                        block_reason = "These contact and deal checks need a connected language model. This preview makes no API calls."
                    self.send_json(200, {"provider": provider, "model": model_name,
                                         "dataset_hash": app.dataset_hash,
                                         "evaluation_profiles": ["quick", "full"],
                                         "pricing": app.rates(model_name),
                                         "sample_data": app.dataset_hash == app.sample_dataset_hash(),
                                         "evaluations_ready": ready, "evaluation_block_reason": block_reason,
                                         "evaluation_splits": splits, "evaluation_case_counts": counts,
                                         "workspace_name": app.config.get("workspace_name", "Sample workspace"),
                                         "conversations": True, "config_hash": app.config_hash,
                                         "workflow_prompts": app.config.get("workflow_prompts", []),
                                         "data_origin": app.config.get("data_origin", "sample"),
                                         "as_of": app.config.get("as_of"), "dataset_counts": app.dataset_counts,
                                         "suggested_questions": app.config.get("suggested_questions"),
                                         "connections": [{"crm_export": "CRM", "support_export": "Support",
                                                          "hubspot_crm": "HubSpot CRM"}[entry["kind"]]
                                                         for entry in app.config["integrations"]],
                                         "tools": app.registry.list_tools()})
                elif path == "/api/evaluations":
                    self.send_json(200, ReportStore(app.evaluation_dir).list_reports())
                elif path.startswith("/api/evaluations/"):
                    self.send_json(200, ReportStore(app.evaluation_dir).get(path.rsplit("/", 1)[1]))
                elif path == "/api/runs":
                    self.send_json(200, app.traces.list_runs())
                elif path == "/api/conversations":
                    self.send_json(200, app.list_conversations(provider, model_name))
                elif path.startswith("/api/conversations/") and len(path.strip('/').split('/')) == 3:
                    self.send_json(200, app.conversation_detail(path.rsplit('/', 1)[1], provider, model_name))
                elif path.startswith("/api/runs/"):
                    self.send_json(200, app.traces.get(path.rsplit("/", 1)[1]))
                else:
                    self.send_json(404, {"error": "Not found"})
            except ConversationMismatch as error:
                self.send_json(409, {"error": str(error)})
            except (FileNotFoundError, ValueError):
                self.send_json(404, {"error": "Run not found"})

        def do_POST(self):
            if not self.host_allowed():
                return
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in allowed_hosts}:
                self.send_json(403, {"error": "Cross-origin requests are not allowed"})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.send_json(415, {"error": "Send application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 16384:
                    self.send_json(413, {"error": "Request must be between 1 and 16384 bytes"})
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object")
                path = urlsplit(self.path).path
                if path == "/api/run":
                    message = payload.get("message")
                    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
                        raise ValueError("Enter a task between 1 and 8000 characters")
                    record = app.run(message, provider, model_name)
                    self.send_json(200, record.to_dict())
                elif path == "/api/conversations":
                    self.send_json(201, app.create_conversation(provider, model_name))
                elif path.startswith("/api/conversations/") and path.endswith("/messages") and len(path.strip('/').split('/')) == 4:
                    conversation_id = path.strip('/').split('/')[2]
                    try:
                        record = app.send_message(conversation_id, payload.get('message'), provider, model_name,
                                                  request_id=payload.get('request_id'))
                    except RunError as error:
                        self.send_json(422, {"error": str(error), "run": error.record.to_dict(),
                                             "conversation": app.conversation_detail(conversation_id, provider, model_name)})
                    else:
                        self.send_json(200, {"run": record.to_dict(),
                                             "conversation": app.conversation_detail(conversation_id, provider, model_name)})
                elif path == "/api/evaluate":
                    profile = payload.get("profile", "quick" if provider == "openai" else "full")
                    self.send_json(200, evaluate(app, provider, model_name, profile=profile,
                                                 split=payload.get("split", "development")))
                else:
                    self.send_json(404, {"error": "Not found"})
            except RunError as error:
                self.send_json(422, {"error": str(error), "run": error.record.to_dict()})
            except (ConversationBusy, ConversationMismatch) as error:
                self.send_json(409, {"error": str(error)})
            except FileNotFoundError:
                self.send_json(404, {"error": "Conversation not found"})
            except (ValueError, UnicodeDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception:
                self.send_json(500, {"error": "Workspace operation failed; inspect the terminal and saved run records"})

    return Handler


def serve(app, port=8765, provider="demo", model_name=None):
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    # Fail early for missing key/model instead of failing on the first browser task.
    model = app.model(provider, model_name)
    resolved_model = model.model_name
    close = getattr(model, "close", None)
    if close:
        close()
    handler = make_handler(app, port, provider, resolved_model)
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Agent Runtime: http://127.0.0.1:{port} ({provider}: {resolved_model})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
