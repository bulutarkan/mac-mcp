"""The owner's iPhone through iPhone Mirroring: the owner's access choice, this Mac's profile coming first,
privacy, how actions reach the helper, and the Devices routes.

The helper itself (iphone_helper.swift) is checked live against a real iPhone; here it is replaced.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import iphone, privacy_policy, screen_lock
from mcp_server.fleet_routes import create_fleet_routes
from mcp_server.policy import Capability, resolve_risk

HELPER = Path(iphone.__file__).with_name("iphone_helper.swift").read_text()


class IPhoneCase(unittest.TestCase):
    def setUp(self) -> None:
        settings = Path(tempfile.mkdtemp()) / "settings.json"
        patcher = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile = "trusted"
        profile = patch("mcp_server.policy.permission_profile_name", side_effect=lambda: self.profile)
        profile.start()
        self.addCleanup(profile.stop)
        unlocked = patch.object(screen_lock, "is_screen_locked", return_value=False)
        unlocked.start()
        self.addCleanup(unlocked.stop)
        self.calls = []
        self.answer = {"ok": True, "connected": True, "width": 390, "height": 845, "hash": "00ff",
                       "image_base64": base64.b64encode(b"\xff\xd8jpeg").decode()}

        def fake_run(args, timeout_s=30.0, stdin=None):
            self.calls.append(args + ([json.loads(stdin)] if stdin else []))
            return dict(self.answer)
        helper = patch.object(iphone, "_run", side_effect=fake_run)
        helper.start()
        self.addCleanup(helper.stop)


class AccessTests(IPhoneCase):
    def test_read_only_is_the_default_and_the_owner_chooses(self) -> None:
        self.assertEqual("read_only", iphone.access_setting())
        for level in ("full", "off", "read_only"):
            iphone.set_access(level)
            self.assertEqual(level, iphone.access_setting())
        with self.assertRaises(iphone.IPhoneError):
            iphone.set_access("admin")

    def test_this_macs_profile_comes_first(self) -> None:
        iphone.set_access("full")
        self.assertEqual("full", iphone.effective_access()["effective"])
        self.profile = "read_only"
        access = iphone.effective_access()
        self.assertEqual(("full", "read_only", True), (access["granted"], access["effective"], access["limited_by_this_mac"]))

    def test_off_refuses_everything_but_status(self) -> None:
        iphone.set_access("off")
        self.assertEqual("iphone_access_off", iphone.run("see")["error"])
        self.assertEqual("iphone_access_off", iphone.run("tap", x=10, y=10)["error"])
        self.assertTrue(iphone.run("status")["ok"])
        self.assertEqual([["status"]], self.calls)

    def test_read_only_sees_but_never_touches(self) -> None:
        text, picture = iphone.run("see")
        self.assertEqual("read_only", json.loads(text)["access"])
        self.assertEqual(b"\xff\xd8jpeg", picture.data)
        for action, extra in (("tap", {"x": 5, "y": 5}), ("type", {"text": "hi"}), ("open_app", {"app": "Notes"}),
                              ("home", {}), ("connect", {})):
            result = iphone.run(action, **extra)
            self.assertEqual("iphone_read_only", result["error"], action)
        self.assertEqual([["see"]], self.calls)

    def test_full_access_on_a_read_only_mac_says_why_it_only_reads(self) -> None:
        iphone.set_access("full")
        self.profile = "read_only"
        result = iphone.run("tap", x=5, y=5)
        self.assertEqual("iphone_read_only", result["error"])
        self.assertIn("Read Only in Permissions & Safety", result["message"])


class ActionTests(IPhoneCase):
    def setUp(self) -> None:
        super().setUp()
        iphone.set_access("full")

    def test_single_actions_are_one_step_batches_in_phone_points(self) -> None:
        iphone.run("tap", x=120, y=480.5)
        iphone.run("scroll", x=195, y=500, dy=-300)
        iphone.run("type", text="Merhaba dünya")
        iphone.run("open_app", app="Hava Durumu", image=False, ocr=True)
        steps = [call[1]["steps"] for call in self.calls]
        self.assertEqual([[{"type": "tap", "x": 120, "y": 480.5}],
                          [{"type": "scroll", "x": 195, "y": 500, "dy": -300}],
                          [{"type": "type", "text": "Merhaba dünya"}],
                          [{"type": "open_app", "app": "Hava Durumu"}]], steps)
        self.assertEqual(["act"], self.calls[0][:1])
        self.assertEqual((False, True), (self.calls[-1][1]["image"], self.calls[-1][1]["ocr"]))

    def test_a_whole_job_goes_in_one_call(self) -> None:
        job = [{"type": "open_app", "app": "Safari"}, {"type": "tap_text", "text": "Search"},
               {"type": "type", "text": "hava durumu", "submit": True}, {"type": "wait_for_text", "text": "Hava"},
               {"type": "scroll", "dy": -500}, {"type": "home"}]
        iphone.run("act", steps=job)
        self.assertEqual(1, len(self.calls))
        self.assertEqual(job, self.calls[0][1]["steps"])

    def test_a_batch_that_stops_part_way_shows_where(self) -> None:
        self.answer = {"ok": False, "reason_code": "IPHONE_TEXT_NOT_FOUND", "error": "Step 2 failed: “Go” is not on the iPhone screen.",
                       "done": 1, "failed": {"step": 1}, "image_base64": self.answer["image_base64"], "hash": "01"}
        text, picture = iphone.run("act", steps=[{"type": "home"}, {"type": "tap_text", "text": "Go"}])
        body = json.loads(text)
        self.assertEqual(("iphone_text_not_found", 1, 1), (body["error"], body["done"], body["failed"]["step"]))
        self.assertIn("Step 2 failed", body["message"])
        self.assertTrue(picture.data)

    def test_reading_part_of_the_screen_and_several_texts_at_once(self) -> None:
        self.answer = {"ok": True, "matches": {}}
        iphone.run("read", region="footer")
        iphone.run("find", texts=["Ozan", "Send"], region={"top": 600})
        iphone.run("act", steps=[{"type": "tap_text", "text": "Ayşe", "region": {"top": 120}},
                                 {"type": "expect_text", "text": "Ayşe", "region": "header"},
                                 {"type": "expect_text", "text": "Error", "absent": True}], ocr=True, region="bottom")
        self.assertEqual(["read", "--region", "footer"], self.calls[0])
        self.assertEqual(["find", "--texts", '["Ozan", "Send"]', "--region", '{"top": 600.0}'], self.calls[1])
        batch = self.calls[2][1]
        self.assertEqual(({"top": 120.0}, "header", "bottom"), (batch["steps"][0]["region"], batch["steps"][1]["region"], batch["region"]))

    def test_open_taps_a_row_again_only_when_asked(self) -> None:
        iphone.run("act", steps=[{"type": "tap_text", "text": "Automator", "open": True}, {"type": "tap", "x": 1, "y": 1, "open": False}])
        self.assertEqual(True, self.calls[0][1]["steps"][0]["open"])
        for bad in ({"type": "tap", "x": 1, "y": 1, "open": "yes"}, {"type": "home", "open": True}):
            self.assertEqual("iphone_argument_invalid", iphone.run("act", steps=[bad])["error"], bad)
        self.assertEqual(1, len(self.calls))

    def test_regions_are_checked_before_anything_runs(self) -> None:
        for bad in ("left half", {"middle": 3}, {"top": "x"}, ["top"]):
            self.assertEqual("iphone_argument_invalid", iphone.run("read", region=bad)["error"], bad)
            self.assertEqual("iphone_argument_invalid", iphone.run(
                "act", steps=[{"type": "expect_text", "text": "a", "region": bad}])["error"], bad)
        self.assertEqual("iphone_argument_invalid", iphone.run("act", steps=[{"type": "expect_text"}])["error"])
        self.assertEqual([], self.calls)

    def test_missing_or_bad_arguments_never_reach_the_iphone(self) -> None:
        for action, extra in (("tap", {"x": 5}), ("swipe", {"x": 1, "y": 1}), ("type", {}), ("key", {}),
                              ("open_app", {}), ("tap", {"x": "left", "y": 2}), ("wipe", {}), ("find", {}),
                              ("act", {"steps": []}), ("act", {"steps": [{"type": "explode"}]}),
                              ("act", {"steps": [{"type": "type", "text": "two\nlines"}]}),
                              ("act", {"steps": [{"type": "home"}] * 41}),
                              ("act", {"steps": [{"type": "home"}, {"type": "tap_text"}]})):
            self.assertEqual("iphone_argument_invalid", iphone.run(action, **extra)["error"], (action, extra))
        self.assertEqual([], self.calls)

    def test_an_unchanged_screen_costs_no_image(self) -> None:
        self.answer = {"ok": True, "unchanged": True, "hash": "00ff", "width": 390, "height": 845}
        result = iphone.run("see", previous_hash="00ff")
        self.assertIsInstance(result, str)
        self.assertTrue(json.loads(result)["unchanged"])
        self.assertEqual(["see", "--previous", "00ff"], self.calls[-1])

    def test_helper_refusals_come_back_with_their_reason(self) -> None:
        self.answer = {"ok": False, "reason_code": "IPHONE_NOT_CONNECTED", "error": "iPhone in Use"}
        result = iphone.run("tap", x=1, y=1)
        self.assertEqual("iphone_not_connected", result["error"])

    def test_a_locked_mac_screen_is_said_plainly(self) -> None:
        with patch.object(screen_lock, "is_screen_locked", return_value=True):
            self.assertEqual("mac_screen_locked", iphone.run("see")["error"])
        self.assertEqual([], self.calls)


class PrivacyAndPolicyTests(IPhoneCase):
    def test_pausing_observation_stops_the_iphone_too(self) -> None:
        self.assertIn("iphone", privacy_policy.PAUSED_TOOLS)

    def test_excluding_iphone_mirroring_keeps_agents_off_it(self) -> None:
        excluded = dict(privacy_policy.policy(fresh=True), excluded_apps=["iphone mirroring"])
        with patch.object(privacy_policy, "policy", return_value=excluded):
            with self.assertRaises(privacy_policy.PrivacyBlocked):
                iphone.run("see")
        self.assertEqual([], self.calls)

    def test_seeing_reads_and_touching_acts(self) -> None:
        _, see = resolve_risk("iphone", {"action": "see"})
        self.assertEqual({Capability.READ, Capability.NATIVE_ACCESSIBILITY}, set(see.capabilities))
        self.assertFalse(see.destructive)
        for arguments in ({"action": "tap"}, {"action": "type"}, {"action": "act", "steps": []},
                          {"action": "see", "steps": [{"type": "tap", "x": 1, "y": 1}]}):
            _, act = resolve_risk("iphone", arguments)
            action = arguments
            self.assertIn(Capability.UI_ACTION, act.capabilities, action)
            self.assertTrue(act.destructive, action)


class RouteTests(IPhoneCase):
    def setUp(self) -> None:
        super().setUp()
        self.answer = {"ok": True, "open": True, "running": True}
        self.client = TestClient(Starlette(routes=create_fleet_routes(lambda request: None)))

    def test_the_devices_pane_reads_and_sets_access(self) -> None:
        body = self.client.get("/dashboard/api/iphone").json()
        self.assertEqual((True, "read_only"), (body["mirroring"]["open"], body["access"]["granted"]))
        changed = self.client.post("/dashboard/api/iphone/access", json={"access": "full"}).json()
        self.assertEqual("full", changed["access"]["granted"])
        self.assertEqual(400, self.client.post("/dashboard/api/iphone/access", json={"access": "root"}).status_code)
        self.assertEqual("full", iphone.access_setting())


class ToolInvokeImageTests(unittest.TestCase):
    def test_pictures_stay_pictures_through_tool_invoke(self) -> None:
        import asyncio

        from mcp.types import ImageContent, TextContent

        from mcp_server.main import _invoke_registered_tool

        class FakeMCP:
            class _tool_manager:
                @staticmethod
                def get_tool(name):
                    return object()

            async def call_tool(self, name, arguments):
                return [TextContent(type="text", text=json.dumps({"ok": True, "width": 390})),
                        ImageContent(type="image", data="AAAA", mimeType="image/jpeg")]

        result = asyncio.run(_invoke_registered_tool(FakeMCP(), "iphone", {"action": "see"}))
        self.assertIsInstance(result, list)
        wrapper = json.loads(result[0])
        self.assertEqual((True, "iphone", {"ok": True, "width": 390}), (wrapper["ok"], wrapper["tool"], wrapper["result"]))
        self.assertEqual("image", result[1].type)
        self.assertNotIn("AAAA", result[0])


class HelperSourceTests(unittest.TestCase):
    def test_a_closed_mirror_is_opened_without_taking_focus_and_waited_for(self) -> None:
        self.assertIn("configuration.activates = false", HELPER)
        self.assertIn('object["opened_mirroring"] = true', HELPER)
        self.assertIn("waitStill(mirror, limit: 3.0, minimum: 1.0)", HELPER)  # past the fade-in blur

    def test_opening_an_app_waits_for_the_screen_not_a_fixed_time(self) -> None:
        opening = HELPER[HELPER.index('case "open_app":'):HELPER.index('case "wait":')]
        self.assertIn("waitChange(", opening)
        self.assertNotIn("usleep(650_000)", opening)

    def test_a_second_tap_only_when_the_first_changed_nothing(self) -> None:
        self.assertIn("if let openAt, !changed {", HELPER)

    def test_text_is_read_only_where_asked(self) -> None:
        self.assertIn("request.regionOfInterest = interest", HELPER)
        self.assertIn('case "expect_text":', HELPER)

    def test_input_is_bounded_to_the_phone_screen_and_one_at_a_time(self) -> None:
        self.assertIn("IPHONE_POINT_OUTSIDE_SCREEN", HELPER)
        self.assertIn("flock(lock, LOCK_EX)", HELPER)
        self.assertIn("IPHONE_MIRRORING_NOT_FRONT", HELPER)  # input only once the mirror is in front
        self.assertIn("CGWarpMouseCursorPosition(cursor)", HELPER)  # the pointer goes back
        self.assertIn("kCGWindowIsOnscreen", HELPER)  # never the hidden welcome window

    def test_a_disconnected_mirror_takes_no_input(self) -> None:
        self.assertIn("IPHONE_NOT_CONNECTED", HELPER)


if __name__ == "__main__":
    unittest.main()
