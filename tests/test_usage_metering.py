from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import threading
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.mobile_auth import MobileAuthStore
from mcp_server.mobile_routes import create_mobile_routes
from mcp_server import runtime_settings
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings
from mcp_server.usage_metering import (
    MEASUREMENT_CLASS,
    TOKENIZER_ID,
    UsageCollector,
    UsageSample,
    canonical_json_text,
    measure_payload,
)


# Usage metering reads ~/.mac-mcp/settings.json; never let the owner's real
# privacy settings decide these results.
_SETTINGS_DIR = tempfile.TemporaryDirectory(prefix="mac-mcp-usage-settings-")
_SETTINGS_ENV = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(Path(_SETTINGS_DIR.name) / "settings.json")})


def setUpModule() -> None:
    _SETTINGS_ENV.start()
    runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})


def tearDownModule() -> None:
    _SETTINGS_ENV.stop()
    runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})
    _SETTINGS_DIR.cleanup()


DASHBOARD_TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
DASHBOARD_AUTH = {"authorization": f"Bearer {DASHBOARD_TOKEN}"}


class CanonicalPayloadMeteringTests(unittest.TestCase):
    def test_key_order_is_canonical_and_token_count_is_deterministic(self) -> None:
        first = {"z": "tail", "nested": {"b": 2, "a": "hello"}, "a": [1, 2, 3]}
        second = {"a": [1, 2, 3], "nested": {"a": "hello", "b": 2}, "z": "tail"}
        text_a, images_a, binary_a = canonical_json_text(first)
        text_b, images_b, binary_b = canonical_json_text(second)
        self.assertEqual(text_a, text_b)
        self.assertEqual(measure_payload(first), measure_payload(second))
        self.assertEqual((0, 0), (images_a, binary_a))
        self.assertEqual((0, 0), (images_b, binary_b))
        self.assertGreater(measure_payload(first).tokens, 0)

    def test_data_uri_image_is_counted_even_under_image_key(self) -> None:
        raw = "A" * 4096
        metrics = measure_payload({"image": "data:image/png;base64," + raw})
        self.assertEqual(1, metrics.image_count)
        self.assertGreater(metrics.binary_bytes, 3000)
        self.assertLess(metrics.tokens, 50)

    def test_large_text_count_is_stable(self) -> None:
        one_k = {"text": "abcd " * 205}
        hundred_k = {"text": "abcd " * 20_480}
        a = measure_payload(one_k)
        b = measure_payload(one_k)
        large = measure_payload(hundred_k)
        self.assertEqual(a, b)
        self.assertGreater(a.canonical_bytes, 1000)
        self.assertGreater(large.canonical_bytes, 100_000)
        self.assertGreater(large.tokens, a.tokens)

    def test_image_and_binary_are_not_tokenized_as_base64_text(self) -> None:
        image_data = "A" * 4096
        with_image = {
            "content": [
                {"type": "text", "text": "visible label"},
                {"type": "image", "mimeType": "image/png", "data": image_data},
            ],
            "blob": b"x" * 512,
        }
        metrics = measure_payload(with_image)
        text, image_count, binary_bytes = canonical_json_text(with_image)
        self.assertEqual(1, image_count)
        self.assertGreaterEqual(binary_bytes, 512 + 3000)
        self.assertNotIn(image_data, text)
        self.assertLess(metrics.tokens, 100)
        self.assertEqual(TOKENIZER_ID, "mac_mcp_payload_v1")
        self.assertEqual(MEASUREMENT_CLASS, "canonical_json_text_v1")


