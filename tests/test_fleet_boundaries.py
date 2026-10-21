"""#79: what another Mac with read-only access really can and cannot do here, shown by behaviour.

Each call goes the way a paired Mac's call goes: its credential is resolved to this Mac's policy context, and the
tool runs through ObservedFastMCP's policy gate. The tools are stand-ins under the real tools' names (so the real
risk entries apply) that count every side effect they would have made: a refused call must leave the count at 0.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import external_mcp, fleet, policy
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.scoped_auth import resolve_request_identity
from mcp_server.security import load_settings

B_ID, A_ID = "mac_bbbbbbbbbbbbbbbb", "mac_aaaaaaaaaaaaaaaa"
EXTERNAL = "notion__update_page"


class FleetBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = fleet.FleetStore(Path(self.temp.name) / "fleet")
        me = {"device_id": B_ID, "name": "Studio", "model": "Mac", "available": True}
        self.profile = "trusted"  # this Mac's own permission profile
        for patcher in (patch.object(fleet, "_STORE", self.store), patch.object(fleet, "identity", return_value=me),
                        patch("mcp_server.policy.permission_profile_name", side_effect=lambda: self.profile),
                        # An owner's MCP server tool that says it only reads (it may not).
                        patch.dict(policy.RISK_REGISTRY, {EXTERNAL: policy.RiskEntry(
                            EXTERNAL, external_mcp.FAMILY, frozenset({policy.Capability.READ, policy.Capability.NETWORK_ACCESS}),
                            False, True, None)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.effects: list[str] = []
        self.mcp = self.build()

    def build(self) -> ObservedFastMCP:
        mcp = ObservedFastMCP(name="fleet-boundary", telemetry=TelemetryManager(
            db_path=Path(self.temp.name) / "telemetry.sqlite3", max_events=100))
        effects = self.effects

        def counted(name: str):
            # Every argument the cases pass, so the stand-in accepts each tool's call.
            def tool(command: str = "", path: str = "", content: str = "", app: str = "", action: str = "",
                     method: str = "", url: str = "", mac: str = "", arguments: Optional[dict] = None,
                     prompt: str = "", access_mode: str = "", page: str = "") -> dict:
                effects.append(name)
                return {"ok": True}
            return tool

        for name in ("run_command", "write_file", "mac_app", "http_request", "fleet", "spawn_agent", "read_file", EXTERNAL):
            mcp.tool(name=name)(counted(name))

        @mcp.tool(name="computer_plan")
        async def computer_plan(steps: Optional[list] = None) -> dict:
            # The real plan runs each step through call_tool again; so does this one.
            return {"step": await mcp.call_tool("run_command", {"command": "touch /tmp/x"})}

        return mcp

    def credential(self, permission: str) -> str:
        made = self.store.request_access(self.store.issue_offer(permission)["code"], client_device_id=A_ID,
                                         client_name="Air", server_device_id=B_ID, source="s")
        self.store.decide(made["request_id"], allow=True)
        return self.store.poll_request(made["request_id"], made["poll_secret"])["credential"]

    def call(self, credential: str, tool: str, arguments: dict) -> bool:
        """True when the call ran; the policy context is the one a request with this credential gets."""
        _, context = resolve_request_identity(load_settings(), f"Bearer {credential}")
        token = policy.set_policy_context(context)
        try:
            asyncio.run(self.mcp.call_tool(tool, arguments))
            return True
        except Exception as exc:  # the gate refuses with a ToolError carrying profile_denied
            self.assertIn("profile_denied", str(exc))
            return False
        finally:
            policy.reset_policy_context(token)

    def test_read_only_changes_nothing_by_any_path(self) -> None:
        reader = self.credential("read_only")
        refused = [
            ("run_command", {"command": "touch /tmp/x"}),
            ("write_file", {"path": "/tmp/x", "content": "x"}),
            ("mac_app", {"app": "mail", "action": "send"}),
            ("http_request", {"method": "POST", "url": "https://example.test"}),
            ("fleet", {"action": "call", "mac": "Other", "tool": "run_command", "arguments": {"command": "true"}}),
            ("spawn_agent", {"prompt": "x", "access_mode": "read_only"}),
            (EXTERNAL, {"page": "x"}),  # its own word that it only reads is not taken from another Mac
        ]
        for tool, arguments in refused:
            with self.subTest(tool=tool):
                self.assertFalse(self.call(reader, tool, arguments))
        self.assertEqual([], self.effects)
        # A step nested in a plan meets the same gate: the plan runs, its write does not.
        self.assertFalse(self.call(reader, "computer_plan", {"steps": []}))
        self.assertEqual([], self.effects)
        # Reading still works.
        self.assertTrue(self.call(reader, "read_file", {"path": "/tmp/x"}))
        self.assertEqual(["read_file"], self.effects)

    def test_full_access_never_goes_past_this_macs_own_profile(self) -> None:
        full = self.credential("full")
        self.assertTrue(self.call(full, "run_command", {"command": "true"}))
        self.assertTrue(self.call(full, EXTERNAL, {"page": "x"}))
        self.effects.clear()
        self.profile = "read_only"  # this Mac's owner set it to Read Only
        self.assertFalse(self.call(full, "run_command", {"command": "true"}))
        self.assertFalse(self.call(full, EXTERNAL, {"page": "x"}))
        self.assertEqual([], self.effects)

    def test_the_owners_own_read_only_clients_keep_their_servers(self) -> None:
        # The rule is for other Macs; this Mac's own read-only clients are unchanged here (#16 covers them).
        context = policy.PolicyContext(profile="read_only", actor="authenticated")
        _, risk = policy.resolve_risk(EXTERNAL, {})
        self.assertTrue(policy.evaluate_call(context, risk).allowed)

    def test_removing_a_mac_ends_its_next_request(self) -> None:
        full = self.credential("full")
        self.assertTrue(self.call(full, "read_file", {"path": "/tmp/x"}))
        client = self.store.clients()[0]
        self.assertTrue(self.store.revoke_client(client["client_id"]))
        with self.assertRaises(HTTPException) as gone:
            resolve_request_identity(load_settings(), f"Bearer {full}")
        self.assertEqual(401, gone.exception.status_code)

    def test_a_read_only_caller_saves_no_recipe_draft(self) -> None:
        from mcp_server.main import _caller_may_write

        for profile, may in (("read_only", False), ("standard", True), ("trusted", True)):
            token = policy.set_policy_context(policy.PolicyContext(profile=profile, actor="fleet:Air"))
            try:
                self.assertEqual(may, _caller_may_write(), profile)
            finally:
                policy.reset_policy_context(token)


if __name__ == "__main__":
    unittest.main()
