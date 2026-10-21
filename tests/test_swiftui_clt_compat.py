from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"


class SwiftUICommandLineToolsCompatibilityTests(unittest.TestCase):
    def test_state_uses_property_wrapper_alias_instead_of_sdk_macro_spelling(self) -> None:
        compat = (SOURCES / "SwiftUICompat.swift").read_text(encoding="utf-8")
        self.assertIn("typealias MacMCPState<Value> = SwiftUI.State<Value>", compat)

        bare_state = re.compile(r"(?<![A-Za-z0-9_])@State\b")
        offenders: list[str] = []
        for path in sorted(SOURCES.glob("*.swift")):
            if bare_state.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual([], offenders)

        menu = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        settings = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        # 9 before #200/#201 added three sizing properties; the session inspector adds one more; #20 the failed filter.
        self.assertEqual(14, menu.count("@MacMCPState "))
        # +1: the Apps pane's shortcut list (#195); +2: its app support list (#262) and Permissions Details (#246);
        # +1: why that list could not load (#19); +1: the memory review sheet (#60)
        self.assertEqual(24, settings.count("@MacMCPState "))

    def test_build_script_compiles_compatibility_source(self) -> None:
        script = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        compat = '"${SCRIPT_DIR}/Sources/SwiftUICompat.swift"'
        app_source = '"${SCRIPT_DIR}/Sources/MacMCPMenuApp.swift"'
        self.assertIn(compat, script)
        self.assertIn(app_source, script)
        self.assertLess(script.index(compat), script.index(app_source))


if __name__ == "__main__":
    unittest.main()
