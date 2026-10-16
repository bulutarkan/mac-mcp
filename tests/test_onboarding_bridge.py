"""The onboarding page talks to the app through a closed set of actions (OnboardingBridge.swift)."""
from __future__ import annotations

import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "menu_app" / "Sources" / "OnboardingBridge.swift"
ONBOARDING = ROOT / "menu_app" / "Onboarding"

HARNESS = r'''
import Foundation

func check(_ condition: Bool, _ message: String) {
    if !condition { print("FAIL: \(message)"); exit(1) }
}

@main
struct Harness {
    static func main() {
        check(OnboardingBridge.parse(["id": 1, "action": "status", "args": [:]]) == OnboardingMessage(id: 1, action: .status), "status")
        check(OnboardingBridge.parse(["id": 2, "action": "requestPermission", "args": ["permission": "accessibility"]])?.action == .requestPermission("accessibility"), "permission")
        check(OnboardingBridge.parse(["id": 3, "action": "requestPermission", "args": ["permission": "camera"]]) == nil, "unknown permission")
        check(OnboardingBridge.parse(["id": 4, "action": "connectConfig", "args": ["client": "codex"]])?.action == .connectConfig("codex"), "client")
        check(OnboardingBridge.parse(["id": 5, "action": "connectConfig", "args": ["client": "rm -rf"]]) == nil, "unknown client")
        check(OnboardingBridge.parse(["id": 12, "action": "connectApp", "args": ["client": "claude-desktop"]])?.action == .connectApp("claude-desktop"), "connect app")
        check(OnboardingBridge.parse(["id": 13, "action": "connectApp", "args": ["client": "../etc"]]) == nil, "connect unknown app")
        check(OnboardingBridge.parse(["id": 14, "action": "apps"])?.action == .apps, "apps")
        check(OnboardingBridge.parse(["id": 15, "action": "agents"])?.action == .agents, "agents")
        check(OnboardingBridge.parse(["id": 16, "action": "enableAgent", "args": ["provider": "codex"]])?.action == .enableAgent("codex"), "enable agent")
        check(OnboardingBridge.parse(["id": 17, "action": "enableAgent", "args": ["provider": "claude-code"]]) == nil, "agents use provider ids")
        check(OnboardingBridge.parse(["id": 18, "action": "enableAgent", "args": [:]]) == nil, "enable needs a provider")
        check(OnboardingBridge.parse(["id": 19, "action": "enableAgent", "args": ["provider": "kimi"]])?.action == .enableAgent("kimi"), "enable kimi")
        check(OnboardingBridge.parse(["id": 20, "action": "disableAgent", "args": ["provider": "grok"]])?.action == .disableAgent("grok"), "disable agent")
        check(OnboardingBridge.parse(["id": 21, "action": "disableAgent", "args": ["provider": "chatgpt"]]) == nil, "disable known providers only")
        check(OnboardingBridge.jsonResult("warning: x\n{\"ok\": true}\n")?["ok"] as? Bool == true, "json after warnings")
        check(OnboardingBridge.jsonResult("no json") == nil, "no json")
        check(OnboardingBridge.parse(["id": 6, "action": "copy", "args": ["text": String(repeating: "a", count: 5000)]]) == nil, "copy limit")
        check(OnboardingBridge.parse(["id": 7, "action": "copy", "args": ["text": "hello"]])?.action == .copy("hello"), "copy")
        check(OnboardingBridge.parse(["id": 8, "action": "runShell", "args": ["cmd": "ls"]]) == nil, "unknown action")
        check(OnboardingBridge.parse(["action": "status"]) == nil, "missing id")
        check(OnboardingBridge.parse(["id": 9, "action": "finish", "args": ["outcome": "skipped"]])?.action == .finish("skipped"), "finish")
        check(OnboardingBridge.parse(["id": 10, "action": "finish", "args": ["outcome": "whatever"]]) == nil, "finish outcome")
        check(OnboardingBridge.parse(["id": 11, "action": "openSettings", "args": ["section": "connections"]])?.action == .openSettings("connections"), "settings")
        check(OnboardingBridge.parse(["id": 22, "action": "setupBrowser", "args": ["browser": "chrome"]])?.action == .setupBrowser("chrome"), "set up chrome")
        check(OnboardingBridge.parse(["id": 23, "action": "setupBrowser", "args": ["browser": "safari"]])?.action == .setupBrowser("safari"), "set up safari")
        check(OnboardingBridge.parse(["id": 24, "action": "setupBrowser", "args": ["browser": "firefox"]]) == nil, "known browsers only")
        check(OnboardingBridge.systemSettingsURL("accessibility")?.absoluteString == "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility", "ax url")
        check(OnboardingBridge.systemSettingsURL("screen_recording")?.absoluteString == "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture", "screen url")
        let reply = OnboardingBridge.replyScript(id: 3, ok: false, result: nil, error: "Quote \" and </script>")
        check(reply.hasPrefix("window.macmcp.receive(") && reply.hasSuffix(");"), "reply shape")
        check(!reply.contains("</script>"), "reply escapes markup")
        print("BRIDGE_PASS")
    }
}
'''


class OnboardingBridgeTests(unittest.TestCase):
    def test_bridge_accepts_only_known_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "Harness.swift"
            binary = Path(td) / "bridge-harness"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            built = subprocess.run(["xcrun", "swiftc", "-parse-as-library", str(BRIDGE), str(harness), "-o", str(binary)],
                                   capture_output=True, text=True, timeout=180)
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            self.assertIn("BRIDGE_PASS", run.stdout)

    def test_page_is_local_only(self) -> None:
        html = (ONBOARDING / "index.html").read_text(encoding="utf-8")
        self.assertIn("default-src 'none'", html)
        self.assertIn("connect-src 'none'", html)
        # WKWebView does not load ES modules from file:// pages: classic scripts only.
        self.assertNotIn('type="module"', html)
        for script in ("steps.js", "onboarding.js"):
            self.assertNotRegex((ONBOARDING / script).read_text(encoding="utf-8"), r"(?m)^\s*(import|export) ")
        for path in ONBOARDING.rglob("*"):
            if path.suffix in {".html", ".css", ".js"}:
                self.assertIsNone(re.search(r"https?://(?!127\.0\.0\.1)", path.read_text(encoding="utf-8")), path.name)

    def test_the_build_bundles_the_page_and_links_webkit(self) -> None:
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("-framework WebKit", build)
        self.assertIn("Sources/OnboardingBridge.swift", build)
        self.assertIn("Sources/OnboardingWindowController.swift", build)
        self.assertIn('"${SCRIPT_DIR}/Onboarding"', build)
        self.assertIn('"${RESOURCES}/Assets/AppIcon.png"', build)

    def test_steps_logic_passes_node_tests(self) -> None:
        node = subprocess.run(["/usr/bin/env", "which", "node"], capture_output=True, text=True).stdout.strip()
        if not node:
            self.skipTest("node is not installed")
        # Files, not the folder: older Node versions do not accept a directory here.
        files = sorted(str(path) for path in (ONBOARDING / "tests").glob("*.test.mjs"))
        run = subprocess.run([node, "--test", *files], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
