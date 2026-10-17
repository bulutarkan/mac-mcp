"""#255: frequent paths do no needless work. find_files reads a tree only as far as a page needs,
in the same order and with the same matches as before; tasks watching one folder share one read
of it; Live Island gets only the task fields it shows."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import folder_triggers
from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store
from mcp_server import tools_files
from tests.test_scheduled_tasks import AUTH, DashboardApiTests, StoreCase

ROOT = Path(__file__).resolve().parents[1]


class FindFilesOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-find-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        tree = ["a.py", "B.PY", "notes.md", ".hidden.py", "src/main.py", "src/util.txt", "src/deep/x.py",
                "src/deep/y.md", "src-old/z.py", "zz/.cache/c.py", "docs/a/readme.md", "docs/b.md", "Ä.py"]
        for name in tree:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        outside = Path(tempfile.mkdtemp(prefix="mac-mcp-find-out-")).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "linked.py").write_text("x", encoding="utf-8")
        (self.root / "link").symlink_to(outside)
        (self.root / "file-link.py").symlink_to(self.root / "a.py")

    def everything(self, pattern: str, file_type: str = "any", limit: int = 3) -> list:
        out, cursor = [], None
        while True:
            page = tools_files.find_files(None, pattern, path=str(self.root), file_type=file_type, limit=limit,
                                          cursor=cursor)
            out += [item["path"] for item in page["results"]]
            cursor = page["page"].get("next_cursor")
            if not cursor:
                return out

    def before(self, pattern: str, file_type: str = "any") -> list:
        """What the earlier sort-everything code returned."""
        found = sorted(self.root.rglob(pattern), key=lambda item: item.relative_to(self.root).parts)
        if file_type == "file":
            found = [item for item in found if item.is_file()]
        elif file_type == "dir":
            found = [item for item in found if item.is_dir()]
        return [str(item) for item in found]

    def test_matches_and_order_are_those_of_before(self) -> None:
        for pattern, file_type in (("*.py", "any"), ("*", "any"), ("*", "file"), ("*", "dir"), ("src*", "any"),
                                   ("deep/*.py", "any"), ("**/*.md", "any"), ("*.PY", "any"), ("a/*", "any"),
                                   ("nothing*", "any")):
            with self.subTest(pattern=pattern, file_type=file_type):
                self.assertEqual(self.before(pattern, file_type), self.everything(pattern, file_type))

    def test_a_page_reads_only_as_far_as_it_needs(self) -> None:
        big = self.root / "big"
        for i in range(60):
            folder = big / f"d{i:02d}"
            folder.mkdir(parents=True)
            for j in range(20):
                (folder / f"f{j:02d}.txt").write_text("x", encoding="utf-8")
        real = os.listdir
        listed = []

        def counting(path):
            listed.append(path)
            return real(path)

        with patch.object(tools_files.os, "listdir", counting):
            page = tools_files.find_files(None, "*.txt", path=str(big), limit=5)
        self.assertEqual(5, page["count"])
        self.assertLessEqual(len(listed), 3)
        with patch.object(tools_files.os, "listdir", counting):
            listed.clear()
            tools_files.find_files(None, "*.txt", path=str(big), limit=5, cursor=page["page"]["next_cursor"])
        self.assertLessEqual(len(listed), 3)  # folders before the cursor are skipped, not listed again


class SharedFolderReadTests(StoreCase):
    def test_twenty_tasks_on_one_folder_read_it_once_a_pass(self) -> None:
        inbox = self.dir / "inbox"
        inbox.mkdir()
        (inbox / "old.pdf").write_text("x")
        for i in range(20):
            store.create_task(self.ai(prompt=f"Task {i} {{file}}",
                                      schedule={"type": "folder", "folder": str(inbox), "match": "pdf"}))
        triggers = runner.Scheduler(None, launch=lambda *args: None).folders
        real = os.scandir
        reads = []

        def counting(path):
            reads.append(path)
            return real(path)

        with patch.object(folder_triggers.os, "scandir", counting):
            triggers.check(time.time())
        self.assertEqual([str(inbox)], reads)

    def test_tasks_on_one_folder_still_see_only_their_own_matches(self) -> None:
        inbox = self.dir / "inbox"
        inbox.mkdir()
        files = {"a.pdf": "x", "b.txt": "y"}
        for name, body in files.items():
            (inbox / name).write_text(body)
        scanned: dict = {}
        pdfs = folder_triggers.listing({"type": "folder", "folder": str(inbox), "match": "pdf"}, scanned)
        texts = folder_triggers.listing({"type": "folder", "folder": str(inbox), "match": "txt"}, scanned)
        self.assertEqual((["a.pdf"], ["b.txt"]), (sorted(pdfs), sorted(texts)))
        pdfs["a.pdf"][0] = -1  # a task's copy can't change what the next task sees
        self.assertNotEqual(-1, folder_triggers.listing({"type": "folder", "folder": str(inbox), "match": "pdf"},
                                                        scanned)["a.pdf"][0])


class IslandViewTests(StoreCase):
    client = DashboardApiTests.client  # the route client, without running that class's tests again

    def test_the_island_view_leaves_definitions_out(self) -> None:
        client = self.client()
        for i in range(30):
            store.create_task(self.ai(prompt="Write the weekly report. " * 40, name=f"Report {i}"))
        store.create_task(self.ai(name="Suggested"), by="Claude")
        full = client.get("/dashboard/api/scheduled-tasks", headers=AUTH).json()
        island = client.get("/dashboard/api/scheduled-tasks?view=island", headers=AUTH).json()
        self.assertEqual([task["id"] for task in full["tasks"]], [task["id"] for task in island["tasks"]])
        self.assertNotIn("prompt", json.dumps(island))
        self.assertNotIn("agents", island)
        self.assertLess(len(json.dumps(island)), len(json.dumps(full)) / 4)
        suggested = next(task for task in island["tasks"] if task.get("proposal"))
        self.assertEqual({"name", "proposed_by", "is_new"}, set(suggested["proposal"]))
        for key in ("name", "state", "approved", "next_run_at"):
            self.assertIn(key, island["tasks"][-1])

    def test_live_island_asks_for_the_island_view(self) -> None:
        source = (ROOT / "menu_app" / "Sources" / "LiveIsland.swift").read_text(encoding="utf-8")
        self.assertIn('taskAPI("dashboard/api/scheduled-tasks?view=island")', source)


if __name__ == "__main__":
    unittest.main()
