"""#137: browser_do tabs=[...] reads several open tabs in one call, in the background, without taking a held tab."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError

import mcp_server.main as main
from mcp_server import browser_tabs
from mcp_server import tools_browser_agent as agent
from tests.test_core_catalog_descriptions import _build_mcp

_HELD = HTTPException(409, {"ok": False, "error": "tab_owned_by_other_agent", "retryable": True,
                            "owner": "another_agent", "message": "The tab is still leased by another delegated agent."})


class BrowserDoTabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()

    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, arguments: dict):
        result = asyncio.run(self.mcp.call_tool("browser_do", {"description": "Read tabs", "browser": "Safari", **arguments}))
        content = result[0] if isinstance(result, tuple) else result
        return json.loads(content[0].text)

    def test_three_tabs_in_one_call_with_a_held_tab_reported_not_taken(self) -> None:
        seen = []

        def fake_act(_settings, **kwargs):
            seen.append(kwargs)
            if kwargs["tab_handle"] == "t-held":
                raise _HELD
            return {"ok": True, "actions": [{"type": "extract", "ok": True, "data": {"h1": kwargs["tab_handle"]},
                                             "url": f"https://{kwargs['tab_handle']}.test/", "title": "T"}]}

        with patch.object(main, "browser_act", side_effect=fake_act), \
                patch.object(main, "browser_open_url", side_effect=AssertionError("opened")), \
                patch.object(main, "browser_close_tab", side_effect=AssertionError("closed")):
            out = self._call({"tabs": ["t-a", "t-held", "t-b", "t-a"],
                              "actions": [{"type": "extract", "fields": [{"name": "h1", "selector": "h1"}]}]})
        self.assertEqual(["t-a", "t-held", "t-b"], [call["tab_handle"] for call in seen])
        self.assertFalse(any(call.get("allow_foreground") for call in seen))
        self.assertEqual((3, 1, False), (out["tab_count"], out["failed_count"], out["ok"]))
        first, held, last = out["tabs"]
        self.assertEqual(({"h1": "t-a"}, "t-a", False), (first["data"], first["tab_handle"], first["mutation_dispatched"]))
        self.assertEqual(("t-held", "tab_owned_by_other_agent", "another_agent"), (held["tab_handle"], held["error"], held["owner"]))
        self.assertEqual({"h1": "t-b"}, last["data"])

    def test_pdf_and_network_results_reach_the_caller(self) -> None:
        def fake_act(_settings, **kwargs):
            return {"ok": True, "actions": [
                {"type": "pdf", "ok": True, "method": "chrome_print", "path": "/tmp/x.pdf", "artifact_id": "art_1",
                 "sha256": "ab", "size": 10, "url": "https://a.test/", "title": "A"},
                {"type": "network", "ok": True, "requests": [{"seq": 1, "url": "https://a.test/api"}], "next_since": 1},
            ]}

        with patch.object(main, "browser_act", side_effect=fake_act):
            out = self._call({"tabs": ["t-a"], "actions": [{"type": "pdf"}, {"type": "network"}]})
        page = out["tabs"][0]
        self.assertEqual({"path": "/tmp/x.pdf", "artifact_id": "art_1", "sha256": "ab", "size": 10,
                          "method": "chrome_print"}, page["pdf"])
        self.assertEqual(1, page["network"]["next_since"])
        self.assertEqual("https://a.test/api", page["network"]["requests"][0]["url"])

    def test_tabs_refuse_page_changing_actions_and_url_before_anything_runs(self) -> None:
        cases = [
            {"tabs": ["t-a"], "actions": [{"type": "click", "query": "Buy"}]},
            {"tabs": ["t-a"], "url": "https://example.com"},
            {"tabs": ["t-a"], "tab_handle": "t-b"},
            {"tabs": []},
            {"tabs": [f"t-{n}" for n in range(11)]},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with patch.object(main, "browser_act", side_effect=AssertionError("ran")), \
                        patch.object(main, "browser_open_url", side_effect=AssertionError("ran")):
                    with self.assertRaises(ToolError) as ctx:
                        self._call(arguments)
                self.assertNotIn("ran", str(ctx.exception))


class ReadOnlyLeaseTests(unittest.TestCase):
    def _lease_kwargs(self, actions):
        seen = {}

        @contextmanager
        def fake_lease(browser, handle, window_index, tab_index, **kwargs):
            seen.update(kwargs)
            yield MagicMock(browser="Safari", window_index=1, tab_index=1, tab_handle=handle, lease_generation=0)

        with patch.object(agent, "_tab_lease", side_effect=fake_lease), \
                patch.object(agent, "_ensure_visual_companion"), \
                patch.object(agent, "_require_stable_handle_for_mutation"), \
                patch.object(agent, "_browser_act_locked", return_value={"ok": True, "actions": []}):
            agent._browser_act_impl(MagicMock(), "Safari", actions, tab_handle="t-a")
        return seen

    def test_a_read_only_batch_leases_like_observe_and_a_click_like_a_mutation(self) -> None:
        self.assertEqual({"allow_rebind": True, "mutation": False},
                         self._lease_kwargs([{"type": "read"}, {"type": "network"}, {"type": "pdf"}]))
        self.assertEqual({"allow_rebind": False, "mutation": True},
                         self._lease_kwargs([{"type": "read"}, {"type": "click", "query": "Next"}]))

    def test_another_agents_lease_is_never_taken_by_a_read(self) -> None:
        row = {"tab_handle": "t-held", "url": "https://a.test/"}
        browser_tabs._LOGICAL_LEASES.clear()
        self.addCleanup(browser_tabs._LOGICAL_LEASES.clear)
        with patch.object(browser_tabs, "_logical_owner", return_value=("agent:a1", "a1", None)):
            browser_tabs._claim_logical_lease(row, allow_rebind=True)
        with patch.object(browser_tabs, "_logical_owner", return_value=("agent:b2", "b2", None)):
            with self.assertRaises(HTTPException) as ctx:
                browser_tabs._claim_logical_lease(row, allow_rebind=True)
        self.assertEqual("tab_owned_by_other_agent", ctx.exception.detail["error"])
        self.assertEqual("agent:a1", browser_tabs._LOGICAL_LEASES["t-held"]["owner"])


if __name__ == "__main__":
    unittest.main()
