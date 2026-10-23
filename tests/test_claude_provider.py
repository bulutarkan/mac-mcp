"""Claude Code as a delegated-agent provider (provider=claude)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import provider_compat, provider_usage
from mcp_server import tools_agents as agents
from mcp_server import workflow_checkpoints as workflows

PROMPT = "--model haiku: summarise the repo"


def claude_meta(root: Path, access_mode: str = "workspace_write", **extra) -> dict:
    cwd = root / "repo"
    cwd.mkdir(parents=True, exist_ok=True)
    meta = {
        "provider": "claude",
        "binary": str(root / "bin" / "claude"),
        "cwd": str(cwd),
        "access_mode": access_mode,
        "scope": {"access_mode": access_mode, "path_roots": [str(cwd)]},
        "scoped_mcp": True,
        "mcp_endpoint": "http://127.0.0.1:8765/mcp",
        "model": None,
        "reasoning": None,
    }
    meta.update(extra)
    return meta


class ClaudeTempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.config = self.root / "claude-config"
        self.config.mkdir()
        patches = [
            patch.object(agents, "AGENTS_DIR", self.root / "agents"),
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def owner_settings(self, **values) -> None:
        (self.config / "settings.json").write_text(json.dumps(values), encoding="utf-8")


class CatalogAndSelectionTests(ClaudeTempDirTestCase):
    def test_catalog_offers_aliases_efforts_and_the_owner_default(self) -> None:
        self.owner_settings(model="opus", effortLevel="high")
        with patch.object(agents, "provider_enabled", side_effect=lambda name: name == "claude"), \
             patch.object(agents, "_find_binary", return_value="/usr/bin/true"), \
             patch.object(agents, "_version", return_value="2.1.296 (Claude Code)"):
            catalog = agents.agent_catalog(None, provider="claude")["providers"]["claude"]
        self.assertEqual(["fable", "opus", "sonnet", "haiku"], catalog["models"])
        self.assertEqual(["low", "medium", "high", "xhigh", "max"], catalog["reasoning_values"])
        self.assertEqual("opus", catalog["default_model"])
        self.assertEqual("high", catalog["default_reasoning"])
        self.assertTrue(catalog["access_modes"]["full"]["supported"])
        self.assertTrue(catalog["scoped_mcp"])

    def test_disabled_provider_is_hidden(self) -> None:
        with patch.object(agents, "provider_enabled", return_value=False):
            self.assertEqual({}, agents.agent_catalog(None, provider="claude")["providers"])

    def test_model_and_effort_validation(self) -> None:
        validate = agents._validate_provider_model_selection
        self.assertTrue(validate("claude", "/usr/bin/true", "opus", "max")["validated"])
        self.assertFalse(validate("claude", "/usr/bin/true", "claude-opus-5-5", None)["validated"])
        self.assertFalse(validate("claude", "/usr/bin/true", "claude-sonnet-5-5[1m]", "low")["validated"])
        for model, effort in (("gpt-6-luna", None), ("opus; rm -rf /", None), ("opus", "extra-high")):
            with self.subTest(model=model, effort=effort), self.assertRaises(HTTPException) as ctx:
                validate("claude", "/usr/bin/true", model, effort)
            self.assertEqual(400, ctx.exception.status_code)

    def test_chatgpt_is_no_longer_a_provider(self) -> None:
        self.assertEqual({"opencode", "codex", "claude", "kimi", "grok", "antigravity", "pi", "cline", "kilo", "goose",
                          "copilot"},
                         agents._PROVIDER_NAMES)
        self.assertNotIn("chatgpt", agents._PROVIDER_NAMES)
        with self.assertRaises(HTTPException):
            agents._resolve_agent_selection("chatgpt", None, None)


class CommandTests(ClaudeTempDirTestCase):
    def test_full_mode_command(self) -> None:
        meta = claude_meta(self.root, "full", model="sonnet", reasoning="xhigh", resume_session_id="sess-1")
        cmd = agents._build_provider_command(meta, PROMPT, self.root / "result.txt")
        self.assertEqual(meta["binary"], cmd[0])
        for flag in ("--print", "--verbose", "--strict-mcp-config"):
            self.assertIn(flag, cmd)
        self.assertEqual("stream-json", cmd[cmd.index("--output-format") + 1])
        self.assertEqual("bypassPermissions", cmd[cmd.index("--permission-mode") + 1])
        self.assertEqual("sonnet", cmd[cmd.index("--model") + 1])
        self.assertEqual("xhigh", cmd[cmd.index("--effort") + 1])
        self.assertEqual("sess-1", cmd[cmd.index("--resume") + 1])
        self.assertNotIn("--add-dir", cmd)
        self.assertNotIn("--allowedTools", cmd)
        # The prompt is the last argument, after "--", even when it looks like an option.
        self.assertEqual(["--", PROMPT], cmd[-2:])
        denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--")]
        for tool in ("CronCreate", "RemoteTrigger", "PushNotification", "SendMessage", "Workflow", "EnterWorktree"):
            self.assertIn(tool, denied)
        self.assertNotIn("Edit", denied)
        config = json.loads(cmd[cmd.index("--mcp-config") + 1])
        server = config["mcpServers"]["mac-mcp"]
        self.assertEqual("http", server["type"])
        self.assertEqual("Bearer ${MAC_MCP_AGENT_TOKEN}", server["headers"]["Authorization"])

    def test_read_only_removes_writers_and_carries_owner_defaults(self) -> None:
        self.owner_settings(model="opus", effortLevel="medium")
        extra = self.root / "docs"
        extra.mkdir()
        meta = claude_meta(self.root, "read_only")
        meta["scope"]["path_roots"].append(str(extra))
        cmd = agents._build_provider_command(meta, "hi", self.root / "result.txt")
        denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--")]
        allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
        self.assertIn("Bash", allowed)
        self.assertIn("mcp__mac-mcp", allowed)
        self.assertFalse(set(allowed) & set(agents._CLAUDE_LIFECYCLE_TOOLS))
        for tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
            self.assertIn(tool, denied)
        self.assertNotIn("Bash", denied)  # writes are denied by the Seatbelt boundary instead
        self.assertEqual("opus", cmd[cmd.index("--model") + 1])
        self.assertEqual("medium", cmd[cmd.index("--effort") + 1])
        self.assertEqual(str(extra.resolve()), cmd[cmd.index("--add-dir") + 1])

    def test_no_scoped_mcp_means_no_servers(self) -> None:
        meta = claude_meta(self.root, "full", scoped_mcp=False)
        cmd = agents._build_provider_command(meta, "hi", self.root / "result.txt")
        self.assertEqual({"mcpServers": {}}, json.loads(cmd[cmd.index("--mcp-config") + 1]))

    def test_compat_contract_names_every_flag_the_command_uses(self) -> None:
        cmd = agents._build_provider_command(
            claude_meta(self.root, "read_only", model="opus", reasoning="low", resume_session_id="s"),
            "hi", self.root / "result.txt",
        )
        flags = {part for part in cmd if part.startswith("--") and part != "--"}
        contract = set(provider_compat.REQUIRED_OPTIONS["claude"][0][1])
        self.assertEqual(set(), flags - contract)


class SignInTests(ClaudeTempDirTestCase):
    def test_freshest_token_wins_and_refresh_token_is_never_returned(self) -> None:
        now = time.time()
        records = [
            {"accessToken": "old", "refreshToken": "r1", "expiresAt": (now + 600) * 1000},
            {"accessToken": "new", "refreshToken": "r2", "expiresAt": (now + 3600) * 1000},
        ]
        with patch.dict(os.environ, {"MAC_MCP_CLAUDE_CODE_OAUTH_TOKEN": ""}), \
             patch.object(agents, "_claude_oauth_records", return_value=records):
            self.assertEqual(("new", "ok"), agents._claude_sandbox_token())

    def test_expiring_token_is_renewed_once_then_refused(self) -> None:
        now = time.time()
        stale = [{"accessToken": "stale", "expiresAt": (now + 30) * 1000}]
        with patch.dict(os.environ, {"MAC_MCP_CLAUDE_CODE_OAUTH_TOKEN": ""}), \
             patch.object(agents, "_claude_oauth_records", return_value=stale), \
             patch.object(agents.subprocess, "run") as run:
            self.assertEqual((None, "claude_sign_in_expired"), agents._claude_sandbox_token("/bin/claude"))
        self.assertEqual(["/bin/claude", "auth", "status"], run.call_args.args[0])
        with patch.dict(os.environ, {"MAC_MCP_CLAUDE_CODE_OAUTH_TOKEN": ""}), \
             patch.object(agents, "_claude_oauth_records", return_value=[]):
            self.assertEqual((None, "claude_not_signed_in"), agents._claude_sandbox_token())

    def test_dedicated_token_takes_precedence(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_CLAUDE_CODE_OAUTH_TOKEN": "setup-token"}), \
             patch.object(agents, "_claude_oauth_records") as records:
            self.assertEqual(("setup-token", "ok"), agents._claude_sandbox_token())
        records.assert_not_called()

    def test_restricted_spawn_fails_before_work_without_a_sign_in(self) -> None:
        with patch.object(agents, "_claude_sandbox_token", return_value=(None, "claude_not_signed_in")):
            with self.assertRaises(HTTPException) as ctx:
                agents._require_provider_sign_in("claude", "workspace_write", "/bin/claude")
            agents._require_provider_sign_in("claude", "full", "/bin/claude")
            agents._require_provider_sign_in("codex", "workspace_write", "/bin/codex")
        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("claude_not_signed_in", str(ctx.exception.detail))


class EnvironmentAndBoundaryTests(ClaudeTempDirTestCase):
    def agent(self, agent_id: str = "agt_claude_env") -> str:
        (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
        return agent_id

    def test_sandboxed_environment_is_private_and_carries_only_the_access_token(self) -> None:
        agent_id = self.agent()
        meta = claude_meta(self.root, "workspace_write")
        with patch.object(agents, "_claude_sandbox_token", return_value=("access-token", "ok")):
            env, cleanup = agents._provider_env(agent_id, meta, "scoped-token")
        state = agents.AGENTS_DIR / agent_id / "provider_state" / "claude"
        self.assertEqual(str(state / "home"), env["HOME"])
        self.assertEqual(str(state / "config"), env["CLAUDE_CONFIG_DIR"])
        self.assertEqual(str(state / "tmp"), env["TMPDIR"])
        self.assertEqual(str(state / "tmp"), env["CLAUDE_CODE_TMPDIR"])
        self.assertEqual("access-token", env["CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertEqual("1", env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"])
        # The scrub also blanks ${MAC_MCP_AGENT_TOKEN}, so the scoped token goes in a private config file.
        self.assertNotIn("MAC_MCP_AGENT_TOKEN", env)
        config = cleanup / agents._CLAUDE_MCP_CONFIG_NAME
        self.assertEqual(agents.AGENTS_DIR / agent_id / "provider_config", cleanup)
        self.assertEqual(0o600, config.stat().st_mode & 0o777)
        server = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["mac-mcp"]
        self.assertEqual("Bearer scoped-token", server["headers"]["Authorization"])
        cmd = agents._build_provider_command(meta, "hi", self.root / "r.txt", mcp_config_path=config)
        self.assertEqual(str(config), cmd[cmd.index("--mcp-config") + 1])
        self.assertEqual(0o700, (state / "config").stat().st_mode & 0o777)

    def test_full_environment_keeps_the_owner_home_and_sign_in(self) -> None:
        agent_id = self.agent()
        with patch.object(agents, "_claude_sandbox_token") as token:
            env, cleanup = agents._provider_env(agent_id, claude_meta(self.root, "full"), "scoped-token")
        token.assert_not_called()
        self.assertIsNone(cleanup)
        self.assertEqual("scoped-token", env["MAC_MCP_AGENT_TOKEN"])
        self.assertEqual(str(Path.home().resolve()), env["HOME"])
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_sandboxed_environment_without_sign_in_fails_closed(self) -> None:
        agent_id = self.agent()
        with patch.object(agents, "_claude_sandbox_token", return_value=(None, "claude_sign_in_expired")):
            with self.assertRaises(HTTPException) as ctx:
                agents._provider_env(agent_id, claude_meta(self.root, "read_only"), "")
        self.assertEqual(409, ctx.exception.status_code)

    def test_resumed_agent_reuses_its_first_agent_state(self) -> None:
        parent = self.agent("agt_claude_parent")
        child = self.agent("agt_claude_child")
        meta = claude_meta(self.root, "read_only", resume_session_id="sess", parent_agent_id=parent)
        agents._write_meta(parent, claude_meta(self.root, "read_only"))
        state = agents._restricted_claude_state(child, meta)
        self.assertEqual(agents.AGENTS_DIR / parent / "provider_state" / "claude", state)

    @unittest.skipUnless(agents._sandbox_exec_available(), "macOS sandbox-exec required")
    def test_seatbelt_profile(self) -> None:
        agent_id = self.agent()
        install = self.root / "share" / "claude" / "versions"
        install.mkdir(parents=True)
        binary = install / "2.1.296"
        binary.write_text("", encoding="utf-8")
        launcher = self.root / "bin"
        launcher.mkdir()
        (launcher / "claude").symlink_to(binary)
        meta = claude_meta(self.root, "read_only", binary=str(launcher / "claude"))
        cmd, profile = agents._provider_process_command(agent_id, meta, ["claude", "--print"])
        self.assertEqual([str(agents._SANDBOX_EXEC), "-f", str(profile), "claude", "--print"], cmd)
        text = profile.read_text(encoding="utf-8")
        self.assertIn('(deny file-read* (subpath "/Users")', text)
        self.assertIn(f'(subpath "{install.resolve()}")', text)
        self.assertIn(f'(subpath "{launcher.resolve()}")', text)
        self.assertIn('(deny process-exec (literal "/usr/bin/security"))', text)
        repo = str(Path(meta["cwd"]).resolve())
        write_line = next(line for line in text.splitlines() if line.startswith("(allow file-write* (subpath"))
        self.assertNotIn(repo, write_line)
        meta["access_mode"] = "workspace_write"
        meta["scope"]["access_mode"] = "workspace_write"
        _, profile = agents._provider_process_command(agent_id, meta, ["claude"])
        write_line = next(
            line for line in profile.read_text(encoding="utf-8").splitlines()
            if line.startswith("(allow file-write* (subpath")
        )
        self.assertIn(repo, write_line)
        _, none = agents._provider_process_command(agent_id, claude_meta(self.root, "full"), ["claude"])
        self.assertIsNone(none)


def stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


INIT = {
    "type": "system", "subtype": "init", "session_id": "sess-42", "model": "claude-opus-5-5",
    "mcp_servers": [{"name": "mac-mcp", "status": "connected"}], "tools": ["Bash", "Read"],
}
TOOL_USE = {
    "type": "assistant", "session_id": "sess-42",
    "message": {"id": "msg_1", "model": "claude-opus-5-5", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
    ]},
}
TOOL_RESULT = {"type": "user", "session_id": "sess-42", "message": {"content": [{"type": "tool_result"}]}}
TEXT = {
    "type": "assistant", "session_id": "sess-42",
    "message": {"id": "msg_2", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "done"}]},
}
USAGE = {
    "input_tokens": 4, "output_tokens": 203, "cache_read_input_tokens": 38799,
    "cache_creation_input_tokens": 16194, "output_tokens_details": {"thinking_tokens": 50},
}
RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "result": "All clean.",
    "session_id": "sess-42", "uuid": "res-1", "num_turns": 3, "total_cost_usd": 0.0037, "usage": USAGE,
}


class EventTests(unittest.TestCase):
    def test_stream_updates_phase_tools_model_usage_and_cost(self) -> None:
        meta = {"provider": "claude", "requested_model": "opus", "status": "running"}
        now = 1000.0
        agents._apply_provider_event(meta, INIT, now)
        self.assertEqual(("starting", "sess-42", "claude-opus-5-5", "connected"),
                         (meta["phase"], meta["session_id"], meta["effective_model"], meta["scoped_mcp_status"]))
        agents._apply_provider_event(meta, TOOL_USE, now + 1)
        self.assertEqual(("tool", "Bash", 1, 1), (meta["phase"], meta["last_tool"], meta["tool_call_count"], meta["step_count"]))
        agents._apply_provider_event(meta, TOOL_RESULT, now + 3)
        self.assertEqual(("reasoning", 2000), (meta["phase"], meta["last_tool_duration_ms"]))
        agents._apply_provider_event(meta, TEXT, now + 4)
        agents._apply_provider_event(meta, RESULT, now + 5)
        self.assertEqual("finalizing", meta["phase"])
        self.assertEqual(1, meta["turn_count"])
        self.assertEqual(2, meta["step_count"])
        self.assertEqual({"input": 4, "output": 203, "reasoning": 50, "cache": {"read": 38799, "write": 16194}}, meta["usage"])
        self.assertAlmostEqual(0.0037, meta["provider_cost_usd"])
        self.assertNotIn("model_mismatch", meta)  # an alias resolves to a dated id
        self.assertNotIn("unknown_event_types", meta)

    def test_full_model_id_mismatch_is_flagged(self) -> None:
        meta = {"provider": "claude", "requested_model": "claude-sonnet-5-5"}
        agents._apply_provider_event(meta, INIT, 1.0)
        self.assertTrue(meta["model_mismatch"])

    def test_error_result_and_rejected_rate_limit(self) -> None:
        meta = {"provider": "claude"}
        agents._apply_provider_event(meta, {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 5000}}, 1000.0)
        self.assertEqual((1, "five_hour", 5000.0), (meta["throttle_count"], meta["last_throttle_reason"], meta["cooldown_until"]))
        agents._apply_provider_event(meta, {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}, 1001.0)
        self.assertEqual(1, meta["throttle_count"])
        agents._apply_provider_event(meta, {
            "type": "result", "subtype": "success", "is_error": True, "api_error_status": 401, "result": "API Error: 401",
        }, 1002.0)
        self.assertEqual("failed", meta["phase"])
        self.assertEqual(401, meta["provider_error"]["api_error_status"])

    def test_unknown_events_are_recorded_but_hooks_are_passive(self) -> None:
        meta = {"provider": "claude"}
        agents._apply_provider_event(meta, {"type": "system", "subtype": "hook_started"}, 1.0)
        agents._apply_provider_event(meta, {"type": "brand_new_event"}, 2.0)
        self.assertEqual(["brand_new_event"], meta["unknown_event_types"])


class ResultAndRetryTests(ClaudeTempDirTestCase):
    def write(self, *events: dict) -> Path:
        path = self.root / "stdout.log"
        path.write_text(stream(*events), encoding="utf-8")
        return path

    def test_final_result_session_and_usage_are_extracted(self) -> None:
        text, session, usage = agents._extract_claude(self.write(INIT, TOOL_USE, TOOL_RESULT, TEXT, RESULT))
        self.assertEqual(("All clean.", "sess-42"), (text, session))
        self.assertEqual(203, usage["output"])
        self.assertEqual(("", None, None), agents._extract_claude(self.root / "missing.log"))

    def test_failures_are_classified_from_the_result_not_from_tool_output(self) -> None:
        stderr = self.root / "stderr.log"
        stderr.write_text("", encoding="utf-8")
        meta = {"provider": "claude"}
        cases = {
            401: ("provider_auth_expired", False), 403: ("permission_error", False), 429: ("rate_limited", True),
            529: ("provider_overloaded", True), 503: ("provider_unavailable", True), 400: ("invalid_request", False),
        }
        for status_code, (reason, retryable) in cases.items():
            with self.subTest(status=status_code):
                path = self.write(INIT, {"type": "result", "subtype": "success", "is_error": True,
                                         "api_error_status": status_code, "result": "API Error"})
                decision = agents._adaptive_retry_decision(meta, 1, None, path, stderr)
                self.assertEqual((reason, retryable), (decision["reason"], decision["retryable"]))
        # A file the agent read mentions "unauthorized", but the run itself failed on overload.
        noisy = dict(TOOL_RESULT, message={"content": [{"type": "tool_result", "content": "401 unauthorized"}]})
        path = self.write(INIT, noisy, {"type": "result", "is_error": True, "api_error_status": 529})
        self.assertEqual("provider_overloaded", agents._adaptive_retry_decision(meta, 1, None, path, stderr)["reason"])

    def test_only_the_current_attempt_is_classified(self) -> None:
        path = self.write({"type": "result", "is_error": True, "api_error_status": 401})
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as handle:
            handle.write(stream({"type": "result", "is_error": True, "api_error_status": 529}))
        self.assertEqual(("provider_overloaded", True, 4.0), agents._claude_failure_reason(path, offset))
        self.assertIsNone(agents._claude_failure_reason(self.write(RESULT)))


class UsageTests(unittest.TestCase):
    def test_normalized_total_counts_cache_tokens_once(self) -> None:
        usage = provider_usage.normalize_claude_usage(USAGE)
        self.assertTrue(usage.recognized)
        self.assertEqual(4 + 203 + 38799 + 16194, usage.total_tokens)
        self.assertEqual((38799, 16194, 50), (usage.cache_read_tokens, usage.cache_write_tokens, usage.reasoning_tokens))
        self.assertFalse(provider_usage.normalize_claude_usage({"foo": 1}).recognized)

    def test_result_event_is_ingested_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent_id = "agt_claude_usage"
            (root / "agents" / agent_id).mkdir(parents=True)
            (root / "agents" / agent_id / "meta.json").write_text(json.dumps({
                "provider": "claude", "status": "running", "requested_model": "opus", "model": "opus",
                "model_selection_verified": True, "attempt": 1,
            }), encoding="utf-8")
            with patch.object(agents, "AGENTS_DIR", root / "agents"), \
                 patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(root / "state")}):
                for event in (INIT, TOOL_USE, TEXT, RESULT, RESULT):
                    agents._record_provider_event(agent_id, json.dumps(event))
                row = provider_usage.ProviderUsageStore(
                    root / "state" / "state" / "provider_usage.sqlite3"
                ).summary(days=1)["providers"]["claude"]
                meta = agents._read_meta(agent_id)
        self.assertEqual(1, row["turns"])
        self.assertEqual(4 + 203 + 38799 + 16194, row["total_tokens"])
        self.assertEqual((4 + 203 + 38799 + 16194, True), agents._agent_token_usage(meta))


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": self.temp.name}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def workflow(self, agent_id: str) -> None:
        input_hash = workflows.workflow_input_hash(
            prompt="p", provider="claude", cwd="/tmp", access_mode="workspace_write",
            scope={"access_mode": "workspace_write"}, role=None,
        )
        workflows.create_workflow(agent_id=agent_id, input_hash=input_hash, provider="claude")

    def tool(self, name: str) -> dict:
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name}]}}

    def test_native_state_changing_tools_make_replay_unsafe(self) -> None:
        for name, safe in (("Read", True), ("Grep", True), ("mcp__mac-mcp__write_file", True),
                           ("Bash", False), ("Edit", False), ("Task", False), ("mcp__other__thing", False)):
            agent_id = f"agt_claude_ckpt_{name.replace('_', '').replace('-', '').lower()[:20]}"
            with self.subTest(tool=name):
                self.workflow(agent_id)
                workflows.note_provider_event(agent_id, "claude", INIT)
                workflows.note_provider_event(agent_id, "claude", self.tool(name))
                state = workflows.workflow_for_agent(agent_id)
                self.assertEqual(safe, not state.get("uncertain_events"), state.get("uncertain_events"))

    def test_cursor_kinds(self) -> None:
        kind = workflows._provider_cursor_kind
        self.assertEqual("session_started", kind("claude", INIT))
        self.assertEqual("native_tool_event", kind("claude", TOOL_USE))
        self.assertEqual("mcp_tool_event", kind("claude", self.tool("mcp__mac-mcp__read_file")))
        self.assertEqual("agent_message", kind("claude", TEXT))
        self.assertEqual("turn_completed", kind("claude", RESULT))


@unittest.skipUnless(agents._find_binary("claude"), "Claude Code CLI not installed")
class InstalledCliTests(unittest.TestCase):
    def test_installed_cli_accepts_every_option_mac_mcp_passes(self) -> None:
        provider_compat.clear_cache()
        result = provider_compat.check("claude", agents._find_binary("claude"), env=agents._base_env())
        self.assertEqual("compatible", result["status"], result)


if __name__ == "__main__":
    unittest.main()
