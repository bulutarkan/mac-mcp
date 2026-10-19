from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import os
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP

from mcp_server import security
from mcp_server.security import load_settings

ENV = "MCP_SESSION_IDLE_TIMEOUT_S"


def _settings_with(value):
    env = {k: v for k, v in os.environ.items() if k != ENV}
    if value is not None:
        env[ENV] = value
    with patch.dict(os.environ, env, clear=True):
        return load_settings()


class SessionIdleTimeoutSettingTests(unittest.TestCase):
    def test_default_is_thirty_minutes(self) -> None:
        self.assertEqual(1800, _settings_with(None).session_idle_timeout_s)

    def test_custom_value_is_used(self) -> None:
        self.assertEqual(7200, _settings_with("7200").session_idle_timeout_s)

    def test_value_is_clamped_to_bounds(self) -> None:
        self.assertEqual(security.SESSION_IDLE_TIMEOUT_MIN_S, _settings_with("5").session_idle_timeout_s)
        self.assertEqual(security.SESSION_IDLE_TIMEOUT_MIN_S, _settings_with("-100").session_idle_timeout_s)
        self.assertEqual(security.SESSION_IDLE_TIMEOUT_MAX_S, _settings_with("9999999").session_idle_timeout_s)
        self.assertEqual(60, security.SESSION_IDLE_TIMEOUT_MIN_S)
        self.assertEqual(86400, security.SESSION_IDLE_TIMEOUT_MAX_S)

    def test_unparseable_value_falls_back_to_default(self) -> None:
        self.assertEqual(1800, _settings_with("soon").session_idle_timeout_s)
        self.assertEqual(1800, _settings_with("").session_idle_timeout_s)


class SessionManagerWiringTests(unittest.TestCase):
    def _managers_for(self, value):
        from mcp_server.main import create_app

        seen: list = []
        original = FastMCP.streamable_http_app

        def spy(mcp_self, *args, **kwargs):
            app = original(mcp_self, *args, **kwargs)
            seen.append(mcp_self.session_manager)
            return app

        env = {k: v for k, v in os.environ.items() if k != ENV}
        if value is not None:
            env[ENV] = value
        with patch.dict(os.environ, env, clear=True), patch.object(FastMCP, "streamable_http_app", spy):
            create_app()
        self.assertEqual(1, len(seen))
        return seen[0]

    def test_create_app_applies_default_to_session_manager(self) -> None:
        self.assertEqual(1800.0, self._managers_for(None).session_idle_timeout)

    def test_create_app_applies_configured_value_to_session_manager(self) -> None:
        self.assertEqual(14400.0, self._managers_for("14400").session_idle_timeout)

    def test_create_app_applies_clamped_value_to_session_manager(self) -> None:
        self.assertEqual(86400.0, self._managers_for("999999").session_idle_timeout)


if __name__ == "__main__":
    unittest.main()
