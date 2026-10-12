"""{type:'fill', values:{label: value}}: a whole form in one browser_act action.

An agent filled a 9-field form one field per call until told to batch, although the
instructions already said "batch, never field-by-field". The post action showed that a
ready one-shot action changes behaviour where instructions did not, so forms get one too,
and browser_observe hands back a ready fill_template when it sees form fields.
"""
from __future__ import annotations

import itertools
import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server import browser_schemas, data_guard
from mcp_server import tools_browser_agent as agent
from mcp_server.tools_browser_agent import _browser_act_locked, _fill_kind, _fill_template, _fill_truthy
from mcp_server.workflow_checkpoints import exception_not_executed


def field(element_id, label, **extra):
    return {"element_id": element_id, "role": "textbox", "tag": "input", "association_text": label,
            "actionable": True, "confidence": 0.98, **extra}


class FillValidationTests(unittest.TestCase):
    def test_bad_values_are_refused_before_anything_runs(self) -> None:
        for values in (None, {}, [], {"": "x"}, {"Address": {"street": "x"}}, {f"f{i}": "x" for i in range(31)}):
            with self.subTest(values=values), \
                 patch.object(agent, "_tab_lease") as lease, patch.object(agent, "_run_json_js") as run_js:
                with self.assertRaises(HTTPException) as ctx:
                    agent.browser_act(MagicMock(), "Safari", [{"type": "fill", "values": values}], tab_handle="tab")
                self.assertEqual(400, ctx.exception.status_code)
                self.assertTrue(exception_not_executed(ctx.exception))
                lease.assert_not_called()
                run_js.assert_not_called()
            with self.subTest(schema=values), self.assertRaises(ValueError):
                browser_schemas.validate_action(0, {"type": "fill", "values": values})
        self.assertIn("fill", browser_schemas.ACTION_ITEM_SCHEMA["properties"]["type"]["enum"])

    def test_values_are_redacted_and_scanned_but_labels_stay(self) -> None:
        args = {"actions": [{"type": "fill", "values": {"Email": "me@example.com", "Terms": True}}]}
        clean = data_guard.sanitize_tool_arguments("browser_act", args)
        self.assertEqual({"Email": "[BROWSER INPUT REDACTED]", "Terms": "[BROWSER INPUT REDACTED]"},
                         clean["actions"][0]["values"])
        secret = "Zq8vL2mX9pR4tY7wK3nB6cF1hJ5sD0gA"
        known = data_guard.secret_fingerprints(secret, include_entropy=True, include_whole=True)
        leaking = {"actions": [{"type": "fill", "values": {"Notes": f"key {secret}"}}]}
        self.assertTrue(data_guard.scan_sensitive_egress("browser_act", leaking, known).sensitive)
        self.assertFalse(data_guard.scan_sensitive_egress("browser_act", args, known).sensitive)


