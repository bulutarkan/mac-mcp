"""#165: controls inside open shadow roots (web components) are seen, named and acted on."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from mcp_server import tools_browser_agent as agent


def _extract(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.find("\nfunction ", start + 10)
    return source[start:] if end < 0 else source[start:end]


def _functions(*names: str) -> str:
    source = agent._bootstrap_functions_source()
    return "\n".join(_extract(source, name) for name in names)


def _node(script: str) -> dict:
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


# Reddit-like tree: <auth-modal> renders <dialog><slot> in its shadow root; the light-DOM
# form is slotted into it, and each field keeps its <input> in its own shadow root.
_TREE = r"""
function el(tag, attrs, opts){opts=opts||{};return {nodeType:1,tagName:tag.toUpperCase(),attrs:attrs||{},parentElement:null,assignedSlot:null,
  shadowRoot:opts.shadowRoot||null,childElementCount:opts.children||0,innerText:opts.text||'',textContent:opts.text||'',root:null,
  getAttribute(n){return n in this.attrs?this.attrs[n]:null;},getRootNode(){return this.root||globalThis.document;},
  getBoundingClientRect(){return opts.rect||{width:200,height:30};}};}
globalThis.document={};
const modalHost=el('auth-modal'),dialog=el('dialog',{open:''}),slot=el('slot');
const shadowOfModal={host:modalHost};dialog.root=shadowOfModal;slot.root=shadowOfModal;slot.parentElement=dialog;
const form=el('form');form.parentElement=modalHost;form.assignedSlot=slot;
const fieldHost=el('faceplate-text-input',{label:'Password'});fieldHost.parentElement=form;
const input=el('input',{type:'password'});input.root={host:fieldHost};
const outside=el('button',{},{text:'Background'});
"""

_CONTAINS_HARNESS = _TREE + r"""
__FUNCTIONS__
console.log(JSON.stringify({
  inside:__mcpComposedContains(dialog,input),
  outside:__mcpComposedContains(dialog,outside),
  formParent:__mcpFlatParent(form)===slot,
  inputParent:__mcpFlatParent(input)===fieldHost,
}));
"""

_NAME_HARNESS = _TREE + r"""
__FUNCTIONS__
const named=el('input',{'aria-label':'Own name'});named.root={host:fieldHost};
console.log(JSON.stringify({
  hostLabel:__mcpText(input),own:__mcpText(named),plain:__mcpText(outside),
  host:(__mcpShadowHost(input)||{}).tagName||null,lightHost:__mcpShadowHost(outside),
}));
"""

_CLOSED_HARNESS = r"""
__FUNCTIONS__
globalThis.window=globalThis;globalThis.customElements={get:(t)=>['closed-widget','tiny-icon','open-widget','wrapper-el'].includes(t)?function(){}:undefined};
globalThis.__mcpVisible=()=>true;
function el(tag,o){return Object.assign({tagName:tag.toUpperCase(),shadowRoot:null,childElementCount:0,getBoundingClientRect(){return {width:120,height:40};}},o||{});}
const all=[el('closed-widget'),el('closed-widget'),el('tiny-icon',{getBoundingClientRect(){return {width:12,height:12};}}),
  el('open-widget',{shadowRoot:{}}),el('wrapper-el',{childElementCount:2}),el('undefined-el'),el('div')];
console.log(JSON.stringify({found:__mcpClosedShadowHosts(all)||null,none:__mcpClosedShadowHosts([el('div')])||null}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ShadowTreeTests(unittest.TestCase):
    def test_slotted_content_is_inside_the_shadow_dialog_it_renders_in(self) -> None:
        out = _node(_CONTAINS_HARNESS.replace("__FUNCTIONS__", _functions("__mcpParent", "__mcpFlatParent", "__mcpComposedContains")))
        self.assertEqual({"inside": True, "outside": False, "formParent": True, "inputParent": True}, out)

    def test_a_field_named_only_on_its_host_takes_the_host_label(self) -> None:
        script = _NAME_HARNESS.replace("__FUNCTIONS__", _functions("__mcpPlaceholder", "__mcpText", "__mcpShadowHost"))
        out = _node(script)
        self.assertEqual("Password", out["hostLabel"])
        self.assertEqual("Own name", out["own"])
        self.assertEqual("Background", out["plain"])
        self.assertEqual("FACEPLATE-TEXT-INPUT", out["host"])
        self.assertIsNone(out["lightHost"])

    def test_drawn_custom_elements_without_an_open_root_are_reported_as_closed(self) -> None:
        out = _node(_CLOSED_HARNESS.replace("__FUNCTIONS__", _functions("__mcpClosedShadowHosts")))
        self.assertEqual(2, out["found"]["count"])
        self.assertEqual(["closed-widget"], out["found"]["tags"])
        self.assertIsNone(out["none"])


class ShadowWiringTests(unittest.TestCase):
    def test_visibility_and_modal_scope_follow_the_rendering_tree(self) -> None:
        source = agent._bootstrap_functions_source()
        self.assertIn("cur=__mcpFlatParent(cur);", _extract(source, "__mcpSemanticVisible"))
        self.assertIn("__mcpComposedContains(modal,el)", _extract(source, "__mcpModalScope"))

    def test_observe_reports_shadow_hosts_and_closed_roots(self) -> None:
        self.assertIn("out.shadow_host=", _extract(agent._bootstrap_functions_source(), "__mcpDescribe"))
        self.assertIn("closed_shadow_hosts:__mcpClosedShadowHosts(all)", agent._observe_js("interactive", 20))

    def test_act_changes_look_inside_shadow_roots(self) -> None:
        js = agent._changes_js("obs")
        self.assertIn("nodes=__mcpQueryAll(sel)", js)
        self.assertIn("var invalid=[];__mcpQueryAll('input,select,textarea')", js)
        self.assertNotIn("document.querySelectorAll(sel)", js)


if __name__ == "__main__":
    unittest.main()
