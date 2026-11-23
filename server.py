"""Workspace HTTP server.

Local mode (default) is a single-user, loopback-only workspace. Public mode
(`serve --public`) is a shared demo: it binds to all interfaces for a hosting
platform, accepts only configured hostnames, scopes conversations and runs to
the visitor's session, rate-limits model requests, enforces a daily estimated
spend ceiling, and serves bundled evaluation evidence read-only.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from evaluations.runner import evaluate, load_cases, workspace_cases_path
from runtime import RunError
from evaluations.store import ReportStore
from conversations import ConversationBusy, ConversationMismatch
from public_demo import (DemoGuard, DemoLimit, Ownership, SESSION_COOKIE, bundled_reports, bundled_run_ids,
                         load_bundled_run, parse_cookie)

WEB_ROOT = Path(__file__).with_name("web")
LOOPBACK_NAMES = ("127.0.0.1", "localhost")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _is_loopback(host):
    return (host or "").split(":")[0] in LOOPBACK_NAMES


def make_handler(app, port, provider="demo", model_name=None, public=None):
    if public:
        allowed_hosts = set(public.hostnames)
        allowed_origins = {"https://" + host for host in allowed_hosts}
        allowed_origins |= {"http://" + host for host in allowed_hosts if _is_loopback(host)}
        guard = DemoGuard(public)
        ownership = Ownership()
        public_runs = bundled_run_ids(public.runs_dir)
    else:
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        allowed_origins = {"http://" + host for host in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, *args):
            pass  # Do not put task text or trace contents in HTTP logs.

        # ---- response helpers -------------------------------------------------
        def send_content(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            if public:
                if not _is_loopback(self.headers.get("Host")):
                    self.send_header("Strict-Transport-Security", "max-age=31536000")
                if getattr(self, "new_session", False):
                    secure = "" if _is_loopback(self.headers.get("Host")) else "; Secure"
                    self.send_header("Set-Cookie", f"{SESSION_COOKIE}={self.session}; Path=/; HttpOnly; "
                                                   f"SameSite=Lax; Max-Age=2592000{secure}")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, payload):
            self.send_content(status, json.dumps(payload).encode())

        def host_allowed(self):
            if (self.headers.get("Host") or "").lower() not in allowed_hosts:
                self.send_json(403, {"error": "Use the workspace address"})
                return False
            return True

        def start_request(self):
            if not self.host_allowed():
                return False
            if public:
                session = parse_cookie(self.headers.get("Cookie"))
                self.new_session = not Ownership.valid(session)
                self.session = Ownership.new_session() if self.new_session else session
                self.client = guard.client_ip(self.headers, self.client_address[0])
            return True

        # ---- public-mode helpers ----------------------------------------------
        def owns(self, kind, item_id):
            return not public or ownership.owns(self.session, kind, item_id)

        def public_conversations(self):
            items = []
            for conversation_id in ownership.items(self.session, "conversations"):
                try:
                    items.append(app.conversation_detail(conversation_id, provider, model_name))
                except (FileNotFoundError, ValueError):
                    continue
            items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
            return [{key: item[key] for key in ("conversation_id", "title", "created_at", "updated_at",
                                                "provider", "model", "revision")} for item in items[:30]]

        def public_runs_list(self):
            runs = []
            for run_id in ownership.items(self.session, "runs"):
                try:
                    record = app.traces.get(run_id)
                except (FileNotFoundError, ValueError):
                    continue
                if record.get("metadata", {}).get("purpose") in ("evaluation", "conversation"):
                    continue
                runs.append({key: record[key] for key in (
                    "run_id", "user_message", "status", "provider", "model", "duration_ms", "started_at")})
            runs.sort(key=lambda run: run["started_at"], reverse=True)
            return runs[:50]

        def get_run(self, run_id):
            if public and run_id in public_runs:
                return load_bundled_run(public.runs_dir, run_id)
            if not self.owns("runs", run_id):
                raise FileNotFoundError(run_id)
            return app.traces.get(run_id)

        def check_message(self, message):
            limit = public.max_message_chars if public else 8000
            if not isinstance(message, str) or not message.strip() or len(message) > limit:
                raise ValueError(f"Enter a message between 1 and {limit} characters")

        def model_request(self, action):
            """Run one model-backed action under the public guard (no-op locally)."""
            if not public:
                return action()
            guard.admit(self.client)
            cost = None
            try:
                record = action()
                cost = record.estimated_cost_usd
                ownership.add(self.session, "runs", record.run_id)
                return record
            except RunError as error:
                cost = error.record.estimated_cost_usd
                ownership.add(self.session, "runs", error.record.run_id)
                raise
            finally:
                guard.release(cost)

        # ---- routes -----------------------------------------------------------
        def status_payload(self):
            splits = ["development"] + (["heldout"] if app.config.get("evaluation_heldout_cases") else [])
            counts = {split: {profile: len(load_cases(workspace_cases_path(app, split=split), profile))
                              for profile in ("quick", "full")} for split in splits}
            ready = app.dataset_hash == app.sample_dataset_hash() or bool(app.config.get("evaluation_cases"))
            block_reason = None
            if provider == "demo" and app.config.get("demo_evaluation_supported") is False:
                ready = False
                block_reason = "These contact and deal checks need a connected language model. This preview makes no API calls."
            if public:
                ready = False
                block_reason = ("Running checks is turned off on the public demo because each run makes paid model calls. "
                                "The saved reports below are from real runs of this harness. Open one and choose "
                                "Read answer to see the exact answer, sources and tool calls. Replays are the same "
                                "answers regraded after a documented grading fix (docs/BASELINE.md).")
            payload = {"provider": provider, "model": model_name,
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
                       "tools": app.registry.list_tools()}
            if public:
                payload["public_demo"] = guard.status(self.client)
            return payload

        def do_GET(self):
            if not self.start_request():
                return
            path = urlsplit(self.path).path
            try:
                assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                          "/style.css": ("style.css", "text/css")}
                if path in assets:
                    name, kind = assets[path]
                    self.send_content(200, (WEB_ROOT / name).read_bytes(), kind + "; charset=utf-8")
                elif path == "/favicon.ico":
                    self.send_content(204, b"", "image/x-icon")
                elif path == "/healthz":
                    self.send_json(200, {"ok": True})
                elif path == "/api/status":
                    self.send_json(200, self.status_payload())
                elif path == "/api/evaluations":
                    self.send_json(200, bundled_reports(public.reports_dir) if public
                                   else ReportStore(app.evaluation_dir).list_reports())
                elif path.startswith("/api/evaluations/"):
                    directory = public.reports_dir if public else app.evaluation_dir
                    self.send_json(200, ReportStore(directory).get(path.rsplit("/", 1)[1]))
                elif path == "/api/runs":
                    self.send_json(200, self.public_runs_list() if public else app.traces.list_runs())
                elif path == "/api/conversations":
                    self.send_json(200, self.public_conversations() if public
                                   else app.list_conversations(provider, model_name))
                elif path.startswith("/api/conversations/") and len(path.strip('/').split('/')) == 3:
                    conversation_id = path.rsplit('/', 1)[1]
                    if not self.owns("conversations", conversation_id):
                        raise FileNotFoundError(conversation_id)
                    self.send_json(200, app.conversation_detail(conversation_id, provider, model_name))
                elif path.startswith("/api/runs/"):
                    self.send_json(200, self.get_run(path.rsplit("/", 1)[1]))
                else:
                    self.send_json(404, {"error": "Not found"})
            except ConversationMismatch as error:
                self.send_json(409, {"error": str(error)})
            except (FileNotFoundError, ValueError):
                self.send_json(404, {"error": "Not found"})

        def do_POST(self):
            if not self.start_request():
                return
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
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
                    self.check_message(message)
                    record = self.model_request(lambda: app.run(message, provider, model_name))
                    self.send_json(200, record.to_dict())
                elif path == "/api/conversations":
                    if public:
                        guard.light_check(self.client)
                    conversation = app.create_conversation(provider, model_name)
                    if public:
                        ownership.add(self.session, "conversations", conversation["conversation_id"])
                    self.send_json(201, conversation)
                elif path.startswith("/api/conversations/") and path.endswith("/messages") and len(path.strip('/').split('/')) == 4:
                    conversation_id = path.strip('/').split('/')[2]
                    if not self.owns("conversations", conversation_id):
                        raise FileNotFoundError(conversation_id)
                    message = payload.get('message')
                    self.check_message(message)
                    try:
                        record = self.model_request(lambda: app.send_message(
                            conversation_id, message, provider, model_name, request_id=payload.get('request_id')))
                    except RunError as error:
                        self.send_json(422, {"error": str(error), "run": error.record.to_dict(),
                                             "conversation": app.conversation_detail(conversation_id, provider, model_name)})
                    else:
                        self.send_json(200, {"run": record.to_dict(),
                                             "conversation": app.conversation_detail(conversation_id, provider, model_name)})
                elif path == "/api/evaluate":
                    if public:
                        self.send_json(403, {"error": "Running checks is turned off on the public demo."})
                        return
                    profile = payload.get("profile", "quick" if provider == "openai" else "full")
                    self.send_json(200, evaluate(app, provider, model_name, profile=profile,
                                                 split=payload.get("split", "development"),
                                                 max_cost_usd=payload.get("max_cost", 0.05)))
                else:
                    self.send_json(404, {"error": "Not found"})
            except DemoLimit as error:
                self.send_json(error.status, {"error": str(error)})
            except RunError as error:
                self.send_json(422, {"error": str(error), "run": error.record.to_dict()})
            except (ConversationBusy, ConversationMismatch) as error:
                self.send_json(409, {"error": str(error)})
            except FileNotFoundError:
                self.send_json(404, {"error": "Conversation not found"})
            except (ValueError, UnicodeDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception:
                self.send_json(500, {"error": "Workspace operation failed; inspect the server log and saved run records"})

    return Handler


def serve(app, port=8765, provider="demo", model_name=None, public=None):
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if public and any(entry["kind"] == "hubspot_crm" for entry in app.config["integrations"]):
        raise ValueError("Public mode serves bundled or synthetic data only; remove live CRM connectors")
    # Fail early for missing key/model instead of failing on the first browser task.
    model = app.model(provider, model_name)
    resolved_model = model.model_name
    close = getattr(model, "close", None)
    if close:
        close()
    handler = make_handler(app, port, provider, resolved_model, public)
    bind = "0.0.0.0" if public else "127.0.0.1"
    with ThreadingHTTPServer((bind, port), handler) as server:
        if public:
            print(f"Agent Runtime public demo on port {port} for {', '.join(public.hostnames)} "
                  f"({provider}: {resolved_model}; budget ${public.daily_budget_usd:.2f}/day)", flush=True)
        else:
            print(f"Agent Runtime: http://127.0.0.1:{port} ({provider}: {resolved_model})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
