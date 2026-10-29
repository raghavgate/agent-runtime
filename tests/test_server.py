import json
import tempfile
import threading
import unittest
from uuid import uuid4
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from app import Application
from server import make_handler


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = Application()
        self.app.traces.directory = Path(self.directory.name) / "runs"
        self.app.conversations.directory = Path(self.directory.name) / "conversations"
        self.app.evaluation_dir = Path(self.directory.name) / "evaluations"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app, 0))
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = make_handler(self.app, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method, path, payload=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=json.dumps(payload) if payload is not None else None,
                           headers=headers or {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read()
        status = response.status
        connection.close()
        return status, body

    def test_task_trace_and_evaluation_endpoints(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Ask your business data", body)
        status, body = self.request("POST", "/api/run", {"message": "Prepare an account brief for Acme"})
        self.assertEqual(status, 200)
        record = json.loads(body)
        self.assertIn("Maya Chen", record["answer"])
        status, body = self.request("GET", "/api/runs/" + record["run_id"])
        self.assertEqual(json.loads(body)["run_id"], record["run_id"])
        status, body = self.request("POST", "/api/evaluate", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["summary"]["passed"], 10)

    def test_invalid_input_and_cross_origin_blocked(self):
        for payload in ({"message": ""}, {"message": 7}, []):
            status, _ = self.request("POST", "/api/run", payload)
            self.assertEqual(status, 400)
        status, _ = self.request("POST", "/api/run", {"message": "hi"}, {"Content-Type": "application/json", "Origin": "https://other.example"})
        self.assertEqual(status, 403)

    def test_quick_profile_and_saved_reports(self):
        status, body = self.request("GET", "/api/status")
        self.assertEqual(json.loads(body)["evaluation_profiles"], ["quick", "full"])
        status, body = self.request("POST", "/api/evaluate", {"profile": "quick"})
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report["summary"]["total"], 3)
        status, body = self.request("GET", "/api/evaluations/" + report["evaluation_id"])
        self.assertEqual(json.loads(body)["evaluation_id"], report["evaluation_id"])
        status, body = self.request("GET", "/api/evaluations")
        self.assertEqual(len(json.loads(body)), 1)
        status, body = self.request("POST", "/api/evaluate", {"profile": []})
        self.assertEqual(status, 400)
        status, _ = self.request("GET", "/api/runs/../../config")
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/api/runs", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)

    def test_conversation_api_retains_context_without_exposing_model_state(self):
        status, body = self.request('POST', '/api/conversations', {})
        self.assertEqual(status, 201)
        conversation = json.loads(body)
        identifier = conversation['conversation_id']
        url = '/api/conversations/' + identifier + '/messages'
        status, body = self.request('POST', url, {'message': 'Find open tickets for Northstar', 'request_id': uuid4().hex})
        self.assertEqual(status, 200)
        self.assertIn('Which account', json.loads(body)['run']['answer'])
        request_id = uuid4().hex
        status, body = self.request('POST', url, {'message': 'Labs', 'request_id': request_id})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn('Northstar Labs', payload['run']['answer'])
        self.assertEqual(len(payload['conversation']['turns']), 2)
        self.assertNotIn('model_state', payload['conversation'])
        status, body = self.request('POST', url, {'message': 'Labs', 'request_id': request_id})
        self.assertEqual(json.loads(body)['run']['run_id'], payload['run']['run_id'])
        status, body = self.request('GET', '/api/conversations')
        self.assertEqual(len(json.loads(body)), 1)
        status, body = self.request('GET', '/api/conversations/' + identifier)
        self.assertEqual(len(json.loads(body)['turns']), 2)
        status, body = self.request('GET', '/api/conversations/../../config')
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
