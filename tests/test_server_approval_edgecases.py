from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server.policy import PolicyContext, permission_semantics, resolve_risk
from mcp_server.runtime_settings import server_approval_profile_setting
from mcp_server.security_context import SecurityContextManager


class ServerApprovalStartupTests(unittest.TestCase):
    def test_runtime_loader_preserves_profile_and_marks_corruption_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            settings_path = Path(td) / "settings.json"
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings_path)}, clear=False):
                self.assertEqual("off", server_approval_profile_setting())

                settings_path.write_text(
                    json.dumps({"security": {"server_approval_profile": "high_risk"}}),
                    encoding="utf-8",
                )
                self.assertEqual("high_risk", server_approval_profile_setting())

                settings_path.write_text("{broken", encoding="utf-8")
                self.assertEqual("__invalid__", server_approval_profile_setting())

                settings_path.write_text(json.dumps({"security": "bad"}), encoding="utf-8")
                self.assertEqual("__invalid__", server_approval_profile_setting())

                settings_path.write_text(
                    json.dumps({"security": {"server_approval_profile": 42}}),
                    encoding="utf-8",
                )
                self.assertEqual("__invalid__", server_approval_profile_setting())

    def test_main_uses_live_server_approval_profile_without_restart(self) -> None:
        main_source = (
            Path(__file__).resolve().parents[1] / "mcp_server" / "main.py"
        ).read_text(encoding="utf-8")
        self.assertIn("security_context = SecurityContextManager()", main_source)
        self.assertNotIn(
            "server_approval_profile=server_approval_profile_setting()",
            main_source,
        )

    def test_invalid_startup_profile_fails_closed_only_for_high_risk(self) -> None:
        manager = SecurityContextManager(server_approval_profile="__invalid__")
        context = PolicyContext(profile="trusted", actor="test")
        key = manager.identity_key(context, None)

        _, run_risk = resolve_risk("run_command", {"command": "echo blocked"})
        blocked = manager.evaluate(
            key=key,
            public_session_id="sess-invalid",
            tool="run_command",
            risk=run_risk,
            arguments={"command": "echo blocked"},
            profile="trusted",
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual("server_approval_config_invalid", blocked.code)

        _, read_risk = resolve_risk("get_system_info", {})
        safe = manager.evaluate(
            key=key,
            public_session_id="sess-invalid",
            tool="get_system_info",
            risk=read_risk,
            arguments={},
            profile="trusted",
        )
        self.assertTrue(safe.allowed)


class ServerApprovalConcurrencyTests(unittest.TestCase):
    def test_two_exact_actions_keep_independent_pending_grants(self) -> None:
        manager = SecurityContextManager(server_approval_profile="critical")
        context = PolicyContext(profile="trusted", actor="test")
        key = manager.identity_key(context, None)
        session_id = "sess-concurrent"
        _, risk = resolve_risk("run_command", {"command": "echo a"})

        first = manager.evaluate(
            key=key,
            public_session_id=session_id,
            tool="run_command",
            risk=risk,
            arguments={"command": "echo a"},
            profile="trusted",
        )
        second = manager.evaluate(
            key=key,
            public_session_id=session_id,
            tool="run_command",
            risk=risk,
            arguments={"command": "echo b"},
            profile="trusted",
        )
        self.assertFalse(first.allowed)
        self.assertFalse(second.allowed)
        self.assertNotEqual(first.request_id, second.request_id)

        manager.grant_escalation(
            session_id,
            "run_command",
            request_id=first.request_id,
        )
        allowed_first = manager.evaluate(
            key=key,
            public_session_id=session_id,
            tool="run_command",
            risk=risk,
            arguments={"command": "echo a"},
            profile="trusted",
        )
        still_blocked_second = manager.evaluate(
            key=key,
            public_session_id=session_id,
            tool="run_command",
            risk=risk,
            arguments={"command": "echo b"},
            profile="trusted",
        )
        self.assertTrue(allowed_first.allowed)
        self.assertEqual("server_risk_escalated", allowed_first.code)
        self.assertFalse(still_blocked_second.allowed)
        self.assertEqual("server_risk_approval_required", still_blocked_second.code)


class ServerApprovalSurfaceTests(unittest.TestCase):
    def test_invalid_semantics_identify_server_owned_fail_closed_gate(self) -> None:
        semantics = permission_semantics(
            "trusted",
            server_approval_profile="__invalid__",
        )
        approval = semantics["server_approval"]
        self.assertFalse(approval["config_valid"])
        self.assertEqual("server", approval["source"])
        self.assertEqual("deny", approval["headless_behavior"])
        self.assertEqual("deny", approval["timeout_behavior"])

    def test_native_and_docs_surface_server_approval_separately(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_state = (root / "menu_app/Sources/AppState.swift").read_text(encoding="utf-8")
        settings_view = (root / "menu_app/Sources/SettingsView.swift").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        terminology = (root / "docs/TERMINOLOGY.md").read_text(encoding="utf-8")

        self.assertIn("ServerApprovalInfo", app_state)
        self.assertIn("setServerApprovalProfile", app_state)
        self.assertIn('GroupBox("Server Approval")', settings_view)
        self.assertIn("Headless behavior", settings_view)
        self.assertIn("doublePromptGuidance", settings_view)
        self.assertIn("Remote callers cannot approve locally", settings_view)
        self.assertIn("optional approval overlay", readme)
        self.assertIn("client-supplied", readme)
        self.assertIn("optional Server Approval overlay", terminology)


if __name__ == "__main__":
    unittest.main()
