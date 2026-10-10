"""#120: a DOM visual capture finishes with few bridge calls and no separate image read."""
from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

from mcp_server import tools_browser_agent as agent

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
DATA_URL = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode("ascii")
DONE = json.dumps({"status": "done", "meta": {"output_width": 800}, "data": DATA_URL})
RUNNING = json.dumps({"status": "running", "meta": {}})
STARTED = json.dumps({"ok": True, "status": "running"})


def _capture(browser, replies, *, companion=False):
    calls = []
    replies = iter(replies)

    def run(_browser, js, **kwargs):
        calls.append(js)
        if "__macMcpVisualCapture" in js and "delete window" in js:
            return "cleaned"
        return next(replies)

    with patch.object(agent, "_ensure_dom_rasterizer"), \
         patch.object(agent, "_wait_for_render_readiness", return_value={"ready": True}), \
         patch.object(agent.chrome_background_bridge, "is_connected", return_value=companion), \
         patch.object(agent, "cancellable_sleep"), \
         patch.object(agent, "_execute_js_unbounded", side_effect=run):
        image, error, meta = agent._capture_dom_visual_locked(browser, "viewport", None, 1, 1, "tab-1")
    return image, error, meta, calls


class CaptureStatusTests(unittest.TestCase):
    def test_polling_transport_gets_the_image_with_the_finished_status(self) -> None:
        image, error, meta, calls = _capture("Safari", [STARTED, RUNNING, DONE])
        self.assertIsNone(error)
        self.assertEqual(JPEG, image)
        self.assertEqual(2, meta["status_calls"])
        self.assertEqual(800, meta["output_width"])
        self.assertNotIn("data", meta)
        # start, two status polls, cleanup: no separate data-URL read.
        self.assertEqual(4, len(calls))
        self.assertIn("waitMs=0;", calls[1])

    def test_chrome_companion_waits_in_the_page_with_one_status_call(self) -> None:
        image, error, meta, calls = _capture("Google Chrome", [STARTED, DONE], companion=True)
        self.assertIsNone(error)
        self.assertEqual(JPEG, image)
        self.assertEqual(1, meta["status_calls"])
        self.assertNotIn("waitMs=0;", calls[1])
        self.assertIn("return new Promise", calls[1])
        self.assertEqual(3, len(calls))  # start, one awaited status, cleanup

    def test_a_transport_that_cannot_await_falls_back_to_polling(self) -> None:
        # Apple Events JavaScript returns no JSON for a promise; the next calls poll.
        image, error, meta, calls = _capture("Google Chrome", [STARTED, "", RUNNING, DONE], companion=True)
        self.assertIsNone(error)
        self.assertEqual(JPEG, image)
        self.assertEqual(3, meta["status_calls"])
        self.assertIn("waitMs=0;", calls[2])

    def test_status_script_without_wait_is_synchronous(self) -> None:
        js = agent._dom_capture_status_js("__macMcpVisualCapture_x", 0)
        self.assertIn("waitMs=0;", js)
        self.assertIn('if(s.status==="done")o.data=s.data||""', js)
        waiting = agent._dom_capture_status_js("__macMcpVisualCapture_x", 5000)
        self.assertIn("waitMs=5000;", waiting)


if __name__ == "__main__":
    unittest.main()


class CloneLoaderPatchTests(unittest.TestCase):
    """#159: the clone wait is driven by status calls, not by onload or page timers."""

    def test_rasterizer_waits_on_the_live_clone_and_exposes_a_pump(self) -> None:
        source = agent._dom_rasterizer_source()
        self.assertNotIn('var A=setInterval(function(){0<r.body.childNodes.length&&"complete"===r.readyState', source)
        self.assertEqual(1, source.count("window.__macMcpClonePump=__mcpCheck"))
        self.assertIn("B.contentWindow&&B.contentWindow.document||r", source)
        self.assertIn('"interactive"===s&&Date.now()-__mcpT0>%d' % agent._CLONE_INTERACTIVE_GRACE_MS, source)
        # The pump is released once the clone resolves so a later capture installs its own.
        self.assertIn("window.__macMcpClonePump=null,e(B)", source)

    def test_every_status_call_drives_the_clone_wait(self) -> None:
        for wait_ms in (0, 5000):
            js = agent._dom_capture_status_js("__macMcpVisualCapture_x", wait_ms)
            self.assertIn("window.__macMcpClonePump&&window.__macMcpClonePump()", js)
            self.assertLess(js.index("__macMcpClonePump()"), js.index("var s=window[key]"))

    def test_a_changed_vendor_build_fails_loudly(self) -> None:
        original = agent._DOM_RASTERIZER_RUNTIME.copy()
        agent._DOM_RASTERIZER_RUNTIME.clear()
        try:
            with patch.object(agent._DOM_RASTERIZER_PATH.__class__, "read_text", return_value="(function(){})()"):
                with self.assertRaises(Exception):
                    agent._dom_rasterizer_source()
        finally:
            agent._DOM_RASTERIZER_RUNTIME.clear()
            agent._DOM_RASTERIZER_RUNTIME.update(original)


class CompanionExclusionTests(unittest.TestCase):
    def test_the_visual_companion_overlay_is_never_captured(self) -> None:
        # Its full-screen host painted a gray box over Medium and its badge hid page controls.
        for mode in ("viewport", "full_page", "element"):
            js = agent._dom_capture_start_js(mode, "e1" if mode == "element" else None, "k")
            ignore = js[js.index("ignoreElements:function(el){"):]
            self.assertLess(ignore.index('el.id==="mac-mcp-visual-companion-root"'), ignore.index('if(mode!=="viewport")'))
        visual = (agent.Path(agent.__file__).resolve().parents[1] / "menu_app/BrowserVisualCompanion/visual.js").read_text()
        self.assertIn("const HOST_ID = 'mac-mcp-visual-companion-root';", visual)
