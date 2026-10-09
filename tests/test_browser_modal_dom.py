import base64
import json
import os
import unittest

from mcp_server.tools_browser_agent import _find_candidates_js, _observe_js


@unittest.skipUnless(
    os.environ.get("MAC_MCP_BROWSER_TESTS") == "1",
    "set MAC_MCP_BROWSER_TESTS=1 to run browser DOM regression tests",
)
class BrowserModalDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep the optional dependency import inside the opt-in setup. A normal
        # test run skips this class without requiring Playwright to be installed;
        # an opted-in run reports a missing dependency as an error.
        from playwright.sync_api import sync_playwright

        cls._playwright = sync_playwright().start()
        try:
            cls._browser = cls._playwright.chromium.launch(headless=True)
        except Exception:
            cls._playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        browser = getattr(cls, "_browser", None)
        if browser:
            browser.close()
        playwright = getattr(cls, "_playwright", None)
        if playwright:
            playwright.stop()

    def setUp(self):
        self._context = self._browser.new_context(viewport={"width": 900, "height": 600})
        self.page = self._context.new_page()

    def tearDown(self):
        self._context.close()

    def _set_content(self, body):
        self.page.set_content(
            """
            <!doctype html>
            <style>
              html, body { margin: 0; width: 100%; height: 100%; }
              input { box-sizing: border-box; }
              .outside-editor {
                position: fixed; left: 20px; top: 20px;
                width: 220px; height: 32px; z-index: 1;
              }
              .floating {
                position: fixed; left: 320px; top: 20px;
                width: 320px; height: 220px;
                padding: 20px; box-sizing: border-box;
                background: white; border: 1px solid black;
              }
              .floating input {
                position: absolute; left: 20px; top: 20px;
                width: 260px; height: 32px;
              }
            </style>
            """
            + body
        )

    def _payload(self, script):
        raw = self.page.evaluate(script)
        return json.loads(base64.b64decode(raw).decode("utf-8"))

    def _find(self, query):
        return self._payload(_find_candidates_js(query, "textbox", None, 20, actionable_only=True))

    @staticmethod
    def _texts(payload):
        return [str(item.get("text") or "") for item in payload.get("elements", [])]

    def test_open_fixed_chat_with_aria_modal_false_is_non_modal(self):
        self._set_content(
            """
            <input class="outside-editor" aria-label="Outside editor">
            <div class="floating" role="dialog" aria-modal="false" data-state="open" aria-label="Open chat">
              <input aria-label="Chat editor">
            </div>
            """
        )

        found = self._find("Outside editor")
        self.assertFalse(found["modal_scope"]["active"])
        self.assertIn("Outside editor", self._texts(found))

        observed = self._payload(_observe_js("interactive", 20))
        self.assertFalse(observed["modal_scope"]["active"])
        self.assertIn("Outside editor", self._texts(observed))

    def test_role_only_fixed_dialog_is_non_modal(self):
        self._set_content(
            """
            <input class="outside-editor" aria-label="Outside editor">
            <div class="floating" role="dialog" aria-label="Role-only panel">
              <input aria-label="Panel editor">
            </div>
            """
        )

        found = self._find("Outside editor")
        self.assertFalse(found["modal_scope"]["active"])
        self.assertIn("Outside editor", self._texts(found))

    def test_native_show_is_non_modal(self):
        self._set_content(
            """
            <input class="outside-editor" aria-label="Outside editor">
            <dialog id="native-dialog" class="floating">
              <input aria-label="Native dialog editor">
            </dialog>
            """
        )
        self.page.evaluate("document.getElementById('native-dialog').show()")
        self.assertFalse(
            self.page.evaluate("document.getElementById('native-dialog').matches(':modal')")
        )

        found = self._find("Outside editor")
        self.assertFalse(found["modal_scope"]["active"])
        self.assertIn("Outside editor", self._texts(found))

    def test_radix_portal_without_aria_modal_keeps_outside_scope_blocked(self):
        for blocked in ('aria-hidden="true"', 'inert'):
            for portal in (False, True):
                with self.subTest(blocked=blocked, portal=portal):
                    dialog = '<div class="floating" role="dialog" data-state="open"><input aria-label="Radix editor"></div>'
                    if portal:
                        dialog = '<div id="portal">' + dialog + '</div>'
                    self._set_content(f'<main {blocked}><input class="outside-editor" aria-label="Blocked editor"></main>' + dialog + '<input aria-label="Unhidden outside editor">')
                    outside = self._find("Unhidden outside editor")
                    self.assertTrue(outside["modal_scope"]["active"])
                    self.assertNotIn("Unhidden outside editor", self._texts(outside))
                    inside = self._find("Radix editor")
                    self.assertIn("Radix editor", self._texts(inside))
                    observed = self._payload(_observe_js("interactive", 20))
                    self.assertTrue(observed["modal_scope"]["active"])
                    self.assertNotIn("Unhidden outside editor", self._texts(observed))

    def test_explicit_false_and_open_role_without_blocked_outside_are_nonmodal(self):
        for attributes, outside in (
            ('aria-modal="false" data-state="open"', '<main aria-hidden="true">hidden outside</main>'),
            ('data-state="open"', '<main>unblocked outside</main>'),
            ('data-state="closed"', '<main inert>blocked outside</main>'),
        ):
            with self.subTest(attributes=attributes):
                self._set_content(outside + f'<div class="floating" role="dialog" {attributes}><input aria-label="Panel editor"></div><input class="outside-editor" aria-label="Outside editor">')
                found = self._find("Outside editor")
                self.assertFalse(found["modal_scope"]["active"])
                self.assertIn("Outside editor", self._texts(found))

    def test_native_show_stays_nonmodal_with_blocked_sibling(self):
        self._set_content('<main aria-hidden="true">hidden outside</main><input class="outside-editor" aria-label="Outside editor"><dialog id="native-dialog" class="floating"><input aria-label="Native dialog editor"></dialog>')
        self.page.evaluate("document.getElementById('native-dialog').show()")
        self.assertFalse(self._find("Outside editor")["modal_scope"]["active"])

    def test_native_show_modal_blocks_outside_but_allows_inside(self):
        self._set_content(
            """
            <input class="outside-editor" aria-label="Outside editor">
            <dialog id="native-modal" class="floating">
              <input aria-label="Native modal editor">
            </dialog>
            """
        )
        self.page.evaluate("document.getElementById('native-modal').showModal()")
        self.assertTrue(
            self.page.evaluate("document.getElementById('native-modal').matches(':modal')")
        )

        outside = self._find("Outside editor")
        self.assertTrue(outside["modal_scope"]["active"])
        self.assertNotIn("Outside editor", self._texts(outside))

        inside = self._find("Native modal editor")
        self.assertTrue(inside["modal_scope"]["active"])
        self.assertIn("Native modal editor", self._texts(inside))

    def test_aria_modal_blocks_outside_but_allows_inside(self):
        self._set_content(
            """
            <input class="outside-editor" aria-label="Outside editor">
            <div class="floating" role="dialog" aria-modal="true" aria-label="ARIA modal">
              <input aria-label="ARIA modal editor">
            </div>
            """
        )

        outside = self._find("Outside editor")
        self.assertTrue(outside["modal_scope"]["active"])
        self.assertNotIn("Outside editor", self._texts(outside))

        inside = self._find("ARIA modal editor")
        self.assertTrue(inside["modal_scope"]["active"])
        self.assertIn("ARIA modal editor", self._texts(inside))

    def test_hidden_and_closed_modals_are_ignored(self):
        self._set_content(
            """
            <input class="outside-editor" aria-label="Outside editor">
            <div class="floating" aria-modal="true" aria-label="Hidden modal" style="display:none">
              <input aria-label="Hidden modal editor">
            </div>
            <div class="floating" role="dialog" aria-modal="true" data-state="closed" aria-label="Closed modal">
              <input aria-label="Closed modal editor">
            </div>
            """
        )

        found = self._find("Outside editor")
        self.assertFalse(found["modal_scope"]["active"])
        self.assertIn("Outside editor", self._texts(found))

    def test_nested_and_z_order_selection_stays_topmost(self):
        self._set_content(
            """
            <input class="outside-editor" aria-label="Outside editor">
            <div class="floating" aria-modal="true" style="z-index:10">
              <input aria-label="Outer modal editor">
              <div class="floating" aria-modal="true" style="left:20px; top:90px; z-index:1">
                <input aria-label="Nested modal editor">
              </div>
            </div>
            """
        )

        nested = self._find("Nested modal editor")
        self.assertIn("Nested modal editor", self._texts(nested))
        outer = self._find("Outer modal editor")
        self.assertNotIn("Outer modal editor", self._texts(outer))

        self._set_content(
            """
            <input class="outside-editor" aria-label="Outside editor">
            <div class="floating" aria-modal="true" style="z-index:10">
              <input aria-label="Lower modal editor">
            </div>
            <div class="floating" aria-modal="true" style="z-index:20">
              <input aria-label="Higher modal editor">
            </div>
            """
        )
        higher = self._find("Higher modal editor")
        self.assertIn("Higher modal editor", self._texts(higher))
        lower = self._find("Lower modal editor")
        self.assertNotIn("Lower modal editor", self._texts(lower))


if __name__ == "__main__":
    unittest.main()
