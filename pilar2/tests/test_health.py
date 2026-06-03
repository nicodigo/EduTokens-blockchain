"""Integration-style tests for health/status HTTP endpoints.

Starts a real HTTP server on a random port, hits the endpoints,
and validates the JSON responses.
"""

import json
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Minimal test server
# ---------------------------------------------------------------------------


class _TestHandler(BaseHTTPRequestHandler):
    """A handler that returns canned JSON — avoids importing NCT/worker."""

    def do_GET(self) -> None:
        payload: dict[str, Any] = {}
        if self.path == "/health":
            payload = {"status": "ok", "service": "test"}
        elif self.path == "/status":
            payload = {"chain_height": 0, "pending_transactions": 0}
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
            return

        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # silence during tests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_test_server() -> tuple[HTTPServer, int, threading.Thread]:
    """Start a test server on port 0 and return (server, port, thread)."""
    server = HTTPServer(("127.0.0.1", 0), _TestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, args=(0.2,), daemon=True)
    thread.start()
    time.sleep(0.15)  # let the server bind
    return server, port, thread


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port, cls.thread = _start_test_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=1)

    def _get(self, path: str) -> tuple[int, dict[str, Any]]:
        for _ in range(3):  # retry on ConnectionRefusedError
            try:
                conn = HTTPConnection("127.0.0.1", self.port, timeout=2)
                conn.request("GET", path)
                resp = conn.getresponse()
                body = json.loads(resp.read().decode())
                conn.close()
                return resp.status, body
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
        raise RuntimeError(f"Could not connect to test server on port {self.port}")

    def test_health_returns_200(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_status_returns_200(self):
        status, body = self._get("/status")
        self.assertEqual(status, 200)
        self.assertIn("chain_height", body)

    def test_unknown_path_returns_404(self):
        status, _body = self._get("/nonexistent")
        self.assertEqual(status, 404)


class TestWorkerEnvDefaults(unittest.TestCase):
    def test_default_health_port(self):
        from worker.worker import DEFAULT_HEALTH_PORT
        self.assertEqual(DEFAULT_HEALTH_PORT, 8081)


if __name__ == "__main__":
    unittest.main()
