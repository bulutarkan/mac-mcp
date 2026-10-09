from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_browser, tools_browser_agent as agent
from mcp_server.browser_tabs import TabTarget
from mcp_server.chrome_background_bridge import ChromeBackgroundBridge

EXTENSION = Path(__file__).resolve().parents[1] / "menu_app" / "ChromeVisualCompanion" / "background.js"


def _target():
    return TabTarget(browser="Google Chrome", window_index=1, tab_index=1, native_id="42", tab_handle="t",
                     title="Checkout", url="https://shop.example/checkout")


class FrameRoutingTests(unittest.TestCase):
    def test_scripts_in_a_frame_go_to_the_companion_with_the_selector(self) -> None:
        with tools_browser.frame_scope("pay.stripe.example"), \
                patch.object(tools_browser.chrome_background_bridge, "is_connected", return_value=True), \
                patch.object(tools_browser.chrome_background_bridge, "request_execute_js", return_value="b2s=") as send:
            self.assertEqual("b2s=", tools_browser._execute_js_for_target("Google Chrome", "1", _target(), 10))
        self.assertEqual("pay.stripe.example", send.call_args.kwargs["frame"])
        self.assertIsNone(tools_browser.current_frame_scope())

    def test_frames_need_chrome_with_the_companion(self) -> None:
        for browser, connected in (("Safari", True), ("Google Chrome", False)):
            with self.subTest(browser=browser), tools_browser.frame_scope("pay.example"), \
                    patch.object(tools_browser.chrome_background_bridge, "is_connected", return_value=connected):
                with self.assertRaises(HTTPException) as ctx:
                    tools_browser._execute_js_for_target(browser, "1", _target(), 10)
                self.assertEqual("frame_unsupported", ctx.exception.detail["error"])

    def test_bridge_requires_the_frames_feature(self) -> None:
        bridge = ChromeBackgroundBridge()
        bridge._attach(object(), object(), features=["dialogs"])
        with patch.object(bridge, "_request") as sent:
            with self.assertRaises(HTTPException) as ctx:
                bridge.request_execute_js(7, "1", frame="pay.example")
        sent.assert_not_called()
        self.assertEqual("frames", ctx.exception.detail["feature"])
        bridge._attach(object(), object(), features=["frames"])
        with patch.object(bridge, "_request", return_value={"ok": True, "result": "x"}) as sent:
            bridge.request_execute_js(7, "1", frame="pay.example")
        self.assertEqual("pay.example", sent.call_args.args[1]["frame"])

    def test_each_frame_is_its_own_document_for_the_helper_cache(self) -> None:
        top = agent._document_key("Google Chrome", _target())
        with tools_browser.frame_scope("pay.example"):
            inner = agent._document_key("Google Chrome", _target())
        self.assertNotEqual(top, inner)

    def test_wrappers_report_the_frame_and_reject_visual_capture(self) -> None:
        with patch.object(agent, "_browser_observe_impl",
                          side_effect=lambda *a: {"ok": True, "url": "https://pay.example/card",
                                                  "seen_frame": tools_browser.current_frame_scope()}):
            out = agent.browser_observe(None, "Google Chrome", tab_handle="t", frame="pay.example")
        self.assertEqual("pay.example", out["seen_frame"])
        self.assertEqual({"selector": "pay.example", "url": "https://pay.example/card"}, out["frame"])
        with self.assertRaises(HTTPException):
            agent.browser_observe(None, "Google Chrome", tab_handle="t", visual="viewport", frame="pay.example")
        # Observations are compact JSON text; the frame is added there too.
        import json as _json
        with patch.object(agent, "_browser_observe_impl", return_value='{"ok":true,"url":"https://pay.example/card"}'):
            text = agent.browser_observe(None, "Google Chrome", tab_handle="t", frame="pay.example")
        self.assertEqual("pay.example", _json.loads(text)["frame"]["selector"])
        with patch.object(agent, "_browser_act_impl", return_value={"ok": True, "progress": {"url": "https://pay.example/x"}}):
            acted = agent.browser_act(None, "Google Chrome", [{"type": "click", "query": "Pay"}], tab_handle="t",
                                      frame="pay.example")
        self.assertEqual("https://pay.example/x", acted["frame"]["url"])
        with patch.object(agent, "_browser_act_impl", return_value={"ok": True}) as act:
            agent.browser_act(None, "Google Chrome", [{"type": "click", "query": "Pay"}], tab_handle="t")
        self.assertNotIn("frame", act.return_value)

    def test_coordinate_input_is_refused_inside_a_frame(self) -> None:
        with tools_browser.frame_scope("pay.example"):
            point = agent._point_action(None, "Google Chrome", {"type": "click", "x": 1, "y": 1}, "o", 1, 1, "t")
            gesture = agent._gesture_action(None, "Google Chrome", "hover", {"element_id": "e_1"}, {}, 1, 1, "t",
                                            resolve_target=lambda a: ({}, None))
            companion = agent._ensure_visual_companion(None, "Google Chrome", 1, 1, "t")
        self.assertEqual("TRUSTED_INPUT_IN_FRAME", point["reason_code"])
        self.assertEqual("TRUSTED_INPUT_IN_FRAME", gesture["reason_code"])
        self.assertFalse(companion)


class CompanionFrameSourceTests(unittest.TestCase):
    def test_companion_resolves_same_process_and_out_of_process_frames(self) -> None:
        source = EXTENSION.read_text(encoding="utf-8")
        for needle in ("async function resolveFrame", "Target.setAutoAttach", "flatten: true",
                       "Runtime.executionContextCreated", "auxData.isDefault", "'frames'", "frame_ambiguous"):
            self.assertIn(needle, source)


if __name__ == "__main__":
    unittest.main()
