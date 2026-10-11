"""#158: rich-text editors receive typed text through their own input path, and a typed
value is reported as applied only when the field (and so the editor model) holds it."""
from __future__ import annotations

import json
import shutil
import subprocess
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

    def test_settling_pass_selects_backward_so_the_second_select_all_reaches_draft(self) -> None:
        # React drops a select event identical to the previous one; the busy Draft editor ignores the
        # first, so a forward select-all in both passes left Draft pasting at its old caret.
        source = agent._bootstrap_functions_source()
        set_text = source[source.index("function __mcpSetText"):]
        settling = set_text[:set_text.index("s.lastInputMethod='focus_settling'")]
        self.assertIn("__mcpSelectEditable(el,clearFirst,true)", settling[-200:])
        select = source[source.index("function __mcpSelectEditable"):]
        select = select[:select.index("\nfunction ")]
        self.assertIn("sel.setBaseAndExtent(range.endContainer,range.endOffset,range.startContainer,range.startOffset)", select)
        self.assertIn("backward&&clearFirst!==false", select)

    def test_every_unfocused_rich_field_settles_first(self) -> None:
        # Slate reads a selection a task later and Draft ignores one made while it takes focus, so an
        # unfocused editor gets its caret before the paste also when it is empty or text is appended.
        source = agent._bootstrap_functions_source()
        set_text = source[source.index("function __mcpSetText"):]
        self.assertIn("if(!wasActive&&!focusSettled){try{__mcpSelectEditable(el,clearFirst,true);}", set_text)

    def test_type_step_does_not_call_an_unannounced_page_paste_accepted(self) -> None:
        js = agent._batch_js([{"type": "type", "element_id": "e1", "text": TEXT}], None)
        self.assertIn("unconfirmed?'editor_model_unconfirmed':'value_applied'", js)
        self.assertIn("if(pending&&unconfirmed)typed._model_unconfirmed=true", js)
        self.assertIn("verification==='editor_model_unconfirmed'){typed.editor_state_unverified=true;typed.observe_again=true;}", js)


_PASTE_HARNESS = r"""
class FakeEl extends EventTarget {
  constructor(editorClass) { super(); this.editorClass = editorClass; this.text = ''; }
  closest(selector) { return this.editorClass && selector.split(',').includes(this.editorClass) ? this : null; }
}
class DataTransfer { constructor() { this.data = {}; } setData(k, v) { this.data[k] = v; } getData(k) { return this.data[k] || ''; } }
class ClipboardEvent extends Event { constructor(type, init) { super(type, init); this.clipboardData = init.clipboardData; } }
const state = {};
const window = { DataTransfer, ClipboardEvent };
function __mcpOwnerWindow() { return window; }
function __mcpState() { return state; }
__FUNCTIONS__
function run(editorClass, onPaste) {
  const el = new FakeEl(editorClass);
  if (onPaste) el.addEventListener('paste', e => onPaste(e, el));
  const handled = __mcpPasteInsert(el, 'Hello brave world');
  return { handled, unconfirmed: state.lastPasteUnconfirmed, text: el.text };
}
const drawOnly = (e, el) => { e.preventDefault(); el.text = e.clipboardData.getData('text/plain'); };
const announced = (e, el) => { drawOnly(e, el); el.dispatchEvent(new Event('input', { bubbles: true })); };
console.log(JSON.stringify({
  draws_without_telling_framework: run('', drawOnly),
  announces_with_input: run('', announced),
  known_model_editor: run('.ProseMirror', drawOnly),
  lexical_root: run('[data-lexical-editor]', drawOnly),
  not_handled: run('', null),
}));
"""


_SELECT_HARNESS = r"""
const log = [];
class FocusEvent { constructor(type) { this.type = type; } }
class Ev { constructor(type) { this.type = type; } }
function make(hasFocus, active) {
  const el = { dispatchEvent: e => log.push('el:' + e.type) };
  const range = { selectNodeContents() {}, collapse() {}, startContainer: el, startOffset: 0, endContainer: el, endOffset: 1 };
  const sel = { removeAllRanges() {}, addRange() { log.push('range:forward'); }, setBaseAndExtent() { log.push('range:backward'); } };
  el.ownerDocument = { hasFocus: () => hasFocus, activeElement: active ? el : null, createRange: () => range,
                       dispatchEvent: e => log.push('doc:' + e.type) };
  return { el, win: { FocusEvent, Event: Ev, getSelection: () => sel } };
}
let current;
function __mcpOwnerWindow() { return current.win; }
__FUNCTIONS__
const run = (hasFocus, active, clearFirst, backward) => { log.length = 0; current = make(hasFocus, active);
  __mcpSelectEditable(current.el, clearFirst, backward); return log.slice(); };
console.log(JSON.stringify({
  background: run(false, false, true, false),
  focused_field: run(true, true, true, false),
  other_field_focused: run(true, false, true, false),
  settling_replace: run(true, true, true, true),
  settling_append: run(true, true, false, true),
}));
"""


