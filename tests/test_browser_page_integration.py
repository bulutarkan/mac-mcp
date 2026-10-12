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
        deadline = time.monotonic() + 10
        while page.js("document.readyState") != "complete" and time.monotonic() < deadline:
            time.sleep(0.05)
        return page

    def first_actionable(self, page: Page, query: str, role=None) -> dict:
        found = page.payload(agent._find_candidates_js(query, role, None))
        matches = [e for e in found.get("elements", []) if e.get("actionable")]
        self.assertTrue(matches, f"nothing actionable matches {query!r}")
        return matches[0]

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

    def test_unconfirmed_post_reports_once_without_retrying(self) -> None:
        tools = self.tools("thread.html")
        result = tools.act([{"type": "post", "text": "Please fail this integration comment.", "timeout_s": 1.5}])
        self.assertFalse(result["ok"])
        post = result["actions"][0]
        self.assertEqual(("post_not_confirmed", True, None), (post["error"], post["submitted"], post["posted"]))
        self.assertEqual([0, 1], tools.page.js("[window.__posts,window.__clicks]"))

    def test_scan_reads_a_long_thread_in_one_action(self) -> None:
        tools = self.tools("thread.html")
        scanned = tools.act([{"type": "scan", "max_items": 100}])
        self.assertTrue(scanned["ok"], scanned)
        texts = [item["text"] for item in scanned["actions"][0]["items"]]
        self.assertTrue(any("Comment number 29" in text for text in texts), texts[-3:])
        self.assertGreaterEqual(len(texts), 30)

if __name__ == "__main__":
    unittest.main()
