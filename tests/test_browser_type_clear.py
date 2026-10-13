"""#162: browser type with clear=false appends in every text field, as it already did in rich editors."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from mcp_server import browser_schemas, tools_browser
from mcp_server import tools_browser_agent as agent
from mcp_server.security import load_settings


def _extract(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.find("\nfunction ", start + 10)
    return source[start:] if end < 0 else source[start:end]


_FIELD_HARNESS = r"""
__FUNCTIONS__
const field = (tagName, type) => ({ tagName, type });
console.log(JSON.stringify({
  textarea: __mcpAppendable(field('TEXTAREA')),
  text: __mcpAppendable(field('INPUT', 'text')),
  untyped: __mcpAppendable(field('INPUT', '')),
  email: __mcpAppendable(field('INPUT', 'email')),
  search: __mcpAppendable(field('INPUT', 'search')),
  number: __mcpAppendable(field('INPUT', 'number')),
  date: __mcpAppendable(field('INPUT', 'date')),
  select: __mcpAppendable(field('SELECT')),
}));
"""


class AgentTypeTests(unittest.TestCase):
    def test_form_fields_keep_their_value_when_clear_is_false(self) -> None:
        source = agent._bootstrap_functions_source()
        set_text = _extract(source, "__mcpSetText")
        self.assertIn("var keep=clearFirst===false?String(el.value==null?'':el.value):''", set_text)
        self.assertIn("__mcpNativeValueSetter(el,keep+value)", set_text)
        self.assertIn("if(editable&&clearFirst===false&&!__mcpAppendable(el))throw new Error('append_unsupported", set_text)

    def test_verification_expects_the_appended_value_for_form_fields(self) -> None:
        js = agent._batch_js([{"type": "type", "element_id": "e1", "text": "x", "clear": False}], None)
        self.assertIn("priorText=('value' in el)?String(el.value==null?'':el.value):__mcpEditableText(el)", js)
        self.assertIn("expectedText=a.clear===false?priorText+value:value", js)
        self.assertIn("hasValue?actual===expectedText:", js)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_only_text_like_fields_accept_appending(self) -> None:
        script = _FIELD_HARNESS.replace("__FUNCTIONS__", _extract(agent._bootstrap_functions_source(), "__mcpAppendable"))
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual({"textarea": True, "text": True, "untyped": True, "email": True, "search": True,
                          "number": False, "date": False, "select": False}, json.loads(proc.stdout))

    def test_clear_is_described_in_the_action_schema(self) -> None:
        # #176: the warning sits on clear itself, beside the argument that loses text.
        clear = browser_schemas.ACTION_ITEM_SCHEMA["properties"]["clear"]["description"]
        self.assertIn("false appends", clear)
        self.assertIn("removes ALL existing text", clear)


class SelectorTypeTests(unittest.TestCase):
    def script(self, clear: bool) -> str:
        with patch.object(tools_browser, "browser_execute_js", return_value={"ok": True}) as run:
            tools_browser.browser_type_selector(load_settings(), "Safari", "#f", " more", clear=clear)
        return run.call_args.args[2]

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_selector_typing_appends_when_clear_is_false(self) -> None:
        results = {}
        for clear in (True, False):
            harness = (
                "const events=[];const el={value:'Old',focus(){},dispatchEvent(e){events.push(e.type)}};"
                "globalThis.document={querySelector:()=>el};globalThis.Event=class{constructor(t){this.type=t}};"
                f"const r={self.script(clear)};console.log(JSON.stringify({{r,value:el.value,events}}));"
            )
            proc = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
            self.assertEqual(0, proc.returncode, proc.stderr)
            results[clear] = json.loads(proc.stdout)
        self.assertEqual({"r": "OK", "value": " more", "events": ["input", "change"]}, results[True])
        self.assertEqual({"r": "OK", "value": "Old more", "events": ["input", "change"]}, results[False])


if __name__ == "__main__":
    unittest.main()
