"""#73: Mac MCP's real page scripts against real pages in a real browser engine (opt-in).

The fast suite checks page scripts as strings or in node with stub DOMs. This tier
runs the same observe/find/act scripts in headless Chrome against test-owned
fixture pages (tests/fixtures/browser), served from a local port, in a throwaway
Chrome profile. It needs no Accessibility/TCC permission and never touches the
user's browsers.

Run it with:
    MAC_MCP_BROWSER_INTEGRATION=1 python -m unittest tests.test_browser_page_integration -v
MAC_MCP_TEST_CHROME overrides the Chrome binary.
"""
from __future__ import annotations

import base64
import functools
import http.server
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from mcp_server import applescript_host, browser_tabs, tools_browser
from mcp_server import tools_browser_agent as agent
from mcp_server.security import load_settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "browser"
CHROME = os.environ.get("MAC_MCP_TEST_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
ENABLED = os.environ.get("MAC_MCP_BROWSER_INTEGRATION") == "1"


class Page:
    """One Chrome tab driven over the DevTools protocol (synchronous)."""

    _ids = itertools.count(1)

    def __init__(self, ws, target_id: str, port: int) -> None:
        self.ws, self.target_id, self.port = ws, target_id, port

    def cmd(self, method: str, **params):
        message_id = next(self._ids)
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv(timeout=30))
            if message.get("id") == message_id:
                if "error" in message:
                    raise AssertionError(f"{method}: {message['error']}")
                return message.get("result", {})

    def js(self, expression: str):
        result = self.cmd("Runtime.evaluate", expression=expression, returnByValue=True)
        if "exceptionDetails" in result:
            raise AssertionError(f"page script threw: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def payload(self, script: str) -> dict:
        raw = self.js(script)
        if not isinstance(raw, str):
            raise AssertionError(f"page script returned {raw!r}")
        return json.loads(base64.b64decode(raw).decode("utf-8"))

    def close(self) -> None:
        self.ws.close()
        urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/close/{self.target_id}", timeout=10).read()


def label(element: dict) -> str:
    return str(element.get("text") or element.get("aria_label") or element.get("placeholder") or "")


class Tools:
    """The real browser_observe/find/act, with only the page-JavaScript transport sent to Chrome.

    Helper-library caching, the cached binding, batching and deferred steps all run as in
    production. Any AppleScript call fails the test instead of reaching a real browser.
    """

    HANDLE = "btab_safari_integration"

    def __init__(self, page: Page, url: str) -> None:
        self.page, self.settings = page, load_settings()
        self.target = browser_tabs.TabTarget(browser="Safari", window_index=1, tab_index=1, tab_handle=self.HANDLE,
                                             native_id=page.target_id, title="fixture", url=url, active=False)

    @contextmanager
    def active(self):
        @contextmanager
        def lease(*_args, **_kwargs):
            yield self.target

        def no_applescript(*_args, **_kwargs):
            raise AssertionError("the integration tier must not run AppleScript")

        with ExitStack() as stack:
            stack.enter_context(patch.object(agent, "_raw_execute_js_for_target",
                                             side_effect=lambda _b, js, _t, _timeout: self.page.js(js)))
            stack.enter_context(patch.object(agent, "_tab_lease", lease))
            row = {"browser": "Safari", "window_index": 1, "tab_index": 1, "tab_handle": self.HANDLE,
                   "native_id": self.target.native_id, "title": "fixture", "url": self.target.url, "active": False}
            stack.enter_context(patch.object(browser_tabs, "resolve_tab", return_value=(1, 1, dict(row))))
            stack.enter_context(patch.object(browser_tabs, "list_tabs", return_value=[dict(row)]))
            stack.enter_context(patch.object(agent, "_require_stable_handle_for_mutation"))
            stack.enter_context(patch.object(agent, "_ensure_visual_companion", return_value=False))
            stack.enter_context(patch.object(agent, "delegated_agent_identity", return_value=None))
            for name in ("_run_osascript", "_run_osascript_process"):
                stack.enter_context(patch.object(tools_browser, name, side_effect=no_applescript))
            stack.enter_context(patch.object(browser_tabs, "_osascript", side_effect=no_applescript))
            stack.enter_context(patch.object(applescript_host, "run", side_effect=no_applescript))
            yield self

    def observe(self, **kwargs) -> dict:
        result = agent.browser_observe(self.settings, "Safari", tab_handle=self.HANDLE, **kwargs)
        return json.loads(result) if isinstance(result, str) else result

    def find(self, query: str, role=None) -> dict:
        found = agent.browser_find(self.settings, "Safari", query, role=role, tab_handle=self.HANDLE,
                                   actionable_only=True)
        matches = found.get("matches") or []
        assert matches, f"nothing actionable matches {query!r}"
        return matches[0]

    def act(self, actions, observation_id=None) -> dict:
        return agent.browser_act(self.settings, "Safari", actions, observation_id=observation_id, tab_handle=self.HANDLE)


@unittest.skipUnless(ENABLED, "set MAC_MCP_BROWSER_INTEGRATION=1 to run the browser page tier")
@unittest.skipUnless(Path(CHROME).exists(), "Google Chrome is not installed")
class BrowserPageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
        handler.log_message = lambda *args: None
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.profile = Path(tempfile.mkdtemp(prefix="mac-mcp-chrome-"))
        cls.chrome = subprocess.Popen(
            [CHROME, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={cls.profile}",
             "--no-first-run", "--no-default-browser-check", "--window-size=1440,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        port_file = cls.profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not port_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        cls.devtools_port = int(port_file.read_text().splitlines()[0])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.chrome.terminate()
        try:
            cls.chrome.wait(10)
        except subprocess.TimeoutExpired:
            cls.chrome.kill()
        cls.server.shutdown()
        shutil.rmtree(cls.profile, ignore_errors=True)

    def open(self, name: str) -> Page:
        from websockets.sync.client import connect

        url = f"http://localhost:{self.server.server_address[1]}/{name}"
        self.url = url
        request = urllib.request.Request(f"http://127.0.0.1:{self.devtools_port}/json/new?{url}", method="PUT")
        target = json.load(urllib.request.urlopen(request, timeout=10))
        page = Page(connect(target["webSocketDebuggerUrl"], max_size=50_000_000), target["id"], self.devtools_port)
        self.addCleanup(page.close)
        page.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
        # A new tab reports readyState=complete for about:blank before it navigates, so a slow
        # runner could start on an empty page: wait for the fixture itself to finish loading.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not page.js(
            f"location.href.startsWith({json.dumps(url)}) && document.readyState === 'complete'"
        ):
            time.sleep(0.05)
        return page

    def first_actionable(self, page: Page, query: str, role=None) -> dict:
        found = page.payload(agent._find_candidates_js(query, role, None))
        matches = [e for e in found.get("elements", []) if e.get("actionable")]
        self.assertTrue(matches, f"nothing actionable matches {query!r}")
        return matches[0]

    def test_selector_extraction_pages_without_losing_records_or_text(self) -> None:
        page = self.open("extraction.html")
        fields = [{"name": "reviews", "selector": "#reviews > div", "all": True, "max_items": 2}]
        first = page.payload(agent._extract_action_js(fields, 4000))
        self.assertEqual(["Review One", "Review Two"], first["data"]["reviews"])
        self.assertEqual(4, first["matched_counts"]["reviews"])
        self.assertEqual(3, first["fields"]["reviews"]["eligible_count"])
        self.assertEqual(2, first["fields"]["reviews"]["returned_count"])
        self.assertTrue(first["truncated"])
        fields[0].update(first["fields"]["reviews"]["continuation"])
        second = page.payload(agent._extract_action_js(fields, 4000))
        self.assertEqual(["Review Three"], second["data"]["reviews"])
        self.assertTrue(second["complete"])
        long = [{"name": "long", "selector": "#long_text", "all": True}]
        chunks = []
        for _ in range(12):
            result = page.payload(agent._extract_action_js(long, 1000))
            chunks.extend(result["data"]["long"])
            if result["complete"]:
                break
            long[0].update(result["fields"]["long"]["continuation"])
        self.assertEqual("BEGIN " + "x" * 9000 + " END", "".join(chunks))

    def test_invalid_selector_is_an_error_and_zero_matches_is_not(self) -> None:
        page = self.open("extraction.html")
        invalid = page.payload(agent._extract_action_js([{"name": "x", "selector": "div["}], 1000))
        self.assertFalse(invalid["ok"])
        self.assertEqual("invalid_selector", invalid["error"])
        empty = page.payload(agent._extract_action_js([{"name": "x", "selector": ".absent", "all": True}], 1000))
        self.assertTrue(empty["ok"])
        self.assertEqual([], empty["data"]["x"])
        self.assertTrue(empty["complete"])

    def test_scan_continuation_drains_static_rows_before_scrolling(self) -> None:
        tools = self.tools("extraction.html")
        action = {"type": "scan", "item_selector": "#large article", "max_items": 150, "max_steps": 50, "settle_ms": 100}
        rows = []
        for _ in range(5):
            result = tools.act([action])["actions"][0]
            self.assertTrue(result["ok"], result)
            rows.extend(item["text"] for item in result["items"])
            if result["complete"]:
                break
            self.assertIsNotNone(result["next_cursor"])
            action["cursor"] = result["next_cursor"]
        self.assertEqual(["Record " + str(i) for i in range(450)], rows)
        self.assertTrue(result["complete"])

    def test_scan_continuation_keeps_every_virtualized_row(self) -> None:
        tools = self.tools("extraction.html")
        action = {"type": "scan", "item_selector": "#virtual article", "max_items": 17, "max_steps": 50, "settle_ms": 100}
        rows = []
        for _ in range(8):
            result = tools.act([action])["actions"][0]
            self.assertTrue(result["ok"], result)
            rows.extend(item["text"] for item in result["items"])
            if result["complete"]:
                break
            action["cursor"] = result["next_cursor"]
        self.assertEqual(["Virtual " + str(i) for i in range(80)], rows)
        self.assertTrue(result["complete"])

    def test_scan_long_record_returns_all_text_in_resumable_chunks(self) -> None:
        tools = self.tools("extraction.html")
        action = {"type": "scan", "item_selector": "#long_text", "max_chars": 1000, "settle_ms": 100}
        chunks = []
        for _ in range(12):
            result = tools.act([action])["actions"][0]
            chunks.extend(item["text"] for item in result["items"])
            if result["complete"]:
                break
            action["cursor"] = result["next_cursor"]
        self.assertEqual("BEGIN " + "x" * 9000 + " END", "".join(chunks))
        self.assertTrue(result["complete"])

    def test_scan_rejects_cursor_after_navigation_or_query_change(self) -> None:
        tools = self.tools("extraction.html")
        action = {"type": "scan", "item_selector": "#large article", "max_items": 2}
        first = tools.act([action])["actions"][0]
        cursor = first["next_cursor"]
        changed = tools.act([{**action, "cursor": cursor, "item_selector": "#reviews div"}])["actions"][0]
        self.assertFalse(changed["ok"])
        self.assertEqual("invalid_scan_cursor", changed["error"])
        # A cursor unknown to this document must not restart from the current scroll position.
        other = self.open("extraction.html")
        invalid = other.payload(agent._scan_step_js("", "#large article", "text", {"cursor": cursor}))
        self.assertFalse(invalid["ok"])
        self.assertEqual("invalid_scan_cursor", invalid["error"])

    def test_scan_ignores_script_and_hidden_content_and_has_no_false_completion(self) -> None:
        tools = self.tools("extraction.html")
        result = tools.act([{"type": "scan", "item_selector": "#empty > *", "max_steps": 2, "settle_ms": 100}])["actions"][0]
        self.assertEqual([], result["items"])
        self.assertEqual("no_usable_candidates", result["stopped"])
        self.assertFalse(result["complete"])

    def test_extract_and_scan_never_split_surrogate_pairs(self) -> None:
        tools = self.tools("extraction.html")
        original = "a" * 255 + "😀z"
        tools.page.js("document.getElementById('long_text').textContent=" + json.dumps(original))
        fields = [{"name": "x", "selector": "#long_text", "all": True}]
        extracted = tools.page.payload(agent._extract_action_js(fields, 256))
        first = extracted["data"]["x"][0]
        first.encode("utf-8")
        fields[0].update(extracted["fields"]["x"]["continuation"])
        second = tools.page.payload(agent._extract_action_js(fields, 256))
        self.assertEqual(original, first + second["data"]["x"][0])
        action = {"type": "scan", "item_selector": "#long_text", "max_chars": 256, "settle_ms": 100}
        scan = tools.act([action])["actions"][0]
        prefix = "".join(row["text"] for row in scan["items"])
        prefix.encode("utf-8")
        action["cursor"] = scan["next_cursor"]
        suffix = tools.act([action])["actions"][0]
        self.assertEqual(original, prefix + "".join(row["text"] for row in suffix["items"]))

    def test_scan_keeps_distinct_rows_with_identical_text(self) -> None:
        tools = self.tools("extraction.html")
        tools.page.js("document.getElementById('large').innerHTML='<article>Same</article><article>Same</article><article>Different</article>'")
        scan = tools.act([{"type": "scan", "item_selector": "#large article", "settle_ms": 100}])["actions"][0]
        self.assertEqual(["Same", "Same", "Different"], [item["text"] for item in scan["items"]])

    def test_scan_cursor_rejects_intervening_scroll_without_claiming_completion(self) -> None:
        tools = self.tools("extraction.html")
        action = {"type": "scan", "item_selector": "#virtual article", "max_items": 2}
        first = tools.act([action])["actions"][0]
        tools.page.js("var v=document.getElementById('virtual');v.scrollTop=3040;v.dispatchEvent(new Event('scroll'))")
        resumed = tools.act([{**action, "cursor": first["next_cursor"]}])["actions"][0]
        self.assertFalse(resumed["ok"])
        self.assertEqual("invalid_scan_cursor", resumed["error"])
        self.assertFalse(resumed["complete"])

    def test_extract_excludes_hidden_composed_ancestors_and_semantic_parent_text(self) -> None:
        page = self.open("extraction.html")
        page.js("""var host=document.createElement('div');host.style.opacity='0';host.attachShadow({mode:'open'}).innerHTML='<span class="sensitive">TRANSPARENT SHADOW</span>';document.body.appendChild(host);
            var address=document.createElement('div');address.innerHTML='<span>Address</span><span style="opacity:0">SECRET HIDDEN VALUE</span>';document.body.appendChild(address);""")
        result = page.payload(agent._extract_action_js([{"name": "x", "selector": ".sensitive", "all": True}, {"name": "address", "semantic": "address", "all": True}], 4000))
        self.assertEqual([], result["data"]["x"])
        self.assertNotIn("SECRET HIDDEN VALUE", str(result["data"]["address"]))

    def test_scan_resume_accepts_the_original_child_element_target(self) -> None:
        tools = self.tools("extraction.html")
        child = tools.page.js(agent._browser_state_bootstrap() + "\n__mcpId(document.querySelector('#large article'),__mcpState())")
        action = {"type": "scan", "element_id": child, "item_selector": "#large article", "max_items": 2}
        first = tools.act([action])["actions"][0]
        resumed = tools.act([{**action, "cursor": first["next_cursor"]}])["actions"][0]
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(["Record 2", "Record 3"], [row["text"] for row in resumed["items"]])

    def test_scan_only_rerenders_do_not_retain_detached_item_nodes(self) -> None:
        tools = self.tools("extraction.html")
        for _ in range(6):
            tools.page.js("document.getElementById('large').innerHTML=Array.from({length:100},(_,i)=>'<article>Record '+i+'</article>').join('')")
            result = tools.act([{"type": "scan", "item_selector": "#large article", "max_items": 2}])["actions"][0]
            self.assertTrue(result["ok"], result)
        detached = tools.page.js(agent._browser_state_bootstrap() + "\nObject.values(__mcpState().elements).filter(el=>!el.isConnected).length")
        self.assertEqual(0, detached)

    def test_display_contents_records_remain_extractable_and_scannable(self) -> None:
        tools = self.tools("extraction.html")
        tools.page.js("var el=document.createElement('article');el.id='contents';el.style.display='contents';el.textContent='VISIBLE CONTENTS TEXT';document.body.appendChild(el)")
        extracted = tools.page.payload(agent._extract_action_js([{"name": "x", "selector": "#contents", "all": True}], 1000))
        self.assertEqual(["VISIBLE CONTENTS TEXT"], extracted["data"]["x"])
        scan = tools.act([{"type": "scan", "item_selector": "#contents", "max_steps": 1}])["actions"][0]
        self.assertEqual(["VISIBLE CONTENTS TEXT"], [row["text"] for row in scan["items"]])

    def test_cached_helper_binding_runs_every_script(self) -> None:
        # The fast path sends only a binding to the helpers installed by the first call.
        # A helper missing from it (e.g. an apostrophe in a JS comment) breaks here, in a real engine.
        page = self.open("shadow.html")
        observe = agent._observe_js("interactive", 30)
        full = page.payload(observe)
        scripts = {
            "observe": observe,
            "find": agent._find_candidates_js("Password", None, None),
            "changes": agent._changes_js(full["observation_id"]),
            "batch": agent._batch_js([{"type": "scroll", "dy": 10}], full["observation_id"]),
        }
        for name, script in scripts.items():
            binding = agent._with_binding_only(script)
            with self.subTest(script=name):
                self.assertIsNotNone(binding)
                self.assertNotEqual(agent._AGENT_API_MISSING, page.js(binding))
                self.assertTrue(page.payload(binding).get("ok", True))

    def tools(self, name: str) -> Tools:
        page = self.open(name)
        tools = Tools(page, self.url)
        stack = ExitStack()
        stack.enter_context(tools.active())
        self.addCleanup(stack.close)
        return tools

    def test_floating_composer_is_seen_named_and_usable(self) -> None:
        # #164: a fixed composer appended after hundreds of controls.
        tools = self.tools("composer.html")
        before = tools.observe(max_elements=20)  # the baseline that "changes" is measured against
        opened = tools.act([{"type": "click", "query": "New Topic", "role": "button"}], before["observation_id"])
        self.assertTrue(opened["ok"], opened)
        appeared = {str(e.get("text") or "") for e in opened["changes"]["appeared"]}
        self.assertTrue({"Create Topic", "Discard"} <= appeared, sorted(appeared))
        time.sleep(0.3)  # the composer's 0.2 s height transition
        observed = tools.observe(max_elements=20)
        names = {label(e) for e in observed["elements"]}
        for wanted in ("What is this discussion about in one brief sentence?", "Uncategorized", "Create Topic", "Discard"):
            self.assertTrue(any(wanted in name for name in names), f"{wanted!r} not in {sorted(names)}")
        self.assertEqual("foreground_first", observed["ordering"])
        body = [e for e in observed["elements"] if label(e).startswith("Type here")]
        self.assertEqual(["textbox"], [e["role"] for e in body], "the editor body is one named textbox")
        create = tools.find("Create Topic", "button")
        self.assertEqual(("button", "Create Topic"), (create["tag"], create["text"]))

        filled = tools.act([
            {"type": "type", "query": "What is this discussion about", "text": "Integration title"},
            {"type": "type", "element_id": body[0]["element_id"], "text": "Integration body"},
            {"type": "click", "query": "Uncategorized", "role": "listbox"},
        ], observed["observation_id"])
        self.assertTrue(filled["ok"], filled)
        self.assertIn("macOS", [e.get("text") for e in filled["changes"]["appeared"]])
        submitted = tools.act([
            {"type": "click", "query": "macOS", "role": "menuitemradio"},
            {"type": "click", "query": "Create Topic", "role": "button"},
            {"type": "wait", "for": "text", "text": "Mock topic created"},
        ])
        self.assertTrue(submitted["ok"], submitted)
        self.assertEqual("Mock topic created: Integration title / Integration body (#1)|1|macOS",
                         tools.page.js("document.getElementById('receipt').textContent+'|'+window.__submits+'|'"
                                       "+document.querySelector('#cat summary').textContent"))

    def test_rich_text_model_and_dom_disagreements_are_reported_honestly(self) -> None:
        # #205: an editor's model and its DOM can disagree; each case gets the outcome its evidence allows.
        tools = self.tools("rerender.html")

        def type_into(label: str, text: str) -> dict:
            result = tools.act([{"type": "type", "query": label, "role": "textbox", "text": text}])
            return result["actions"][-1]

        consistent = type_into("Consistent editor", "Hello consistent")
        self.assertEqual("value_applied", consistent["verification"], consistent)
        self.assertEqual("confirmed", consistent["outcome"])
        self.assertTrue(consistent.get("rendered_after_settle"), consistent)

        delayed = type_into("Delayed editor", "Hello delayed")
        self.assertEqual("value_applied_async", delayed["verification"], delayed)

        # Drawn at once, then re-rendered back to the old model: not a success.
        reverts = type_into("Reverting editor", "Hello reverts")
        self.assertFalse(reverts["ok"], reverts)
        self.assertEqual("editor_rerender_lost", reverts["verification"])
        self.assertEqual("INPUT_LOST_AFTER_RENDER", reverts["reason_code"])
        self.assertFalse(reverts.get("automatic_retry", False))
        self.assertEqual("Draft", tools.page.js("document.getElementById('reverts').textContent"))

        # The model took the text but the DOM never showed it: unknown, not "rejected", no retry.
        model_only = type_into("Model only editor", "Hello model")
        self.assertFalse(model_only["ok"], model_only)
        self.assertEqual("editor_render_unconfirmed", model_only["verification"])
        self.assertEqual("unconfirmed", model_only["outcome"])
        self.assertFalse(model_only.get("automatic_retry", False))
        self.assertEqual("Hello model", tools.page.js("window.__models['model-only']"))

        # The editor kept the text in its own form: not lost, not confirmed either.
        reformats = type_into("Reformatting editor", "1. First point")
        self.assertTrue(reformats["ok"], reformats)
        self.assertEqual("editor_rerendered_text", reformats["verification"])
        self.assertEqual("unconfirmed", reformats["outcome"])
        self.assertEqual("First point", tools.page.js("document.getElementById('reformats').textContent"))

    def test_a_lost_rich_text_never_reaches_the_submit_after_it(self) -> None:
        tools = self.tools("rerender.html")
        result = tools.act([
            {"type": "type", "query": "Reverting editor", "role": "textbox", "text": "Lost text"},
            {"type": "wait", "wait_s": 0.1},
        ])
        self.assertFalse(result["ok"], result)
        self.assertEqual("editor_rerender_lost", result["actions"][-1]["verification"])

    def test_web_component_dialog_controls_are_seen_and_usable(self) -> None:
        # #165: a dialog in a shadow root with its form slotted in; inputs in their own shadow roots.
        tools = self.tools("shadow.html")
        observed = tools.observe(max_elements=40)
        self.assertTrue(observed["modal_scope"]["active"])
        names = {label(e) for e in observed["elements"]}
        self.assertTrue({"Email or username", "Password", "Region", "Log In"} <= names, sorted(names))
        self.assertEqual(["closed-widget"], observed["closed_shadow_hosts"]["tags"])
        self.assertEqual("input", tools.find("Password")["tag"])
        result = tools.act([
            {"type": "type", "query": "Email or username", "text": "fixture_user"},
            {"type": "type", "query": "Password", "text": "not-a-real-secret"},
            {"type": "select", "query": "Region", "option": "US"},
            {"type": "click", "query": "Log In", "role": "button"},
            {"type": "wait", "for": "text", "text": "Mock login"},
        ], observed["observation_id"])
        self.assertTrue(result["ok"], result)
        self.assertNotIn("not-a-real-secret", json.dumps(result))
        self.assertEqual("Mock login: fixture_user / password set / US (#1)",
                         tools.page.js("document.getElementById('receipt').textContent"))
        again = tools.observe(max_elements=40)
        self.assertEqual(["[hidden]"], [e.get("value") for e in again["elements"] if e.get("input_type") == "password"])
        self.assertNotIn("not-a-real-secret", json.dumps(again))

    def test_post_comments_and_replies_in_one_action(self) -> None:
        # A comment box in a shadow root with a formatting toolbar, a search field above it,
        # and Reply buttons that open a box inside one comment whose reply arrives later.
        tools = self.tools("thread.html")
        posted = tools.act([{"type": "post", "text": "Integration comment: one call is enough."}])
        self.assertTrue(posted["ok"], posted)
        self.assertEqual((True, "text_visible"), (posted["actions"][0]["posted"], posted["actions"][0]["verification"]))
        self.assertEqual("comment", posted["actions"][0]["submit"]["label"])
        self.assertEqual([1, 1, ""], tools.page.js(
            "[window.__posts,window.__clicks,document.querySelector('header input').value]"))
        self.assertIn("Integration comment", tools.page.js("document.querySelector('#thread article .body').textContent"))

        phrase = "Do you let users teach it new skills"
        replied = tools.act([
            {"type": "click", "query": "Reply", "role": "button", "within": phrase},
            {"type": "post", "text": "Integration reply under that comment.", "within": phrase},
        ])
        self.assertTrue(replied["ok"], replied)
        self.assertTrue(replied["actions"][1]["posted"])
        self.assertEqual(2, tools.page.js("window.__posts"))
        self.assertEqual("Integration reply under that comment.", tools.page.js(
            "Array.from(document.querySelectorAll('article')).find(a=>a.querySelector('.body').textContent.startsWith('Do you let'))"
            ".querySelector('.replies .body').textContent"))

    def test_reply_without_opening_the_box_never_lands_in_the_main_composer(self) -> None:
        # Live run: a delegated agent sent post within=<comment> without clicking Reply, and the
        # reply was published as a top-level comment through the post's main composer.
        tools = self.tools("thread.html")
        phrase = "Do you let users teach it new skills"
        result = tools.act([{"type": "post", "text": "Auto-opened reply under that comment.", "within": phrase}])
        self.assertTrue(result["ok"], result)
        post = result["actions"][0]
        self.assertEqual((True, "Reply"), (post["posted"], post["opened_reply"]))
        self.assertEqual([1, 0], tools.page.js(
            "[window.__posts,Array.from(document.querySelectorAll('#thread > article > .body')).filter(b=>b.textContent.startsWith('Auto-opened')).length]"))
        self.assertEqual("Auto-opened reply under that comment.", tools.page.js(
            "Array.from(document.querySelectorAll('article')).find(a=>a.querySelector('.body').textContent.startsWith('Do you let'))"
            ".querySelector('.replies .body').textContent"))

    def test_unconfirmed_post_reports_once_without_retrying(self) -> None:
        tools = self.tools("thread.html")
        result = tools.act([{"type": "post", "text": "Please fail this integration comment.", "timeout_s": 1.5}])
        self.assertFalse(result["ok"])
        post = result["actions"][0]
        self.assertEqual(("post_not_confirmed", True, None), (post["error"], post["submitted"], post["posted"]))
        self.assertEqual([0, 1], tools.page.js("[window.__posts,window.__clicks]"))

    def test_observe_hands_back_a_fill_template_and_fill_does_the_form_in_one_call(self) -> None:
        tools = self.tools("form.html")
        observed = tools.observe(max_elements=60)
        template = observed.get("fill_template") or {}
        self.assertEqual("fill", template.get("type"), observed.get("fill_template"))
        self.assertTrue({"Full name", "Email", "Country", "Message", "I accept the terms"} <= set(template["values"]),
                        template)
        self.assertNotIn("Search the site", template["values"])
        filled = tools.act([{"type": "fill", "values": {
            "Full name": "Integration Tester", "Email": "tester@example.invalid", "Country": "United States",
            "State": "California", "Plan": "Pro", "Message": "Filled in one call.",
            "Newsletter": True, "I accept the terms": True,
        }}])
        self.assertTrue(filled["ok"], filled)
        self.assertEqual(7, filled["actions"][0]["filled"])  # Newsletter was already checked
        self.assertEqual(
            ["Integration Tester", "tester@example.invalid", "United States", "California", "Pro", "Filled in one call.",
             True, True, 0, ""],
            tools.page.js("['name','email','country','state'].map(i=>document.getElementById(i).value)"
                          ".concat([document.getElementById('plan').textContent,document.getElementById('message').value,"
                          "document.getElementById('news').checked,document.getElementById('terms').checked,window.__submits,"
                          "document.querySelector('header input').value])"))
        # Agents often pass the choice as value=; select takes it like option=.
        chosen = tools.act([{"type": "select", "query": "Plan", "value": "Free"}])
        self.assertTrue(chosen["ok"], chosen)
        self.assertEqual("Free", tools.page.js("document.getElementById('plan').textContent"))

    def test_fill_closes_a_picker_that_covers_the_next_field(self) -> None:
        # Live run on a real form: the date field left its calendar open over the range field,
        # so fill reported ELEMENT_OCCLUDED for it and the agent needed three more calls.
        tools = self.tools("picker.html")
        filled = tools.act([{"type": "fill", "values": {"Arrival date": "2026-10-20", "Guests": "7", "Notes": "Window seat"}}])
        self.assertTrue(filled["ok"], filled)
        guests = next(row for row in filled["actions"][0]["fields"] if row["field"] == "Guests")
        self.assertTrue(guests.get("closed_popup_with_escape"), guests)
        self.assertEqual(["2026-10-20", "7", "Window seat", 1], tools.page.js(
            "[arrival.value,document.getElementById('guests').value,document.getElementById('notes').value,window.__escapes]"))

    def test_visual_companion_arrow_lands_on_the_target_with_its_label(self) -> None:
        # The redesigned companion: a graphite arrow whose tip sits on the target, the action label
        # next to it (flipped near the edges), a page-level label without coordinates, and an
        # overlay that never takes a click.
        page = self.open("form.html")
        page.js((ROOT / "menu_app" / "BrowserVisualCompanion" / "visual.js").read_text(encoding="utf-8"))

        def send(**event):
            payload = {"seq": f"s{time.time_ns()}", "claim": True, "phase": "working", "ttl_ms": 5000, **event}
            raw = base64.b64encode(json.dumps(payload).encode()).decode()
            page.js(f"document.documentElement.setAttribute('data-mac-mcp-visual-event', {json.dumps(raw)});"
                    "window.dispatchEvent(new Event('mac-mcp-visual'));")

        state = ("(()=>{const root=document.getElementById('mac-mcp-visual-companion-root').shadowRoot;"
                 "const c=root.querySelector('.cursor'),t=c.querySelector('.tip'),p=root.querySelector('.page-tip');"
                 "const tip=c.querySelector('svg').getBoundingClientRect();"
                 "return {x:tip.left+3.5,y:tip.top+3.5,label:t.textContent,shown:t.classList.contains('show'),"
                 "flip:[c.classList.contains('flip-x'),c.classList.contains('flip-y')],page:p.classList.contains('show')?p.textContent:'',"
                 "cursorOn:c.classList.contains('active')}})()")
        send(action="Inspecting", target_kind="Page")
        time.sleep(0.3)
        first = page.js(state)
        self.assertEqual(("Inspecting", False), (first["page"], first["cursorOn"]))
        target = page.js("(()=>{const r=document.querySelector('#email').getBoundingClientRect();"
                         "return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}})()")
        send(action="Clicking", target_kind="Text field", x=target["x"], y=target["y"], effect="click")
        time.sleep(0.6)
        clicked = page.js(state)
        self.assertLess(abs(clicked["x"] - target["x"]) + abs(clicked["y"] - target["y"]), 3, clicked)
        self.assertEqual(("Clicking", True, [False, False], ""), (clicked["label"], clicked["shown"], clicked["flip"], clicked["page"]))
        send(action="Scrolling", detail="Down", target_kind="Page", x=1400, y=870)
        time.sleep(0.8)
        edge = page.js(state)
        self.assertEqual(("Scrolling down", [True, True]), (edge["label"], edge["flip"]))
        # The overlay is pointer-transparent: a click at the arrow reaches the page.
        hit = page.js(f"document.elementFromPoint({target['x']}, {target['y']}).id")
        self.assertEqual("email", hit)

    def test_range_slider_is_a_slider_and_drags_without_trusted_input(self) -> None:
        # Live: a drag on a Chrome range input failed because Chrome would not attach the debugger,
        # and role=slider found nothing because range inputs were reported as textboxes.
        tools = self.tools("picker.html")
        slider = tools.find("Guests", role="slider")
        self.assertEqual(("input", "slider"), (slider["tag"], slider["role"]))
        moved = tools.act([{"type": "drag", "query": "Guests", "role": "slider", "dx": 120, "dy": 0}])
        self.assertTrue(moved["ok"], moved)
        step = moved["actions"][0]
        self.assertEqual("dom_range_value", step["activation_mode"])
        self.assertGreater(int(step["value"]), int(step["value_before"]))
        self.assertEqual(step["value"], tools.page.js("document.getElementById('guests').value"))
        back = tools.act([{"type": "drag", "query": "Guests", "dx": -400}])
        self.assertTrue(back["ok"], back)
        self.assertEqual("1", tools.page.js("document.getElementById('guests').value"))

    def test_scan_reads_a_long_thread_in_one_action(self) -> None:
        tools = self.tools("thread.html")
        scanned = tools.act([{"type": "scan", "max_items": 100}])
        self.assertTrue(scanned["ok"], scanned)
        texts = [item["text"] for item in scanned["actions"][0]["items"]]
        self.assertTrue(any("Comment number 29" in text for text in texts), texts[-3:])
        self.assertGreaterEqual(len(texts), 30)

    def test_read_returns_the_whole_page_with_lazy_content_and_no_hidden_text(self) -> None:
        tools = self.tools("read.html")
        read = tools.act([{"type": "read"}])
        self.assertTrue(read["ok"], read)
        result = read["actions"][0]
        text = result["text"]
        self.assertIn("Blue Fixture", text)
        self.assertIn("Opening paragraph with extra spaces.", text)
        self.assertIn("Lazy review 30 says the dry down is warm.", text, "content loaded by scrolling is read")
        for marker in ("SCRIPT_TEXT", "HIDDEN_TEXT", "ALSO_HIDDEN", "NOSCRIPT_TEXT", "TEMPLATE_TEXT"):
            self.assertNotIn(marker, text)
        self.assertTrue(result["complete"])
        self.assertIn("Blue Fixture", result["headings"])
        pieces, offset = [], 0
        for _ in range(50):
            part = tools.act([{"type": "read", "max_chars": 1000, "offset": offset, "load_more": False}])["actions"][0]
            pieces.append(part["text"])
            if part["complete"]:
                break
            offset = part["next_offset"]
        self.assertGreater(len(pieces), 1)
        self.assertEqual(text, "".join(pieces), "pieces join back into the whole page")


if __name__ == "__main__":
    unittest.main()
