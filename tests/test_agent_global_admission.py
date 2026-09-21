from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import agent_admission as admission


class GlobalAdmissionCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "agents"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {
            "MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT": "4",
            "MAC_MCP_AGENT_PROVIDER_LIMIT": "4",
            "MAC_MCP_AGENT_PROVIDER_LIMIT_OPENCODE": "2",
            "MAC_MCP_AGENT_ADMISSION_TTL_S": "30",
            "MAC_MCP_AGENT_ADMISSION_QUEUE_LIMIT": "16",
        }, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def admit(self, suffix: str, *, provider: str = "opencode", resources=None, blocked_until=None):
        return admission.request_admission(
            self.root,
            request_id=f"req-{suffix}",
            team_id=f"team-{suffix}",
            task_id="task",
            provider=provider,
            resources=resources or [],
            provider_blocked_until=blocked_until,
        )

    # ASSURANCE: SEC-SCHED-001
    def test_three_teams_exceed_provider_limit_and_third_is_queued(self) -> None:
        one = self.admit("one")
        two = self.admit("two")
        three = self.admit("three")
        self.assertTrue(one["admitted"])
        self.assertTrue(two["admitted"])
        self.assertFalse(three["admitted"])
        self.assertEqual("provider_capacity", three["reason"])
        snap = admission.snapshot(self.root)
        self.assertEqual(2, snap["provider_active"]["opencode"])
        self.assertEqual(1, snap["queued_count"])

    def test_overlapping_workspace_write_claims_serialize(self) -> None:
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        first = self.admit("one", resources=[{"kind":"workspace","id":str(repo),"mode":"write"}])
        second = self.admit("two", resources=[{"kind":"path","id":str(repo / "src"),"mode":"write"}])
        self.assertTrue(first["admitted"])
        self.assertFalse(second["admitted"])
        self.assertEqual("resource_busy", second["reason"])
        blocker = second["details"]["blockers"][0]
        self.assertEqual("team-one", blocker["team_id"])

    def test_read_read_share_but_read_write_conflicts(self) -> None:
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        one = self.admit("one", resources=[{"kind":"workspace","id":str(repo),"mode":"read"}])
        two = self.admit("two", resources=[{"kind":"path","id":str(repo / "src"),"mode":"read"}])
        three = self.admit("three", resources=[{"kind":"file","id":str(repo / "src" / "x"),"mode":"write"}])
        self.assertTrue(one["admitted"])
        self.assertTrue(two["admitted"])
        self.assertFalse(three["admitted"])
        self.assertEqual("provider_capacity", three["reason"])
        # Free one provider slot; resource conflict must then be visible.
        admission.release(self.root, lease_id=two["lease_id"])
        three = self.admit("three", resources=[{"kind":"file","id":str(repo / "src" / "x"),"mode":"write"}])
        self.assertFalse(three["admitted"])
        self.assertEqual("resource_busy", three["reason"])

    def test_distinct_resources_run_in_parallel(self) -> None:
        a = Path(self.temp.name) / "a"; a.mkdir()
        b = Path(self.temp.name) / "b"; b.mkdir()
        one = self.admit("one", resources=[{"kind":"workspace","id":str(a),"mode":"write"}])
        two = self.admit("two", resources=[{"kind":"workspace","id":str(b),"mode":"write"}])
        self.assertTrue(one["admitted"])
        self.assertTrue(two["admitted"])

    def test_same_native_window_and_clipboard_are_exclusive(self) -> None:
        one = self.admit("one", resources=[
            {"kind":"native_window","id":"TextEdit:win_1","mode":"write"},
            {"kind":"clipboard","id":"system","mode":"write"},
        ])
        two = self.admit("two", provider="codex", resources=[
            {"kind":"native_window","id":"TextEdit:win_1","mode":"write"},
        ])
        three = self.admit("three", provider="codex", resources=[
            {"kind":"clipboard","id":"system","mode":"write"},
        ])
        self.assertTrue(one["admitted"])
        self.assertFalse(two["admitted"]); self.assertEqual("resource_busy", two["reason"])
        self.assertFalse(three["admitted"]); self.assertEqual("resource_busy", three["reason"])

    def test_provider_limit_override_reduces_chatgpt_concurrency(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_AGENT_PROVIDER_LIMIT_CHATGPT": "8"}, clear=False):
            first = admission.request_admission(
                self.root, request_id="req-chat-1", team_id="team-chat-1", task_id="task",
                provider="chatgpt", resources=[], provider_limit_override=1,
            )
            second = admission.request_admission(
                self.root, request_id="req-chat-2", team_id="team-chat-2", task_id="task",
                provider="chatgpt", resources=[], provider_limit_override=1,
            )
            self.assertTrue(first["admitted"])
            self.assertEqual(1, first["provider_limit"])
            self.assertFalse(second["admitted"])
            self.assertEqual("provider_capacity", second["reason"])
            self.assertEqual(1, second["details"]["provider_limit"])

    def test_provider_cooldown_queues_without_consuming_capacity(self) -> None:
        with patch.object(admission, "_now", return_value=100.0):
            queued = self.admit("cool", blocked_until=130.0)
            self.assertFalse(queued["admitted"])
            self.assertEqual("provider_cooldown", queued["reason"])
            snap = admission.snapshot(self.root)
            self.assertEqual(0, snap["global_active"])

    # ASSURANCE: SEC-SCHED-001
    def test_fifo_fairness_prevents_younger_conflicting_request_bypass(self) -> None:
        repo = Path(self.temp.name) / "repo"; repo.mkdir()
        holder = self.admit("holder", resources=[{"kind":"workspace","id":str(repo),"mode":"write"}])
        old = self.admit("old", resources=[{"kind":"workspace","id":str(repo),"mode":"write"}])
        young = self.admit("young", provider="codex", resources=[{"kind":"workspace","id":str(repo),"mode":"write"}])
        self.assertEqual("resource_busy", old["reason"])
        self.assertEqual("resource_busy", young["reason"])
        admission.release(self.root, lease_id=holder["lease_id"])
        # Younger request is now blocked by the older runnable queued request.
        young = self.admit("young", provider="codex", resources=[{"kind":"workspace","id":str(repo),"mode":"write"}])
        self.assertFalse(young["admitted"])
        self.assertEqual("fair_queue_resource", young["reason"])
        old = self.admit("old", resources=[{"kind":"workspace","id":str(repo),"mode":"write"}])
        self.assertTrue(old["admitted"])

    def test_older_resource_blocked_request_does_not_block_independent_younger(self) -> None:
        a = Path(self.temp.name) / "a"; a.mkdir()
        b = Path(self.temp.name) / "b"; b.mkdir()
        holder = self.admit("holder", resources=[{"kind":"workspace","id":str(a),"mode":"write"}])
        old = self.admit("old", resources=[{"kind":"workspace","id":str(a),"mode":"write"}])
        young = self.admit("young", provider="codex", resources=[{"kind":"workspace","id":str(b),"mode":"write"}])
        self.assertFalse(old["admitted"])
        self.assertTrue(young["admitted"])
        self.assertTrue(holder["admitted"])

    def test_cancel_queued_request_and_release_team(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_AGENT_PROVIDER_LIMIT_OPENCODE":"1"}, clear=False):
            one = self.admit("one")
            two = self.admit("two")
            self.assertFalse(two["admitted"])
            removed = admission.release(self.root, request_id="req-two")
            self.assertEqual(1, removed["queue"])
            removed_team = admission.release(self.root, team_id="team-one")
            self.assertEqual(1, removed_team["leases"])
            self.assertEqual(0, admission.snapshot(self.root)["global_active"])

    # ASSURANCE: SEC-SCHED-001
    def test_bind_heartbeat_and_ttl_prune_release_crashed_agent(self) -> None:
        with patch.object(admission, "_now", return_value=100.0):
            one = self.admit("one")
            lease = admission.bind_agent(self.root, one["lease_id"], "agt_one")
            self.assertEqual("agt_one", lease["agent_id"])
        with patch.object(admission, "_now", return_value=110.0):
            self.assertTrue(admission.heartbeat(self.root, agent_id="agt_one"))
            snap = admission.snapshot(self.root)
            self.assertEqual(1, snap["global_active"])
        with patch.object(admission, "_now", return_value=141.0):
            snap = admission.snapshot(self.root)
            self.assertEqual(0, snap["global_active"])
            self.assertEqual(1, len(snap["expired_leases"]))

    def test_browser_tab_claim_parity_is_exclusive(self) -> None:
        one = self.admit("one", resources=[{"kind":"browser_tab","id":"tab_123","mode":"write"}])
        two = self.admit("two", provider="codex", resources=[{"kind":"browser_tab","id":"tab_123","mode":"write"}])
        self.assertTrue(one["admitted"])
        self.assertFalse(two["admitted"])
        self.assertEqual("resource_busy", two["reason"])


if __name__ == "__main__":
    unittest.main()
