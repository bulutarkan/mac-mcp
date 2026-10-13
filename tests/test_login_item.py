"""The menu bar app becomes a login item only as the installed app (LoginItem.swift)."""
from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "menu_app" / "Sources" / "LoginItem.swift"

HARNESS = r'''
import Foundation

func check(_ condition: Bool, _ message: String) {
    if !condition { print("FAIL: \(message)"); exit(1) }
}

@main
struct Harness {
    static func main() {
        let home = URL(fileURLWithPath: "/Users/someone")
        let id = LoginItem.defaultBundleID
        check(LoginItem.applies(bundleURL: URL(fileURLWithPath: "/Users/someone/Applications/Mac MCP.app"), bundleID: id, home: home), "installed in ~/Applications")
        check(LoginItem.applies(bundleURL: URL(fileURLWithPath: "/Applications/Mac MCP.app"), bundleID: id, home: home), "installed in /Applications")
        check(!LoginItem.applies(bundleURL: URL(fileURLWithPath: "/tmp/mac-mcp-build-test/Mac MCP.app"), bundleID: id, home: home), "test build")
        check(!LoginItem.applies(bundleURL: URL(fileURLWithPath: "/Users/someone/Applications/Mac MCP.app"), bundleID: "com.example.isolated", home: home), "isolated bundle id")
        check(!LoginItem.applies(bundleURL: URL(fileURLWithPath: "/Users/someone/Applications/Mac MCP.app"), bundleID: nil, home: home), "no bundle id")
        print("LOGIN_ITEM_PASS")
    }
}
'''


class LoginItemTests(unittest.TestCase):
    def test_only_the_installed_app_registers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "Harness.swift"
            binary = Path(td) / "login-item-harness"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            built = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-framework", "ServiceManagement",
                                    str(SOURCE), str(harness), "-o", str(binary)],
                                   capture_output=True, text=True, timeout=180)
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            self.assertIn("LOGIN_ITEM_PASS", run.stdout)

    def test_the_app_keeps_the_login_item_in_step_with_the_setting(self) -> None:
        app = (ROOT / "menu_app" / "Sources" / "MacMCPMenuApp.swift").read_text(encoding="utf-8")
        state = (ROOT / "menu_app" / "Sources" / "AppState.swift").read_text(encoding="utf-8")
        self.assertIn("LoginItem.sync(enabled: appState.settings.startAtLogin)", app)
        self.assertIn("syncLoginItem?(enabled)", state)
        self.assertIn("LoginItem.swift", (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
