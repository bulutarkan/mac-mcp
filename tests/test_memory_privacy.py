from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import tools_memory as tm
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

TOKEN = "memory-privacy-test-token-0123456789abcd"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class _MemoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        self.settings = Path(self.temp.name) / "settings.json"
        self.env = patch.dict(os.environ, {
            "MAC_MCP_MEMORY_DIR": str(self.root),
            "MAC_MCP_MEMORY_EMBEDDING": "feature_hash",
            "MAC_MCP_MEMORY_MODEL_CACHE": str(Path(self.temp.name) / "model-cache"),
            "MAC_MCP_SETTINGS_PATH": str(self.settings),
        })
        self.env.start()
        tm._last_retention_check.clear()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def set_privacy(self, **values) -> None:
        self.settings.write_text(json.dumps({"privacy": values}), encoding="utf-8")

    def add(self, content: str, *, days_ago: int = 0, importance: str = "normal") -> dict:
        when = tm._now() - timedelta(days=days_ago)
        with patch.object(tm, "_now", return_value=when):
            return tm.memory_add(content, importance=importance)

    def index_bytes(self) -> bytes:
        index = tm._index_path(self.root)
        return b"".join(Path(str(index) + suffix).read_bytes()
                        for suffix in ("", "-wal") if Path(str(index) + suffix).exists())


class MemoryPrivacyTests(_MemoryCase):
    def test_custom_root_and_every_file_become_owner_only(self) -> None:
        added = self.add("private fact")
        day_file = self.root / added["file_path"]
        self.assertEqual(0o700, mode(self.root))
        self.assertEqual(0o700, mode(day_file.parent))
        self.assertEqual(0o700, mode(day_file.parent.parent))
        self.assertEqual(0o600, mode(day_file))
        index = tm._index_path(self.root)
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(index) + suffix)
            if path.exists():
                self.assertEqual(0o600, mode(path), path.name)

    def test_existing_folders_are_tightened_on_the_next_read(self) -> None:
        added = self.add("older note")
        day_file = self.root / added["file_path"]
        for folder in (day_file.parent, day_file.parent.parent):
            folder.chmod(0o755)
        day_file.chmod(0o644)
        tm.memory_search(query="older")
        self.assertEqual((0o700, 0o700, 0o600),
                         (mode(day_file.parent), mode(day_file.parent.parent), mode(day_file)))

    def test_deleting_the_last_memory_of_a_day_leaves_nothing_behind(self) -> None:
        secret = "zebracactus-unique-7f3a"
        added = self.add(f"My locker code note {secret}")
        day_file = self.root / added["file_path"]
        tm.memory_delete(added["memory_id"], confirm=True)
        self.assertFalse(day_file.exists())
        self.assertFalse(day_file.parent.exists())
        self.assertEqual(0, tm.memory_search(query=secret)["count"])
        with sqlite3.connect(tm._index_path(self.root)) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0])
        self.assertNotIn(secret.encode(), self.index_bytes())

    def test_clear_previews_first_then_removes_every_representation(self) -> None:
        secret = "quokkalantern-unique-91bd"
        for index in range(3):
            self.add(f"memory {index} {secret}", days_ago=index)
        preview = tm.memory_clear()
        self.assertEqual((False, 3), (preview["ok"], preview["count"]))
        self.assertEqual(3, len(preview["sample"]))
        self.assertEqual(3, tm.memory_overview()["count"], "a preview must not delete")
        done = tm.memory_clear(confirm=True)
        self.assertEqual(3, done["deleted"])
        self.assertEqual(0, tm.memory_overview()["count"])
        self.assertEqual([], tm._day_files(self.root))
        self.assertNotIn(secret.encode(), self.index_bytes())

    def test_clear_can_be_limited_to_a_date_range(self) -> None:
        self.add("old", days_ago=10)
        self.add("new")
        today = tm._now().date()
        result = tm.memory_clear(confirm=True, date_to=(today - timedelta(days=5)).isoformat())
        self.assertEqual(1, result["deleted"])
        self.assertEqual(["new"], [m["content"] for m in tm.memory_export_all()["memories"]])

    def test_retention_is_off_by_default_and_keeps_important_memories(self) -> None:
        self.add("ancient normal", days_ago=400)
        self.add("ancient critical", days_ago=400, importance="critical")
        self.add("recent", days_ago=1)
        self.assertEqual(0, tm._apply_retention(self.root, force=True))
        self.assertEqual(3, tm.memory_overview()["count"])

        self.set_privacy(memory_retention_days=365)
        self.assertEqual(1, tm._apply_retention(self.root, force=True))
        contents = sorted(m["content"] for m in tm.memory_export_all()["memories"])
        self.assertEqual(["ancient critical", "recent"], contents)

        self.set_privacy(memory_retention_days=365, memory_keep_important=False)
        self.assertEqual(1, tm._apply_retention(self.root, force=True))
        self.assertEqual(["recent"], [m["content"] for m in tm.memory_export_all()["memories"]])
        overview = tm.memory_overview()
        self.assertEqual((365, False), (overview["retention_days"], overview["keep_important"]))

    def test_export_contains_every_memory_with_metadata(self) -> None:
        self.add("one", importance="high")
        self.add("two", days_ago=3)
        exported = tm.memory_export_all()
        self.assertEqual((2, "mac-mcp-memory-export"), (exported["count"], exported["kind"]))
        self.assertEqual({"one", "two"}, {m["content"] for m in exported["memories"]})
        self.assertTrue(all("memory_id" in m and "date" in m for m in exported["memories"]))


class MemoryEndpointTests(_MemoryCase):
    def client(self) -> TestClient:
        telemetry = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3")
        return TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)))

    def test_endpoints_need_the_token_and_confirmation(self) -> None:
        client = self.client()
        auth = {"Authorization": "Bearer " + TOKEN}
        self.add("keep me")
        for method, path in (("get", "/dashboard/api/memory"), ("get", "/dashboard/api/memory/export"),
                             ("post", "/dashboard/api/memory/clear"), ("post", "/dashboard/api/memory/settings")):
            self.assertEqual(401, getattr(client, method)(path).status_code, path)
        self.assertEqual(1, client.get("/dashboard/api/memory", headers=auth).json()["count"])
        self.assertEqual(1, client.get("/dashboard/api/memory/export", headers=auth).json()["count"])
        preview = client.post("/dashboard/api/memory/clear", json={}, headers=auth).json()
        self.assertTrue(preview["confirmation_required"])
        self.assertEqual(1, tm.memory_overview()["count"])
        bad = client.post("/dashboard/api/memory/settings", json={"retention_days": 7}, headers=auth)
        self.assertEqual(400, bad.status_code)
        saved = client.post("/dashboard/api/memory/settings", json={"retention_days": 90, "keep_important": False}, headers=auth).json()
        self.assertEqual((90, False), (saved["retention_days"], saved["keep_important"]))
        cleared = client.post("/dashboard/api/memory/clear", json={"confirm": True}, headers=auth).json()
        self.assertEqual(1, cleared["deleted"])


if __name__ == "__main__":
    unittest.main()
