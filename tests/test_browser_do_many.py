"""#140: browser_do opens several URLs in one call, and acts in several tabs in one call."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError

import mcp_server.main as main
from mcp_server import browser_tabs, computer_plan, data_guard, workspace_arbitration
from mcp_server import tools_browser_agent as agent
from mcp_server.security_context import SecurityContextManager
from tests.test_core_catalog_descriptions import _build_mcp

_HELD = {"ok": False, "error": "tab_owned_by_other_agent", "retryable": True,
         "owner": "another_agent", "message": "The tab is still leased by another delegated agent."}


def _opened(url: str) -> dict:
    handle = "t-" + url.rsplit("/", 1)[-1]
    return {"ok": True, "url": url, "tab_handle": handle, "window_index": 1, "tab_index": 2, "background": True}


class BrowserDoManyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()

    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, arguments: dict):
        result = asyncio.run(self.mcp.call_tool("browser_do", {"description": "Several pages", "browser": "Safari", **arguments}))
        content = result[0] if isinstance(result, tuple) else result
        return json.loads(content[0].text)

    def test_urls_open_in_background_tabs_and_one_failure_does_not_stop_the_others(self) -> None:
        opens, acts, closed = [], [], []

        def fake_open(_settings, **kwargs):
            opens.append(kwargs)
            if kwargs["url"].endswith("/b"):
                raise HTTPException(502, {"ok": False, "error": "open_failed", "message": "Safari did not open it."})
            return _opened(kwargs["url"])

        def fake_act(_settings, **kwargs):
            acts.append(kwargs)
            handle = kwargs["tab_handle"]
            return {"ok": True, "actions": [
                {"type": "wait", "ok": True, "matched": True},
                {"type": "read", "ok": True, "text": f"text of {handle}", "total_chars": 11, "complete": True,
                 "url": f"http://127.0.0.1/{handle}", "title": handle},
            ]}

        with patch.object(main, "browser_open_url", side_effect=fake_open), \
                patch.object(main, "browser_act", side_effect=fake_act), \
                patch.object(main, "browser_close_tab", side_effect=lambda _s, **kw: closed.append(kw["tab_handle"]) or {"ok": True}):
            out = self._call({"urls": ["http://127.0.0.1/a", "http://127.0.0.1/b", "http://127.0.0.1/c"],
                              "actions": [{"type": "read"}], "close_after": True})
        # Every tab opens first, new and in the background; then each page is read.
        self.assertEqual([(True, True)] * 3, [(call["new_tab"], call["background"]) for call in opens])
        self.assertEqual(["t-a", "t-c"], [call["tab_handle"] for call in acts])
        self.assertFalse(any(call.get("allow_foreground") for call in acts))
        self.assertEqual("network_idle", acts[0]["actions"][0]["for"])
        self.assertEqual((3, 1, False, "sequential"), (out["tab_count"], out["failed_count"], out["ok"], out["dispatch"]))
        first, failed, last = out["tabs"]
        self.assertEqual(("t-a", "text of t-a", True, True), (first["tab_handle"], first["read"]["text"], first["closed"],
                                                            first["mutation_dispatched"]))
        self.assertEqual("http://127.0.0.1/a", first["requested_url"])
        self.assertEqual(("http://127.0.0.1/b", "open_failed"), (failed["requested_url"], failed["error"]))
        self.assertNotIn("tab_handle", failed)
        self.assertEqual("text of t-c", last["read"]["text"])
        self.assertEqual(["t-a", "t-c"], closed)

    def test_close_after_also_closes_a_tab_whose_actions_failed(self) -> None:
        closed = []

        def fake_act(_settings, **kwargs):
            if kwargs["tab_handle"] == "t-a":
                raise HTTPException(502, {"ok": False, "error": "page_script_failed", "message": "boom"})
            return {"ok": True, "actions": [{"type": "extract", "ok": True, "data": {"h1": "C"}}]}

        with patch.object(main, "browser_open_url", side_effect=lambda _s, **kw: _opened(kw["url"])), \
                patch.object(main, "browser_act", side_effect=fake_act), \
                patch.object(main, "browser_close_tab", side_effect=lambda _s, **kw: closed.append(kw["tab_handle"]) or {"ok": True}):
            out = self._call({"urls": ["http://127.0.0.1/a", "http://127.0.0.1/c"], "close_after": True,
                              "actions": [{"type": "extract", "fields": [{"name": "h1", "selector": "h1"}]}]})
        failed, done = out["tabs"]
        self.assertEqual((False, "t-a", True, "page_script_failed"), (failed["ok"], failed["tab_handle"], failed["closed"], failed["error"]))
        # The tab was opened, so the call did act: it is not reported as never run.
        self.assertFalse(failed["not_executed"])
        self.assertEqual(({"h1": "C"}, True), (done["data"], done["closed"]))
        self.assertEqual(["t-a", "t-c"], closed)

    def test_urls_may_change_their_own_new_tabs_and_unconfirmed_clicks_are_named(self) -> None:
        def fake_act(_settings, **kwargs):
            return {"ok": True, "unconfirmed_actions": [1], "confirm_hint": "check it", "actions": [
                {"type": "wait", "ok": True, "matched": True},
                {"type": "click", "ok": True, "outcome": "page_reacted"},
            ], "mutation_dispatched": True}

        with patch.object(main, "browser_open_url", side_effect=lambda _s, **kw: _opened(kw["url"])), \
                patch.object(main, "browser_act", side_effect=fake_act):
            out = self._call({"urls": ["http://127.0.0.1/a"], "actions": [{"type": "click", "query": "More"}]})
        page = out["tabs"][0]
        self.assertEqual(([1], "check it", "t-a"), (page["unconfirmed_actions"], page["confirm_hint"], page["tab_handle"]))

    def test_every_result_kind_reaches_the_caller(self) -> None:
        def fake_act(_settings, **kwargs):
            return {"ok": True, "actions": [
                {"type": "wait", "ok": True, "matched": True},
                {"type": "extract", "ok": True, "data": {"h1": "A"}},
                {"type": "read", "ok": True, "text": "All of A", "total_chars": 8},
                {"type": "scan", "ok": True, "items": [{"text": "row"}], "count": 1, "complete": True},
                {"type": "pdf", "ok": True, "method": "webkit_snapshot", "path": "/tmp/a.pdf", "artifact_id": "art_a",
                 "sha256": "cd", "size": 20},
                {"type": "network", "ok": True, "requests": [{"seq": 1, "url": "http://127.0.0.1/api"}], "next_since": 1},
            ]}

        with patch.object(main, "browser_open_url", side_effect=lambda _s, **kw: _opened(kw["url"])), \
                patch.object(main, "browser_act", side_effect=fake_act):
            out = self._call({"urls": ["http://127.0.0.1/a"], "actions": [
                {"type": "extract", "fields": [{"name": "h1", "selector": "h1"}]}, {"type": "read"},
                {"type": "scan", "selector": ".row"}, {"type": "pdf"}, {"type": "network"}]})
        page = out["tabs"][0]
        self.assertEqual({"h1": "A"}, page["data"])
        self.assertEqual("All of A", page["read"]["text"])
        self.assertEqual([{"text": "row"}], page["scan"]["items"])
        self.assertEqual(("/tmp/a.pdf", "art_a"), (page["pdf"]["path"], page["pdf"]["artifact_id"]))
        self.assertEqual("http://127.0.0.1/api", page["network"]["requests"][0]["url"])

    def test_tabs_with_their_own_actions_return_each_browser_act_result(self) -> None:
        seen = []

        def fake_act(_settings, **kwargs):
            seen.append(kwargs)
            if kwargs["tab_handle"] == "t-read":
                return {"ok": True, "actions": [{"type": "extract", "ok": True, "data": {"h1": "R"}}]}
            return {"ok": True, "action_count": 2, "changes": {"url_changed": True}, "actions": [
                {"type": "type", "ok": True, "outcome": "confirmed"},
                {"type": "network", "ok": True, "requests": [{"seq": 4, "url": "http://127.0.0.1/save"}]},
            ]}

        with patch.object(main, "browser_act", side_effect=fake_act), \
                patch.object(main, "browser_open_url", side_effect=AssertionError("opened")):
            out = self._call({"tabs": [{"tab_handle": "t-a", "actions": [{"type": "type", "query": "Name", "text": "Ada"},
                                                                          {"type": "network"}]}, "t-read"],
                              "actions": [{"type": "extract", "fields": [{"name": "h1", "selector": "h1"}]}]})
        acted, read = out["tabs"]
        self.assertEqual("type", seen[0]["actions"][0]["type"])
        self.assertFalse(any(call.get("allow_foreground") for call in seen))
        # The acting tab gets browser_act's own result: every action with its outcome and output.
        self.assertEqual(("t-a", "confirmed", {"url_changed": True}), (acted["tab_handle"], acted["actions"][0]["outcome"], acted["changes"]))
        self.assertEqual("http://127.0.0.1/save", acted["actions"][1]["requests"][0]["url"])
        self.assertEqual(({"h1": "R"}, "t-read"), (read["data"], read["tab_handle"]))

    def test_a_known_failure_in_one_tab_leaves_the_call_outcome_known(self) -> None:
        # A delegated agent's checkpoint stops every later side effect after an unknown outcome; a fill
        # that failed on one field it did not change is a known failure, not an unknown one.
        known = {"ok": False, "error": "fill_incomplete", "outcome_unknown": False, "mutation_dispatched": True,
                 "actions": [{"type": "fill", "ok": False, "outcome": "failed", "outcome_unknown": False}]}
        unclear = {"ok": False, "error": "chrome_debugger_evaluate_failed", "mutation_dispatched": True, "actions": []}
        for second, expected in ((known, False), (unclear, True)):
            def fake_act(_settings, **kwargs):
                if kwargs["tab_handle"] == "t-a":
                    return {"ok": True, "actions": [{"type": "fill", "ok": True, "outcome": "confirmed"}]}
                return dict(second)

            with self.subTest(expected=expected), patch.object(main, "browser_act", side_effect=fake_act):
                out = self._call({"tabs": [{"tab_handle": "t-a", "actions": [{"type": "fill", "values": {"A": "1"}}]},
                                           {"tab_handle": "t-b", "actions": [{"type": "fill", "values": {"A": "2"}}]}]})
            self.assertEqual((False, 1, expected), (out["ok"], out["failed_count"], out["outcome_unknown"]))

    def test_refusals_happen_before_anything_runs(self) -> None:
        cases = [
            {"urls": [], "actions": [{"type": "read"}]},
            {"urls": [f"http://127.0.0.1/{n}" for n in range(11)]},
            {"urls": ["http://127.0.0.1/a"], "url": "http://127.0.0.1/b"},
            {"urls": ["http://127.0.0.1/a"], "tab_handle": "t-b"},
            {"urls": ["http://127.0.0.1/a"], "background": False},
            {"urls": ["http://127.0.0.1/a"], "new_tab": False},
            {"urls": ["http://127.0.0.1/a"], "tabs": ["t-a"]},
            {"tabs": [{"tab_handle": "t-a", "actions": []}]},
            {"tabs": [{"tab_handle": "t-a", "actions": [{"type": "teleport"}]}]},
            {"tabs": [{"actions": [{"type": "read"}]}]},
            {"tabs": [{"tab_handle": "t-a", "actions": [{"type": "read"}]}, "t-a"]},
            # Shared actions for tabs stay read-only, also when one tab has its own.
            {"tabs": ["t-a", {"tab_handle": "t-b", "actions": [{"type": "read"}]}], "actions": [{"type": "click", "query": "Buy"}]},
            {"tabs": [{"tab_handle": "t-a", "actions": [{"type": "read"}]}], "close_after": True},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with patch.object(main, "browser_act", side_effect=AssertionError("ran")), \
                        patch.object(main, "browser_open_url", side_effect=AssertionError("ran")):
                    with self.assertRaises(ToolError) as ctx:
                        self._call(arguments)
                self.assertNotIn("ran", str(ctx.exception))

    def test_chrome_runs_pages_at_the_same_time_and_safari_one_after_another(self) -> None:
        for browser, expected in (("Chrome", "concurrent"), ("Safari", "sequential")):
            with self.subTest(browser=browser):
                barrier = threading.Barrier(3, timeout=3)
                lock = threading.Lock()
                state = {"active": 0, "peak": 0}

                def fake_act(_settings, **kwargs):
                    with lock:
                        state["active"] += 1
                        state["peak"] = max(state["peak"], state["active"])
                    try:
                        if browser == "Chrome":
                            barrier.wait()  # all three tabs must be in flight at once
                        else:
                            time.sleep(0.02)
                    finally:
                        with lock:
                            state["active"] -= 1
                    return {"ok": True, "actions": [{"type": "read", "ok": True, "text": kwargs["tab_handle"]}]}

                with patch.object(main, "browser_act", side_effect=fake_act):
                    out = self._call({"browser": browser, "tabs": ["t-1", "t-2", "t-3"], "actions": [{"type": "read"}]})
                self.assertEqual(expected, out["dispatch"])
                self.assertEqual(["t-1", "t-2", "t-3"], [page["read"]["text"] for page in out["tabs"]])
                self.assertEqual(3 if browser == "Chrome" else 1, state["peak"])


class MultiTabLeaseTests(unittest.TestCase):
    """A held tab and an owner-taken-over tab go through the real lease and are reported, never taken."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()

    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for registry in (browser_tabs._LOGICAL_LEASES, workspace_arbitration._OWNER_TAKEOVERS):
            registry.clear()
            self.addCleanup(registry.clear)

    def test_held_and_taken_over_tabs_are_reported_and_the_free_tab_acts(self) -> None:
        now = time.time()
        for handle, agent_id in (("t-held", "a1"), ("t-free", "b2")):  # b2 observed t-free before, as for browser_act
            browser_tabs._LOGICAL_LEASES[handle] = {
                "owner": f"agent:{agent_id}", "agent_id": agent_id, "generation": 3, "origin": "http://127.0.0.1",
                "created_at": now, "last_seen_at": now, "expires_at": now + 300,
            }
        workspace_arbitration.record_owner_takeover("t-owner", {"browser": "Safari"})
        locked = []

        def fake_locked(**kwargs):
            locked.append(kwargs["tab_handle"])
            return {"ok": True, "actions": [{"type": "click", "ok": True, "outcome": "confirmed"}]}

        def row(_browser, handle):
            return 1, 2, {"browser": "Safari", "window_index": 1, "tab_index": 2, "tab_handle": handle,
                          "url": "http://127.0.0.1/page", "title": "Page", "active": False}

        click = [{"type": "click", "query": "Next"}]
        with patch.object(browser_tabs, "_logical_owner", return_value=("agent:b2", "b2", "trusted")), \
                patch.object(browser_tabs, "resolve_tab", side_effect=row), \
                patch.object(browser_tabs, "browser_human_takeover", return_value=None), \
                patch.object(browser_tabs, "claim_delegated_resource", return_value=None), \
                patch.object(agent, "_ensure_visual_companion"), \
                patch.object(agent, "_require_stable_handle_for_mutation"), \
                patch.object(agent, "_browser_act_locked", side_effect=fake_locked):
            result = asyncio.run(self.mcp.call_tool("browser_do", {"description": "Act", "browser": "Safari", "tabs": [
                {"tab_handle": "t-free", "actions": click}, {"tab_handle": "t-held", "actions": click},
                {"tab_handle": "t-owner", "actions": click}]}))
        out = json.loads((result[0] if isinstance(result, tuple) else result)[0].text)
        free, held, owner = out["tabs"]
        self.assertEqual(["t-free"], locked)
        self.assertEqual((True, "confirmed"), (free["ok"], free["actions"][0]["outcome"]))
        self.assertEqual(("t-held", "tab_owned_by_other_agent", True), (held["tab_handle"], held["error"], held["not_executed"]))
        self.assertEqual(("t-owner", "OWNER_TOOK_OVER", False, True),
                         (owner["tab_handle"], owner["reason_code"], owner["retryable"], owner["not_executed"]))
        self.assertEqual("agent:a1", browser_tabs._LOGICAL_LEASES["t-held"]["owner"])
        self.assertEqual((3, 2), (out["tab_count"], out["failed_count"]))


