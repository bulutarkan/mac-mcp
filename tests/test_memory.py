import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_memory as memory


IST = timezone(timedelta(hours=3))


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-memory-test-")
        self.root = Path(self.temp.name) / "memory"
        self.old_env = os.environ.get("MAC_MCP_MEMORY_DIR")
        self.old_embed = os.environ.get("MAC_MCP_MEMORY_EMBEDDING")
        os.environ["MAC_MCP_MEMORY_DIR"] = str(self.root)
        os.environ["MAC_MCP_MEMORY_EMBEDDING"] = "feature_hash"

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("MAC_MCP_MEMORY_DIR", None)
        else:
            os.environ["MAC_MCP_MEMORY_DIR"] = self.old_env
        if self.old_embed is None:
            os.environ.pop("MAC_MCP_MEMORY_EMBEDDING", None)
        else:
            os.environ["MAC_MCP_MEMORY_EMBEDDING"] = self.old_embed
        self.temp.cleanup()

    def add_at(self, dt, content, **kwargs):
        with patch("mcp_server.tools_memory._now", return_value=dt):
            return memory.memory_add(content, **kwargs)

    def test_memory_add_creates_daily_markdown_and_sqlite_index(self):
        result = self.add_at(
            datetime(2026, 9, 7, 12, 54, 18, tzinfo=IST),
            "User prefers semantic browser actions.",
            tags=["browser", "Mac MCP"], importance="high", source="conversation",
        )
        self.assertTrue(result["ok"])
        self.assertEqual("2026-09-07", result["date"])
        self.assertEqual("12:54:18", result["time"])
        self.assertTrue(result["created_at"].endswith("+03:00"))
        day = self.root / "2026" / "09" / "2026-09-07.md"
        self.assertTrue(day.exists())
        text = day.read_text()
        self.assertIn("# Memory — 2026-09-07", text)
        self.assertIn("## 12:54:18", text)
        self.assertIn(result["memory_id"], text)
        self.assertIn("User prefers semantic browser actions.", text)
        self.assertTrue((self.root / "memory-index.sqlite3").exists())

    def test_search_get_and_date_range(self):
        a = self.add_at(datetime(2026, 9, 5, 9, 0, tzinfo=IST), "User likes brutalist architecture.", tags=["travel"])
        b = self.add_at(datetime(2026, 9, 6, 10, 0, tzinfo=IST), "Mac MCP browser automation should batch Sahibinden filters.", tags=["mac-mcp", "browser"], importance="high")
        c = self.add_at(datetime(2026, 9, 7, 11, 0, tzinfo=IST), "Coffee preference: decaf with low caffeine.", tags=["coffee"])

        found = memory.memory_search(query="Sahibinden browser filters", date_from="2026-09-05", date_to="2026-09-07")
        self.assertEqual("hybrid_search", found["mode"])
        self.assertGreaterEqual(found["count"], 1)
        self.assertEqual(b["memory_id"], found["results"][0]["memory_id"])
        self.assertEqual("sqlite_fts5", found["search_backend"]["fts"])
        self.assertEqual("feature_hash_v1", found["search_backend"]["vector"])

        listed = memory.memory_search(date_from="2026-09-06", date_to="2026-09-07", sort="oldest")
        self.assertEqual([b["memory_id"], c["memory_id"]], [x["memory_id"] for x in listed["results"]])

        exact = memory.memory_get(a["memory_id"])
        self.assertEqual("User likes brutalist architecture.", exact["content"])
        self.assertEqual(["travel"], exact["tags"])

    def test_update_selection_and_exact_edit(self):
        first = self.add_at(datetime(2026, 9, 7, 12, 0, tzinfo=IST), "Old browser preference.", tags=["browser"])
        self.add_at(datetime(2026, 9, 7, 12, 5, tzinfo=IST), "Another memory.")

        selection = memory.memory_update(date="2026-09-07")
        self.assertTrue(selection["selection_required"])
        self.assertEqual("update", selection["action"])
        self.assertEqual(2, selection["total_matches"])
        self.assertIn("time", selection["results"][0])

        with patch("mcp_server.tools_memory._now", return_value=datetime(2026, 9, 7, 13, 30, tzinfo=IST)):
            updated = memory.memory_update(
                memory_id=first["memory_id"],
                content="Use semantic batch actions for browser filters.",
                tags=["browser", "automation"], importance="high",
            )
        self.assertEqual("updated", updated["action"])
        self.assertEqual("Use semantic batch actions for browser filters.", updated["content"])
        self.assertEqual(["browser", "automation"], updated["tags"])
        self.assertTrue(updated["updated_at"].endswith("+03:00"))
        exact = memory.memory_get(first["memory_id"])
        self.assertEqual(updated["content"], exact["content"])

    def test_delete_selection_confirmation_and_delete(self):
        target = self.add_at(datetime(2026, 9, 6, 8, 15, tzinfo=IST), "Temporary memory to delete.")
        self.add_at(datetime(2026, 9, 7, 8, 15, tzinfo=IST), "Keep this one.")

        selection = memory.memory_delete(date_from="2026-09-06", date_to="2026-09-07")
        self.assertTrue(selection["selection_required"])
        self.assertEqual("delete", selection["action"])
        self.assertEqual(2, selection["total_matches"])

        preview = memory.memory_delete(memory_id=target["memory_id"])
        self.assertTrue(preview["confirmation_required"])
        self.assertFalse(preview["ok"])

        deleted = memory.memory_delete(memory_id=target["memory_id"], confirm=True)
        self.assertTrue(deleted["ok"])
        self.assertEqual("deleted", deleted["action"])
        with self.assertRaises(HTTPException) as ctx:
            memory.memory_get(target["memory_id"])
        self.assertEqual(404, ctx.exception.status_code)

    def test_manual_markdown_edit_is_reindexed(self):
        item = self.add_at(datetime(2026, 9, 7, 14, 0, tzinfo=IST), "Original searchable wording.")
        day = self.root / "2026" / "09" / "2026-09-07.md"
        text = day.read_text().replace("Original searchable wording.", "Manually edited filesystem memory about orchids.")
        day.write_text(text)
        result = memory.memory_search(query="orchids")
        self.assertEqual(item["memory_id"], result["results"][0]["memory_id"])
        self.assertIn("Manually edited", memory.memory_get(item["memory_id"])["content"])

    def test_date_validation(self):
        with self.assertRaises(HTTPException):
            memory.memory_search(date="2026-09-07", date_from="2026-09-01")
        with self.assertRaises(HTTPException):
            memory.memory_search(date_from="2026-09-08", date_to="2026-09-01")


if __name__ == "__main__":
    unittest.main()
