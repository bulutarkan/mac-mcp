"""#79: a pale blue glow sweeps over the agent's browser tab, once per tool use, then fades.

Only for a tab the owner is not on (the server gives no frame for the tab on show). The panel sits
directly above the browser window, ignores the mouse and stays out of screen captures.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
STATE = (SOURCES / "AppState.swift").read_text(encoding="utf-8")
PULSE = (SOURCES / "BrowserTabPulse.swift").read_text(encoding="utf-8")

HARNESS = r'''
import AppKit

@main
struct TabPulseHarness {
    static func main() {
        let ax = CGRect(x: 881, y: 85, width: 290, height: 32)
        let out = BrowserTabPulseController.appKitFrame(ax: ax, primaryHeight: 956)
        print("frame", Int(out.minX), Int(out.minY), Int(out.width), Int(out.height))
        let total = BrowserTabPulseController.sweepSeconds * Double(BrowserTabPulseController.sweeps)
            + BrowserTabPulseController.fadeSeconds
        print("total_ms", Int((total * 1000).rounded()))
        print("sweeps", Int(BrowserTabPulseController.sweeps))
    }
}
'''


class BrowserTabPulseTests(unittest.TestCase):
    def test_frame_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, binary = Path(temp) / "harness.swift", Path(temp) / "harness"
            source.write_text(HARNESS, encoding="utf-8")
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "BrowserTabPulse.swift"), str(source), "-o", str(binary)],
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            out = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, out.returncode, out.stderr)
        result = dict(line.split(" ", 1) for line in out.stdout.splitlines())
        # AX y-down (top 85, 32 high) on a 956-point display is AppKit y-up 839.
        self.assertEqual("881 839 290 32", result["frame"])
        self.assertEqual("2", result["sweeps"])
        self.assertLessEqual(int(result["total_ms"]), 2000)
        self.assertGreaterEqual(int(result["total_ms"]), 1000)

    def test_panel_is_click_through_above_the_browser_and_out_of_captures(self) -> None:
        self.assertIn("panel.ignoresMouseEvents = true", PULSE)
        self.assertIn(".nonactivatingPanel", PULSE)
        self.assertIn("panel.order(.above, relativeTo: windowID)", PULSE)
        self.assertIn("panel.sharingType = capturableForTesting ? .readOnly : .none", PULSE)
        self.assertIn("band.isHidden = reduceMotion", PULSE)
        # A fade that ends after a newer pulse began leaves that pulse alone.
        self.assertIn("self.generation == mine", PULSE)

    def test_each_tool_use_pulses_once_and_only_for_a_tab_not_on_show(self) -> None:
        handler = STATE[STATE.index("private func handleToolActivityPayload("):STATE.index("/// \"Chat 2\"")]
        self.assertIn('if kind == "call_started" || kind == "call_finished" {\n            pulseBrowserTab(for: payload)', handler)
        pulse = STATE[STATE.index("private func pulseBrowserTab("):STATE.index("private static let tabPulseSkippedTools")]
        self.assertIn("settings.showBrowserTabPulse", pulse)
        self.assertIn("tabPulseEvents.insert(eventID).inserted", pulse)
        self.assertIn("now.timeIntervalSince(last) < 1.0", pulse)
        self.assertIn('"dashboard/api/browser/tab-frame"', pulse)
        self.assertIn("let target = response.target", pulse)
        models = (SOURCES / "AppModels+Dashboard.swift").read_text(encoding="utf-8")
        self.assertIn("guard ok, active != true, let frame, let windowID, windowID > 0 else { return nil }", models)

    def test_setting_defaults_on_and_has_a_switch(self) -> None:
        store = (SOURCES / "SettingsStore.swift").read_text(encoding="utf-8")
        self.assertIn("showBrowserTabPulse = current.tool_activity?.browser_tab_pulse ?? true", store)
        self.assertIn("browser_tab_pulse: showBrowserTabPulse", store)
        view = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn('Toggle("Glow on the agent\'s browser tab", isOn: browserTabPulseBinding)', view)
        self.assertIn('"${SCRIPT_DIR}/Sources/BrowserTabPulse.swift"', (ROOT / "menu_app/build_app.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
