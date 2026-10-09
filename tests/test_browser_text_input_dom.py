"""Opt-in Chromium tests for generated input JS and delayed acceptance checks."""
import base64
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from mcp_server.tools_browser_agent import (
    _selector_target_js,
    _verified_dom_action,
)


@unittest.skipUnless(os.environ.get("MAC_MCP_BROWSER_TESTS") == "1", "opt-in browser tests")
class BrowserTextInputDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1000, "height": 800})
        fixture = Path(__file__).parent / "fixtures/browser/input-regressions.html"
        self.page.set_content(fixture.read_text())
        self.addCleanup(self.page.close)

    def execute(self, _settings, _browser, js, *_args, **_kwargs):
        return json.loads(base64.b64decode(self.page.evaluate(js)).decode())

    def type_text(self, selector, text, clear=True):
        target = self.execute(None, None, _selector_target_js(selector))
        if not target["ok"]:
            return target
        with patch("mcp_server.tools_browser_agent._run_json_js", side_effect=self.execute):
            return _verified_dom_action(
                None, "Google Chrome", {
                    "type": "type", "element_id": target["element_id"],
                    "text": text, "clear": clear, "readiness_stable_ms": 0,
                    "readiness_timeout_s": 0.1,
                }, None, 1, None, "isolated-test-tab",
            )

    def test_native_replace_append_and_multiline_textarea(self):
        self.assertTrue(self.type_text("#native", "replacement")["ok"])
        self.assertTrue(self.type_text("#native", " 中文", clear=False)["ok"])
        self.assertEqual("replacement 中文", self.page.locator("#native").input_value())
        self.assertTrue(self.type_text("#area", "α² = 4\n第二行")["ok"])
        self.assertEqual("α² = 4\n第二行", self.page.locator("#area").input_value())

    def test_plain_contenteditable_multiline_append_and_clear(self):
        text = "first\n\nsecond 中文"
        self.assertTrue(self.type_text("#plain", text)["ok"])
        self.assertTrue(self.type_text("#plain", " appended", clear=False)["ok"])
        self.assertTrue(self.type_text("#plain", "")["ok"])
        self.assertEqual("", self.page.locator("#plain").text_content())

    def test_editor_selection_model_and_paste_are_used_once(self):
        self.page.evaluate("document.querySelector('#accepted').value='ghost expando'")
        result = self.type_text("#accepted", "<b>literal</b> 中文\nsecond")
        self.assertTrue(result["ok"], result)
        self.assertEqual("editor_paste_event", result["input_method"])
        self.assertEqual("dom_readback_verified", result["verification"])
        self.assertFalse(result["persistence_verified"])
        self.assertEqual(1, self.page.evaluate("window.__inputStats.accepted"))
        self.assertEqual("ghost expando", self.page.evaluate("document.querySelector('#accepted').value"))
        self.assertFalse(self.page.evaluate("!!new DOMParser().parseFromString(window.__lastPasteHTML,'text/html').querySelector('b')"))
        self.assertTrue(self.type_text("#accepted", " appended", clear=False)["ok"])
        self.assertEqual("<b>literal</b> 中文\nsecond appended", self.page.locator("#accepted").text_content())

    def test_rejected_and_reverted_edits_fail_without_second_paste(self):
        for selector in ("#rejected", "#reverted"):
            with self.subTest(selector=selector):
                result = self.type_text(selector, "must not succeed")
                self.assertFalse(result["ok"])
                self.assertEqual("input_not_applied", result["error"])
                self.assertFalse(result["automatic_retry"])
                self.assertFalse(result["foreground_fallback"])
                self.assertEqual("old", self.page.locator(selector).text_content())
                self.assertEqual(1, self.page.evaluate(f"window.__inputStats.{selector[1:]}"))

    def test_delayed_editor_acceptance_is_verified(self):
        self.page.evaluate("""() => {
            document.querySelector('#plain').addEventListener('paste', event => {
                event.preventDefault();const text=event.clipboardData.getData('text/plain');
                setTimeout(()=>{document.querySelector('#plain').textContent=text;},30);
            });
        }""")
        self.assertTrue(self.type_text("#plain", "delayed accepted")["ok"])

    def test_canceled_native_beforeinput_does_not_write(self):
        self.page.evaluate("document.querySelector('#native').addEventListener('beforeinput',e=>e.preventDefault())")
        result = self.type_text("#native", "must not write")
        self.assertFalse(result["ok"])
        self.assertEqual("input_canceled", result["error"])
        self.assertEqual("old", self.page.locator("#native").input_value())

    def test_native_checks_use_one_dispatch_without_delayed_readback(self):
        result = self.type_text("#phone", "5551234567")
        self.assertTrue(result["ok"], result)
        self.assertEqual("value_applied", result["verification"])
        # One readiness probe and one dispatch; no delayed readback calls.
        self.assertEqual(2, result["_js_calls"])
        self.assertFalse(result["persistence_verified"])
        self.page.wait_for_function("document.querySelector('#phone').value === '(555) 123-4567'")

    def test_synchronous_format_is_accepted_as_transformed(self):
        result = self.type_text("#sync-phone", "5551234567")
        self.assertTrue(result["ok"], result)
        self.assertEqual("value_transformed", result["verification"])
        self.assertEqual("(555) 123-4567", result["value"])

    def test_keyup_only_autocomplete_updates_once(self):
        result = self.type_text("#autocomplete", "query")
        self.assertTrue(result["ok"], result)
        self.assertEqual("query suggestion", self.page.locator("#suggestion").text_content())
        self.assertEqual(1, self.page.evaluate("window.__inputStats.keyup"))
        self.assertEqual("y", self.page.evaluate("window.__lastKeyup"))

    def test_native_synchronous_revert_or_truncation_is_refused(self):
        for replacement in ("old", "555123"):
            with self.subTest(replacement=replacement):
                self.page.evaluate("""replacement => {
                    const el=document.querySelector('#native');el.value='old';
                    el.oninput=()=>{el.value=replacement;};
                }""", replacement)
                result = self.type_text("#native", "5551234567")
                self.assertFalse(result["ok"], result)
                self.assertEqual("input_not_applied", result["error"])
                self.assertEqual(2, result["_js_calls"])

    def test_invalid_missing_ambiguous_and_readonly_targets_fail(self):
        for selector, error in (("[", "invalid_selector"), ("#missing", "target_not_found"), ("input", "selector_ambiguous")):
            with self.subTest(selector=selector):
                result = self.type_text(selector, "must not write")
                self.assertFalse(result["ok"])
                self.assertEqual(error, result["error"])
        result = self.type_text("#readonly", "must not write")
        self.assertFalse(result["ok"])
        self.assertEqual("ELEMENT_READONLY", result["reason_code"])

    def test_long_editor_uses_visible_region_but_full_occlusion_still_blocks(self):
        self.page.evaluate("document.querySelector('#plain').style.height='2500px'")
        self.assertTrue(self.type_text("#plain", "long editor replacement")["ok"])
        self.page.evaluate("""() => {
            const overlay=document.createElement('div');
            overlay.style.cssText='position:fixed;inset:0;background:white;z-index:2147483647';
            document.body.appendChild(overlay);
        }""")
        result = self.type_text("#plain", "covered replacement")
        self.assertFalse(result["ok"])
        self.assertEqual("ELEMENT_OCCLUDED", result["reason_code"])
