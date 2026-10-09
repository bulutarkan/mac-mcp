from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server import tools_agents as agents
from mcp_server.security_context import (
    SecurityContextManager, current_delegated_provenance, reset_delegated_provenance, set_delegated_provenance,
)

SEED = {
    "provenance_class": "tainted_untrusted_web", "origin": "https://evil.example", "tab_handle": "tab_1",
    "tab_title": "Evil", "inherited_from_session": "parent", "inheritance_hops": 1,
}


class SecurityContextSeedTests(unittest.TestCase):
    def test_tainted_parent_snapshot_and_child_seeded_on_first_touch(self) -> None:
        manager = SecurityContextManager()
        parent = manager.touch("agent:parent", "parent")
        with manager._lock:
            manager._mark_untrusted_provenance_locked(
                parent, origin="https://evil.example", tab_handle="tab_1", tab_title="Evil", reason="test",
            )
        snapshot = manager.provenance_snapshot("agent:parent", "parent")
        self.assertEqual("https://evil.example", snapshot["origin"])
        self.assertEqual(1, snapshot["inheritance_hops"])
        manager.set_child_seed_loader(lambda agent_id: snapshot if agent_id == "child" else None)
        child = manager.touch("agent:child", "child")
        self.assertEqual("tainted_untrusted_web", child.provenance_class)
        self.assertEqual("https://evil.example", child.provenance_origin)
        self.assertIn("delegated_context_transfer", child.taint_reasons)
        self.assertNotEqual("tainted_untrusted_web", manager.touch("agent:other", "other").provenance_class)

    def test_clean_parent_has_no_snapshot(self) -> None:
        manager = SecurityContextManager()
        manager.touch("agent:clean", "clean")
        self.assertIsNone(manager.provenance_snapshot("agent:clean", "clean"))


class SeedSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-seed-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for name, value in (("AGENTS_DIR", self.root / "agents"), ("TEAMS_DIR", self.root / "teams")):
            patcher = patch.object(agents, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_call_context_then_team_then_parent(self) -> None:
        token = set_delegated_provenance(SEED)
        try:
            self.assertEqual(SEED, agents._inherited_provenance_seed(team_id=None, parent_agent_id=None))
        finally:
            reset_delegated_provenance(token)
        self.assertIsNone(current_delegated_provenance())
        agents._write_team("team_t", {"team_id": "team_t", "inherited_provenance": SEED})
        self.assertEqual(SEED, agents._inherited_provenance_seed(team_id="team_t", parent_agent_id=None))
        agents._write_meta("agt_parent", {"agent_id": "agt_parent", "inherited_provenance": SEED})
        self.assertEqual(SEED, agents._inherited_provenance_seed(team_id=None, parent_agent_id="agt_parent"))
        self.assertEqual(SEED, agents.inherited_provenance_for("agt_parent"))
        self.assertIsNone(agents._inherited_provenance_seed(team_id=None, parent_agent_id=None))

    def test_the_seed_is_on_disk_before_the_worker_starts(self) -> None:
        settings_path = self.root / "settings.json"
        settings_path.write_text(json.dumps({"subagents": {"providers": {"opencode": {"enabled": True}}}}))
        seen_at_start: list = []

        def fake_worker(agent_id, worker_log, nonce=None):
            seen_at_start.append(agents._read_meta(agent_id).get("inherited_provenance"))
            raise OSError("no worker in tests")

        token = set_delegated_provenance(SEED)
        try:
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings_path)}), \
                 patch.object(agents, "_find_binary", return_value="/usr/bin/true"), \
                 patch.object(agents, "_validate_provider_model_selection", return_value={"validated": True}), \
                 patch.object(agents, "_spawn_worker_process", side_effect=fake_worker):
                try:
                    agents.spawn_agent(MagicMock(), provider="opencode", prompt="hi", cwd=str(self.root),
                                       access_mode="read_only", git_isolation="off")
                except HTTPException:
                    pass
        finally:
            reset_delegated_provenance(token)
        self.assertEqual([SEED], seen_at_start)


if __name__ == "__main__":
    unittest.main()
