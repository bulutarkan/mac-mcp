from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import agent_worktrees, tools_agents


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class AgentWorktreeExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-agent-wt-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git("add", "a.txt", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=self.repo)
        self.base = git("rev-parse", "HEAD", cwd=self.repo)
        self.agents = self.root / "agents"
        patcher = patch.object(tools_agents, "AGENTS_DIR", self.agents)
        patcher.start()
        self.addCleanup(patcher.stop)

    def agent(self, agent_id: str, *, status: str = "completed", age_days: float = 30, edit: bool = False) -> Path:
        path = self.root / "worktrees" / agent_id
        branch = f"mac-mcp/{agent_id}"
        git("worktree", "add", "--quiet", "-b", branch, str(path), self.base, cwd=self.repo)
        if edit:
            (path / "a.txt").write_text("changed by the agent\n", encoding="utf-8")
        state = {
            "enabled": True, "status": "active", "repo_root": str(self.repo), "path": str(path),
            "branch": branch, "base_commit": self.base, "created_at": time.time() - age_days * 86400,
            "apply_status": "pending", "applied_snapshot_commit": None,
        }
        (self.agents / agent_id).mkdir(parents=True)
        (self.agents / agent_id / "meta.json").write_text(json.dumps({
            "agent_id": agent_id, "status": status, "worktree": state,
        }), encoding="utf-8")
        return path

    def meta(self, agent_id: str) -> dict:
        return json.loads((self.agents / agent_id / "meta.json").read_text(encoding="utf-8"))

    def test_only_finished_old_worktrees_with_nothing_to_review_are_removed(self) -> None:
        clean = self.agent("agent_clean")
        unreviewed = self.agent("agent_unreviewed", edit=True)
        running = self.agent("agent_running", status="running")
        young = self.agent("agent_young", age_days=1)

        result = tools_agents.expire_agent_worktrees()

        self.assertEqual(["agent_clean"], result["removed"])
        self.assertFalse(clean.exists())
        self.assertEqual("discarded", self.meta("agent_clean")["worktree"]["status"])
        self.assertTrue(self.meta("agent_clean")["worktree"]["expired"])
        for kept in (unreviewed, running, young):
            self.assertTrue(kept.exists(), kept.name)
        self.assertEqual("changed by the agent\n", (unreviewed / "a.txt").read_text(encoding="utf-8"))

    def test_retention_zero_disables_expiry(self) -> None:
        clean = self.agent("agent_clean")
        with patch.dict(os.environ, {"MAC_MCP_AGENT_WORKTREE_RETENTION_DAYS": "0"}):
            self.assertEqual("disabled", tools_agents.expire_agent_worktrees()["status"])
        self.assertTrue(clean.exists())

    def test_inventory_reports_size_age_and_unreviewed_work(self) -> None:
        self.agent("agent_clean")
        self.agent("agent_unreviewed", edit=True)
        meta = self.meta("agent_unreviewed")
        meta["worktree"]["pending_changes"] = True
        (self.agents / "agent_unreviewed" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        rows = {row["agent_id"]: row for row in agent_worktrees.retained_worktrees(self.agents)}
        self.assertEqual({"agent_clean", "agent_unreviewed"}, set(rows))
        self.assertTrue(rows["agent_unreviewed"]["pending_changes"])
        self.assertFalse(rows["agent_clean"]["pending_changes"])
        self.assertGreater(rows["agent_clean"]["bytes"], 0)
        self.assertGreater(rows["agent_clean"]["age_s"], 29 * 86400)

    def test_sweep_runs_only_in_the_managed_server(self) -> None:
        with patch.object(tools_agents.threading, "Thread") as thread:
            tools_agents._WORKTREE_SWEEP["last"] = 0.0
            tools_agents._maybe_expire_agent_worktrees()
            thread.assert_not_called()
            with patch.dict(os.environ, {"MAC_MCP_MANAGED_SERVER": "1"}):
                tools_agents._maybe_expire_agent_worktrees()
                tools_agents._maybe_expire_agent_worktrees()
            thread.assert_called_once()
        tools_agents._WORKTREE_SWEEP.update(last=0.0, running=False)


if __name__ == "__main__":
    unittest.main()
