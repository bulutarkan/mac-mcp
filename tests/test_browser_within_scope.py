from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mcp_server import tools_browser_agent as agent
from mcp_server.tools_browser_agent import _browser_act_locked, _find_candidates_js


def _found(status="ok", matches=None):
    matches = matches or []
    return {
        "ok": True, "within": {"status": status, "levels": 6, "anchor_count": 1 if status == "ok" else 2,
                               "anchors": ["comment text"]},
        "best_match": matches[0] if matches else None, "matches": matches,
    }


class WithinScopeJsTests(unittest.TestCase):
    def test_scoped_search_collects_all_matches_and_reports_scope(self) -> None:
        js = _find_candidates_js("reply", "link", None, 12, within="do you let users teach it", within_levels=6)
        self.assertIn("withinLevels=6", js)
        self.assertIn("anchor_ambiguous", js)
        self.assertIn("within:withinInfo", js)
        self.assertIn(f"out.length<{agent._WITHIN_SCAN_LIMIT}", js)
        plain = _find_candidates_js("reply", "link", None, 12)
        self.assertIn("var withinInfo=null;", plain)
        self.assertNotIn("withinRaw", plain)
        self.assertIn("out.length<12", plain)

    def test_levels_are_bounded(self) -> None:
        self.assertIn(f"withinLevels={agent._WITHIN_MAX_LEVELS}", _find_candidates_js("x", None, None, within="a", within_levels=999))


class WithinGuidanceTests(unittest.TestCase):
    def test_agents_are_told_to_use_one_scoped_call(self) -> None:
        from mcp_server.main import BROWSER_ACT_DESCRIPTION, MCP_AGENT_INSTRUCTIONS
        from mcp_server.tool_summaries import CORE_TOOL_SUMMARIES

        for text in (MCP_AGENT_INSTRUCTIONS, BROWSER_ACT_DESCRIPTION):
            self.assertIn("within=", text)
            self.assertIn("role=button", text)
            self.assertIn("role=textbox", text)
        self.assertIn("do not browser_find each control", MCP_AGENT_INSTRUCTIONS)
        self.assertIn("pass the tab_handle from browser_list_tabs", MCP_AGENT_INSTRUCTIONS)
        self.assertIn("never as a step before acting", MCP_AGENT_INSTRUCTIONS)
        self.assertIn("no find first", CORE_TOOL_SUMMARIES["browser_act"])
        self.assertIn("Not needed before acting", CORE_TOOL_SUMMARIES["browser_find"])
        import inspect

        from mcp_server import main as main_module
        source = inspect.getsource(main_module.create_app)
        self.assertNotIn("use best_match with browser_act", source)
        self.assertIn("To act, do not find first", source)
        self.assertIn("within", CORE_TOOL_SUMMARIES["browser_act"])
        self.assertIn("within=", CORE_TOOL_SUMMARIES["browser_find"])


