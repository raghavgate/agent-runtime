import json
import tempfile
import threading
import unittest
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
        self.assertIn(b"From question to evidence", body)
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
        status, _ = self.request("GET", "/api/runs/../../config")
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/api/runs", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
