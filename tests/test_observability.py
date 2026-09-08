from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from mcp_server.dashboard_routes import _is_loopback
from mcp_server.observability import TelemetryManager, sanitize_value
from mcp_server.version import __version__


class SanitizerTests(unittest.TestCase):
    def test_redacts_secrets_and_preserves_useful_context(self) -> None:
        payload = {
            "authorization": "Bearer top-secret-value-123456",
            "api_key": "sk-proj-secretsecretsecret123456",
            "command": "curl https://example.com?token=url-token-123456",
            "prompt": "PASSWORD=hunter2\nkeep this context visible",
            "nested": {"cookie": "session=secret"},
            "image": "data:image/png;base64," + "A" * 1500,
        }
        sanitized = sanitize_value(payload, preview_chars=512)
        text = json.dumps(sanitized, ensure_ascii=False)
        for secret in ("top-secret-value", "secretsecretsecret", "url-token-123456", "hunter2", "session=secret"):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("keep this context visible", text)
        self.assertIn("image data", text)


class TelemetryTests(unittest.TestCase):
    def test_event_persists_and_summary_counts_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            manager = TelemetryManager(db_path=db, max_events=100)
            ok_id = manager.start_call("mcp", "read_file", {"path": "/tmp/example"})
            manager.finish_call(ok_id, result={"ok": True})
            err_id = manager.start_call("mcp", "run_command", {"command": "false"})
            manager.finish_call(err_id, error=RuntimeError("expected failure"))

            summary = manager.summary(24)
            self.assertEqual(summary["total_calls"], 2)
            self.assertEqual(summary["success_calls"], 1)
            self.assertEqual(summary["error_calls"], 1)

            reopened = TelemetryManager(db_path=db, max_events=100)
            events = reopened.query_events(limit=10)
            self.assertEqual({event["tool"] for event in events}, {"read_file", "run_command"})

    def test_database_recovers_if_storage_directory_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dashboard"
            manager = TelemetryManager(db_path=root / "telemetry.sqlite3", max_events=100)
            shutil.rmtree(root)
            event_id = manager.start_call("mcp", "get_volume", {})
            manager.finish_call(event_id, result={"ok": True})
            self.assertEqual(manager.summary(24)["total_calls"], 1)

    def test_sse_subscription_receives_start_and_finish(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                queue = manager.subscribe()
                event_id = manager.start_call("mcp", "get_volume", {})
                manager.finish_call(event_id, result={"ok": True})
                started = await asyncio.wait_for(queue.get(), timeout=1)
                finished = await asyncio.wait_for(queue.get(), timeout=1)
                manager.unsubscribe(queue)
                self.assertEqual(started["kind"], "call_started")
                self.assertEqual(finished["kind"], "call_finished")
                self.assertEqual(finished["status"], "success")

        asyncio.run(run())


class DashboardSecurityTests(unittest.TestCase):
    def test_only_loopback_addresses_are_local(self) -> None:
        self.assertTrue(_is_loopback("127.0.0.1"))
        self.assertTrue(_is_loopback("::1"))
        self.assertFalse(_is_loopback("8.8.8.8"))
        self.assertFalse(_is_loopback("192.168.1.10"))


class VersionTests(unittest.TestCase):
    def test_runtime_and_package_versions_match(self) -> None:
        project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()
