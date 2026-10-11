"""#164: a floating composer opened in a background tab is laid out, listed first and named."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import browser_tabs
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


_SETTLE_HARNESS = r"""
__FUNCTIONS__
function anim(state, end){return {playState:state,finished:false,effect:{getComputedTiming:()=>({endTime:end})},finish(){this.finished=true;}};}
const list=[anim('running',200),anim('running',Infinity),anim('running',10000),anim('paused',200),anim('finished',200)];
globalThis.document={visibilityState:'visible',getAnimations:()=>list};
const visible=__mcpSettleHidden();
const untouched=list.every(a=>!a.finished);
document.visibilityState='hidden';
const hidden=__mcpSettleHidden();
console.log(JSON.stringify({visible,untouched,hidden,finished:list.map(a=>a.finished)}));
"""

# A tiny element stand-in: enough for the naming/role helpers.
_ELEMENT_HARNESS = r"""
__FUNCTIONS__
function el(tag, attrs, opts){opts=opts||{};return {nodeType:1,tagName:tag.toUpperCase(),attrs:attrs||{},children:opts.children||[],parentElement:opts.parent||null,
  isContentEditable:!!opts.editable,innerText:opts.text||'',textContent:opts.text||'',className:'',
  getAttribute(n){return n in this.attrs?this.attrs[n]:null;},hasAttribute(n){return n in this.attrs;},
  querySelector(sel){return sel==='[data-placeholder]'?(this.children.find(c=>'data-placeholder' in c.attrs)||null):null;},
  getRootNode(){return null;}};}
globalThis.__mcpStyle=()=>({cursor:'auto'});
const emptyP=el('p',{'data-placeholder':'Type here.'},{editable:true});
const editor=el('div',{contenteditable:'true'},{editable:true,children:[emptyP]});emptyP.parentElement=editor;
const typedP=el('p',{'data-placeholder':'Type here.'},{editable:true,text:'hello'});
const typed=el('div',{contenteditable:'true'},{editable:true,children:[typedP],text:'hello'});typedP.parentElement=typed;
const button=el('button',{title:'Or press Cmd Enter'},{text:'Create Topic'});
const labelled=el('button',{'aria-label':'Close',title:'Close panel'},{text:'X'});
const longText=el('a',{title:'Short title'},{text:'x'.repeat(120)});
const row=el('div',{role:'menuitemradio'},{text:'macOS'});
console.log(JSON.stringify({
  placeholder:__mcpPlaceholder(editor),typedPlaceholder:__mcpPlaceholder(typed),
  inputPlaceholder:__mcpPlaceholder(el('input',{placeholder:'Title'})),
  editorText:__mcpText(editor),buttonText:__mcpText(button),labelledText:__mcpText(labelled),longText:__mcpText(longText),
  editorRole:__mcpRole(editor),paragraphRole:__mcpRole(emptyP),
  editorActionable:__mcpActionable(editor),paragraphActionable:__mcpActionable(emptyP),rowActionable:__mcpActionable(row),
}));
"""


class SettleHiddenTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_only_short_running_transitions_finish_and_only_in_a_hidden_tab(self) -> None:
        out = _node(_SETTLE_HARNESS.replace("__FUNCTIONS__", _functions("__mcpSettleHidden")))
        self.assertEqual(0, out["visible"])
        self.assertTrue(out["untouched"])
        self.assertEqual(1, out["hidden"])
        self.assertEqual([True, False, False, False, False], out["finished"])

    def test_every_read_settles_before_looking(self) -> None:
        observe = agent._observe_js("interactive", 40)
        self.assertLess(observe.index("__mcpSettleHidden();"), observe.index("var all=__mcpQueryAll('*');"))
        self.assertIn("__mcpSettleHidden();var s=__mcpState(), q=", agent._find_candidates_js("Create Topic", None, None))
        # Each action resolves its target after the previous one may have opened a panel.
        batch = agent._batch_js([{"type": "click", "query": "New Topic"}], None)
        self.assertIn("  __mcpSettleHidden();\n  var el=a.element_id?target(a):null;", batch)
        self.assertIn("__mcpSettleHidden();var s=__mcpState(),meta=", agent._changes_js("obs"))


_ANIMATING_HARNESS = r"""
__FUNCTIONS__
function anim(state, end, local){return {playState:state,effect:{getComputedTiming:()=>({endTime:end,localTime:local})}};}
globalThis.document={visibilityState:'visible',getAnimations:()=>[anim('running',200,50),anim('running',Infinity,10),anim('running',9000,0),anim('paused',300,0)]};
const visible=__mcpAnimatingMs();
document.visibilityState='hidden';
console.log(JSON.stringify({visible,hidden:__mcpAnimatingMs()}));
"""


class ChangesBaselineTests(unittest.TestCase):
    """An act's changes compare like with like, and wait for a panel that is still opening."""

    def test_observe_records_every_visible_control_as_the_baseline(self) -> None:
        js = agent._observe_js("interactive", 20)
        self.assertIn("ids:__mcpVisibleControls(s).map(function(x){return x.id;})", js)
        self.assertIn("s.lastSeen={ids:s.observationMeta[obs].ids", js)
        changes = agent._changes_js("obs")
        self.assertIn("var now=__mcpVisibleControls(s)", changes)

    def test_a_running_animation_keeps_the_baseline_and_is_read_again(self) -> None:
        changes = agent._changes_js("obs")
        self.assertIn("if(animating)out.animating_ms=animating;", changes)
        self.assertIn("if(!animating)s.lastSeen=", changes)
        import inspect
        act = inspect.getsource(agent._browser_act_locked)
        self.assertIn("cancellable_sleep(min(animating_ms, 600) / 1000 + 0.03)", act)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_only_short_running_animations_in_a_visible_page_count(self) -> None:
        out = _node(_ANIMATING_HARNESS.replace("__FUNCTIONS__", _functions("__mcpAnimatingMs")))
        self.assertEqual({"visible": 150, "hidden": 0}, out)


class NamingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_rich_editor_host_is_one_named_textbox(self) -> None:
        script = _ELEMENT_HARNESS.replace("__FUNCTIONS__", _functions(
            "__mcpParent", "__mcpPlaceholder", "__mcpText", "__mcpRole", "__mcpEditableChild", "__mcpActionable",
        ))
        out = _node(script)
        self.assertEqual("Type here.", out["placeholder"])
        self.assertEqual("", out["typedPlaceholder"])
        self.assertEqual("Title", out["inputPlaceholder"])
        self.assertEqual("Type here.", out["editorText"])
        self.assertEqual("Create Topic", out["buttonText"])
        self.assertEqual("Close", out["labelledText"])
        self.assertEqual("Short title", out["longText"])
        self.assertEqual("textbox", out["editorRole"])
        self.assertEqual("", out["paragraphRole"])
        self.assertTrue(out["editorActionable"])
        self.assertFalse(out["paragraphActionable"])
        self.assertTrue(out["rowActionable"])


class RankingTests(unittest.TestCase):
    def test_interactive_observe_ranks_floating_layers_and_reports_omissions(self) -> None:
        js = agent._observe_js("interactive", 20)
        self.assertIn("var ranked=(scope==='interactive'||scope==='visible')", js)
        self.assertIn("rank=(focusLayer&&layer===focusLayer)?0:(layer?1:(inView?2:3));", js)
        self.assertIn("rank+=label?0.25:4;", js)
        self.assertIn("picked.sort(function(a,b){return a.rank-b.rank||b.layer-a.layer||a.order-b.order;});", js)
        self.assertIn("picked=picked.slice(0,20);", js)
        # The scan stays bounded even though it no longer stops at max_elements.
        self.assertIn("if(ranked&&(picked.length>=1500||Date.now()-scanStart>400)){scanComplete=false;break;}", js)
        for key in ("ordering:", "eligible_count:", "omitted_count:"):
            self.assertIn(key, js)

    def test_content_scope_keeps_reading_order_and_stops_at_the_limit(self) -> None:
        js = agent._observe_js("content", 20)
        self.assertIn("for(var i=0;i<all.length && (ranked || picked.length<20);i++){", js)

    def test_appeared_controls_list_the_opened_layer_first(self) -> None:
        js = agent._changes_js("obs")
        self.assertIn("var base=layered(e)?0:3;", js)
        self.assertIn("appeared.sort(function(x,y){return x.w-y.w;});", js)
        self.assertLess(js.index("appeared.sort("), js.index("appeared:appeared.slice(0,15)"))
        self.assertIn("[role=menuitemradio],[role=menuitemcheckbox]", js)


class PayloadCeilingTests(unittest.TestCase):
    def test_internal_reads_are_not_cut_at_the_raw_execute_js_cap(self) -> None:
        payload = {"ok": True, "elements": [{"text": "x" * 200} for _ in range(150)]}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        self.assertGreater(len(encoded), 20_000)
        self.assertLess(len(encoded), agent._AGENT_PAYLOAD_MAX_CHARS)
        target = browser_tabs.TabTarget(browser="Safari", window_index=1, tab_index=1, tab_handle="btab_safari_x",
                                        native_id="1", title="T", url="https://example.com/", active=False)

        @contextmanager
        def lease(*_args, **_kwargs):
            yield target

        settings = SimpleNamespace(max_wait_s=30, max_js_result_chars=20_000)
        with patch.object(agent, "_tab_lease", lease), \
                patch.object(agent, "_execute_js_for_target", return_value=encoded):
            out = agent._run_json_js(settings, "Safari", "1", tab_handle="btab_safari_x")
        self.assertEqual(150, len(out["elements"]))

        too_big = "A" * (agent._AGENT_PAYLOAD_MAX_CHARS + 10)
        with patch.object(agent, "_tab_lease", lease), \
                patch.object(agent, "_execute_js_for_target", return_value=too_big), \
                self.assertRaises(Exception) as ctx:
            agent._run_json_js(settings, "Safari", "1", tab_handle="btab_safari_x")
        self.assertEqual(413, getattr(ctx.exception, "status_code", None))


if __name__ == "__main__":
    unittest.main()
