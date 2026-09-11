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