class WithinResolutionTests(unittest.TestCase):
    def _act(self, actions, find_side_effect):
        verified = []

        def fake_verified(_settings, _browser, action, *_args, **_kwargs):
            verified.append(action["element_id"])
            return {"ok": True, "type": action["type"], "element_id": action["element_id"], "_js_calls": 1}

        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
             patch.object(agent, "browser_find", side_effect=find_side_effect) as find, \
             patch.object(agent, "_verified_dom_action", side_effect=fake_verified), \
             patch.object(agent, "_run_json_js", return_value={"ok": True, "url": "u", "title": "t", "dom_revision": 1}), \
             patch.object(agent.time, "sleep"):
            result = _browser_act_locked(MagicMock(), "Safari", actions, observation_id=None,
                                         window_index=1, tab_index=1, tab_handle="tab", return_state="none")
        return result, find, verified

    def test_ambiguous_anchor_fails_closed_without_waiting(self) -> None:
        result, find, verified = self._act(
            [{"type": "click", "query": "reply", "role": "link", "within": "davidjones145"}],
            [_found("anchor_ambiguous")],
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["mutation_dispatched"])
        self.assertEqual("WITHIN_ANCHOR_AMBIGUOUS", result["actions"][0]["reason_code"])
        self.assertEqual(1, find.call_count)
        self.assertEqual([], verified)
        self.assertEqual("davidjones145", find.call_args.kwargs["within"])

    def test_nearest_match_wins_and_late_targets_are_polled_in_scope(self) -> None:
        near = {"element_id": "e_near", "text": "reply", "role": "link", "confidence": 1.0, "within_up": 4, "within_down": 3}
        far = {"element_id": "e_far", "text": "reply", "role": "link", "confidence": 1.0, "within_up": 6, "within_down": 3}
        box = {"element_id": "e_box", "text": "", "role": "textbox", "confidence": 0.8, "within_up": 5, "within_down": 4}
        result, find, verified = self._act(
            [
                {"type": "click", "query": "reply", "role": "link", "within": "teach it"},
                {"type": "type", "role": "textbox", "within": "teach it", "text": "hello"},
            ],
            [_found("ok", [near, far]), _found("ok", []), _found("ok", []), _found("ok", [box])],
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["mutation_dispatched"])
        self.assertEqual(["e_near", "e_box"], verified)
        self.assertEqual(4, find.call_count)
        self.assertTrue(all(call.kwargs.get("within") == "teach it" for call in find.call_args_list))
        self.assertTrue(all("wait_timeout_s" not in call.kwargs for call in find.call_args_list))

    def test_action_key_is_accepted_as_the_type(self) -> None:
        # The exact shape a delegated agent sent live: "action" instead of "type".
        near = {"element_id": "e_near", "text": "reply", "role": "link", "confidence": 1.0, "within_up": 4, "within_down": 3}
        box = {"element_id": "e_box", "text": "", "role": "textbox", "confidence": 0.8, "within_up": 5, "within_down": 4}
        result, _find, verified = self._act(
            [
                {"action": "click", "text_match": "reply", "within": "One more thing worth adding"},
                {"action": "type", "role": "textbox", "text": "Draft only, please ignore.",
                 "within": "One more thing worth adding"},
            ],
            [_found("ok", [near]), _found("ok", []), _found("ok", [box])],
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(["e_near", "e_box"], verified)
        self.assertEqual(["click", "type"], [item["type"] for item in result["actions"]])


class BrowserActNotExecutedTests(unittest.TestCase):
    def test_unknown_action_type_is_refused_before_anything_runs(self) -> None:
        from fastapi import HTTPException

        from mcp_server.workflow_checkpoints import exception_not_executed

        for actions in (
            [{"type": "click", "query": "Reply"}, {"type": "hover", "query": "Save"}],
            [{"query": "Reply"}],
        ):
            with self.subTest(actions=actions), \
                 patch.object(agent, "_tab_lease") as lease, \
                 patch.object(agent, "_run_json_js") as run_js:
                with self.assertRaises(HTTPException) as ctx:
                    agent.browser_act(MagicMock(), "Safari", actions, tab_handle="tab")
                self.assertEqual(400, ctx.exception.status_code)
                self.assertIn("type", str(ctx.exception.detail))
                self.assertTrue(exception_not_executed(ctx.exception))
                lease.assert_not_called()
                run_js.assert_not_called()

    def test_refusal_before_actions_is_tagged_not_executed(self) -> None:
        from fastapi import HTTPException

        from mcp_server.workflow_checkpoints import exception_not_executed

        refusal = HTTPException(409, {"error": "stable_tab_handle_required"})
        with patch.object(agent, "_require_stable_handle_for_mutation", side_effect=refusal):
            with self.assertRaises(HTTPException) as ctx:
                agent.browser_act(MagicMock(), "Safari", [{"type": "click", "query": "Reply"}])
        self.assertTrue(exception_not_executed(ctx.exception))

    def test_failure_after_actions_started_is_not_tagged(self) -> None:
        from contextlib import nullcontext
        from types import SimpleNamespace

        from fastapi import HTTPException

        from mcp_server.workflow_checkpoints import exception_not_executed

        target = SimpleNamespace(browser="Safari", window_index=1, tab_index=1, tab_handle="tab", lease_generation=1)
        with patch.object(agent, "_require_stable_handle_for_mutation"), \
             patch.object(agent, "_ensure_visual_companion"), \
             patch.object(agent, "_tab_lease", return_value=nullcontext(target)), \
             patch.object(agent, "_browser_act_locked", side_effect=HTTPException(409, "tab closed mid-batch")):
            with self.assertRaises(HTTPException) as ctx:
                agent.browser_act(MagicMock(), "Safari", [{"type": "click", "query": "Reply"}], tab_handle="tab")
        self.assertFalse(exception_not_executed(ctx.exception))


class ObserveScopeAliasTests(unittest.TestCase):
    def test_page_scope_reads_content_and_unknown_scope_still_fails(self) -> None:
        from fastapi import HTTPException

        class Reached(Exception):
            pass

        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
             patch.object(agent, "_run_json_js", side_effect=Reached) as run_js:
            for scope in ("page", "Full", "all"):
                with self.subTest(scope=scope), self.assertRaises(Reached):
                    agent._browser_observe_locked(MagicMock(), "Safari", tab_handle="tab", scope=scope)
            self.assertEqual(3, run_js.call_count)
            with self.assertRaises(HTTPException) as ctx:
                agent._browser_observe_locked(MagicMock(), "Safari", tab_handle="tab", scope="everything")
        self.assertEqual(400, ctx.exception.status_code)


if __name__ == "__main__":
    unittest.main()
