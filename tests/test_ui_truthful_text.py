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


if __name__ == "__main__":
    unittest.main()