class GuardsSeeEveryTabsActionsTests(unittest.TestCase):
    def test_tab_actions_are_redacted_scanned_and_counted(self) -> None:
        secret = "Zq8vL2mX9pR4tY7wK3nB6cF1hJ5sD0gA"
        args = {"tabs": ["t-r", {"tab_handle": "t-a", "actions": [{"type": "type", "query": "Key", "text": secret},
                                                                    {"type": "click", "query": "Save"}]}]}
        clean = data_guard.sanitize_tool_arguments("browser_do", args)
        self.assertEqual("[BROWSER INPUT REDACTED]", clean["tabs"][1]["actions"][0]["text"])
        self.assertEqual(secret, args["tabs"][1]["actions"][0]["text"])
        known = data_guard.secret_fingerprints(secret, include_entropy=True, include_whole=True)
        self.assertTrue(data_guard.scan_sensitive_egress("browser_do", args, known).sensitive)
        self.assertEqual(3, computer_plan._action_units("browser_do", {**args, "actions": [{"type": "read"}]}))
        self.assertEqual(4, computer_plan._action_units("browser_do", {"urls": ["a", "b"], "actions": [{"type": "read"}]}))

    def test_an_untrusted_page_among_several_marks_the_session(self) -> None:
        result = {"ok": True, "tabs": [{"ok": True, "url": "http://127.0.0.1/a", "tab_handle": "t-a"},
                                       {"ok": True, "url": "https://example.com/b", "tab_handle": "t-b"}]}
        self.assertEqual(["http://127.0.0.1/a", "https://example.com/b"],
                         SecurityContextManager._context_values((["text"], result), "url"))
        manager = SecurityContextManager()
        state = manager.observe_browser_result(key="k", public_session_id="s", tool="browser_do",
                                               arguments={"browser": "Safari", "tabs": ["t-a", "t-b"]}, result=result)
        self.assertEqual("https://example.com", state.current_origin)
        self.assertEqual("https://example.com", state.provenance_origin)


if __name__ == "__main__":
    unittest.main()
