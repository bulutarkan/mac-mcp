from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

import mcp_server.provider_usage as provider_usage
from mcp_server import runtime_settings
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings
from mcp_server.usage_metering import UsageCollector, UsageSample, clear_usage, query_usage_summary

DASHBOARD_TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
DASHBOARD_AUTH = {"authorization": f"Bearer {DASHBOARD_TOKEN}"}


def _sample(ts: float, tool: str = "fixture") -> UsageSample:
    return UsageSample(timestamp=ts, tool=tool, actor_class="primary", status="success",
                       duration_ms=1, arguments={"secret_prompt": "do not store me"}, result={"ok": True})


def _record(event_id: str, ts: float) -> provider_usage.UsageRecord:
    return provider_usage.UsageRecord(
        event_key=f"codex:s:{event_id}", provider="codex", session_id="s", event_id=event_id,
        timestamp=ts, agent_id="agt_x", model="gpt-6-luna", model_verified=True,
        source=provider_usage.SOURCE_REPORT, input_tokens=10, output_tokens=5, reasoning_tokens=None,
        cache_read_tokens=None, cache_write_tokens=None, total_tokens=15,
    )


class UsagePrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-usage-privacy-")
        self.root = Path(self.temp.name)
        self.settings = self.root / "settings.json"
        self.env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(self.settings)})
        self.env.start()
        runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})

    def tearDown(self) -> None:
        self.env.stop()
        runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})
        self.temp.cleanup()

    def _set(self, **privacy) -> None:
        self.settings.write_text(json.dumps({"server": {"port": 8765}, "privacy": privacy}), encoding="utf-8")
        runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})

    def test_defaults_are_enabled_with_365_days(self) -> None:
        self.assertEqual({"enabled": True, "retention_days": 365}, runtime_settings.usage_privacy(fresh=True))

    def test_unknown_retention_snaps_to_an_offered_choice(self) -> None:
        self._set(usage_retention_days=100)
        self.assertEqual(90, runtime_settings.usage_privacy()["retention_days"])
        self._set(usage_retention_days="bogus")
        self.assertEqual(365, runtime_settings.usage_privacy()["retention_days"])

    def test_disabled_metering_stores_no_new_rows_in_either_store(self) -> None:
        self._set(usage_metering=False)
        db = self.root / "telemetry.sqlite3"
        collector = UsageCollector(db, enabled=True)
        self.assertFalse(collector.submit(_sample(time.time())))
        store = provider_usage.ProviderUsageStore(self.root / "provider.sqlite3")
        self.assertFalse(store.ingest(_record("e1", time.time())))
        summary = store.summary(days=365)
        self.assertFalse(summary["metering_enabled"])
        self.assertEqual({}, {k: v for k, v in summary.get("providers", {}).items() if v.get("turns")})

    def test_shorter_retention_prunes_existing_rows_in_both_stores(self) -> None:
        db = self.root / "telemetry.sqlite3"
        collector = UsageCollector(db, enabled=True)
        store = provider_usage.ProviderUsageStore(self.root / "provider.sqlite3")
        for days_ago in (200, 60, 2):
            collector.submit(_sample(time.time() - days_ago * 86400))
            self.assertTrue(store.ingest(_record(f"e{days_ago}", time.time() - days_ago * 86400)))
        self.assertTrue(collector.wait_idle(3.0))
        with sqlite3.connect(db) as conn:
            self.assertEqual(3, conn.execute("SELECT COUNT(*) FROM usage_daily").fetchone()[0])

        self._set(usage_retention_days=30)
        summary = query_usage_summary(db, days=365)
        self.assertEqual(30, summary["retention_days"])
        self.assertEqual(30, summary["days"])
        self.assertIn("no prompts", summary["stored_data"])
        with sqlite3.connect(db) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM usage_daily").fetchone()[0])
            stored = json.dumps(conn.execute("SELECT * FROM usage_daily").fetchall())
        self.assertNotIn("do not store me", stored)
        self.assertEqual(30, store.summary(days=365)["retention_days"])
        with sqlite3.connect(self.root / "provider.sqlite3") as conn:
            for table in ("provider_usage_events", "provider_usage_daily", "provider_usage_daily_agents"):
                dates = [row[0] for row in conn.execute(f"SELECT local_date FROM {table}")]
                self.assertEqual(1, len(dates), table)

    def test_clear_removes_both_stores(self) -> None:
        db = self.root / "telemetry.sqlite3"
        collector = UsageCollector(db, enabled=True)
        collector.submit(_sample(time.time()))
        self.assertTrue(collector.wait_idle(3.0))
        store = provider_usage.ProviderUsageStore(self.root / "provider.sqlite3")
        store.ingest(_record("e1", time.time()))
        self.assertEqual(1, clear_usage(db))
        self.assertEqual(3, store.clear())
        with sqlite3.connect(db) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM usage_daily").fetchone()[0])
        self.assertEqual(0, store.clear())

    def test_dashboard_settings_and_clear_endpoints(self) -> None:
        self._set()
        manager = TelemetryManager(db_path=self.root / "telemetry.sqlite3", usage_enabled=True)
        event_id = manager.start_call("mcp", "get_volume", {})
        manager.finish_call(event_id, result={"ok": True})
        self.assertTrue(manager.wait_usage_idle(2.0))
        client = TestClient(Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN)))

        self.assertEqual(401, client.post("/dashboard/api/usage/settings", json={"retention_days": 30}).status_code)
        self.assertEqual(400, client.post("/dashboard/api/usage/settings", json={"retention_days": 45},
                                          headers=DASHBOARD_AUTH).status_code)
        self.assertEqual(400, client.post("/dashboard/api/usage/settings", json={"metering_enabled": "no"},
                                          headers=DASHBOARD_AUTH).status_code)
        saved = client.post("/dashboard/api/usage/settings", json={"retention_days": 90, "metering_enabled": False},
                            headers=DASHBOARD_AUTH).json()
        self.assertEqual({"ok": True, "enabled": False, "retention_days": 90}, saved)
        on_disk = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual({"usage_retention_days": 90, "usage_metering": False}, on_disk["privacy"])
        self.assertEqual({"port": 8765}, on_disk["server"])

        self.assertEqual(400, client.post("/dashboard/api/usage/clear", json={}, headers=DASHBOARD_AUTH).status_code)
        with patch.object(provider_usage, "db_path", return_value=self.root / "provider.sqlite3"):
            provider_usage._STORE = None
            cleared = client.post("/dashboard/api/usage/clear", json={"confirm": True}, headers=DASHBOARD_AUTH)
            provider_usage._STORE = None
        self.assertEqual(200, cleared.status_code)
        self.assertEqual(1, cleared.json()["tool_usage_rows"])
        self.assertEqual(0, manager.usage_summary(days=365)["totals"]["calls"])


if __name__ == "__main__":
    unittest.main()
