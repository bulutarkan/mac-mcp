from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_agents as agents
from mcp_server.policy_scope import ResourceScope


READ_ONLY_GIT = "git status --short && git log -1 --oneline && git diff --stat && git blame inside.txt"


def _init_git_repo(path: Path) -> None:
    git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "init", "-q"], cwd=path, check=True)
    subprocess.run([*git, "add", "inside.txt"], cwd=path, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=path, check=True)


class ProviderEnvironmentTests(unittest.TestCase):
    def test_provider_environment_does_not_inherit_unrelated_server_secrets(self) -> None:
        sentinel = "MAC_MCP_TEST_SENTINEL_SECRET"
        with patch.dict(
            os.environ,
            {
                sentinel: "must-not-cross-provider-boundary",
                "OPENROUTER_API_KEY": "required-openrouter-key",
                "OPENAI_API_KEY": "required-codex-key",
                "CHATGPT_CLI_IDLE_SECONDS": "777",
            },
            clear=False,
        ), patch.object(agents, "_keychain_secret", return_value=None):
            open_env = agents._minimal_provider_env("opencode", {"model": "openrouter/example/model"})
            codex_env = agents._minimal_provider_env("codex", {"model": "gpt-test"})
            chat_env = agents._minimal_provider_env("chatgpt", {})
        self.assertNotIn(sentinel, open_env)
        self.assertNotIn(sentinel, codex_env)
        self.assertNotIn(sentinel, chat_env)
        self.assertEqual("required-openrouter-key", open_env["OPENROUTER_API_KEY"])
        self.assertEqual("required-codex-key", codex_env["OPENAI_API_KEY"])
        self.assertNotIn("OPENROUTER_API_KEY", codex_env)
        self.assertNotIn("OPENAI_API_KEY", chat_env)
        self.assertEqual("777", chat_env["CHATGPT_CLI_IDLE_SECONDS"])

    def test_opencode_restricted_environment_uses_private_synthetic_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            old_agents = agents.AGENTS_DIR
            agents.AGENTS_DIR = Path(td) / "agents"
            self.addCleanup(setattr, agents, "AGENTS_DIR", old_agents)
            agent_id = "agt_env_boundary"
            (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
            meta = {
                "provider": "opencode",
                "model": "opencode/test-free",
                "access_mode": "read_only",
            }
            env, cleanup = agents._provider_env(agent_id, meta, "scoped-token")
            try:
                state = agents.AGENTS_DIR / agent_id / "provider_state"
                self.assertTrue(Path(env["HOME"]).is_relative_to(state))
                self.assertTrue(Path(env["TMPDIR"]).is_relative_to(state))
                self.assertTrue(Path(env["XDG_CACHE_HOME"]).is_relative_to(state))
                self.assertTrue(Path(env["XDG_DATA_HOME"]).is_relative_to(state))
                self.assertEqual(0o700, state.stat().st_mode & 0o777)
                self.assertNotIn("MAC_MCP_AGENT_TOKEN", env)
                config = Path(env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.jsonc"
                payload = __import__("json").loads(config.read_text(encoding="utf-8"))
                self.assertEqual("deny", payload["permission"]["bash"])
                self.assertEqual("deny", payload["permission"]["task"])
                self.assertEqual("deny", payload["permission"]["lsp"])
                self.assertEqual("deny", payload["permission"]["external_directory"])
                self.assertEqual("deny", payload["permission"]["edit"])
            finally:
                agents._cleanup_provider_config(cleanup)


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists(), "macOS Seatbelt required")
class OpenCodeSeatbeltBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.outside = self.root / "outside-secret.txt"
        self.outside.write_text("SENTINEL-SECRET", encoding="utf-8")
        self.inside = self.workspace / "inside.txt"
        self.inside.write_text("INSIDE", encoding="utf-8")
        self.old_agents = agents.AGENTS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        self.agent_id = "agt_boundary"
        (agents.AGENTS_DIR / self.agent_id).mkdir(parents=True)

    def tearDown(self) -> None:
        agents.AGENTS_DIR = self.old_agents
        self.tmp.cleanup()

    def _meta(self, mode: str) -> dict:
        scope = ResourceScope(path_roots=(str(self.workspace),), access_mode=mode)
        return {
            "provider": "opencode",
            "binary": "/opt/homebrew/bin/opencode",
            "cwd": str(self.workspace),
            "model": "opencode/test-free",
            "access_mode": mode,
            "scope": scope.to_dict(),
        }

    def _run(self, mode: str, shell: str) -> subprocess.CompletedProcess[str]:
        meta = self._meta(mode)
        env, cleanup = agents._provider_env(self.agent_id, meta, "scoped-token")
        profile = None
        try:
            cmd, profile = agents._provider_process_command(
                self.agent_id, meta, ["/bin/sh", "-c", shell]
            )
            return subprocess.run(
                cmd,
                cwd=self.workspace,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
        finally:
            if profile is not None:
                profile.unlink(missing_ok=True)
            agents._cleanup_provider_config(cleanup)

    def test_read_only_can_read_workspace_but_not_outside_or_write(self) -> None:
        allowed = self._run("read_only", f'/bin/cat "{self.inside}"')
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertEqual("INSIDE", allowed.stdout.strip())

        outside = self._run("read_only", f'/bin/cat "{self.outside}"')
        self.assertNotEqual(0, outside.returncode)
        self.assertNotIn("SENTINEL-SECRET", outside.stdout)

        nested = self._run("read_only", f'/bin/sh -c \'/bin/cat "{self.outside}"\'')
        self.assertNotEqual(0, nested.returncode)
        self.assertNotIn("SENTINEL-SECRET", nested.stdout)

        write_inside = self._run("read_only", f'echo CHANGED >> "{self.inside}"')
        self.assertNotEqual(0, write_inside.returncode)
        self.assertEqual("INSIDE", self.inside.read_text(encoding="utf-8"))

    def test_read_only_can_run_read_only_git(self) -> None:
        _init_git_repo(self.workspace)
        proc = self._run("read_only", READ_ONLY_GIT)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("init", proc.stdout)

        write_git = self._run("read_only", "touch .git/probe")
        self.assertNotEqual(0, write_git.returncode)
        self.assertFalse((self.workspace / ".git" / "probe").exists())

    def test_workspace_write_is_scoped_and_nested_children_cannot_widen(self) -> None:
        write_inside = self._run("workspace_write", f'echo OK >> "{self.inside}"')
        self.assertEqual(0, write_inside.returncode, write_inside.stderr)
        self.assertIn("OK", self.inside.read_text(encoding="utf-8"))

        write_outside = self._run("workspace_write", f'echo LEAK >> "{self.outside}"')
        self.assertNotEqual(0, write_outside.returncode)
        self.assertEqual("SENTINEL-SECRET", self.outside.read_text(encoding="utf-8"))

        nested_outside = self._run(
            "workspace_write", f'/bin/sh -c \'echo LEAK2 >> "{self.outside}"\''
        )
        self.assertNotEqual(0, nested_outside.returncode)
        self.assertEqual("SENTINEL-SECRET", self.outside.read_text(encoding="utf-8"))

    def test_restricted_process_cannot_launch_keychain_helper(self) -> None:
        proc = self._run("workspace_write", "/usr/bin/security list-keychains")
        self.assertNotEqual(0, proc.returncode)

    def test_full_mode_is_explicitly_unwrapped(self) -> None:
        meta = self._meta("full")
        cmd = ["/bin/echo", "ok"]
        wrapped, profile = agents._provider_process_command(self.agent_id, meta, cmd)
        self.assertEqual(cmd, wrapped)
        self.assertIsNone(profile)
        info = agents._access_mode_info("opencode", "full")
        self.assertFalse(info["enforced"])
        self.assertEqual("explicit_full", info["boundary"])


@unittest.skipUnless(
    sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists(),
    "macOS Seatbelt required",
)
class CodexSeatbeltBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.outside = self.root / "outside-secret.txt"
        self.outside.write_text("SENTINEL-SECRET", encoding="utf-8")
        self.inside = self.workspace / "inside.txt"
        self.inside.write_text("INSIDE", encoding="utf-8")
        self.source_codex_home = self.root / "source-codex"
        self.source_codex_home.mkdir()
        (self.source_codex_home / "auth.json").write_text(
            '{"auth_mode":"dummy-secret"}',
            encoding="utf-8",
        )

        self.old_agents = agents.AGENTS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        self.agent_id = "agt_codex_boundary"
        (agents.AGENTS_DIR / self.agent_id).mkdir(parents=True)

    def tearDown(self) -> None:
        agents.AGENTS_DIR = self.old_agents
        self.tmp.cleanup()

    def _meta(self, mode: str) -> dict:
        scope = ResourceScope(path_roots=(str(self.workspace),), access_mode=mode)
        return {
            "provider": "codex",
            "binary": "/opt/homebrew/bin/codex",
            "cwd": str(self.workspace),
            "model": "gpt-test",
            "access_mode": mode,
            "scope": scope.to_dict(),
        }

    def _run(self, mode: str, shell: str) -> subprocess.CompletedProcess[str]:
        meta = self._meta(mode)
        with patch.dict(
            os.environ,
            {"CODEX_HOME": str(self.source_codex_home)},
            clear=False,
        ), patch.object(
            agents,
            "_codex_native_binary",
            return_value=Path("/usr/bin/true"),
        ):
            env, cleanup = agents._provider_env(
                self.agent_id, meta, "scoped-token"
            )
            profile = None
            try:
                cmd, profile = agents._provider_process_command(
                    self.agent_id, meta, ["/bin/sh", "-c", shell]
                )
                return subprocess.run(
                    cmd,
                    cwd=self.workspace,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
            finally:
                if profile is not None:
                    profile.unlink(missing_ok=True)
                agents._cleanup_provider_config(cleanup)

    def test_read_only_can_read_workspace_but_not_outside_or_write(self) -> None:
        allowed = self._run("read_only", f'/bin/cat "{self.inside}"')
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertEqual("INSIDE", allowed.stdout.strip())

        outside = self._run("read_only", f'/bin/cat "{self.outside}"')
        self.assertNotEqual(0, outside.returncode)
        self.assertNotIn("SENTINEL-SECRET", outside.stdout)

        write_inside = self._run(
            "read_only", f'echo CHANGED >> "{self.inside}"'
        )
        self.assertNotEqual(0, write_inside.returncode)
        self.assertEqual("INSIDE", self.inside.read_text(encoding="utf-8"))

    def test_read_only_can_run_read_only_git(self) -> None:
        _init_git_repo(self.workspace)
        proc = self._run("read_only", READ_ONLY_GIT)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("init", proc.stdout)

        write_git = self._run("read_only", "touch .git/probe")
        self.assertNotEqual(0, write_git.returncode)
        self.assertFalse((self.workspace / ".git" / "probe").exists())

    def test_workspace_write_is_scoped(self) -> None:
        write_inside = self._run(
            "workspace_write", f'echo OK >> "{self.inside}"'
        )
        self.assertEqual(0, write_inside.returncode, write_inside.stderr)
        self.assertIn("OK", self.inside.read_text(encoding="utf-8"))

        write_outside = self._run(
            "workspace_write", f'echo LEAK >> "{self.outside}"'
        )
        self.assertNotEqual(0, write_outside.returncode)
        self.assertEqual(
            "SENTINEL-SECRET",
            self.outside.read_text(encoding="utf-8"),
        )

    def test_shell_cannot_read_private_codex_auth(self) -> None:
        completed = self._run(
            "read_only",
            '/bin/cat "$CODEX_HOME/auth.json"',
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("dummy-secret", completed.stdout + completed.stderr)

    def test_full_mode_is_unwrapped(self) -> None:
        meta = self._meta("full")
        cmd = ["/bin/echo", "ok"]
        wrapped, profile = agents._provider_process_command(
            self.agent_id, meta, cmd
        )
        self.assertEqual(cmd, wrapped)
        self.assertIsNone(profile)

    def test_resume_chain_reuses_parent_private_state(self) -> None:
        parent_id = "agt_codex_parent"
        parent_dir = agents.AGENTS_DIR / parent_id
        parent_dir.mkdir(parents=True)
        parent_meta = {
            "provider": "codex",
            "resume_session_id": None,
            "parent_agent_id": None,
        }
        (parent_dir / "meta.json").write_text(
            __import__("json").dumps(parent_meta),
            encoding="utf-8",
        )
        child_meta = {
            "provider": "codex",
            "resume_session_id": "thread-1",
            "parent_agent_id": parent_id,
        }
        self.assertEqual(
            parent_id,
            agents._codex_state_owner_agent_id("agt_child", child_meta),
        )


class ProviderModeMatrixTests(unittest.TestCase):
    def test_chatgpt_restricted_modes_fail_closed(self) -> None:
        for mode in ("read_only", "workspace_write"):
            with self.subTest(mode=mode), self.assertRaises(HTTPException) as ctx:
                agents._validate_provider_access_mode("chatgpt", mode)
            self.assertEqual(409, ctx.exception.status_code)
        agents._validate_provider_access_mode("chatgpt", "full")

    def test_opencode_restricted_mode_reports_hard_boundary_on_macos(self) -> None:
        info = agents._access_mode_info("opencode", "read_only")
        if agents._sandbox_exec_available():
            self.assertTrue(info["enforced"])
            self.assertEqual("macos_seatbelt", info["boundary"])
        else:
            self.assertFalse(info["enforced"])
            self.assertEqual("unsupported", info["boundary"])

    def test_codex_restricted_modes_use_hard_boundary_on_macos(self) -> None:
        if agents._sandbox_exec_available():
            agents._validate_provider_access_mode("codex", "read_only")
            agents._validate_provider_access_mode("codex", "workspace_write")
            info = agents._access_mode_info("codex", "read_only")
            self.assertTrue(info["enforced"])
            self.assertEqual("macos_seatbelt", info["boundary"])
        else:
            for mode in ("read_only", "workspace_write"):
                with self.subTest(mode=mode), self.assertRaises(HTTPException) as ctx:
                    agents._validate_provider_access_mode("codex", mode)
                self.assertEqual(409, ctx.exception.status_code)
        agents._validate_provider_access_mode("codex", "full")

    def test_codex_restricted_command_defers_to_outer_seatbelt(self) -> None:
        meta = {
            "provider": "codex",
            "binary": "/opt/homebrew/bin/codex",
            "cwd": "/tmp",
            "model": "gpt-test",
            "reasoning": "high",
            "access_mode": "read_only",
            "scoped_mcp": True,
            "mcp_endpoint": "http://127.0.0.1:8765/mcp",
        }
        cmd = agents._build_provider_command(
            meta, "PROMPT", Path("/tmp/result.txt")
        )
        joined = " ".join(cmd)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", joined)
        self.assertNotIn("--sandbox read-only", joined)
        self.assertIn('shell_environment_policy.inherit="none"', joined)

    def test_codex_full_command_still_strips_shell_environment(self) -> None:
        meta = {
            "provider": "codex",
            "binary": "/opt/homebrew/bin/codex",
            "cwd": "/tmp",
            "model": "gpt-test",
            "reasoning": "high",
            "access_mode": "full",
            "scoped_mcp": True,
            "mcp_endpoint": "http://127.0.0.1:8765/mcp",
        }
        cmd = agents._build_provider_command(meta, "PROMPT", Path("/tmp/result.txt"))
        joined = " ".join(cmd)
        self.assertIn('shell_environment_policy.inherit="none"', joined)
        self.assertIn("shell_environment_policy.set.PATH", joined)
        self.assertIn("danger-full-access", joined)


if __name__ == "__main__":
    unittest.main()
