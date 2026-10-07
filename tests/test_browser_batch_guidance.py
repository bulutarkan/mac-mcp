from __future__ import annotations

import inspect
import unittest
from unittest.mock import MagicMock, patch

from mcp_server.main import (
    BROWSER_ACT_DESCRIPTION,
    BROWSER_OBSERVE_DESCRIPTION,
    MCP_AGENT_INSTRUCTIONS,
    create_app,
)
from mcp_server.tools_browser_agent import _browser_act_locked, browser_act


EXACT_RULE = (
    "For form filling and repetitive browser interactions, batch independent actions; "
    "never field-by-field unless dependencies require it."
)


class BrowserBatchGuidanceTests(unittest.TestCase):
    def test_agent_instruction_contains_exact_batch_first_rule_and_safety_exceptions(self) -> None:
        self.assertIn(EXACT_RULE, MCP_AGENT_INSTRUCTIONS)
        for phrase in (
            "materially changes later controls",
            "stale-target",
            "human-takeover",
            "consequential step",
            "separate verification boundary",
        ):
            self.assertIn(phrase, MCP_AGENT_INSTRUCTIONS)

    def test_browser_act_description_makes_batch_first_workflow_explicit(self) -> None:
        description = BROWSER_ACT_DESCRIPTION
        self.assertTrue(description.startswith("BATCH-FIRST:"))
        self.assertIn(
            "one browser_observe -> one batched browser_act -> one browser_observe verification",
            description,
        )
        self.assertIn("instead of one call per field", description)
        self.assertIn("type/select/click/scroll", description)
        self.assertIn("custom dropdowns can use select", description)
        for target in ("query", "role", "text_match"):
            self.assertIn(target, description)
        self.assertIn("element IDs are not always required", description)
        self.assertIn("materially changes later controls", description)
        self.assertIn("consequential step needs separate verification", description)

    def test_browser_observe_description_points_to_one_batched_followup(self) -> None:
        description = BROWSER_OBSERVE_DESCRIPTION
        self.assertIn("BATCH-FIRST HINT", description)
        self.assertIn("one browser_act", description)
        self.assertIn("instead of repeated field-by-field observe/action calls", description)
        self.assertIn("Re-observe between action groups only when", description)
        self.assertIn("materially changes later controls", description)

    def test_fastmcp_registration_uses_batch_guidance_constants(self) -> None:
        source = inspect.getsource(create_app)
        self.assertIn("instructions=MCP_AGENT_INSTRUCTIONS", source)
        self.assertIn('name="browser_observe"', source)
        self.assertIn("description=BROWSER_OBSERVE_DESCRIPTION", source)
        self.assertIn('name="browser_act"', source)
        self.assertIn("description=BROWSER_ACT_DESCRIPTION", source)

    def test_browser_act_api_still_accepts_action_lists_without_new_form_tool(self) -> None:
        signature = inspect.signature(browser_act)
        self.assertIn("actions", signature.parameters)
        self.assertIsNone(signature.parameters["observation_id"].default)
        self.assertEqual("compact", signature.parameters["return_state"].default)

        representative_form_batch = [
            {"type": "type", "query": "First name", "role": "textbox", "text": "Ada"},
            {"type": "type", "text_match": "Last name", "role": "textbox", "text": "Lovelace"},
            {"type": "select", "query": "Country", "role": "combobox", "option": "Türkiye"},
            {"type": "scroll", "dy": 500},
            {"type": "click", "query": "Continue", "role": "button"},
        ]
        self.assertEqual(5, len(representative_form_batch))
        self.assertTrue(all("type" in action for action in representative_form_batch))

    def test_one_browser_act_transaction_handles_independent_form_batch(self) -> None:
        actions = [
            {"type": "type", "query": "First name", "role": "textbox", "text": "Ada"},
            {"type": "type", "text_match": "Last name", "role": "textbox", "text": "Lovelace"},
            {"type": "select", "query": "Country", "role": "combobox", "option": "Türkiye"},
            {"type": "scroll", "dy": 500},
            {"type": "click", "query": "Continue", "role": "button"},
        ]
        found = iter(
            [
                {"element_id": "e1", "text": "First name", "role": "textbox", "tag": "input", "confidence": 0.99},
                {"element_id": "e2", "text": "Last name", "role": "textbox", "tag": "input", "confidence": 0.99},
                {"element_id": "e3", "text": "Country", "role": "combobox", "tag": "button", "confidence": 0.98},
                {"element_id": "e4", "text": "Continue", "role": "button", "tag": "button", "confidence": 0.99},
            ]
        )
        compact = {"ok": True, "url": "https://example.test/form", "title": "Form", "dom_revision": 5}

        def fake_find(*args, **kwargs):
            return {"ok": True, "best_match": next(found)}

        def fake_verified(*args, **kwargs):
            action = args[2]
            return {
                "ok": True,
                "type": action["type"],
                "element_id": action["element_id"],
                "_js_calls": 1,
                "_compact_state": compact,
            }

        def fake_select(*args, **kwargs):
            action = args[2]
            return {
                "ok": True,
                "type": "select",
                "element_id": action["element_id"],
                "_js_calls": 1,
            }

        with patch(
            "mcp_server.tools_browser_agent._resolve_tab_target",
            return_value=(1, 1),
        ), patch(
            "mcp_server.tools_browser_agent.browser_find",
            side_effect=fake_find,
        ) as find, patch(
            "mcp_server.tools_browser_agent._verified_dom_action",
            side_effect=fake_verified,
        ), patch(
            "mcp_server.tools_browser_agent._select_action",
            side_effect=fake_select,
        ), patch(
            "mcp_server.tools_browser_agent._run_json_js",
            return_value={
                "ok": True,
                "actions": [{"ok": True, "type": "scroll"}],
                "state": compact,
            },
        ):
            result = _browser_act_locked(
                MagicMock(),
                "Safari",
                actions,
                observation_id="obs-form",
                window_index=1,
                tab_index=1,
                tab_handle="tab-form",
                return_state="compact",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(5, result["action_count"])
        self.assertEqual(
            ["type", "type", "select", "scroll", "click"],
            [item["type"] for item in result["actions"]],
        )
        self.assertEqual(4, find.call_count)
        self.assertEqual("First name", find.call_args_list[0].kwargs["query"])
        self.assertEqual("textbox", find.call_args_list[0].kwargs["role"])
        self.assertEqual("Last name", find.call_args_list[1].kwargs["text"])
        self.assertEqual("combobox", find.call_args_list[2].kwargs["role"])
        self.assertEqual("button", find.call_args_list[3].kwargs["role"])

    def test_guidance_allows_split_when_selection_reveals_new_control(self) -> None:
        first_group = [
            {"type": "select", "query": "Account type", "role": "combobox", "option": "Business"}
        ]
        dependent_group = [
            {"type": "type", "query": "Company registration number", "role": "textbox", "text": "123"}
        ]
        self.assertEqual("select", first_group[0]["type"])
        self.assertEqual("type", dependent_group[0]["type"])
        self.assertIn("materially changes later controls", BROWSER_ACT_DESCRIPTION)
        self.assertIn("Re-observe between action groups only when", BROWSER_OBSERVE_DESCRIPTION)


if __name__ == "__main__":
    unittest.main()
