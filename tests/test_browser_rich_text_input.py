"""#158: rich-text editors receive typed text through their own input path, and a typed
value is reported as applied only when the field (and so the editor model) holds it."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import tools_browser_agent as agent
from mcp_server.security import load_settings

SETTINGS = load_settings()
TEXT = "Thanks for reading!"


def _run(first_result, *, follow_ups=(), fused=None):
    """Run one verified type action; first_result is what the in-page type step reports."""
    calls = []
    replies = iter(follow_ups)

    def run_json(settings, browser, js, *args, **kwargs):
        calls.append(js)
        if len(calls) == 1 and fused is None:
            return {"ok": True, "actions": [dict(first_result)]}
        return next(replies)

    fused_reply = {"js_calls": 1, "out": {"ok": True, "actions": [dict(first_result)]}, "readiness": {"ready": True}} \
        if fused else {"js_calls": 0}
    with patch.object(agent, "_try_fused_action", return_value=fused_reply), \
         patch.object(agent, "_wait_for_element_readiness", return_value={"ready": True}), \
         patch.object(agent, "cancellable_sleep"), \
         patch.object(agent, "_run_json_js", side_effect=run_json):
        result = agent._verified_dom_action(
            SETTINGS, "Safari", {"type": "type", "element_id": "e1", "text": TEXT}, None, 1, 1, "tab-1",
        )
    return result, calls


class PageScriptTests(unittest.TestCase):
    def test_contenteditable_gets_an_editor_paste_before_any_dom_write(self) -> None:
        source = agent._bootstrap_functions_source()
        set_text = source[source.index("function __mcpSetText"):]
        end = set_text.find("\nfunction ", 10)
        set_text = set_text if end < 0 else set_text[:end]
        paste, execute, dom = (set_text.index("__mcpPasteInsert(el,value)"),
                               set_text.index("__mcpExecInsert(el,value,clearFirst)"),
                               set_text.index("el.textContent=value"))
        self.assertLess(paste, execute)
        self.assertLess(execute, dom)
        # The untrusted beforeinput that made Lexical insert twice is only sent to form fields now.
        self.assertIn("if(!rich){var before=__mcpInputEvent(el,'beforeinput'", set_text)
        select = source[source.index("function __mcpSelectEditable"):]
        self.assertIn("'selectionchange'", select[:select.index("\nfunction ")])

    def test_every_new_helper_is_installed_with_the_page_api(self) -> None:
        names = agent._top_level_function_names(agent._bootstrap_functions_source())
        for name in ("__mcpSetText", "__mcpSelectEditable", "__mcpExecInsert", "__mcpPasteInsert",
                     "__mcpEditableText", "__mcpTextMatches"):
            self.assertIn(name, names)

    def test_type_step_reports_how_text_went_in_and_never_trusts_dom_only_text(self) -> None:
        js = agent._batch_js([{"type": "type", "element_id": "e1", "text": TEXT}], None)
        self.assertIn("input_method:method.replace(/_async$/,'')", js)
        self.assertIn("verification==='dom_text_only'", js)
        self.assertIn("typed.editor_state_unverified=true", js)
        self.assertIn("a._focus_settled===true", js)


class VerificationTests(unittest.TestCase):
    def test_text_an_editor_renders_a_moment_later_is_confirmed(self) -> None:
        pending = {"ok": True, "type": "type", "verification": "editor_pending", "input_method": "paste_event",
                   "_verify_text": TEXT, "effect_observed": False}
        result, calls = _run(pending, follow_ups=[{"text": ""}, {"text": TEXT}])
        self.assertTrue(result["ok"])
        self.assertEqual("value_applied_async", result["verification"])
        self.assertTrue(result["effect_observed"])
        self.assertNotIn("_verify_text", result)
        self.assertEqual(3, len(calls))  # type + two read-only checks

    def test_text_that_never_reaches_the_field_fails_instead_of_passing(self) -> None:
        pending = {"ok": True, "type": "type", "verification": "editor_pending", "_verify_text": TEXT}
        with patch.object(agent, "_TYPE_VERIFY_TIMEOUT_S", 0.0001):
            result, _ = _run(pending, follow_ups=[{"text": ""}] * 50)
        self.assertFalse(result["ok"])
        self.assertEqual("editor_did_not_accept", result["verification"])
        self.assertEqual("INPUT_NOT_APPLIED", result["reason_code"])
        self.assertTrue(result["observe_again"])

    def test_multi_paragraph_text_matches_without_its_line_breaks(self) -> None:
        pending = {"ok": True, "type": "type", "verification": "editor_pending", "_verify_text": "Line one\nLine two"}
        result, _ = _run(pending, follow_ups=[{"text": "Line oneLine two"}])
        self.assertEqual("value_applied_async", result["verification"])

    def test_focus_settling_types_once_more_with_the_settled_flag(self) -> None:
        settling = {"ok": True, "type": "type", "verification": "focus_settling", "effect_observed": False}
        done = {"ok": True, "actions": [{"ok": True, "type": "type", "verification": "value_applied",
                                         "input_method": "paste_event", "effect_observed": True}]}
        result, calls = _run(settling, follow_ups=[done], fused=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["focus_settled"])
        self.assertEqual("value_applied", result["verification"])
        self.assertEqual(1, len(calls))
        self.assertIn('"_focus_settled":true', calls[0].replace(" ", ""))

    def test_input_and_textarea_keep_the_value_setter_path(self) -> None:
        source = agent._bootstrap_functions_source()
        self.assertIn("if(editable){if(clearFirst!==false)__mcpNativeValueSetter(el,'');__mcpNativeValueSetter(el,value);}", source)


if __name__ == "__main__":
    unittest.main()
