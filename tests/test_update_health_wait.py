"""#78: a passing network hiccup no longer rolls a good update back.

The updater waits for the restarted server by time while its post-update gate says "running", and stops at once
when the gate says "failed". The gate's public endpoint check keeps trying, with growing pauses, for a while.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx

from mcp_server import post_update_health as gate
from mcp_server import update_helper


class _Health(BaseHTTPRequestHandler):
    replies: list = []

    def do_GET(self) -> None:  # noqa: N802
        status, body = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


class UpdaterHealthWaitTests(unittest.TestCase):
    def serve(self, replies: list) -> str:
        _Health.replies = list(replies)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/health"

    def test_a_gate_that_takes_longer_than_thirty_quick_answers_is_waited_for(self) -> None:
        running = (503, {"ok": False, "server": "mac-mcp", "update_gate": "running"})
        url = self.serve([running] * 60 + [(200, {"ok": True, "server": "mac-mcp", "update_gate": "passed"})])
        self.assertTrue(update_helper._health_ok(url, timeout_s=30, delay=0.01))

    def test_a_failed_gate_ends_the_wait_at_once(self) -> None:
        url = self.serve([(503, {"ok": False, "update_gate": "failed", "target": "abc"})])
        started = time.monotonic()
        self.assertFalse(update_helper._health_ok(url, timeout_s=30, delay=0.01))
        self.assertLess(time.monotonic() - started, 5)

    def test_no_server_gives_up_at_the_deadline(self) -> None:
        started = time.monotonic()
        self.assertFalse(update_helper._health_ok("http://127.0.0.1:9/health", timeout_s=0.5, delay=0.05))
        self.assertLess(time.monotonic() - started, 5)


class PublicSmokeTests(unittest.TestCase):
    def run_smoke(self, outcomes: list, budget_s: float) -> tuple:
        calls = {"n": 0}

        class Client:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc) -> None:
                return None

            async def get(self, url, headers=None):
                calls["n"] += 1
                outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
                if isinstance(outcome, Exception):
                    raise outcome
                return httpx.Response(outcome[0], json=outcome[1])

        async def no_sleep(_seconds: float) -> None:
            return None

        with patch.object(gate.httpx, "AsyncClient", Client), patch.object(gate.asyncio, "sleep", no_sleep):
            result = asyncio.run(gate._public_endpoint_smoke("https://mac.example.net/health?probe=basic",
                                                             budget_s=budget_s))
        return result, calls["n"]

    def test_a_few_connect_timeouts_and_a_530_are_ridden_out(self) -> None:
        timeout = httpx.ConnectTimeout("connect timed out")
        (ok, status, error), calls = self.run_smoke(
            [timeout, timeout, (530, {"error": "tunnel"}), timeout, (200, {"ok": True})], budget_s=45)
        self.assertEqual((True, 200, None, 5), (ok, status, error, calls))

    def test_an_endpoint_that_stays_down_still_fails(self) -> None:
        (ok, _, error), calls = self.run_smoke([httpx.ConnectTimeout("down")], budget_s=0.2)
        self.assertEqual((False, "ConnectTimeout"), (ok, error))
        self.assertGreaterEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
