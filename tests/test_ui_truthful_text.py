"""#245: what the app tells people matches what Mac MCP does: Private mode still keeps the security log,
an error from a running server does not say it is stopped, and tool payload estimates are not model tokens."""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"


def read(name: str) -> str:
    return (SOURCES / name).read_text(encoding="utf-8")


class TruthfulTextTests(unittest.TestCase):
    def test_private_mode_never_claims_nothing_is_kept(self) -> None:
        menu, state = read("MenuBarView.swift"), read("AppState.swift")
        for text in (menu, state):
            self.assertNotIn("keeps nothing about this work", text)
            self.assertNotIn("Private mode on: nothing is kept", text)
        self.assertIn("The security log still records tool names and outcomes.", menu)

    def test_errors_say_stopped_only_when_the_server_is_stopped(self) -> None:
        privacy = read("PrivacyPane.swift")
        self.assertNotIn("Is Mac MCP running?", privacy)
        self.assertIn('state.serverRunning ? "Couldn’t load Privacy settings. Try again."', privacy)
        self.assertIn('state.serverRunning ? "Couldn’t save this change. Try again."', privacy)

    def test_tool_payload_is_not_called_model_tokens(self) -> None:
        settings = read("SettingsView.swift")
        self.assertNotIn('Text("Payload Tokens")', settings)
        self.assertIn('Text("Tool data processed")', settings)
        self.assertIn("Not your AI app’s model tokens or bill.", settings)


class PlainSettingsTextTests(unittest.TestCase):
    """#249: choosing the bubble says what else it changes; the Agents pane uses plain words."""

    def test_bubble_discloses_the_tool_description_change(self) -> None:
        settings = read("SettingsView.swift")
        self.assertIn("It needs Require Tool Descriptions, which turns on with it", settings)
        self.assertIn("Bubble on, with Require Tool Descriptions. Reconnect your AI apps so they see it.", settings)

    def test_agents_pane_speaks_plainly(self) -> None:
        settings = read("SettingsView.swift")
        for jargon in ("CLI provider", "cannot be spawned", '"Detected"', '"Not detected"'):
            self.assertNotIn(jargon, settings)
        self.assertIn("Choose which AI tools on this Mac can take work from Mac MCP.", settings)
        self.assertIn('"Found on this Mac"', settings)


if __name__ == "__main__":
    unittest.main()