class FillKindAndTemplateTests(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertEqual("text", _fill_kind({"tag": "input", "role": "textbox"}))
        self.assertEqual("text", _fill_kind({"tag": "input", "role": "combobox"}))  # autocomplete input: typed
        self.assertEqual("select", _fill_kind({"tag": "select", "role": "combobox"}))
        self.assertEqual("select", _fill_kind({"tag": "summary", "role": "listbox"}))
        self.assertEqual("checkbox", _fill_kind({"tag": "input", "input_type": "checkbox"}))
        self.assertEqual("radio", _fill_kind({"tag": "div", "role": "radio"}))
        self.assertTrue(all(_fill_truthy(v) for v in (True, "yes", "Evet", "1", "on")))
        self.assertFalse(any(_fill_truthy(v) for v in (False, "no", "0", "", "false")))

    def test_template_lists_the_empty_fields_of_a_form(self) -> None:
        elements = [
            field("e1", "Full name"), field("e2", "Email", input_type="email"),
            field("e3", "Already filled", value="Tarkan"),
            field("e4", "Password", input_type="password"), field("e5", "Search the site"),
            {"element_id": "e6", "tag": "select", "role": "combobox", "association_text": "Country", "actionable": True},
            {"element_id": "e7", "tag": "input", "input_type": "checkbox", "role": "checkbox",
             "association_text": "I accept the terms", "actionable": True},
            {"element_id": "e8", "tag": "button", "role": "button", "text": "Submit", "actionable": True},
        ]
        self.assertEqual({"type": "fill", "values": {"Full name": "", "Email": "", "Country": "", "I accept the terms": False}},
                         _fill_template(elements))
        self.assertIsNone(_fill_template([field("e1", "Comment")]))
        out = json.loads(agent._format_observation({"ok": True, "elements": elements}, None))
        self.assertEqual("fill", out["fill_template"]["type"])


class FillFlowTests(unittest.TestCase):
    def _fill(self, values, finds):
        calls = []

        def find(_settings, _browser, **kwargs):
            calls.append(("find", kwargs.get("query"), kwargs.get("wait_timeout_s")))
            return finds[kwargs.get("query")]

        def verified(_settings, _browser, item, *_args, **_kwargs):
            calls.append((item["type"], item["element_id"], item.get("text")))
            return {"ok": True, "type": item["type"], "element_id": item["element_id"], "_js_calls": 1}

        def select(_settings, _browser, item, *_args, **_kwargs):
            calls.append(("select", item["element_id"], item.get("option")))
            return {"ok": True, "type": "select", "_js_calls": 1}

        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
             patch.object(agent, "browser_find", side_effect=find), \
             patch.object(agent, "_verified_dom_action", side_effect=verified), \
             patch.object(agent, "_select_action", side_effect=select), \
             patch.object(agent, "_run_json_js", return_value={"ok": True, "url": "u", "title": "t", "dom_revision": 1}), \
             patch.object(agent.time, "sleep"), \
             patch.object(agent.time, "monotonic", side_effect=lambda clock=itertools.count(0.0, 0.5): next(clock)):
            result = _browser_act_locked(MagicMock(), "Safari", [{"type": "fill", "values": values}], observation_id=None,
                                         window_index=1, tab_index=1, tab_handle="tab", return_state="none")
        return result, calls

    @staticmethod
    def _found(*matches):
        return {"ok": True, "best_match": matches[0] if matches else None, "matches": list(matches)}

    def test_one_action_fills_text_selects_and_checkboxes(self) -> None:
        label_el = {"element_id": "e_lab", "tag": "label", "role": "", "text": "Full name", "confidence": 1.0,
                    "associated_control": {"element_id": "e_name", "tag": "input", "role": "textbox"}}
        result, calls = self._fill(
            {"Full name": "Tarkan", "Country": "Turkey", "Newsletter": True, "I accept the terms": "yes"},
            {
                "Full name": self._found(label_el),
                "Country": self._found({"element_id": "e_cty", "tag": "select", "role": "combobox", "confidence": 1.0,
                                        "association_text": "Country"}),
                "Newsletter": self._found({"element_id": "e_news", "tag": "input", "input_type": "checkbox",
                                           "role": "checkbox", "checked": True, "confidence": 1.0, "text": "Newsletter"}),
                "I accept the terms": self._found({"element_id": "e_terms", "tag": "input", "input_type": "checkbox",
                                                   "role": "checkbox", "confidence": 1.0,
                                                   "association_text": "I accept the terms"}),
            },
        )
        self.assertTrue(result["ok"], result)
        fill = result["actions"][0]
        self.assertEqual(3, fill["filled"])
        self.assertTrue(next(row for row in fill["fields"] if row["field"] == "Newsletter")["unchanged"])
        acted = [call for call in calls if call[0] != "find"]
        self.assertEqual([("type", "e_name", "Tarkan"), ("select", "e_cty", "Turkey"), ("click", "e_terms", None)], acted)
        self.assertTrue(result["mutation_dispatched"])

    def test_missing_field_is_reported_and_the_others_are_still_filled(self) -> None:
        result, calls = self._fill(
            {"Email": "me@example.com", "Fax": "123", "City": "Istanbul"},
            {"Email": self._found(field("e_mail", "Email")), "Fax": self._found(),
             "City": self._found(field("e_city", "City"))},
        )
        self.assertFalse(result["ok"])
        fill = result["actions"][0]
        self.assertEqual(("fill_incomplete", ["Fax"], 2), (fill["error"], fill["failed_fields"], fill["filled"]))
        # Fax was never typed into and the others were verified: the outcome is known.
        self.assertIs(False, result["outcome_unknown"])
        self.assertEqual([("type", "e_mail", "me@example.com"), ("type", "e_city", "Istanbul")],
                         [call for call in calls if call[0] != "find"])

    def test_field_revealed_by_an_earlier_choice_is_awaited(self) -> None:
        result, calls = self._fill(
            {"Country": "United States", "State": "California"},
            {"Country": self._found({"element_id": "e_cty", "tag": "select", "role": "combobox", "confidence": 1.0,
                                     "association_text": "Country"}),
             "State": self._found()},
        )
        self.assertFalse(result["ok"])
        finds = [call for call in calls if call[0] == "find" and call[1] == "State"]
        # Not found right after a change: the lookup polls while the field renders.
        self.assertGreater(len(finds), 1, finds)

    def test_option_text_never_stands_in_for_a_field_label(self) -> None:
        # Live bug: State (not rendered yet) matched the Country select that lists "United States".
        country = {"element_id": "e_cty", "tag": "select", "role": "combobox", "association_text": "Country",
                   "options": [{"text": "United States"}], "confidence": 0.9}
        caption = {"element_id": "e_cap", "tag": "span", "role": "", "text": "Plan", "confidence": 1.0}
        self.assertIsNone(agent._pick_fill_control([country], "State"))
        self.assertIsNone(agent._pick_fill_control([caption], "Plan"))  # a caption with no control is not a field
        plan = {"element_id": "e_plan", "tag": "button", "role": "combobox", "aria_label": "Plan", "text": "Choose a plan"}
        self.assertEqual("e_plan", agent._pick_fill_control([caption, plan], "Plan")["element_id"])


class FillGuidanceTests(unittest.TestCase):
    def test_agents_are_told_about_fill(self) -> None:
        from mcp_server.main import BROWSER_ACT_DESCRIPTION, BROWSER_OBSERVE_DESCRIPTION, MCP_AGENT_INSTRUCTIONS
        from mcp_server.tool_summaries import CORE_TOOL_SUMMARIES

        self.assertIn("{type:'fill', values:{'<field label>': '<value>', ...}}", MCP_AGENT_INSTRUCTIONS)
        self.assertIn("failed_fields", BROWSER_ACT_DESCRIPTION)
        self.assertIn("fill_template", BROWSER_OBSERVE_DESCRIPTION)
        self.assertIn("{type:'fill',values:{label:value}}", CORE_TOOL_SUMMARIES["browser_act"])


if __name__ == "__main__":
    unittest.main()
