from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from mcp_server import observability, tools_ui
from mcp_server.observability import TelemetryManager, publish_native_pointer, set_live_telemetry
from tests._menu_sources import feature_files

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"


def _collect(fn) -> list:
    """Run fn() with a live telemetry subscriber and return what it published."""

    async def run() -> list:
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            set_live_telemetry(telemetry)
            queue = telemetry.subscribe()
            try:
                fn()
                await asyncio.sleep(0)
                events = []
                while not queue.empty():
                    events.append(queue.get_nowait())
                return events
            finally:
                telemetry.unsubscribe(queue)
                set_live_telemetry(None)

    return asyncio.run(run())


class NativePointerEventTests(unittest.TestCase):
    def test_event_is_live_only_and_carries_no_content(self) -> None:
        events = _collect(lambda: publish_native_pointer(
            action="Clicking", target="Button", pid=4242, point=(209.4, 170.06), press=True,
        ))
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("native_pointer", event["kind"])
        self.assertEqual((209.4, 170.1), (event["x"], event["y"]))
        self.assertEqual(("Clicking", "Button", 4242, True), (event["action"], event["target"], event["pid"], event["press"]))
        self.assertEqual(
            {"kind", "event_id", "session_id", "action", "target", "pid", "press", "timestamp", "x", "y"},
            set(event),
        )

    def test_without_a_running_server_nothing_is_published(self) -> None:
        set_live_telemetry(None)
        publish_native_pointer(action="Clicking", target="Button", pid=1, point=(1, 1))  # no error

    def test_observed_server_registers_its_telemetry(self) -> None:
        source = (ROOT / "mcp_server" / "observability.py").read_text(encoding="utf-8")
        init = source[source.index("class ObservedFastMCP(FastMCP):"):]
        init = init[:init.index("self.steering = steering or SteeringManager()")]
        self.assertIn("set_live_telemetry(telemetry)", init)
        self.assertIsNotNone(observability.publish_native_pointer)


class AnnounceNativePointerTests(unittest.TestCase):
    NODE = {"role": "AXButton", "title": "Send to Alice", "value": "secret", "position": {"x": 100, "y": 40, "width": 20, "height": 10}}

    def test_element_click_lands_on_the_element_centre_without_its_title(self) -> None:
        events = _collect(lambda: tools_ui._announce_native_pointer(
            "click", {"type": "click", "element_id": "w1/3"}, self.NODE, 77,
        ))
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual((110, 45), (event["x"], event["y"]))
        self.assertEqual(("Clicking", "Button", 77, True), (event["action"], event["target"], event["pid"], event["press"]))
        self.assertNotIn("Alice", repr(event))
        self.assertNotIn("secret", repr(event))

    def test_typing_moves_without_a_press_and_never_carries_the_text(self) -> None:
        node = dict(self.NODE, role="AXTextField")
        events = _collect(lambda: tools_ui._announce_native_pointer(
            "type", {"type": "type", "element_id": "w1/4", "text": "my password"}, node, 77,
        ))
        self.assertEqual(("Typing", "Text field", False), (events[0]["action"], events[0]["target"], events[0]["press"]))
        self.assertNotIn("password", repr(events))

    def test_coordinate_input_is_not_doubled_by_a_synthetic_cursor(self) -> None:
        events = _collect(lambda: tools_ui._announce_native_pointer(
            "click", {"type": "click", "x": 10, "y": 20}, None, 77,
        ))
        self.assertEqual([], events)
        self.assertEqual([], _collect(lambda: tools_ui._announce_native_pointer(
            "drag", {"type": "drag", "element_id": "w1/3"}, self.NODE, 77,
        )))

    def test_keys_update_the_label_in_place(self) -> None:
        events = _collect(lambda: tools_ui._announce_native_pointer("key", {"type": "key", "key": "a"}, None, 77))
        self.assertEqual("Pressing keys", events[0]["action"])
        self.assertNotIn("x", events[0])

    def test_unknown_roles_stay_generic(self) -> None:
        node = dict(self.NODE, role="AXCustomPrivateThing")
        events = _collect(lambda: tools_ui._announce_native_pointer("click", {"type": "click", "element_id": "w1/3"}, node, 77))
        self.assertEqual("", events[0]["target"])

    def test_mac_act_announces_before_performing(self) -> None:
        source = (ROOT / "mcp_server" / "tools_ui.py").read_text(encoding="utf-8")
        announce = source.index("_announce_native_pointer(\n                        action_type, resolved_action, node,")
        perform = source.index("ok, message = _perform_action(", announce)
        self.assertLess(perform - announce, 400)


