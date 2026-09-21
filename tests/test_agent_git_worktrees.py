from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_agents as agents
from mcp_server.agent_worktrees import (
    AgentWorktreeError,
    apply_worktree,
    cleanup_worktree,
    inspect_worktree,
    prepare_worktree,
    reuse_worktree,
    seed_worktree,
)
from mcp_server.policy_scope import ResourceScope


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("a0\n", encoding="utf-8")
    (repo / "b.txt").write_text("b0\n", encoding="utf-8")
    (repo / "user.txt").write_text("user0\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "nested.txt").write_text("nested0\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    return repo


class AgentGitWorktreeCoreTests(unittest.TestCase):
    # ASSURANCE: SEC-GIT-001
    def test_two_parallel_write_worktrees_are_isolated_and_disjoint_apply_preserves_dirty_main(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            (repo / "user.txt").write_text("user-dirty\n", encoding="utf-8")
            first = prepare_worktree(
                agent_id="agt_one", cwd=repo, path_roots=[str(repo)],
                mode="required", access_mode="workspace_write",
            )
            second = prepare_worktree(
                agent_id="agt_two", cwd=repo, path_roots=[str(repo)],
                mode="required", access_mode="workspace_write",
            )
            self.addCleanup(cleanup_worktree, first, force=True)
            self.addCleanup(cleanup_worktree, second, force=True)
            self.assertNotEqual(first["path"], second["path"])
            self.assertNotEqual(first["branch"], second["branch"])
            self.assertEqual("user-dirty\n", (repo / "user.txt").read_text())
            self.assertNotIn(".mac-mcp-worktrees", git(repo, "status", "--short", "--untracked-files=all"))

            Path(first["path"], "a.txt").write_text("agent-one\n", encoding="utf-8")
            Path(second["path"], "b.txt").write_text("agent-two\n", encoding="utf-8")
            first = inspect_worktree(first)
            second = inspect_worktree(second)
            self.assertEqual(["a.txt"], first["changed_files"])
            self.assertEqual(["b.txt"], second["changed_files"])
            self.assertEqual("a0\n", (repo / "a.txt").read_text())
            self.assertEqual("b0\n", (repo / "b.txt").read_text())

            first, apply_one = apply_worktree(first)
            self.assertTrue(apply_one["ok"])
            second, apply_two = apply_worktree(second)
            self.assertTrue(apply_two["ok"])
            self.assertEqual("agent-one\n", (repo / "a.txt").read_text())
            self.assertEqual("agent-two\n", (repo / "b.txt").read_text())
            self.assertEqual("user-dirty\n", (repo / "user.txt").read_text())

    def test_apply_handles_create_delete_and_rename_without_global_git_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            (repo / "old.txt").write_text("old\n", encoding="utf-8")
            git(repo, "add", "old.txt"); git(repo, "commit", "-q", "-m", "add old")
            state = prepare_worktree(agent_id="agt_shapes", cwd=repo, path_roots=[str(repo)], mode="required")
            self.addCleanup(cleanup_worktree, state, force=True)
            wt = Path(state["path"])
            (wt / "new.txt").write_text("new\n", encoding="utf-8")
            (wt / "old.txt").rename(wt / "renamed.txt")
            (wt / "b.txt").unlink()
            state, result = apply_worktree(state)
            self.assertTrue(result["ok"])
            self.assertEqual("new\n", (repo / "new.txt").read_text())
            self.assertFalse((repo / "old.txt").exists())
            self.assertEqual("old\n", (repo / "renamed.txt").read_text())
            self.assertFalse((repo / "b.txt").exists())

    # ASSURANCE: SEC-GIT-001
    def test_overlapping_agent_edits_fail_closed_after_first_apply(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            first = prepare_worktree(agent_id="agt_one", cwd=repo, path_roots=[str(repo)], mode="required")
            second = prepare_worktree(agent_id="agt_two", cwd=repo, path_roots=[str(repo)], mode="required")
            self.addCleanup(cleanup_worktree, first, force=True)
            self.addCleanup(cleanup_worktree, second, force=True)
            Path(first["path"], "a.txt").write_text("one\n", encoding="utf-8")
            Path(second["path"], "a.txt").write_text("two\n", encoding="utf-8")
            first, result = apply_worktree(first)
            self.assertTrue(result["ok"])
            second, conflict = apply_worktree(second)
            self.assertFalse(conflict["ok"])
            self.assertEqual("conflict", conflict["status"])
            self.assertTrue(any(row["path"] == "a.txt" for row in conflict["conflicts"]))
            self.assertEqual("one\n", (repo / "a.txt").read_text())

    def test_disjoint_base_advance_can_apply_without_touching_new_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            state = prepare_worktree(agent_id="agt_disjoint", cwd=repo, path_roots=[str(repo)], mode="required")
            self.addCleanup(cleanup_worktree, state, force=True)
            Path(state["path"], "a.txt").write_text("agent-a\n", encoding="utf-8")
            (repo / "b.txt").write_text("main-b1\n", encoding="utf-8")
            git(repo, "add", "b.txt")
            git(repo, "commit", "-q", "-m", "advance b")
            advanced = git(repo, "rev-parse", "HEAD")
            state, result = apply_worktree(state)
            self.assertTrue(result["ok"])
            self.assertEqual(advanced, result["applied_to_head"])
            self.assertEqual("agent-a\n", (repo / "a.txt").read_text())
            self.assertEqual("main-b1\n", (repo / "b.txt").read_text())

    def test_base_advance_on_touched_path_conflicts_without_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            state = prepare_worktree(agent_id="agt_overlap", cwd=repo, path_roots=[str(repo)], mode="required")
            self.addCleanup(cleanup_worktree, state, force=True)
            Path(state["path"], "a.txt").write_text("agent-a\n", encoding="utf-8")
            (repo / "a.txt").write_text("main-a1\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-q", "-m", "advance a")
            state, result = apply_worktree(state)
            self.assertFalse(result["ok"])
            self.assertTrue(any(row["reason"] == "base_advanced_on_touched_path" for row in result["conflicts"]))
            self.assertEqual("main-a1\n", (repo / "a.txt").read_text())

    def test_dirty_touched_main_file_conflicts_but_unrelated_dirty_file_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            state = prepare_worktree(agent_id="agt_dirty", cwd=repo, path_roots=[str(repo)], mode="required")
            self.addCleanup(cleanup_worktree, state, force=True)
            Path(state["path"], "a.txt").write_text("agent-a\n", encoding="utf-8")
            (repo / "a.txt").write_text("user-a\n", encoding="utf-8")
            (repo / "user.txt").write_text("user-other\n", encoding="utf-8")
            state, result = apply_worktree(state)
            self.assertFalse(result["ok"])
            self.assertIn({"path": "a.txt", "reason": "source_worktree_dirty"}, result["conflicts"])
            self.assertEqual("user-a\n", (repo / "a.txt").read_text())
            self.assertEqual("user-other\n", (repo / "user.txt").read_text())

    def test_subdirectory_scope_stays_within_parent_scope_and_main_status_stays_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            sub = (repo / "src").resolve()
            state = prepare_worktree(
                agent_id="agt_sub", cwd=sub, path_roots=[str(sub)],
                mode="required", access_mode="workspace_write",
            )
            self.addCleanup(cleanup_worktree, state, force=True)
            isolated_cwd = Path(state["cwd"]).resolve()
            isolated_root = Path(state["path"]).resolve()
            self.assertTrue(isolated_cwd.is_relative_to(sub.resolve()))
            self.assertEqual(isolated_root / "src", isolated_cwd)
            mapped = Path(state["path_map"][0]["isolated"]).resolve()
            self.assertTrue(mapped.is_relative_to(sub.resolve()))
            self.assertEqual("", git(repo, "status", "--short", "--untracked-files=all"))

    def test_non_git_auto_falls_back_required_fails_and_read_only_needs_no_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            auto = prepare_worktree(agent_id="agt_auto", cwd=root, path_roots=[str(root)], mode="auto")
            self.assertFalse(auto["enabled"])
            self.assertEqual("non_git_workspace", auto["reason"])
            with self.assertRaises(AgentWorktreeError) as ctx:
                prepare_worktree(agent_id="agt_required", cwd=root, path_roots=[str(root)], mode="required")
            self.assertEqual("git_repository_required", ctx.exception.code)
            readonly = prepare_worktree(
                agent_id="agt_read", cwd=root, path_roots=[str(root)],
                mode="required", access_mode="read_only",
            )
            self.assertFalse(readonly["enabled"])
            self.assertEqual("read_only_agent", readonly["reason"])

    def test_cleanup_removes_ephemeral_worktree_and_branch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            state = prepare_worktree(agent_id="agt_cleanup", cwd=repo, path_roots=[str(repo)], mode="required")
            branch = state["branch"]
            path = Path(state["path"])
            self.assertTrue(path.exists())
            self.assertIn(branch, git(repo, "branch", "--list", branch))
            cleaned = cleanup_worktree(state, force=True)
            self.assertFalse(path.exists())
            self.assertEqual("discarded", cleaned["status"])
            self.assertNotIn(branch, git(repo, "branch", "--list", branch))

    # ASSURANCE: SEC-GIT-001
    def test_dependency_fan_in_combines_disjoint_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            base = git(repo, "rev-parse", "HEAD")
            first = prepare_worktree(agent_id="agt_dep_a", cwd=repo, path_roots=[str(repo)], mode="required", base_commit=base)
            second = prepare_worktree(agent_id="agt_dep_b", cwd=repo, path_roots=[str(repo)], mode="required", base_commit=base)
            target = prepare_worktree(agent_id="agt_dep_c", cwd=repo, path_roots=[str(repo)], mode="required", base_commit=base)
            self.addCleanup(cleanup_worktree, first, force=True)
            self.addCleanup(cleanup_worktree, second, force=True)
            self.addCleanup(cleanup_worktree, target, force=True)
            Path(first["path"], "a.txt").write_text("from-a\n", encoding="utf-8")
            Path(second["path"], "b.txt").write_text("from-b\n", encoding="utf-8")
            seeded = seed_worktree(target, [first, second])
            self.assertEqual("from-a\n", Path(seeded["path"], "a.txt").read_text())
            self.assertEqual("from-b\n", Path(seeded["path"], "b.txt").read_text())
            self.assertEqual("a0\n", (repo / "a.txt").read_text())
            self.assertEqual("b0\n", (repo / "b.txt").read_text())
            self.assertEqual(2, len(seeded["seeded_from"]))

    def test_dependency_fan_in_overlap_fails_without_mutating_target_or_main(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            base = git(repo, "rev-parse", "HEAD")
            first = prepare_worktree(agent_id="agt_dep_a", cwd=repo, path_roots=[str(repo)], mode="required", base_commit=base)
            second = prepare_worktree(agent_id="agt_dep_b", cwd=repo, path_roots=[str(repo)], mode="required", base_commit=base)
            target = prepare_worktree(agent_id="agt_dep_c", cwd=repo, path_roots=[str(repo)], mode="required", base_commit=base)
            self.addCleanup(cleanup_worktree, first, force=True)
            self.addCleanup(cleanup_worktree, second, force=True)
            self.addCleanup(cleanup_worktree, target, force=True)
            Path(first["path"], "a.txt").write_text("from-a\n", encoding="utf-8")
            Path(second["path"], "a.txt").write_text("from-b\n", encoding="utf-8")
            with self.assertRaises(AgentWorktreeError) as ctx:
                seed_worktree(target, [first, second])
            self.assertEqual("git_dependency_conflict", ctx.exception.code)
            # First dependency may already be present in the throwaway target; source tree is never touched.
            self.assertEqual("a0\n", (repo / "a.txt").read_text())

    def test_reuse_keeps_same_worktree_for_resume_or_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = init_repo(Path(td))
            state = prepare_worktree(agent_id="agt_parent", cwd=repo, path_roots=[str(repo)], mode="required")
            self.addCleanup(cleanup_worktree, state, force=True)
            Path(state["path"], "a.txt").write_text("in-progress\n", encoding="utf-8")
            reused = reuse_worktree(state)
            self.assertEqual(state["path"], reused["path"])
            self.assertEqual("in-progress\n", Path(reused["path"], "a.txt").read_text())


class AgentGitWorktreeSpawnIntegrationTests(unittest.TestCase):
    class DummyProc:
        pid = 424242
        returncode = None
        def wait(self, timeout=None):
            self.returncode = 0
            return 0
        def poll(self):
            return self.returncode

    class DummyThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            pass

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = init_repo(self.root)
        self.old_agents = agents.AGENTS_DIR
        self.old_teams = agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        agents.AGENTS_DIR.mkdir()
        agents.TEAMS_DIR.mkdir()
        self.patches = [
            patch.object(agents, "provider_enabled", return_value=True),
            patch.object(agents, "_find_binary", return_value="/tmp/fake-opencode"),
            patch.object(agents, "_validate_provider_access_mode", return_value=None),
            patch.object(agents, "create_workflow", return_value={"workflow_id": "wf_test"}),
            patch.object(agents, "workflow_public_state", return_value={"workflow": None, "resumable": False}),
            patch.object(agents, "_spawn_worker_process", return_value=self.DummyProc()),
            patch.object(agents.threading, "Thread", self.DummyThread),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        if agents.AGENTS_DIR.exists():
            for meta_path in agents.AGENTS_DIR.glob("*/meta.json"):
                try:
                    meta = json.loads(meta_path.read_text())
                    state = meta.get("worktree") or {}
                    if state.get("enabled") and Path(str(state.get("path") or "")).exists():
                        cleanup_worktree(state, force=True)
                except Exception:
                    pass
        agents._WORKERS.clear()
        agents.AGENTS_DIR = self.old_agents
        agents.TEAMS_DIR = self.old_teams
        self.temp.cleanup()

    # ASSURANCE: SEC-GIT-001
    def test_spawn_internal_remaps_workspace_write_cwd_and_scope(self) -> None:
        scope = ResourceScope.from_dict({
            "access_mode": "workspace_write", "path_roots": [str(self.repo.resolve())],
        })
        result = agents._spawn_internal(
            settings=None, provider="opencode", prompt="edit a", model=None, reasoning=None,
            cwd=str(self.repo), timeout_s=60, title="isolated", result_style="concise",
            access_mode="workspace_write", scope=scope, permission_profile="developer",
            git_isolation="required",
        )
        meta = agents._read_meta(result["agent_id"])
        state = meta["worktree"]
        self.assertTrue(state["enabled"])
        self.assertNotEqual(str(self.repo.resolve()), meta["cwd"])
        self.assertEqual(str(self.repo.resolve()), Path(meta["source_cwd"]).resolve().as_posix())
        self.assertTrue(Path(meta["cwd"]).exists())
        self.assertTrue(Path(meta["cwd"]).resolve().is_relative_to(self.repo.resolve()))
        scoped_root = Path(meta["scope"]["path_roots"][0]).resolve()
        self.assertTrue(scoped_root.is_relative_to(self.repo.resolve()))
        self.assertTrue(scoped_root.is_relative_to(Path(state["path"]).resolve()))
        self.assertEqual([str(self.repo.resolve())], [str(Path(x).resolve()) for x in meta["source_scope"]["path_roots"]])
        self.assertEqual("", git(self.repo, "status", "--short", "--untracked-files=all"))

    def test_read_only_spawn_does_not_create_worktree(self) -> None:
        scope = ResourceScope.from_dict({
            "access_mode": "read_only", "path_roots": [str(self.repo.resolve())],
        })
        result = agents._spawn_internal(
            settings=None, provider="opencode", prompt="inspect", model=None, reasoning=None,
            cwd=str(self.repo), timeout_s=60, title="read", result_style="concise",
            access_mode="read_only", scope=scope, permission_profile="read_only",
            git_isolation="required",
        )
        meta = agents._read_meta(result["agent_id"])
        self.assertFalse(meta["worktree"]["enabled"])
        self.assertEqual("read_only_agent", meta["worktree"]["reason"])
        self.assertEqual(str(self.repo.resolve()), str(Path(meta["cwd"]).resolve()))
        self.assertFalse((self.repo / ".mac-mcp-worktrees").exists())

    def test_resume_style_spawn_reuses_parent_worktree(self) -> None:
        scope = ResourceScope.from_dict({
            "access_mode": "workspace_write", "path_roots": [str(self.repo.resolve())],
        })
        parent = agents._spawn_internal(
            settings=None, provider="opencode", prompt="first", model=None, reasoning=None,
            cwd=str(self.repo), timeout_s=60, title="first", result_style="concise",
            access_mode="workspace_write", scope=scope, permission_profile="developer",
            git_isolation="required",
        )
        parent_meta = agents._read_meta(parent["agent_id"])
        Path(parent_meta["worktree"]["path"], "a.txt").write_text("carried\n", encoding="utf-8")
        child = agents._spawn_internal(
            settings=None, provider="opencode", prompt="continue", model=None, reasoning=None,
            cwd=parent_meta["cwd"], timeout_s=60, title="continue", result_style="concise",
            access_mode="workspace_write", scope=ResourceScope.from_dict(parent_meta["scope"]),
            permission_profile="developer", parent_agent_id=parent["agent_id"],
            git_isolation="required", reuse_worktree_agent_id=parent["agent_id"],
        )
        child_meta = agents._read_meta(child["agent_id"])
        self.assertEqual(parent_meta["worktree"]["path"], child_meta["worktree"]["path"])
        self.assertEqual(parent["agent_id"], child_meta["worktree"]["shared_from_agent_id"])
        self.assertEqual("carried\n", Path(child_meta["cwd"], "a.txt").read_text())

    def test_team_tick_revision_passes_worktree_reuse_id(self) -> None:
        team_id = "team_revision_reuse"
        now = time.time()
        agents._write_team(team_id, {
            "team_id": team_id, "title": "team", "provider": "opencode", "model": None,
            "reasoning": None, "project": None, "cwd": str(self.repo), "timeout_s": 60,
            "idle_timeout_s": None, "retries": 0, "result_style": "concise",
            "access_mode": "workspace_write", "permission_profile": "developer",
            "capability_profile": "legacy", "scope": {
                "access_mode": "workspace_write", "path_roots": [str(self.repo.resolve())],
            },
            "created_at": now, "updated_at": now, "parent_team_id": None,
            "owner_agent_id": None, "lineage_version": 1, "lineage_root_agent_id": None,
            "lineage_ancestors": [], "agent_ids": ["agt_previous"], "scheduler_version": 1,
            "max_parallel": 1, "max_revisions": 1, "team_timeout_s": 3600,
            "deadline_at": now + 3600, "max_team_retries": 0, "team_retry_count": 0,
            "next_retry_at": None, "max_total_tool_calls": None, "max_total_tokens": None,
            "budget_exhausted_reason": None, "cancelled": False, "git_isolation": "required",
            "tasks": [{
                "id": "coder", "prompt": "fix", "title": "coder", "scope": {
                    "access_mode": "workspace_write", "path_roots": [str(self.repo.resolve())],
                }, "project": None, "role": None, "depends_on": [], "review_of": None,
                "max_revisions": 1, "state": "ready", "agent_ids": ["agt_previous"],
                "active_agent_id": None, "latest_agent_id": "agt_previous", "revision_count": 1,
                "revision_feedback": "retry", "gate_attempts": 0, "gate_result": None,
                "gate_feedback": None, "failure_reason": None,
            }],
        })
        captured = []
        def fake_spawn(*args, **kwargs):
            captured.append(kwargs)
            return {"ok": True, "agent_id": "agt_revision_new", "status": "running"}
        with patch.object(agents, "_spawn_internal", side_effect=fake_spawn):
            agents._team_tick(team_id)
        self.assertEqual(1, len(captured))
        self.assertEqual("agt_previous", captured[0]["reuse_worktree_agent_id"])
        self.assertEqual("required", captured[0]["git_isolation"])


class AgentGitWorktreeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = init_repo(self.root)
        self.old_agents = agents.AGENTS_DIR
        self.old_teams = agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        agents.AGENTS_DIR.mkdir()
        agents.TEAMS_DIR.mkdir()
        self.state_patch = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.root / "state")}, clear=False)
        self.state_patch.start()

    def tearDown(self) -> None:
        self.state_patch.stop()
        # Clean any still-registered test worktrees before TemporaryDirectory teardown.
        for meta_path in list(agents.AGENTS_DIR.glob("*/meta.json")) if agents.AGENTS_DIR.exists() else []:
            try:
                meta = json.loads(meta_path.read_text())
                state = meta.get("worktree") or {}
                if state.get("enabled") and Path(str(state.get("path") or "")).exists():
                    cleanup_worktree(state, force=True)
            except Exception:
                pass
        agents.AGENTS_DIR = self.old_agents
        agents.TEAMS_DIR = self.old_teams
        self.temp.cleanup()

    def _terminal_meta(self, agent_id: str, state: dict, *, status: str = "completed") -> None:
        adir = agents.AGENTS_DIR / agent_id
        adir.mkdir(parents=True, exist_ok=True)
        for name in ("prompt.txt", "effective_prompt.txt", "stdout.log", "stderr.log", "worker.log", "result.txt"):
            (adir / name).write_text("", encoding="utf-8")
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id,
            "team_id": None,
            "team_task_id": None,
            "title": agent_id,
            "provider": "opencode",
            "cwd": state.get("cwd"),
            "source_cwd": state.get("source_cwd"),
            "source_scope": {"access_mode": "workspace_write", "path_roots": [str(self.repo)]},
            "git_isolation": "required",
            "worktree": state,
            "access_mode": "workspace_write",
            "permission_profile": "trusted",
            "capability_profile": "legacy",
            "scope": {"access_mode": "workspace_write", "path_roots": [state["path"]]},
            "status": status,
            "phase": status,
            "started_at": now - 1,
            "ended_at": now,
            "last_activity_at": now,
            "spawn_requested_at": now - 1,
            "retry_count": 0,
            "retries": 0,
        })

    # ASSURANCE: SEC-GIT-001
    def test_delegated_agent_cannot_apply_isolated_changes_to_source_tree(self) -> None:
        agent_id = "agt_delegated_apply"
        state = prepare_worktree(agent_id=agent_id, cwd=self.repo, path_roots=[str(self.repo)], mode="required")
        Path(state["path"], "a.txt").write_text("delegated\n", encoding="utf-8")
        self._terminal_meta(agent_id, state)
        from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context
        token = set_policy_context(PolicyContext(profile="trusted", actor=f"agent:{agent_id}", agent_id=agent_id))
        try:
            with self.assertRaises(HTTPException) as ctx:
                agents._agent_action_single(None, agent_id, "apply")
            self.assertEqual(403, ctx.exception.status_code)
            self.assertEqual("git_apply_root_required", ctx.exception.detail["error"])
            self.assertEqual("a0\n", (self.repo / "a.txt").read_text())
        finally:
            reset_policy_context(token)

    def test_agent_action_apply_then_despawn_cleans_worktree(self) -> None:
        agent_id = "agt_apply"
        state = prepare_worktree(agent_id=agent_id, cwd=self.repo, path_roots=[str(self.repo)], mode="required")
        Path(state["path"], "a.txt").write_text("applied\n", encoding="utf-8")
        self._terminal_meta(agent_id, state)
        with patch.object(agents, "get_scoped_credential_store") as creds, patch.object(agents.browser_tabs, "release_agent_leases"):
            creds.return_value.revoke_agent.return_value = None
            result = agents._agent_action_single(None, agent_id, "apply")
            self.assertTrue(result["ok"])
            self.assertEqual("applied\n", (self.repo / "a.txt").read_text())
            worktree_path = Path(state["path"])
            self.assertTrue(worktree_path.exists())
            despawn = agents._agent_action_single(None, agent_id, "despawn")
            self.assertTrue(despawn["ok"])
            self.assertFalse(worktree_path.exists())
            self.assertFalse((agents.AGENTS_DIR / agent_id).exists())

    def test_despawn_blocks_unapplied_changes_until_explicit_discard(self) -> None:
        agent_id = "agt_pending"
        state = prepare_worktree(agent_id=agent_id, cwd=self.repo, path_roots=[str(self.repo)], mode="required")
        Path(state["path"], "a.txt").write_text("pending\n", encoding="utf-8")
        self._terminal_meta(agent_id, state)
        with patch.object(agents, "get_scoped_credential_store") as creds, patch.object(agents.browser_tabs, "release_agent_leases"):
            creds.return_value.revoke_agent.return_value = None
            with self.assertRaises(HTTPException) as ctx:
                agents._agent_action_single(None, agent_id, "despawn")
            self.assertEqual(409, ctx.exception.status_code)
            self.assertEqual("unapplied_worktree_changes", ctx.exception.detail["error"])
            self.assertEqual("a0\n", (self.repo / "a.txt").read_text())
            discarded = agents._agent_action_single(None, agent_id, "discard")
            self.assertTrue(discarded["ok"])
            self.assertFalse(Path(state["path"]).exists())
            despawn = agents._agent_action_single(None, agent_id, "despawn")
            self.assertTrue(despawn["ok"])

    def test_shared_worktree_is_not_removed_while_resume_child_references_it(self) -> None:
        parent = "agt_parent"
        child = "agt_child"
        state = prepare_worktree(agent_id=parent, cwd=self.repo, path_roots=[str(self.repo)], mode="required")
        state = inspect_worktree(state)
        state["shared_from_agent_id"] = None
        self._terminal_meta(parent, state)
        child_state = dict(state)
        child_state["shared_from_agent_id"] = parent
        self._terminal_meta(child, child_state)
        with patch.object(agents, "get_scoped_credential_store") as creds, patch.object(agents.browser_tabs, "release_agent_leases"):
            creds.return_value.revoke_agent.return_value = None
            parent_result = agents._agent_action_single(None, parent, "despawn")
            self.assertTrue(parent_result["ok"])
            self.assertIn(child, parent_result["worktree_preserved_for"])
            self.assertTrue(Path(state["path"]).exists())
            child_result = agents._agent_action_single(None, child, "despawn")
            self.assertTrue(child_result["ok"])
            self.assertFalse(Path(state["path"]).exists())


if __name__ == "__main__":
    unittest.main()
