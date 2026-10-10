"""#22: finished agents' local files expire (Settings > Subagents, default 30 days);
#24: Mac MCP's content stores are owner-only directories."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import private_storage, tools_agents

DAY = 86400.0
NOW = 2_000_000_000.0


class RetentionSettingTests(unittest.TestCase):
    def days(self, settings=None, env=""):
        with patch.dict(os.environ, {"MAC_MCP_AGENT_RETENTION_DAYS": env}), \
             patch.object(tools_agents, "load_runtime_settings", return_value=settings or {}):
            return tools_agents.agent_retention_days()

    def test_default_is_thirty_days_and_settings_or_env_change_it(self) -> None:
        self.assertEqual(30, self.days())
        self.assertEqual(7, self.days({"subagents": {"retention_days": 7}}))
        self.assertEqual(0, self.days({"subagents": {"retention_days": 0}}))  # never delete
        self.assertEqual(90, self.days({"subagents": {"retention_days": 7}}, env="90"))
        self.assertEqual(3650, self.days({"subagents": {"retention_days": 99999}}))
        self.assertEqual(30, self.days({"subagents": {"retention_days": "soon"}}))


class ExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.agents = self.tmp / "agents"
        self.teams = self.tmp / "agent_teams"
        self.agents.mkdir()
        self.teams.mkdir()
        patches = [
            patch.object(tools_agents, "AGENTS_DIR", self.agents),
            patch.object(tools_agents, "TEAMS_DIR", self.teams),
            patch.object(tools_agents, "agent_retention_days", return_value=30),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self, agent_id: str, status: str, age_days: float, **extra) -> None:
        path = self.agents / agent_id
        path.mkdir()
        (path / "prompt.txt").write_text("task")
        (path / "provider_state").mkdir()
        meta = {"status": status, "ended_at": NOW - age_days * DAY, **extra}
        (path / "meta.json").write_text(json.dumps(meta))

    def team(self, team_id: str, members, age_days: float) -> None:
        path = self.teams / team_id
        path.mkdir()
        (path / "meta.json").write_text(json.dumps({"agent_ids": members, "updated_at": NOW - age_days * DAY}))

    def test_only_finished_agents_past_retention_are_removed(self) -> None:
        self.agent("old_done", "completed", 45)
        self.agent("old_failed", "failed", 31)
        self.agent("recent_done", "completed", 3)
        self.agent("old_running", "running", 60)
        self.agent("old_open_worktree", "completed", 60, worktree={"enabled": True, "status": "ready"})
        self.agent("old_cleaned_worktree", "completed", 60, worktree={"enabled": True, "status": "cleaned"})
        result = tools_agents.expire_agent_history(now=NOW)
        self.assertEqual(["old_cleaned_worktree", "old_done", "old_failed"], sorted(result["removed"]))
        self.assertEqual({"old_open_worktree", "old_running", "recent_done"}, {p.name for p in self.agents.iterdir()})

    def test_members_of_a_running_team_are_kept_and_finished_teams_go_with_their_agents(self) -> None:
        self.agent("busy_a", "completed", 50, team_id="team_busy")
        self.agent("busy_b", "running", 50, team_id="team_busy")
        self.team("team_busy", ["busy_a", "busy_b"], 50)
        self.agent("done_a", "completed", 50, team_id="team_done")
        self.team("team_done", ["done_a"], 50)
        self.team("team_recent", ["gone"], 2)
        result = tools_agents.expire_agent_history(now=NOW)
        self.assertEqual(["done_a"], result["removed"])
        self.assertEqual(["team_done"], result["removed_teams"])
        self.assertTrue((self.agents / "busy_a").exists())
        self.assertTrue((self.teams / "team_recent").exists())

    def test_never_delete_keeps_everything(self) -> None:
        self.agent("ancient", "completed", 900)
        with patch.object(tools_agents, "agent_retention_days", return_value=0):
            result = tools_agents.expire_agent_history(now=NOW)
        self.assertEqual("disabled", result["status"])
        self.assertTrue((self.agents / "ancient").exists())

    def test_sweeps_run_only_in_the_managed_server(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_MANAGED_SERVER": ""}), \
             patch.object(tools_agents.threading, "Thread") as thread:
            tools_agents._maybe_expire_agent_history()
            tools_agents.start_agent_history_expiry()
        thread.assert_not_called()

    def test_startup_and_spawn_trigger_the_sweep(self) -> None:
        main = (Path(tools_agents.__file__).with_name("main.py")).read_text()
        managed = main[main.index('if os.getenv("MAC_MCP_MANAGED_SERVER") == "1":'):]
        self.assertIn("start_agent_history_expiry()", managed)
        self.assertIn("secure_content_stores(", managed)
        spawn = Path(tools_agents.__file__).read_text()
        self.assertIn("    _maybe_expire_agent_worktrees()\n    _maybe_expire_agent_history()\n", spawn)


class PrivateStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_store_directories_become_owner_only(self) -> None:
        state = self.tmp / "state"
        for name in ("dashboard", "memory", "cache"):
            (state / name).mkdir(parents=True)
            os.chmod(state / name, 0o755)
        agents = self.tmp / "agents"
        agents.mkdir()
        os.chmod(agents, 0o755)
        result = private_storage.secure_content_stores([agents], state_dir=state)
        for path in (state, state / "dashboard", state / "memory", state / "cache", agents):
            self.assertEqual(0o700, self.mode(path), path)
        self.assertIn(str(agents), result["changed"])
        self.assertEqual([], private_storage.secure_content_stores([agents], state_dir=state)["changed"])

    def test_missing_paths_and_symlinks_are_left_alone(self) -> None:
        target = self.tmp / "elsewhere"
        target.mkdir()
        os.chmod(target, 0o755)
        link = self.tmp / "link"
        link.symlink_to(target)
        self.assertFalse(private_storage.make_private(link))
        self.assertFalse(private_storage.make_private(self.tmp / "missing"))
        self.assertEqual(0o755, self.mode(target))

    def test_new_agent_and_team_directories_are_private(self) -> None:
        source = Path(tools_agents.__file__).read_text()
        self.assertIn("path.mkdir(parents=True, exist_ok=False)\n        make_private(AGENTS_DIR)\n        make_private(path)", source)
        self.assertIn("make_private(TEAMS_DIR)\n    make_private(path.parent)", source)


if __name__ == "__main__":
    unittest.main()