HARNESS = r'''
import AppKit

func window(_ id: UInt32, pid: Int32, layer: Int = 0, _ rect: CGRect) -> [String: Any] {
    [
        kCGWindowNumber as String: NSNumber(value: id),
        kCGWindowOwnerPID as String: NSNumber(value: pid),
        kCGWindowLayer as String: NSNumber(value: layer),
        kCGWindowBounds as String: rect.dictionaryRepresentation as NSDictionary,
    ]
}

@main
struct DesktopCursorHarness {
    static func main() {
        typealias C = DesktopCursorOverlayController
        precondition(C.label(agent: "Agent 2", action: "Clicking", target: "Button") == "Agent 2 · Clicking Button")
        precondition(C.label(agent: nil, action: "Pressing keys", target: "") == "Pressing keys")
        precondition(C.label(agent: "", action: "Typing", target: "Text field") == "Typing Text field")

        // AX space is y-down from the primary display's top-left; AppKit frames are y-up.
        let primary = CGRect(x: 0, y: 0, width: 1470, height: 956)
        let above = CGRect(x: 0, y: 956, width: 1920, height: 1080)  // a display above the primary
        var hit = C.locate(CGPoint(x: 500, y: 400), screens: [primary, above])!
        precondition(hit.screen == primary && hit.local == CGPoint(x: 500, y: 400))
        hit = C.locate(CGPoint(x: 100, y: -1000), screens: [primary, above])!
        precondition(hit.screen == above && hit.local == CGPoint(x: 100, y: 80))

        // Front to back: the user's Finder over TextEdit (pid 7); a TextEdit palette at another layer.
        let windows = [
            window(1, pid: 3, CGRect(x: 470, y: 100, width: 540, height: 330)),
            window(2, pid: 7, layer: 3, CGRect(x: 150, y: 150, width: 40, height: 40)),
            window(3, pid: 7, CGRect(x: 140, y: 120, width: 760, height: 520)),
            window(4, pid: 7, CGRect(x: 0, y: 0, width: 1470, height: 900)),
        ]
        precondition(C.targetWindow(pid: 7, point: CGPoint(x: 600, y: 170), windows: windows)! == (3, 0),
                     "the front-most normal window of that app under the point, even when another app covers it")
        precondition(C.targetWindow(pid: 7, point: CGPoint(x: 1200, y: 800), windows: windows)! == (4, 0))
        precondition(C.targetWindow(pid: 7, point: CGPoint(x: 1300, y: 940), windows: windows) == nil)
        precondition(C.targetWindow(pid: 9, point: CGPoint(x: 600, y: 170), windows: windows) == nil)
        precondition(C.isOnScreen(3, in: windows) && !C.isOnScreen(42, in: windows))
        print("ok")
    }
}
'''


