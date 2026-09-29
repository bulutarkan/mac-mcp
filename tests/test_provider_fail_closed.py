from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import mcp_server.tools_agents as agents


class ProviderFailClosedIntegrationTests(unittest.TestCase):
    def test_corrupt_settings_hides_catalog_blocks_spawn_and_skips_versions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-provider-corrupt-") as td:
            settings = Path(td) / "settings.json"
            settings.write_text("{broken", encoding="utf-8")
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=False), \
                 patch.object(agents, "_find_binary", return_value="/tmp/fake-provider"), \
                 patch.object(agents, "_version") as version:
                overview = agents.provider_overview()
                self.assertTrue(overview["ok"])
                self.assertTrue(all(not row["enabled"] for row in overview["providers"]))
                self.assertTrue(all(row["detected"] for row in overview["providers"]))
                self.assertTrue(all(row["version"] is None for row in overview["providers"]))
                version.assert_not_called()

                self.assertEqual({}, agents.agent_catalog(None)["providers"])
                with self.assertRaises(HTTPException) as ctx:
                    agents.spawn_agents(
                        None,
                        tasks=[{"id": "audit", "prompt": "read only"}],
                        provider="codex",
                    )
                self.assertEqual(403, ctx.exception.status_code)
                self.assertIn("provider_disabled", str(ctx.exception.detail))

    def test_missing_settings_blocks_spawn(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-provider-missing-") as td:
            settings = Path(td) / "settings.json"
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=False):
                self.assertEqual({}, agents.agent_catalog(None)["providers"])
                with self.assertRaises(HTTPException) as ctx:
                    agents.spawn_agents(
                        None,
                        tasks=[{"id": "audit", "prompt": "read only"}],
                        provider="opencode",
                    )
                self.assertEqual(403, ctx.exception.status_code)

    def test_explicit_valid_enabled_provider_remains_available_to_catalog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-provider-valid-") as td:
            settings = Path(td) / "settings.json"
            settings.write_text(json.dumps({
                "subagents": {
                    "providers": {
                        "opencode": {"enabled": False},
                        "codex": {"enabled": True},
                        "chatgpt": {"enabled": False},
                    }
                }
            }), encoding="utf-8")
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=False), \
                 patch.object(agents, "_find_binary", return_value=None):
                catalog = agents.agent_catalog(None, provider="codex")
                self.assertIn("codex", catalog["providers"])
                self.assertNotIn("opencode", catalog["providers"])

    def test_menu_settings_source_exposes_fail_closed_configuration_state(self) -> None:
        root = Path(__file__).resolve().parents[1]
        store = (root / "menu_app/Sources/SettingsStore.swift").read_text(encoding="utf-8")
        view = (root / "menu_app/Sources/SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn('settings.json could not be read or decoded', store)
        self.assertIn('settings.json is missing. Delegated providers are disabled', store)
        self.assertIn('providerConfigValid ? (providers["opencode"]?.enabled ?? false) : false', store)
        self.assertIn('providerSettingsLocked', store)
        self.assertIn('.disabled(settings.providerSettingsLocked)', view)
        self.assertIn('Repair settings.json before changing delegated providers.', view)


if __name__ == "__main__":
    unittest.main()
