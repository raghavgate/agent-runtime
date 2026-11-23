import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from app import Application
from public_demo import CONSERVATIVE_RUN_COST_USD, DemoGuard, DemoLimit, PublicSettings
from server import make_handler, serve

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class GuardTests(unittest.TestCase):
    def guard(self, **overrides):
        options = dict(per_minute=2, per_day=3, global_per_day=100, daily_budget_usd=1.0, max_concurrent=1)
        options.update(overrides)
        clock = Clock()
        return DemoGuard(PublicSettings(["demo.example"], **options), clock=clock), clock

    def test_per_minute_and_per_day_limits(self):
        guard, clock = self.guard()
        for _ in range(2):
            guard.admit("1.1.1.1")
            guard.release(0.001)
        with self.assertRaises(DemoLimit) as raised:
            guard.admit("1.1.1.1")
        self.assertEqual(raised.exception.status, 429)
        guard.admit("2.2.2.2")  # Other visitors are unaffected.
        guard.release(0.001)
        clock.now += 61
        guard.admit("1.1.1.1")
        guard.release(0.001)
        clock.now += 61
        with self.assertRaises(DemoLimit):
            guard.admit("1.1.1.1")  # Fourth question today.
        clock.now += 86_400
        guard.admit("1.1.1.1")  # New UTC day resets counters.

    def test_daily_budget_and_conservative_cost(self):
        guard, _ = self.guard(per_minute=100, per_day=100, daily_budget_usd=0.05)
        guard.admit("1.1.1.1")
        guard.release(None)  # No estimate: counted as a conservative cost, never as free.
        self.assertAlmostEqual(guard.spent_usd, CONSERVATIVE_RUN_COST_USD)
        guard.admit("1.1.1.1")
        guard.release(0.03)
        with self.assertRaises(DemoLimit) as raised:
            guard.admit("3.3.3.3")
        self.assertIn("spending limit", str(raised.exception))
        self.assertTrue(guard.status("3.3.3.3")["daily_budget_reached"])

    def test_concurrency_cap_and_global_limit(self):
        guard, _ = self.guard(per_minute=100, per_day=100, global_per_day=2)
        guard.admit("1.1.1.1")
        with self.assertRaises(DemoLimit) as raised:
            guard.admit("2.2.2.2")
        self.assertEqual(raised.exception.status, 503)
        guard.release(0.0)
        guard.admit("2.2.2.2")
        guard.release(0.0)
        with self.assertRaises(DemoLimit):
            guard.admit("3.3.3.3")

    def test_client_ip_prefers_configured_header(self):
        guard, _ = self.guard()
        self.assertEqual(guard.client_ip({"True-Client-IP": "9.9.9.9", "X-Forwarded-For": "1.1.1.1"}, "10.0.0.1"), "9.9.9.9")
        self.assertEqual(guard.client_ip({"X-Forwarded-For": "1.1.1.1, 10.0.0.2"}, "10.0.0.1"), "1.1.1.1")
        self.assertEqual(guard.client_ip({}, "10.0.0.1"), "10.0.0.1")

    def test_settings_require_hostname_and_refuse_live_connectors(self):
        with self.assertRaises(ValueError):
            PublicSettings([])
        app = Application()
        app.config = dict(app.config, integrations=app.config["integrations"] + [{"kind": "hubspot_crm"}])
        with self.assertRaises(ValueError):
            serve(app, 8765, public=PublicSettings(["demo.example"]))


class PublicServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.app = Application()
        self.app.traces.directory = base / "runs"
        self.app.conversations.directory = base / "conversations"
        self.app.evaluation_dir = base / "evaluations"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app, 0))
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        self.settings = PublicSettings([self.host, "demo.example"], per_minute=3, per_day=4,
                                       reports_dir=ROOT / "demo" / "reports", runs_dir=ROOT / "demo" / "runs")
        self.server.RequestHandlerClass = make_handler(self.app, self.port, "demo", "scripted", self.settings)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method, path, payload=None, cookie=None, host=None, origin=None, ip="5.5.5.5"):
        headers = {"Content-Type": "application/json", "Host": host or self.host, "True-Client-IP": ip}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = connection.getresponse()
        body = response.read()
        set_cookie = response.getheader("Set-Cookie")
        connection.close()
        return response.status, json.loads(body) if body.startswith((b"{", b"[")) else body, set_cookie

    def session(self):
        status, _, set_cookie = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", set_cookie)
        return set_cookie.split(";")[0]

    def ask(self, cookie, message="Prepare an account brief for Acme", ip="5.5.5.5"):
        status, conversation, _ = self.request("POST", "/api/conversations", {}, cookie=cookie, ip=ip)
        self.assertEqual(status, 201)
        conversation_id = conversation["conversation_id"]
        status, body, _ = self.request("POST", f"/api/conversations/{conversation_id}/messages",
                                       {"message": message, "request_id": str(uuid4())}, cookie=cookie, ip=ip)
        return conversation_id, status, body

    def test_unknown_host_and_foreign_origin_rejected(self):
        self.assertEqual(self.request("GET", "/api/status", host="evil.example")[0], 403)
        cookie = self.session()
        status, _, _ = self.request("POST", "/api/conversations", {}, cookie=cookie, origin="https://evil.example")
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/conversations", {}, cookie=cookie, origin="https://demo.example",
                                    host="demo.example")
        self.assertEqual(status, 201)

    def test_visitors_only_see_their_own_conversations_and_runs(self):
        alice, bob = self.session(), self.session()
        conversation_id, status, body = self.ask(alice)
        self.assertEqual(status, 200)
        run_id = body["run"]["run_id"]
        self.assertEqual([item["conversation_id"] for item in self.request("GET", "/api/conversations", cookie=alice)[1]],
                         [conversation_id])
        self.assertEqual(self.request("GET", "/api/conversations", cookie=bob)[1], [])
        self.assertEqual(self.request("GET", f"/api/conversations/{conversation_id}", cookie=bob)[0], 404)
        self.assertEqual(self.request("GET", f"/api/runs/{run_id}", cookie=bob)[0], 404)
        self.assertEqual(self.request("GET", f"/api/runs/{run_id}", cookie=alice)[0], 200)
        status, _, _ = self.request("POST", f"/api/conversations/{conversation_id}/messages",
                                    {"message": "And Globex?", "request_id": str(uuid4())}, cookie=bob)
        self.assertEqual(status, 404)
        self.assertEqual(self.request("GET", "/api/conversations", cookie=None)[1], [])

    def test_evaluations_are_read_only_bundled_evidence(self):
        cookie = self.session()
        status, body, _ = self.request("POST", "/api/evaluate", {}, cookie=cookie)
        self.assertEqual(status, 403)
        status, reports, _ = self.request("GET", "/api/evaluations", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(reports), 2)
        self.assertTrue(all(report["kind"] == "live_model" for report in reports))
        run_id = reports[0]["results"][0]["run_id"]
        status, run, _ = self.request("GET", f"/api/runs/{run_id}", cookie=cookie)
        self.assertEqual((status, run["run_id"]), (200, run_id))
        status, payload, _ = self.request("GET", "/api/status", cookie=cookie)
        self.assertFalse(payload["evaluations_ready"])
        self.assertEqual(payload["public_demo"]["questions_left_today"], 4)

    def test_rate_limit_and_message_length(self):
        cookie = self.session()
        _, status, _ = self.ask(cookie, message="x" * 1001)
        self.assertEqual(status, 400)
        for _ in range(3):
            self.assertEqual(self.ask(cookie)[1], 200)
        _, status, body = self.ask(cookie)
        self.assertEqual(status, 429)
        self.assertIn("minute", body["error"])
        self.assertEqual(self.ask(self.session(), ip="6.6.6.6")[1], 200)
        status, payload, _ = self.request("GET", "/api/status", cookie=cookie)
        self.assertEqual(payload["public_demo"]["questions_left_today"], 1)


if __name__ == "__main__":
    unittest.main()
