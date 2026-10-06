import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import runtime_settings


class RuntimeSettingsTests(unittest.TestCase):
    def test_tool_toggle_is_read_live_from_disk(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                path.write_text(json.dumps({"experimental_tools": {"ask_user_voice": {"enabled": False}}}))
                self.assertFalse(runtime_settings.tool_enabled("ask_user_voice"))
                path.write_text(json.dumps({"experimental_tools": {"ask_user_voice": {"enabled": True}}}))
                self.assertTrue(runtime_settings.tool_enabled("ask_user_voice"))

    def test_voice_setting_returns_default_for_missing_or_invalid_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                self.assertEqual("auto", runtime_settings.voice_setting("language", "auto"))
                path.write_text("not-json")
                self.assertEqual(45, runtime_settings.voice_setting("timeout_s", 45))


    def test_steering_setting_reads_session_ttl_minutes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                self.assertEqual(10, runtime_settings.steering_setting("session_ttl_minutes", 10))
                path.write_text(json.dumps({"steering": {"session_ttl_minutes": 120}}))
                self.assertEqual(120, runtime_settings.steering_setting("session_ttl_minutes", 10))

    def test_tool_activity_setting_is_read_live_from_disk(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                self.assertFalse(runtime_settings.tool_activity_setting("require_descriptions", False))
                path.write_text(json.dumps({
                    "tool_activity": {
                        "show_bubble": True,
                        "require_descriptions": True,
                    }
                }))
                self.assertTrue(runtime_settings.tool_activity_setting("show_bubble", False))
                self.assertTrue(runtime_settings.tool_activity_setting("require_descriptions", False))

    def test_provider_settings_fail_closed_until_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                missing = runtime_settings.load_runtime_settings_state()
                self.assertEqual("missing", missing.status)
                self.assertFalse(runtime_settings.provider_enabled("opencode"))
                self.assertFalse(runtime_settings.provider_enabled("codex"))
                self.assertFalse(runtime_settings.provider_enabled("chatgpt"))

                path.write_text("not-json")
                invalid = runtime_settings.load_runtime_settings_state()
                self.assertEqual("invalid_json", invalid.status)
                self.assertFalse(runtime_settings.provider_enabled("opencode"))
                self.assertFalse(runtime_settings.provider_enabled("codex"))

                path.write_text("[]")
                non_object = runtime_settings.load_runtime_settings_state()
                self.assertEqual("root_not_object", non_object.status)
                self.assertFalse(runtime_settings.provider_enabled("opencode"))

                path.write_text(json.dumps({"server": {"port": 8765}}))
                self.assertFalse(runtime_settings.provider_enabled("opencode"))
                self.assertFalse(runtime_settings.provider_enabled("codex"))

                path.write_text(json.dumps({
                    "subagents": {
                        "providers": {
                            "opencode": {"enabled": False},
                            "codex": {"enabled": True},
                            "chatgpt": {
                                "enabled": True,
                                "binary_path": "/tmp/chatgpt-web",
                                "default_project": "Subagents",
                            },
                        }
                    }
                }))
                self.assertFalse(runtime_settings.provider_enabled("opencode"))
                self.assertTrue(runtime_settings.provider_enabled("codex"))
                self.assertTrue(runtime_settings.provider_enabled("chatgpt"))
                self.assertEqual("/tmp/chatgpt-web", runtime_settings.provider_setting("chatgpt", "binary_path"))
                self.assertEqual("Subagents", runtime_settings.provider_setting("chatgpt", "default_project"))

    def test_provider_settings_fail_closed_when_settings_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text("{}")
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}), \
                 patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                state = runtime_settings.load_runtime_settings_state()
                self.assertEqual("unreadable", state.status)
                self.assertEqual("PermissionError", state.error_type)
                self.assertFalse(runtime_settings.provider_enabled("opencode"))

    def test_provider_enabled_requires_boolean_enabled_field(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({
                "subagents": {"providers": {
                    "opencode": {"enabled": "yes"},
                    "codex": {},
                }}
            }))
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                self.assertFalse(runtime_settings.provider_enabled("opencode"))
                self.assertFalse(runtime_settings.provider_enabled("codex"))

    def test_keychain_lookup_uses_service_and_account_without_logging_secret(self):
        fake = type("Result", (), {"stdout": "secret-value\n"})()
        with patch.object(runtime_settings.subprocess, "run", return_value=fake) as run:
            value = runtime_settings.keychain_password()
        self.assertEqual("secret-value", value)
        args = run.call_args.args[0]
        self.assertIn("com.bulutarkan.mac-mcp", args)
        self.assertIn("groq-api-key", args)
        self.assertNotIn("secret-value", args)


if __name__ == "__main__":
    unittest.main()