class DesktopCursorOverlayTests(unittest.TestCase):
    def test_helpers_place_the_cursor_with_the_driven_window(self) -> None:
        import shutil
        import subprocess

        if shutil.which("xcrun") is None:
            self.skipTest("xcrun unavailable")
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "Harness.swift"
            harness.write_text(HARNESS, encoding="utf-8")
            binary = Path(td) / "harness"
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "DesktopCursorOverlay.swift"), str(harness), "-o", str(binary)],
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertEqual("ok", run.stdout.strip())

    def test_overlay_is_not_a_window_to_accessibility(self) -> None:
        # The overlay belongs to Mac MCP's own process. Listed as a window it shifts the window
        # numbers mac_act resolved a moment before, so acting on Mac MCP's own Settings hit the
        # overlay ("Can't get UI element 1 of window 2", #221); VoiceOver would list it too.
        harness = r'''
import AppKit

@main struct PanelHarness {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let real = NSWindow(contentRect: NSRect(x: 100, y: 100, width: 200, height: 100), styleMask: [.titled],
                            backing: .buffered, defer: false)
        real.orderFront(nil)
        let overlay = DesktopCursorPanel(contentRect: NSRect(x: 0, y: 0, width: 300, height: 300),
                                         styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        overlay.orderFront(nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            let windows = app.accessibilityWindows() as? [NSWindow] ?? []
            precondition(windows.contains { $0 === real }, "the real window is listed")
            precondition(!windows.contains { $0 === overlay }, "the overlay must not be an accessibility window")
            print("PANEL_PASS")
            exit(0)
        }
        app.run()
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "Harness.swift"
            binary = Path(td) / "harness"
            source.write_text(harness, encoding="utf-8")
            built = subprocess.run(["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "DesktopCursorOverlay.swift"),
                                    str(source), "-o", str(binary)], capture_output=True, text=True, timeout=180)
            self.assertEqual(0, built.returncode, built.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("PANEL_PASS", ran.stdout)

    def test_overlay_is_stacked_with_the_target_and_stays_out_of_captures(self) -> None:
        source = (SOURCES / "DesktopCursorOverlay.swift").read_text(encoding="utf-8")
        self.assertIn("panel.order(.above, relativeTo: Int(targetWindow))", source)
        self.assertIn("p.level = .normal", source)
        self.assertNotIn(".screenSaver", source)
        self.assertIn("p.ignoresMouseEvents = true", source)
        self.assertIn("[.borderless, .nonactivatingPanel]", source)
        self.assertIn("override var canBecomeKey: Bool { false }", source)
        self.assertIn("p.sharingType = capturableForTesting ? .readOnly : .none", source)
        self.assertIn("var capturableForTesting = false", source)
        self.assertIn("NSWorkspace.didActivateApplicationNotification", source)
        self.assertIn("NSWorkspace.shared.accessibilityDisplayShouldReduceMotion", source)
        self.assertNotIn("orderFrontRegardless", source)

    def test_app_routes_pointer_events_behind_the_setting(self) -> None:
        state = (SOURCES / "AppState.swift").read_text(encoding="utf-8")
        handler = state[state.index("private func handleToolActivityPayload"):]
        handler = handler[:handler.index("let lists = Self.applyActivityStreamEvent")]
        self.assertIn('if kind == "native_pointer" {', handler)
        self.assertIn("settings.showDesktopCursor", handler)
        self.assertIn("DesktopCursorOverlayController.shared.show(pointer", handler)
        self.assertIn("DesktopCursorOverlayController.shared.finish(eventID: eventID)", state)

    def test_setting_defaults_on_and_round_trips(self) -> None:
        store = (SOURCES / "SettingsStore.swift").read_text(encoding="utf-8")
        self.assertIn("@Published var showDesktopCursor = true", store)
        self.assertIn("showDesktopCursor = current.tool_activity?.desktop_cursor ?? true", store)
        view = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn('Toggle("Agent cursor on the desktop", isOn: desktopCursorBinding)', view)
        self.assertIn("DesktopCursorOverlayController.shared.hideImmediately()", view)
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("Sources/DesktopCursorOverlay.swift", build)


class ActivitySettingsPersistenceTests(unittest.TestCase):
    """Settings are saved by merging into the file on disk, so a toggle must write its value
    both ways: an omitted key leaves the earlier choice in place."""

    def test_cursor_and_island_toggles_survive_a_reload_both_ways(self) -> None:
        harness = r'''
import Foundation

@main struct ActivitySettingsHarness {
    @MainActor static func main() throws {
        let store = SettingsStore()
        precondition(store.showDesktopCursor && !store.showLiveIsland)
        store.showDesktopCursor = false
        store.showLiveIsland = true
        try store.save()
        let off = SettingsStore()
        precondition(!off.showDesktopCursor && off.showLiveIsland)
        off.showDesktopCursor = true
        off.showLiveIsland = false
        try off.save()
        let back = SettingsStore()
        precondition(back.showDesktopCursor, "cursor turned back on did not stick")
        precondition(!back.showLiveIsland, "island turned off did not stick")
        print("ACTIVITY_SETTINGS_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "ActivitySettingsHarness.swift"
            binary = Path(td) / "activity-settings-harness"
            source.write_text(textwrap.dedent(harness), encoding="utf-8")
            cmd = [
                "xcrun", "swiftc", "-parse-as-library",
                *map(str, feature_files("AppState")),
                str(SOURCES / "AgentNotificationController.swift"),
                str(SOURCES / "SettingsStore.swift"),
                str(SOURCES / "ToolActivityBubbleController.swift"),
                str(SOURCES / "KeychainStore.swift"),
                str(source),
                "-framework", "SwiftUI", "-framework", "AppKit", "-framework", "Security",
                "-framework", "UserNotifications", "-framework", "Combine",
                "-o", str(binary),
            ]
            compiled = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            env = {
                **os.environ,
                "MAC_MCP_SETTINGS_PATH": str(Path(td) / "settings.json"),
                "MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE": "com.bulutarkan.mac-mcp.settings-test",
                "MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT": "test",
                "MAC_MCP_DECISIONS_KEYCHAIN_SERVICE": "com.bulutarkan.mac-mcp.settings-test",
                "MAC_MCP_DECISIONS_KEYCHAIN_ACCOUNT": "test",
            }
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20, env=env)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("ACTIVITY_SETTINGS_PASS", ran.stdout)


if __name__ == "__main__":
    unittest.main()
