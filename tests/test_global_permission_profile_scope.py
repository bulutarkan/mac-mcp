from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import diagnostics, policy
from mcp_server.dashboard_routes import _persist_permission_profile, create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.policy import PolicyContext
from mcp_server.policy_scope import ResourceScope
from mcp_server.security import load_settings


DASHBOARD_TOKEN = "dashboard-profile-scope-test-token-0123456789"
DASHBOARD_AUTH = {"authorization": f"Bearer {DASHBOARD_TOKEN}"}


class GlobalPermissionProfileScopeTests(unittest.TestCase):
    def test_delegated_and_unknown_global_env_values_normalize_to_standard(self) -> None:
        for configured in ("developer", "browser_only", "totally_unknown"):
            with self.subTest(configured=configured), patch.dict(
                os.environ, {"MAC_MCP_PERMISSION_PROFILE": configured}, clear=False
            ):
                self.assertEqual(configured, policy.configured_permission_profile_name())
                self.assertEqual("standard", policy.permission_profile_name())
                context = policy.environment_policy_context(actor="test-global")
                self.assertEqual("standard", context.profile)

    def test_valid_global_env_values_remain_active(self) -> None:
        for configured in policy.GLOBAL_PROFILE_NAMES:
            with self.subTest(configured=configured), patch.dict(
                os.environ, {"MAC_MCP_PERMISSION_PROFILE": configured}, clear=False
            ):
                self.assertEqual(configured, policy.permission_profile_name())

    def test_semantics_exposes_global_and_delegated_profile_boundaries(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "developer"}, clear=False):
            payload = policy.permission_semantics()
        self.assertEqual("developer", payload["configured_profile"])
        self.assertEqual("delegated_only", payload["configured_profile_scope"])
        self.assertEqual("standard", payload["active_profile"])
        self.assertTrue(payload["profile_was_normalized"])
        self.assertEqual("developer", payload["normalized_from_profile"])
        self.assertEqual(
            ["trusted", "standard", "read_only"],
            payload["global_profile_names"],
        )
        self.assertIn("developer", payload["delegated_profile_names"])
        self.assertIn("browser_only", payload["delegated_profile_names"])
        self.assertEqual(
            ["trusted", "standard", "read_only"],
            [row["name"] for row in payload["profiles"]],
        )

    def test_persist_helper_rejects_delegated_profile_without_touching_env_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-profile-persist-") as td:
            env_file = Path(td) / ".env"
            original = "MCP_ALLOW_NO_AUTH=false\nMAC_MCP_PERMISSION_PROFILE=trusted\nKEEP=yes\n"
            env_file.write_text(original, encoding="utf-8")
            with self.assertRaises(ValueError):
                _persist_permission_profile("developer", env_file)
            self.assertEqual(original, env_file.read_text(encoding="utf-8"))

    def test_dashboard_rejects_delegated_global_profiles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-profile-route-") as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), DASHBOARD_TOKEN))
            with patch("mcp_server.dashboard_routes._persist_permission_profile") as persist, patch.dict(
                os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted"}, clear=False
            ):
                client = TestClient(app)
                for configured in ("developer", "browser_only"):
                    with self.subTest(configured=configured):
                        response = client.post(
                            "/dashboard/api/security/profile",
                            json={"profile": configured},
                            headers=DASHBOARD_AUTH,
                        )
                        self.assertEqual(400, response.status_code)
                        payload = response.json()
                        self.assertEqual("invalid_permission_profile", payload["error"])
                        self.assertEqual(
                            ["trusted", "standard", "read_only"],
                            payload["allowed_profiles"],
                        )
                        self.assertEqual("delegated_only", payload["profile_scope"])
                self.assertEqual("trusted", os.environ["MAC_MCP_PERMISSION_PROFILE"])
                persist.assert_not_called()

    def test_scoped_developer_policy_context_remains_supported(self) -> None:
        scope = ResourceScope(path_roots=("/tmp",), access_mode="workspace_write")
        context = PolicyContext(
            profile="developer",
            actor="agent:test",
            agent_id="agt_test",
            scope=scope,
        )
        _, risk = policy.resolve_risk("run_command", {"command": "pwd"})
        decision = policy.evaluate_profile(context.profile, risk)
        self.assertTrue(decision.allowed)
        self.assertEqual("developer", context.profile)
        self.assertIs(context.scope, scope)

    def test_doctor_warns_when_delegated_profile_is_configured_globally(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "developer"}, clear=False):
            row = diagnostics._check_permission_profile_scope().to_dict()
        self.assertEqual("warn", row["status"])
        self.assertEqual("GLOBAL_PROFILE_DELEGATED_ONLY", row["reason_code"])
        self.assertEqual("developer", row["details"]["configured_profile"])
        self.assertEqual("standard", row["details"]["active_profile"])
        self.assertTrue(row["details"]["normalized"])


if __name__ == "__main__":
    unittest.main()
