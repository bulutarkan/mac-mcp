"""#228: a finished run is checked against an outside system (a URL, or a read-only MCP tool), read-only."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import run_postconditions as post
from mcp_server import scheduled_tasks as store
from tests.test_scheduled_tasks import StoreCase


class Response:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text


class DefinitionTests(StoreCase):
    def test_url_and_tool_checks_sit_next_to_file_checks(self) -> None:
        definition = store.validate_definition(self.sh(cwd=str(self.folder), verify=[
            {"path": "out.csv"},
            {"url": "https://www.reddit.com/r/x/comments/abc/_/def/.json?token=1", "contains": "def"},
            {"tool": "sheets__read_range", "arguments": {"range": "A1:B2"}, "contains": "{today}"},
        ]))
        checks = definition["verify"]
        self.assertEqual("file", checks[0].get("kind", "file"))
        self.assertEqual({"kind": "url", "url": "https://www.reddit.com/r/x/comments/abc/_/def/.json?token=1",
                          "contains": "def", "status": 200}, checks[1])
        self.assertEqual({"kind": "tool", "tool": "sheets__read_range", "arguments": {"range": "A1:B2"},
                          "contains": "{today}"}, checks[2])

    def test_bad_checks_are_refused(self) -> None:
        for bad in ({"url": "ftp://example.com/x"}, {"url": "https://user:pass@example.com/"},
                    {"url": "https://example.com", "status": 99}, {"tool": "no_separator"},
                    {"tool": "sheets__read", "arguments": "A1"}, {"url": "https://x.y", "contains": "{bogus}"}):
            with self.subTest(bad=bad), self.assertRaises(store.TaskError):
                store.validate_definition(self.sh(verify=[bad]))


class UrlCheckTests(unittest.TestCase):
    def check(self, responses, **extra):
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            item = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return item
        with patch.object(post, "_http_get", side_effect=get), patch.object(post, "RETRY_WAITS_S", (0, 0)):
            result = post.run_check({"kind": "url", "url": "https://example.com/p?secret=1", "status": 200, **extra})
        return result, calls

    def test_the_page_must_answer_and_show_the_text(self) -> None:
        result, calls = self.check([Response(200, "comment def here")], contains="def")
        self.assertEqual({"kind": "url", "url": "https://example.com/p", "ok": True, "status": 200}, result)
        self.assertEqual(1, len(calls))
        # The query (which may hold a token) and the page itself are never kept.
        self.assertNotIn("secret", str(result))

    def test_text_that_appears_late_is_waited_for_a_bounded_number_of_times(self) -> None:
        result, calls = self.check([Response(200, "not yet"), Response(200, "now def")], contains="def")
        self.assertTrue(result["ok"])
        self.assertEqual(2, len(calls))
        result, calls = self.check([Response(200, "never")], contains="def")
        self.assertEqual((False, "missing_text", 3), (result["ok"], result["problem"], len(calls)))

    def test_unreachable_is_unknown_not_a_pass_or_a_plain_fail(self) -> None:
        result, _ = self.check([OSError("no route")])
        self.assertEqual((False, "unreachable", True), (result["ok"], result["problem"], result["unknown"]))
        result, _ = self.check([Response(404, "")])
        self.assertEqual((False, "status", 404), (result["ok"], result["problem"], result["status"]))
        self.assertNotIn("unknown", result)


class ToolCheckTests(unittest.TestCase):
    def manager(self, *, read_only=True, connected=True, text="row 2026-10-17 done", error=False):
        calls = []

        def call(server, tool, arguments, timeout=None):
            calls.append((server, tool, arguments))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=error,
                                   structuredContent=None)
        status = {"servers": [{"name": "sheets", "status": "connected" if connected else "error", "tools": [
            {"name": "sheets__read_range", "remote_name": "read_range", "read_only": read_only}]}]}
        return SimpleNamespace(status=lambda: status, call=call), calls

    def run_tool(self, manager, **extra):
        check = {"kind": "tool", "tool": "sheets__read_range", "arguments": {"range": "A1"}, **extra}
        with patch("mcp_server.external_mcp.manager", return_value=manager), \
                patch.object(post, "RETRY_WAITS_S", (0, 0)), \
                patch.object(post, "_central_read_only", return_value=True):
            return post.run_check(check)

    def test_a_read_only_tool_is_called_and_its_answer_checked(self) -> None:
        manager, calls = self.manager()
        result = self.run_tool(manager, contains="2026-10-17")
        self.assertEqual({"kind": "tool", "tool": "sheets__read_range", "ok": True}, result)
        self.assertEqual([("sheets", "read_range", {"range": "A1"})], calls)

    def test_a_tool_that_may_change_things_is_never_called(self) -> None:
        manager, calls = self.manager(read_only=False)
        result = self.run_tool(manager)
        self.assertEqual((False, "not_read_only"), (result["ok"], result["problem"]))
        self.assertEqual([], calls)

    def test_a_server_that_is_not_connected_makes_the_check_unknown(self) -> None:
        manager, calls = self.manager(connected=False)
        result = self.run_tool(manager)
        self.assertEqual((False, "server_unavailable", True), (result["ok"], result["problem"], result["unknown"]))
        with patch("mcp_server.external_mcp.manager", return_value=None):
            missing = post.run_check({"kind": "tool", "tool": "sheets__read_range", "arguments": {}})
        self.assertEqual("server_unavailable", missing["problem"])

    def test_a_tool_error_or_missing_text_fails(self) -> None:
        manager, _ = self.manager(error=True)
        self.assertEqual("tool_error", self.run_tool(manager)["problem"])
        manager, _ = self.manager(text="other")
        self.assertEqual("missing_text", self.run_tool(manager, contains="2026-10-17")["problem"])


class CombinedTests(unittest.TestCase):
    def test_an_unknown_check_makes_the_run_unverified_and_a_failed_one_failed(self) -> None:
        from mcp_server import scheduled_task_runner as runner
        with patch.object(post, "run_check", side_effect=[{"kind": "url", "url": "u", "ok": True},
                                                          {"kind": "url", "url": "v", "ok": False,
                                                           "problem": "unreachable", "unknown": True}]):
            self.assertEqual("unverified", runner.check_outputs([{"kind": "url"}, {"kind": "url"}], 0)["state"])
        with patch.object(post, "run_check", side_effect=[{"kind": "url", "url": "v", "ok": False,
                                                           "problem": "unreachable", "unknown": True},
                                                          {"kind": "url", "url": "u", "ok": False,
                                                           "problem": "missing_text"}]):
            self.assertEqual("failed", runner.check_outputs([{"kind": "url"}, {"kind": "url"}], 0)["state"])


class DateWordTests(unittest.TestCase):
    def test_date_words_in_a_url_check_are_filled_in_when_it_runs(self) -> None:
        from mcp_server import scheduled_task_runner as runner
        seen = []
        with patch.object(post, "_http_get", side_effect=lambda url, **_: seen.append(url) or Response(200, "")):
            runner.check_outputs([{"kind": "url", "url": "https://example.com/{today}", "status": 200}], 0)
        self.assertEqual([f"https://example.com/{store.expand_dates('{today}', None)}"], seen)