def _extract(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.find("\nfunction ", start + 10)
    return source[start:] if end < 0 else source[start:end]


class PasteAcceptanceTests(unittest.TestCase):
    """#161/T8: a page that cancels the paste and draws the text itself, without telling its
    framework, shows the text while its model (and submit button) stay empty."""

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_paste_counts_as_accepted_only_for_known_editors_or_an_announced_change(self) -> None:
        source = agent._bootstrap_functions_source()
        functions = "\n".join(_extract(source, name) for name in ("__mcpModelEditor", "__mcpPasteInsert"))
        proc = subprocess.run(["node", "-e", _PASTE_HARNESS.replace("__FUNCTIONS__", functions)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual({"handled": True, "unconfirmed": True, "text": "Hello brave world"},
                         got["draws_without_telling_framework"])
        self.assertFalse(got["announces_with_input"]["unconfirmed"])
        self.assertFalse(got["known_model_editor"]["unconfirmed"])
        self.assertFalse(got["lexical_root"]["unconfirmed"])
        self.assertEqual({"handled": False, "unconfirmed": False, "text": ""}, got["not_handled"])


class SelectionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_selection_is_announced_so_react_editors_cannot_drop_it(self) -> None:
        source = agent._bootstrap_functions_source()
        script = _SELECT_HARNESS.replace("__FUNCTIONS__", _extract(source, "__mcpSelectEditable"))
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
        got = json.loads(proc.stdout)
        # Background tab: no real focus events, so focus and focusin are synthesized.
        self.assertEqual(["el:focus", "el:focusin", "range:forward", "doc:selectionchange"], got["background"])
        # Focused page, field already focused: focusin makes React forget the previous selection.
        self.assertEqual(["el:focusin", "range:forward", "doc:selectionchange"], got["focused_field"])
        # Another field holds focus: no synthetic focus (the real el.focus() in __mcpSetText handles it).
        self.assertEqual(["range:forward", "doc:selectionchange"], got["other_field_focused"])
        self.assertEqual(["el:focusin", "range:backward", "doc:selectionchange"], got["settling_replace"])
        # A caret for appending has no direction.
        self.assertEqual(["el:focusin", "range:forward", "doc:selectionchange"], got["settling_append"])


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

    def test_unannounced_page_paste_is_reported_unverified_not_accepted(self) -> None:
        drawn = {"ok": True, "type": "type", "verification": "editor_model_unconfirmed", "input_method": "paste_event",
                 "effect_observed": True, "editor_state_unverified": True, "observe_again": True, "value": TEXT}
        result, _ = _run(drawn)
        self.assertEqual("editor_model_unconfirmed", result["verification"])
        self.assertTrue(result["editor_state_unverified"])
        self.assertTrue(result["observe_again"])
        self.assertNotIn(result["verification"], {"value_applied", "value_applied_async"})

    def test_late_rendered_unannounced_paste_is_not_upgraded_to_accepted(self) -> None:
        pending = {"ok": True, "type": "type", "verification": "editor_pending", "input_method": "paste_event",
                   "_verify_text": TEXT, "_model_unconfirmed": True, "effect_observed": False}
        result, _ = _run(pending, follow_ups=[{"text": TEXT}])
        self.assertEqual("editor_model_unconfirmed", result["verification"])
        self.assertTrue(result["editor_state_unverified"])
        self.assertTrue(result["observe_again"])
        self.assertNotIn("_model_unconfirmed", result)

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
        self.assertIn("if(editable){var keep=clearFirst===false?String(el.value==null?'':el.value):'';"
                      "if(clearFirst!==false)__mcpNativeValueSetter(el,'');__mcpNativeValueSetter(el,keep+value);}", source)


if __name__ == "__main__":
    unittest.main()
