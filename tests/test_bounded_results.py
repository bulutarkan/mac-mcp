"""#252: results that are cut say so and can be continued: search_files pages by offset with a true total,
spotlight_search caps max_results and reports what it left out, scheduled_tasks lists in pages and gives
one run's full record by run_id."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server.tools_search as tools_search
from mcp_server import scheduled_tasks as store
from mcp_server.policy import PolicyContext, evaluate_tool_scope, reset_policy_context, set_policy_context  # noqa: F401
from mcp_server.policy_scope import ResourceScope
from tests.test_scheduled_tasks import StoreCase


class SearchFilesPagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-paging-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = self.root / "state"
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        self.work = self.root / "work"
        self.work.mkdir()
        # 3 files x 400 lines of ~100 characters: about 120,000 characters of matches, so three pages.
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.work / name).write_text(
                "".join(f"needle {name} line {i:04d} " + "x" * 70 + "\n" for i in range(400)), encoding="utf-8")

    def pages(self) -> list:
        out, offset = [], 0
        while True:
            page = tools_search.search_files(None, "needle", path=str(self.work), offset=offset)
            out.append(page)
            if not page["has_more"]:
                return out
            self.assertEqual(offset + page["match_count"], page["next_offset"])
            offset = page["next_offset"]

    def assert_every_line_once(self, pages: list) -> None:
        lines = [line for page in pages for line in page["results"].splitlines()]
        self.assertEqual(1200, len(lines))
        self.assertEqual(1200, len(set(lines)))
        self.assertTrue(all(page["total_matches"] == 1200 for page in pages))
        self.assertTrue(all(len(page["results"]) <= tools_search.SEARCH_PAGE_CHARS for page in pages))
        self.assertTrue(pages[0]["truncated"])
        self.assertIn("offset=", pages[0]["note"])
        self.assertFalse(pages[-1]["truncated"])

    def test_grep_results_continue_by_offset_without_gaps(self) -> None:
        pages = self.pages()
        self.assertGreaterEqual(len(pages), 3)
        self.assert_every_line_once(pages)

    def test_scoped_results_continue_by_offset_without_gaps(self) -> None:
        scope = ResourceScope(path_roots=(str(self.work),), tool_families=("files", "search"),
                              access_mode="workspace_write")
        token = set_policy_context(PolicyContext(profile="developer", actor="agent:paging", agent_id="agt_paging",
                                                 scope=scope))
        try:
            pages = self.pages()
        finally:
            reset_policy_context(token)
        self.assertGreaterEqual(len(pages), 3)
        self.assert_every_line_once(pages)

    def test_a_small_search_is_one_complete_page(self) -> None:
        (self.work / "d.md").write_text("one rare word\n", encoding="utf-8")
        page = tools_search.search_files(None, "rare word", path=str(self.work))
        self.assertEqual((1, 1, False, False), (page["match_count"], page["total_matches"], page["has_more"],
                                                page["truncated"]))
        self.assertNotIn("next_offset", page)


class SpotlightLimitTests(unittest.TestCase):
    def run_with(self, count: int, **kwargs) -> dict:
        found = "".join(f"/Users/someone/Documents/report-{i}.pdf\n" for i in range(count))
        done = subprocess.CompletedProcess(["mdfind"], 0, stdout=found, stderr="")
        with patch.object(tools_search.subprocess, "run", return_value=done):
            return tools_search.spotlight_search(None, "report", **kwargs)

    def test_max_results_is_capped_and_the_rest_is_reported(self) -> None:
        first = self.run_with(700, max_results=10_000)
        self.assertEqual((500, 700, True, 500), (first["count"], first["total"], first["has_more"], first["next_offset"]))
        rest = self.run_with(700, max_results=500, offset=first["next_offset"])
        self.assertEqual((200, False), (rest["count"], rest["has_more"]))
        self.assertEqual("/Users/someone/Documents/report-500.pdf", rest["results"][0])

    def test_everything_found_fits(self) -> None:
        answer = self.run_with(3)
        self.assertEqual((3, 3, False), (answer["count"], answer["total"], answer["has_more"]))
        self.assertNotIn("next_offset", answer)


class ScheduledTaskPagingTests(StoreCase):
    def make(self, n: int) -> None:
        for i in range(n):
            store.create_task({"name": f"Report {i:03d}", "kind": "script", "command": "echo hi",
                               "schedule": {"type": "daily", "time": "09:00"}}, by="owner")

    def test_two_hundred_tasks_come_in_pages_with_totals(self) -> None:
        self.make(200)
        tasks = store.list_tasks()
        first = store.page_of(tasks)
        self.assertEqual((50, 200, True, "50"), (len(first["tasks"]), first["total"], first["has_more"],
                                                first["next_cursor"]))
        self.assertEqual(200, sum(first["counts"].values()))
        seen = [task["id"] for task in first["tasks"]]
        cursor = first["next_cursor"]
        while cursor:
            page = store.page_of(tasks, limit=1000, cursor=cursor)
            self.assertLessEqual(len(page["tasks"]), store.LIST_PAGE_MAX)
            seen += [task["id"] for task in page["tasks"]]
            cursor = page.get("next_cursor")
        self.assertEqual([task["id"] for task in tasks], seen)
        with self.assertRaises(store.TaskError):
            store.page_of(tasks, cursor="later")

    def test_brief_tasks_leave_the_definition_out(self) -> None:
        self.make(1)
        task = store.page_of(store.list_tasks(), brief=True)["tasks"][0]
        self.assertEqual("Report 000", task["name"])
        self.assertNotIn("command", task)
        self.assertIn("schedule_text", task)

    def test_one_run_is_read_whole_by_its_id(self) -> None:
        self.make(1)
        task_id = store.list_tasks()[0]["id"]
        run_id = store.start_run(task_id, trigger="manual")
        store.finish_run(run_id, "done", summary="S" * 2000, output="line\n" * 1000)
        short = store.list_runs(task_id)[0]
        self.assertEqual(300, len(short["summary"]))
        self.assertNotIn("output", short)
        whole = store.get_run(short["id"])
        self.assertEqual(2000, len(whole["summary"]))
        self.assertEqual(5000, len(whole["output"]))


class ToolWiringTests(unittest.TestCase):
    def test_the_tools_offer_continuation(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "mcp_server" / "main.py").read_text(encoding="utf-8")
        tool = source[source.index('name="scheduled_tasks"'):source.index('@mcp.tool(name="search_files"')]
        for needle in ('verb == "get_run"', "scheduled_tasks.page_of(tasks, limit=limit, cursor=cursor, brief=brief)",
                       "action=list|get|get_run|create"):
            self.assertIn(needle, tool)
        self.assertIn("offset=offset", source[source.index('@mcp.tool(name="search_files"'):])

    def test_get_run_is_a_read(self) -> None:
        from mcp_server.policy import _scheduled_tasks_risk
        self.assertEqual("read_only", str(_scheduled_tasks_risk({"action": "get_run"}).requested_access_mode.value))


if __name__ == "__main__":
    unittest.main()
