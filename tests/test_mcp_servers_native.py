from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"


class McpServersPaneWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        self.pane = (SOURCES / "McpServersPane.swift").read_text(encoding="utf-8")

    def test_pane_is_a_settings_section_reachable_by_link(self) -> None:
        self.assertIn("    case apps\n    case mcpServers\n    case connections\n", self.view)
        self.assertIn('case .mcpServers: return "MCP Servers"', self.view)
        self.assertIn("case .mcpServers: McpServersPane(state: state)", self.view)
        self.assertIn('"mcpServers": "mcp servers', (SOURCES / "SettingsSearch.swift").read_text(encoding="utf-8"))
        self.assertIn('"mcp-servers": .mcpServers', (SOURCES / "MacMCPMenuApp.swift").read_text(encoding="utf-8"))
        self.assertIn('"${SCRIPT_DIR}/Sources/McpServersPane.swift"', (ROOT / "menu_app" / "build_app.sh").read_text())

    def test_pane_uses_the_dashboard_api_and_never_reads_secrets_back(self) -> None:
        self.assertIn('state.taskAPI("dashboard/api/mcp-servers")', self.pane)
        self.assertIn('"dashboard/api/mcp-servers/action"', self.pane)
        for secret_field in ('object["headers"]', 'object["env"]'):
            self.assertNotIn(secret_field, self.pane)

    def test_switches_have_accessible_names_and_removal_is_confirmed(self) -> None:
        self.assertNotIn('Toggle("", isOn', self.pane)
        self.assertIn('Toggle("Use \\(server.name)"', self.pane)
        self.assertIn("primaryButton: .destructive(Text(\"Remove\"))", self.pane)
        self.assertIn("LoadProblem(message: message)", self.pane)


if __name__ == "__main__":
    unittest.main()
