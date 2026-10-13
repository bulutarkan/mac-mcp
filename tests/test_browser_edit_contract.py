"""#176: agents see the destructive edit semantics of type before they act, and partial edits fail closed."""
from __future__ import annotations

import unittest

from mcp_server.browser_schemas import ACTION_ITEM_SCHEMA, validate_action
from mcp_server.main import BROWSER_ACT_DESCRIPTION, MCP_AGENT_INSTRUCTIONS


class BrowserEditContractTests(unittest.TestCase):
    def test_the_warning_sits_beside_the_arguments_that_lose_text(self) -> None:
        properties = ACTION_ITEM_SCHEMA["properties"]
        self.assertEqual("boolean", properties["clear"]["type"])
        self.assertIn("removes ALL existing text, even when only part is selected", properties["clear"]["description"])

    def test_descriptions_state_replace_all_append_and_no_partial_edits(self) -> None:
        for text in (BROWSER_ACT_DESCRIPTION, MCP_AGENT_INSTRUCTIONS):
            self.assertIn("replaces ALL", text)
            self.assertIn("clear:false appends", text)
            self.assertIn("partial edits are refused", text)

    def test_a_partial_edit_request_is_refused_not_turned_into_replace_all(self) -> None:
        for extra in ({"selection": "brave"}, {"replace": "brave"}, {"range": [6, 11]}, {"selection_start": 6}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "partial edits are not supported"):
                validate_action(0, {"type": "type", "element_id": "e1", "text": "new", **extra})

    def test_clear_must_be_a_boolean(self) -> None:
        # A string such as "selection" is truthy in the page script and would clear the whole field.
        with self.assertRaisesRegex(ValueError, "clear must be true or false"):
            validate_action(0, {"type": "type", "element_id": "e1", "text": "new", "clear": "selection"})

    def test_ordinary_type_actions_still_pass(self) -> None:
        for action in ({"type": "type", "element_id": "e1", "text": "Hello"},
                       {"type": "type", "query": "Reply", "text": " more", "clear": False},
                       {"type": "paste", "element_id": "e2", "text": "x", "clear": True},
                       {"type": "click", "query": "Next", "position": "top"}):
            with self.subTest(action=action):
                self.assertEqual(action, validate_action(0, action))


if __name__ == "__main__":
    unittest.main()