class UsageCollectorTests(unittest.TestCase):
    def test_primary_subagent_daily_rollup_and_restart_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            manager = TelemetryManager(db_path=db, max_events=100, usage_enabled=True)
            primary = manager.start_call("mcp", "read_file", {"path": "/tmp/a", "query": "alpha"})
            manager.finish_call(primary, result={"ok": True, "content": "one"})
            subagent = manager.start_call(
                "mcp",
                "read_file",
                {"path": "/tmp/b", "query": "beta"},
                metadata={"agent_id": "agt_usage", "team_id": "team_usage"},
            )
            manager.finish_call(subagent, result={"ok": False, "error": "fixture"})
            self.assertTrue(manager.wait_usage_idle(2.0))

            all_usage = manager.usage_summary(days=365)
            self.assertEqual(2, all_usage["totals"]["calls"])
            self.assertEqual(1, all_usage["totals"]["error_count"])
            self.assertGreater(all_usage["totals"]["input_tokens"], 0)
            self.assertGreater(all_usage["totals"]["output_tokens"], 0)

            primary_usage = manager.usage_summary(days=365, actor_class="primary")
            subagent_usage = manager.usage_summary(days=365, actor_class="scoped_subagent")
            self.assertEqual(1, primary_usage["totals"]["calls"])
            self.assertEqual(1, subagent_usage["totals"]["calls"])
            self.assertEqual(2, primary_usage["totals"]["calls"] + subagent_usage["totals"]["calls"])

            reopened = TelemetryManager(db_path=db, max_events=100, usage_enabled=True)
            persisted = reopened.usage_summary(days=365)
            self.assertEqual(2, persisted["totals"]["calls"])
            self.assertEqual(TOKENIZER_ID, persisted["tokenizer_id"])
            self.assertIn("not provider billing", persisted["metric_scope"])

    def test_latency_percentiles_report_histogram_bound_direction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            collector = UsageCollector(db, enabled=True)
            for latency in (1200, 1300, 1400, 5200):
                collector.submit(UsageSample(
                    timestamp=time.time(),
                    tool="latency_fixture",
                    actor_class="primary",
                    status="success",
                    duration_ms=latency,
                    arguments={},
                    result={"ok": True},
                ))
            self.assertTrue(collector.wait_idle(3.0))
            from mcp_server.usage_metering import query_usage_summary
            totals = query_usage_summary(db, days=1)["totals"]
            self.assertEqual(5000, totals["p50_latency_ms"])
            self.assertEqual("lt", totals["p50_latency_relation"])
            self.assertEqual(5000, totals["p95_latency_ms"])
            self.assertEqual("gte", totals["p95_latency_relation"])

    def test_usage_db_stores_no_raw_prompt_or_result_text(self) -> None:
        secret_in = "usage-secret-input-never-persist"
        secret_out = "usage-secret-output-never-persist"
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            manager = TelemetryManager(db_path=db, max_events=100, usage_enabled=True)
            event_id = manager.start_call("mcp", "run_command", {"command": secret_in})
            manager.finish_call(event_id, result={"ok": True, "stdout": secret_out})
            self.assertTrue(manager.wait_usage_idle(2.0))
            with sqlite3.connect(db) as conn:
                rows = conn.execute("SELECT * FROM usage_daily").fetchall()
                self.assertTrue(rows)
                dump = json.dumps(rows, default=str)
                self.assertNotIn(secret_in, dump)
                self.assertNotIn(secret_out, dump)
                columns = [row[1] for row in conn.execute("PRAGMA table_info(usage_daily)")]
                self.assertFalse(any("prompt" in c or "result_json" in c or "arguments_json" in c for c in columns))

    def test_usage_daily_retention_is_bounded_to_about_thirteen_months(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            collector = UsageCollector(db, enabled=True)
            old_ts = time.time() - (450 * 86400)
            recent_ts = time.time() - (10 * 86400)
            for ts in (old_ts, recent_ts):
                collector.submit(UsageSample(
                    timestamp=ts,
                    tool="retention_fixture",
                    actor_class="primary",
                    status="success",
                    duration_ms=1,
                    arguments={"x": 1},
                    result={"ok": True},
                ))
            self.assertTrue(collector.wait_idle(3.0))
            with sqlite3.connect(db) as conn:
                dates = [
                    row[0]
                    for row in conn.execute(
                        "SELECT local_date FROM usage_daily ORDER BY local_date"
                    ).fetchall()
                ]
            self.assertEqual(1, len(dates))

    def test_raw_prune_does_not_delete_daily_usage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            manager = TelemetryManager(db_path=db, retention_days=1, max_events=100, usage_enabled=True)
            event_id = manager.start_call("mcp", "get_volume", {})
            manager.finish_call(event_id, result={"ok": True})
            self.assertTrue(manager.wait_usage_idle(2.0))
            before = manager.usage_summary(days=365)["totals"]["calls"]
            self.assertEqual(1, before)
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE tool_events SET timestamp=?", (time.time() - 10 * 86400,))
                conn.commit()
            manager._prune()
            with sqlite3.connect(db) as conn:
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM tool_events").fetchone()[0])
                self.assertEqual(1, conn.execute("SELECT SUM(calls) FROM usage_daily").fetchone()[0])

    def test_timezone_bucket_is_persisted_per_sample(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            buckets = iter([
                ("2026-03-29", "TRT+0300"),
                ("2026-03-30", "TRT+0300"),
            ])
            collector = UsageCollector(db, local_bucket=lambda _ts: next(buckets))
            for idx in range(2):
                collector.submit(UsageSample(
                    timestamp=idx,
                    tool="fixture",
                    actor_class="primary",
                    status="success",
                    duration_ms=10,
                    arguments={"x": idx},
                    result={"ok": True},
                ))
            self.assertTrue(collector.wait_idle(2.0))
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT local_date, timezone, calls FROM usage_daily ORDER BY local_date"
                ).fetchall()
            self.assertEqual(
                [("2026-03-29", "TRT+0300", 1), ("2026-03-30", "TRT+0300", 1)],
                rows,
            )

    def test_parallel_thousand_samples_are_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            collector = UsageCollector(db, queue_size=4096)
            sample = UsageSample(
                timestamp=time.time(), tool="parallel_fixture", actor_class="primary",
                status="success", duration_ms=3,
                arguments={"text": "small"}, result={"ok": True},
            )
            threads = []
            for _ in range(10):
                thread = threading.Thread(
                    target=lambda: [collector.submit(sample) for _ in range(100)]
                )
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
            self.assertTrue(collector.wait_idle(5.0))
            with sqlite3.connect(db) as conn:
                self.assertEqual(
                    1000,
                    conn.execute(
                        "SELECT SUM(calls) FROM usage_daily WHERE tool='parallel_fixture'"
                    ).fetchone()[0],
                )
            self.assertEqual(0, collector.diagnostics()["queue_dropped"])

    def test_metering_failure_never_breaks_tool_completion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(
                db_path=Path(td) / "telemetry.sqlite3",
                usage_enabled=True,
            )
            event_id = manager.start_call("mcp", "get_volume", {})
            with patch.object(manager._usage, "submit", side_effect=RuntimeError("usage db down")):
                event = manager.finish_call(event_id, result={"ok": True, "volume": 25})
            self.assertEqual("success", event["status"])
            self.assertEqual(25, event["result"]["volume"])

    def test_queue_overflow_is_fail_open_and_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            collector = UsageCollector(Path(td) / "telemetry.sqlite3", queue_size=16)
            with patch.object(collector, "_ensure_worker", return_value=None):
                sample = UsageSample(
                    timestamp=time.time(), tool="fixture", actor_class="primary",
                    status="success", duration_ms=1, arguments={}, result={},
                )
                accepted = sum(1 for _ in range(40) if collector.submit(sample))
            self.assertEqual(16, accepted)
            diag = collector.diagnostics()
            self.assertEqual(24, diag["queue_dropped"])
            self.assertEqual(16, diag["queue_depth"])

    def test_submit_hot_path_p95_is_sub_millisecond_without_tokenization(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            collector = UsageCollector(Path(td) / "telemetry.sqlite3", queue_size=4096)
            sample = UsageSample(
                timestamp=time.time(), tool="fixture", actor_class="primary",
                status="success", duration_ms=1,
                arguments={"text": "x" * 100_000},
                result={"text": "y" * 100_000},
            )
            timings = []
            with patch.object(collector, "_ensure_worker", return_value=None):
                for _ in range(1000):
                    start = time.perf_counter_ns()
                    self.assertTrue(collector.submit(sample))
                    timings.append((time.perf_counter_ns() - start) / 1_000_000)
            p95 = sorted(timings)[int(len(timings) * 0.95) - 1]
            self.assertLess(p95, 1.0)


class UsageDashboardRouteTests(unittest.TestCase):
    def test_mobile_usage_endpoint_requires_mobile_session_and_returns_same_metric(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = TelemetryManager(
                db_path=root / "telemetry.sqlite3",
                usage_enabled=True,
            )
            event_id = manager.start_call("mcp", "mac_observe", {"app": "Finder"})
            manager.finish_call(event_id, result={"ok": True, "elements": 3})
            self.assertTrue(manager.wait_usage_idle(3.0))

            store = MobileAuthStore(root / "mobile_auth.sqlite3")
            issued = store.issue_pairing()
            session = store.consume_pairing(issued["code"], device_name="Usage Test")
            self.assertIsNotNone(session)

            app = Starlette(routes=create_mobile_routes(
                manager,
                load_settings(),
                DASHBOARD_TOKEN,
                auth_store=store,
            ))
            client = TestClient(app, base_url="https://testserver")
            self.assertEqual(401, client.get("/mobile/api/usage").status_code)

            response = client.get(
                "/mobile/api/usage?days=365&actor=all",
                headers={"authorization": "Bearer " + session["token"]},
            )
            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertEqual("MCP Payload Tokens", payload["metric_name"])
            self.assertEqual(1, payload["totals"]["calls"])
            self.assertNotIn("Finder", json.dumps(payload))

    def test_usage_endpoint_is_authenticated_and_returns_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3", usage_enabled=True)
            event_id = manager.start_call("mcp", "read_file", {"path": "/tmp/x"})
            manager.finish_call(event_id, result={"ok": True, "content": "hello"})
            self.assertTrue(manager.wait_usage_idle(2.0))
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN))
            client = TestClient(app)
            self.assertEqual(401, client.get("/dashboard/api/usage").status_code)
            response = client.get(
                "/dashboard/api/usage?days=365&actor=all",
                headers=DASHBOARD_AUTH,
            )
            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertEqual("MCP Payload Tokens", payload["metric_name"])
            self.assertEqual(1, payload["totals"]["calls"])
            self.assertEqual("all", payload["actor_class"])
            self.assertNotIn("content", json.dumps(payload))
            self.assertTrue(payload["top_tools"])


if __name__ == "__main__":
    unittest.main()
