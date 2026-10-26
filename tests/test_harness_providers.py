"""Kimi Code and Grok Build as delegated-agent providers (experimental, off by default).

They run headless like Codex, Claude Code and OpenCode: Mac MCP starts the CLI in a macOS
Seatbelt boundary for the access mode, gives it a private CLI home with the owner's model
settings and sign-in copied in for the run and only Mac MCP's scoped MCP server, and reads
its JSON event stream. The event samples below are shaped like real runs of Kimi Code 2.2.0
(`--output-format stream-json`) and Grok Build 1.0.52 (`--output-format streaming-json`).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import agent_timeline, harness_providers as harness
from mcp_server import provider_compat, provider_usage, tools_agents as agents, workflow_checkpoints

KIMI_RUN = [
    {"role": "meta", "type": "system.version", "version": "2.2.0"},
    {"role": "assistant", "tool_calls": [{"type": "function", "id": "call-1",
                                          "function": {"name": "Read", "arguments": "{\"path\":\"sentinel.txt\"}"}}]},
    {"role": "tool", "tool_call_id": "call-1", "content": "1\tharness sentinel line"},
    {"role": "assistant", "tool_calls": [{"type": "function", "id": "call-2",
                                          "function": {"name": "mcp__mac-mcp__get_system_info", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call-2", "content": "{\"ok\": true}"},
    {"role": "assistant", "content": "harness sentinel line"},
    {"role": "meta", "type": "session.resume_hint", "session_id": "session_abc", "command": "kimi -r session_abc"},
]
GROK_RUN = [
    {"type": "available_commands", "tools": ["read_file", "write"]},
    {"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 5}},
    {"type": "text", "data": "Let me look."},
    {"type": "tool_call", "toolCallId": "c1", "title": "read_file", "kind": "read", "status": "pending",
     "toolName": "read_file", "rawInput": {"target_file": "sentinel.txt"}},
    {"type": "tool_call_update", "toolCallId": "c1", "status": None, "content": []},
    {"type": "tool_call_update", "toolCallId": "c1", "status": "completed",
     "content": [{"type": "content", "content": {"type": "text", "text": "1→harness sentinel line\n"}}]},
    {"type": "tool_call", "toolCallId": "c2", "toolName": "use_tool",
     "rawInput": {"tool_name": "mac-mcp__mac_snapshot", "tool_input": {}}},
    {"type": "tool_call_update", "toolCallId": "c2", "status": "completed", "content": []},
    {"type": "text", "data": "har"},
    {"type": "text", "data": "ness sentinel line"},
    {"type": "end", "stopReason": "end_turn", "sessionId": "01a1-session", "requestId": "req-1",
     "usage": {"input_tokens": 26255, "cache_read_input_tokens": 45760, "cache_creation_input_tokens": 0,
               "output_tokens": 166, "reasoning_tokens": 90, "total_tokens": 72181}},
]


def _lines(events) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


class OwnerHomes(unittest.TestCase):
    """A temporary owner home for each CLI, and an agents directory, for one test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.kimi_home = self.tmp / "owner" / ".kimi-code"
        self.grok_home = self.tmp / "owner" / ".grok"
        self.kimi_home.mkdir(parents=True)
        self.grok_home.mkdir(parents=True)
        env = patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.kimi_home), "GROK_HOME": str(self.grok_home)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("KIMI_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY"):
            os.environ.pop(name, None)
        old = agents.AGENTS_DIR
        agents.AGENTS_DIR = self.tmp / "agents"
        self.addCleanup(setattr, agents, "AGENTS_DIR", old)


class CatalogAndConfigTests(unittest.TestCase):
    def test_providers_are_known_and_named(self) -> None:
        self.assertEqual({"kimi": "Kimi Code", "grok": "Grok Build"},
                         {k: harness.LABELS[k] for k in ("kimi", "grok")})
        self.assertEqual({"pi", "kilo", "antigravity", "cline", "goose", "copilot"},
                         set(harness.LABELS) - {"kimi", "grok"})
        self.assertTrue({"kimi", "grok"} <= agents._PROVIDER_NAMES)
        self.assertEqual("Kimi Code", agents._PROVIDER_LABELS["kimi"])

    def test_kimi_models_come_from_the_owner_config_without_tomllib(self) -> None:
        config = 'default_model = "or"\n[providers.openrouter]\ntype = "openai"\n\n[models.or]\nprovider = "openrouter"\n' \
                 '[models."kimi-k3"]\nprovider = "kimi"\n'
        self.assertEqual((["or", "kimi-k3"], "or"), harness.kimi_models(config))
        self.assertEqual(([], None), harness.kimi_models(""))

    def test_grok_models_list_and_default(self) -> None:
        output = "You are using XAI_API_KEY.\n\nDefault model: or\n\nAvailable models:\n  - grok-4.6\n  - grok-4.5\n  * or (default)\n"
        self.assertEqual((["grok-4.6", "grok-4.5", "or"], "or"), harness.parse_grok_models(output))

    def test_key_variables_named_by_model_settings(self) -> None:
        kimi = '[providers.openrouter]\napi_key_env = "OPENROUTER_API_KEY"\n'
        grok = '[model.or]\nenv_key = "LAB_KEY"\n'
        self.assertEqual(["OPENROUTER_API_KEY"], harness.referenced_env_names(kimi))
        self.assertEqual(["LAB_KEY"], harness.referenced_env_names(grok))

    def test_grok_config_keeps_everything_but_the_owners_mcp_servers(self) -> None:
        config = (
            '[model.or]\nmodel = "x"\n\n[mcp_servers.github]\nurl = "https://example.invalid"\n'
            '[mcp_servers.github.headers]\nAuthorization = "Bearer owner-secret"\n\n[models]\ndefault = "or"\n'
        )
        cleaned = harness.grok_config_without_mcp(config)
        self.assertIn('[model.or]', cleaned)
        self.assertIn('default = "or"', cleaned)
        self.assertNotIn("mcp_servers", cleaned)
        self.assertNotIn("owner-secret", cleaned)

    def test_compatibility_contract_covers_the_options_mac_mcp_passes(self) -> None:
        kimi = " ".join(token for _, tokens in provider_compat.REQUIRED_OPTIONS["kimi"] for token in tokens)
        grok = " ".join(token for _, tokens in provider_compat.REQUIRED_OPTIONS["grok"] for token in tokens)
        for token in ("--prompt", "stream-json", "--session", "--add-dir"):
            self.assertIn(token, kimi)
        for token in ("--single", "streaming-json", "bypassPermissions", "--disallowed-tools", "--resume", "--cwd"):
            self.assertIn(token, grok)


class SignInTests(OwnerHomes):
    def test_nothing_configured_is_not_signed_in_and_says_how(self) -> None:
        for provider in ("kimi", "grok"):
            ready, reason = harness.sign_in_state(provider, lambda name: None)
            self.assertFalse(ready)
            self.assertIn("login", reason)

    def test_a_login_an_api_key_or_a_configured_model_is_enough(self) -> None:
        (self.kimi_home / "credentials").mkdir()
        (self.kimi_home / "credentials" / "kimi-code.json").write_text("{}")
        self.assertTrue(harness.sign_in_state("kimi", lambda name: None)[0])
        (self.grok_home / "config.toml").write_text('[model.or]\nenv_key = "OPENROUTER_API_KEY"\n')
        self.assertFalse(harness.sign_in_state("grok", lambda name: None)[0])
        self.assertTrue(harness.sign_in_state("grok", lambda name: "key" if name == "OPENROUTER_API_KEY" else None)[0])
        with patch.dict(os.environ, {"XAI_API_KEY": "x"}):
            (self.grok_home / "config.toml").unlink()
            self.assertTrue(harness.sign_in_state("grok", lambda name: None)[0])

    def test_spawn_refuses_before_starting_when_not_signed_in(self) -> None:
        with self.assertRaises(agents.HTTPException) as raised:
            agents._require_provider_sign_in("grok", "read_only", "/opt/homebrew/bin/grok")
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("provider_sign_in_required", raised.exception.detail["error"])


class PrivateHomeTests(OwnerHomes):
    def test_kimi_home_gets_settings_sign_in_and_only_mac_mcps_server(self) -> None:
        (self.kimi_home / "config.toml").write_text('default_model = "or"\n')
        (self.kimi_home / "mcp.json").write_text('{"mcpServers": {"owner": {"url": "https://owner.invalid"}}}')
        (self.kimi_home / "credentials").mkdir()
        (self.kimi_home / "credentials" / "kimi-code.json").write_text('{"access_token": "t"}')
        state = self.tmp / "state"
        home = harness.prepare_home("kimi", state, "http://127.0.0.1:8877/mcp", "scoped-token")
        self.assertEqual('default_model = "or"\n', (home / "config.toml").read_text())
        servers = json.loads((home / "mcp.json").read_text())["mcpServers"]
        self.assertEqual(["mac-mcp"], list(servers))
        self.assertEqual("Bearer scoped-token", servers["mac-mcp"]["headers"]["Authorization"])
        self.assertEqual(0o600, (home / "credentials" / "kimi-code.json").stat().st_mode & 0o777)
        self.assertEqual(0o600, (home / "mcp.json").stat().st_mode & 0o777)
        harness.remove_credentials("kimi", state)
        self.assertFalse((home / "credentials").exists())
        self.assertFalse((home / "mcp.json").exists())
        self.assertTrue((home / "config.toml").exists())

    def test_grok_home_gets_its_auth_and_a_token_that_is_removed_after_the_run(self) -> None:
        (self.grok_home / "config.toml").write_text('[mcp_servers.owner]\nurl = "https://owner.invalid"\n[models]\ndefault = "or"\n')
        (self.grok_home / "auth.json").write_text('{"key": "k"}')
        state = self.tmp / "state"
        home = harness.prepare_home("grok", state, "http://127.0.0.1:8877/mcp", "scoped-token")
        config = (home / "config.toml").read_text()
        self.assertNotIn("owner.invalid", config)
        self.assertIn("[mcp_servers.mac-mcp]", config)
        self.assertIn('Authorization = "Bearer scoped-token"', config)
        self.assertTrue((home / "auth.json").is_file())
        harness.remove_credentials("grok", state)
        self.assertFalse((home / "auth.json").exists())
        self.assertNotIn("scoped-token", (home / "config.toml").read_text())
        self.assertIn('default = "or"', (home / "config.toml").read_text())

    def test_environment_points_at_the_private_home_and_carries_only_named_keys(self) -> None:
        (self.kimi_home / "config.toml").write_text('[providers.or]\napi_key_env = "OPENROUTER_API_KEY"\n')
        agent_id = "agt_kimi_env"
        (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
        meta = {"provider": "kimi", "access_mode": "read_only", "mcp_endpoint": "http://127.0.0.1:8877/mcp"}
        with patch.dict(os.environ, {"MAC_MCP_TEST_SECRET": "no", "MAC_MCP_AGENT_TOKEN": "no"}), \
                patch.object(agents, "_keychain_secret", return_value="from-keychain"):
            env, cleanup = agents._provider_env(agent_id, meta, "scoped-token")
        state = agents.AGENTS_DIR / agent_id / "provider_state" / "kimi"
        self.assertIsNone(cleanup)
        self.assertEqual(str(state / "kimi-home"), env["KIMI_CODE_HOME"])
        self.assertEqual(str(state / "home"), env["HOME"])
        self.assertEqual("from-keychain", env["OPENROUTER_API_KEY"])
        self.assertNotIn("MAC_MCP_TEST_SECRET", env)
        self.assertNotIn("MAC_MCP_AGENT_TOKEN", env)
        self.assertEqual(0o700, state.stat().st_mode & 0o777)

    def test_a_follow_up_runs_in_its_first_agents_home_where_the_session_is(self) -> None:
        for agent_id in ("agt_first", "agt_follow"):
            (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
        (agents.AGENTS_DIR / "agt_first" / "meta.json").write_text(json.dumps({"agent_id": "agt_first"}))
        meta = {"provider": "grok", "resume_session_id": "s1", "parent_agent_id": "agt_first"}
        self.assertEqual(agents.AGENTS_DIR / "agt_first" / "provider_state" / "grok", agents._harness_state("agt_follow", meta))


class CommandTests(unittest.TestCase):
    def test_kimi_runs_one_prompt_as_a_json_stream(self) -> None:
        cmd = harness.command({"provider": "kimi", "binary": "kimi", "model": "or", "resume_session_id": "session_1",
                               "cwd": "/w", "extra_roots": ["/w", "/x"]}, "do it")
        self.assertEqual(["kimi", "--prompt", "do it", "--output-format", "stream-json", "--model", "or",
                          "--session", "session_1", "--add-dir", "/x"], cmd)

    def test_grok_never_waits_for_approval_and_loses_lifecycle_tools(self) -> None:
        write = harness.command({"provider": "grok", "binary": "grok", "cwd": "/w", "access_mode": "workspace_write",
                                 "reasoning": "high", "resume_session_id": "s"}, "do it")
        self.assertEqual(["grok", "--no-auto-update", "--single=do it", "--output-format", "streaming-json"], write[:5])
        self.assertIn("bypassPermissions", write)
        removed = write[write.index("--disallowed-tools") + 1].split(",")
        self.assertIn("scheduler_create", removed)
        self.assertIn("spawn_subagent", removed)
        self.assertNotIn("write", removed)
        self.assertEqual(["--reasoning-effort", "high", "--resume", "s"], write[-4:])
        read_only = harness.command({"provider": "grok", "binary": "grok", "cwd": "/w", "access_mode": "read_only"}, "x")
        removed = read_only[read_only.index("--disallowed-tools") + 1].split(",")
        self.assertIn("write", removed)
        self.assertIn("search_replace", removed)

    def test_the_agent_command_goes_to_the_harness_not_to_codex(self) -> None:
        meta = {"provider": "kimi", "binary": "/opt/homebrew/bin/kimi", "cwd": "/tmp", "access_mode": "full",
                "scope": {"access_mode": "full"}}
        cmd = agents._build_provider_command(meta, "hello", Path("/tmp/result.txt"))
        self.assertEqual("/opt/homebrew/bin/kimi", cmd[0])
        self.assertNotIn("exec", cmd)


class EventTests(unittest.TestCase):
    def test_kimi_events_count_tools_and_keep_the_session(self) -> None:
        meta = {"provider": "kimi"}
        unknown = []
        for event in KIMI_RUN:
            harness.apply_event(meta, event, 1.0, lambda m, kind: unknown.append(kind))
        self.assertEqual(2, meta["tool_call_count"])
        self.assertEqual("mcp__mac-mcp__get_system_info", meta["last_tool"])
        self.assertEqual("session_abc", meta["session_id"])
        self.assertEqual("finalizing", meta["phase"])
        self.assertEqual([], unknown)

    def test_grok_events_count_tools_usage_and_stop_reason(self) -> None:
        meta = {"provider": "grok"}
        unknown = []
        for event in [*GROK_RUN, {"type": "brand_new_event"}]:
            harness.apply_event(meta, event, 1.0, lambda m, kind: unknown.append(kind))
        self.assertEqual(2, meta["tool_call_count"])
        self.assertEqual("end_turn", meta["stop_reason"])
        self.assertEqual({"input": 26255, "output": 166, "reasoning": 90, "cache": {"read": 45760, "write": 0}}, meta["usage"])
        self.assertEqual(["brand_new_event"], unknown)

    def test_results_are_the_final_answer_session_and_usage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            kimi_log, grok_log = Path(td) / "kimi.log", Path(td) / "grok.log"
            kimi_log.write_text(_lines(KIMI_RUN))
            grok_log.write_text("\n--- provider attempt 1 ---\n" + _lines([{"type": "text", "data": "stale"}])
                                + "\n--- provider attempt 2 ---\n" + _lines(GROK_RUN))
            self.assertEqual(("harness sentinel line", "session_abc", None), harness.extract("kimi", kimi_log))
            result, session, usage = harness.extract("grok", grok_log)
            self.assertEqual("harness sentinel line", result)
            self.assertEqual("01a1-session", session)
            self.assertEqual(45760, usage["cache"]["read"])
            self.assertIsNone(harness.stop_reason_failure("grok", grok_log))
            grok_log.write_text(_lines([{"type": "end", "stopReason": "max_turn_requests", "sessionId": "s"}]))
            self.assertEqual("max_turn_requests", harness.stop_reason_failure("grok", grok_log))

    def test_grok_usage_is_recorded_once_from_its_end_event(self) -> None:
        normalized = provider_usage.normalize_grok_usage(GROK_RUN[-1]["usage"])
        self.assertTrue(normalized.recognized)
        self.assertEqual(72181, normalized.total_tokens)
        self.assertEqual(26255, normalized.input_tokens)
        self.assertIn("grok", provider_usage.REPORTING_PROVIDERS)
        self.assertFalse(provider_usage.normalize_grok_usage({}).recognized)
        self.assertIsNone(harness.grok_usage({}))

    def test_run_report_steps(self) -> None:
        kimi = agent_timeline.from_events(_lines(KIMI_RUN).splitlines())["steps"]
        self.assertEqual(["tool", "tool", "message"], [step["kind"] for step in kimi])
        self.assertEqual("Mac MCP · get_system_info", kimi[1]["detail"])
        grok = agent_timeline.from_events(_lines(GROK_RUN).splitlines())
        kinds = [step["kind"] for step in grok["steps"]]
        self.assertEqual(["message", "tool", "tool", "message"], kinds)
        self.assertEqual("harness sentinel line", grok["steps"][-1]["text"])
        self.assertEqual("Mac MCP · mac_snapshot", grok["steps"][2]["detail"])
        self.assertEqual(26255 + 45760, grok["usage"]["input_tokens"])


def _kimi_wire(home: Path, session: str, agent: str, records: list) -> Path:
    """A Kimi Code 2.2.0 session journal: one durable event per line, `time` in milliseconds."""
    wire = home / "sessions" / "wd_workspace" / session / "agents" / agent / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text(_lines([{"type": "metadata", "protocol_version": "1.5", "created_at": 1}, *records]))
    return wire


def _kimi_usage(time_ms: int, model: str = "kimi-k2.6", **usage: int) -> dict:
    counts = {"inputOther": 120, "output": 30, "inputCacheRead": 900, "inputCacheCreation": 50, **usage}
    return {"type": "usage.record", "agentId": "main", "model": model, "usage": counts,
            "usageScope": "turn", "time": time_ms}


class KimiUsageTests(OwnerHomes):
    """Kimi's stream-json has no usage; its session journal records every model call."""

    def test_records_come_from_the_runs_session_and_start_time_only(self) -> None:
        home = self.tmp / "kimi-home"
        _kimi_wire(home, "session_abc", "main", [
            _kimi_usage(1_000_000),  # an earlier run in the same home (a resume)
            {"type": "context.append_message", "message": {"role": "assistant"}, "time": 2_000_500},
            _kimi_usage(2_000_600),
        ])
        _kimi_wire(home, "session_abc", "sub_1", [_kimi_usage(2_000_700, model="kimi-k2.6-mini", output=5)])
        _kimi_wire(home, "session_other", "main", [_kimi_usage(2_000_800)])

        records = harness.kimi_usage_records(home, "session_abc", since=2_000.0)
        self.assertEqual(["session_abc:main:4", "session_abc:sub_1:2"], [r["event_id"] for r in records])
        self.assertEqual(120 + 30 + 900 + 50, records[0]["total"])
        self.assertEqual({"input": 120, "output": 30, "cache_read": 900, "cache_write": 50},
                         {k: records[0][k] for k in ("input", "output", "cache_read", "cache_write")})
        self.assertEqual("kimi-k2.6-mini", records[1]["model"])
        self.assertAlmostEqual(2_000.6, records[0]["timestamp"])
        # Without a session id every session in the home counts, still from the start time on.
        self.assertEqual(3, len(harness.kimi_usage_records(home, None, since=2_000.0)))
        self.assertEqual([], harness.kimi_usage_records(home, "../session_abc", since=0))
        self.assertEqual([], harness.kimi_usage_records(self.tmp / "missing", "session_abc", since=0))

    def test_a_finished_run_is_counted_once_in_provider_tokens_and_the_agent_total(self) -> None:
        agent_id = "agt_kimi_usage"
        (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
        started = time.time() - 60
        meta = {"agent_id": agent_id, "provider": "kimi", "status": "running", "started_at": started,
                "model": "or", "model_selection_verified": False}
        (agents.AGENTS_DIR / agent_id / "meta.json").write_text(json.dumps(meta))
        home = harness.private_home(agents._harness_state(agent_id, meta), "kimi")
        start_ms = int(started * 1000)
        _kimi_wire(home, "session_abc", "main", [_kimi_usage(start_ms + 600), _kimi_usage(start_ms + 900, output=70)])

        with patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.tmp / "state")}):
            usage = agents._ingest_kimi_usage(agent_id, meta, "session_abc")
            again = agents._ingest_kimi_usage(agent_id, meta, "session_abc")
            row = provider_usage.ProviderUsageStore(
                self.tmp / "state" / "state" / "provider_usage.sqlite3"
            ).summary(days=1)["providers"]["kimi"]
        per_call = 120 + 900 + 50
        self.assertEqual({"input": 240, "output": 100, "cache": {"read": 1800, "write": 100},
                          "total": 2 * per_call + 100}, usage)
        self.assertEqual(usage, again)
        self.assertEqual(2, row["turns"])
        self.assertEqual(2 * per_call + 100, row["total_tokens"])
        self.assertIsNone(row["reasoning_tokens"])  # Kimi does not split reasoning out; unknown, not zero
        self.assertEqual([("kimi-k2.6", 2 * per_call + 100)], [(m["model"], m["total_tokens"]) for m in row["models"]])
        self.assertEqual((2 * per_call + 100, True), agents._agent_token_usage(agents._read_meta(agent_id)))
        self.assertIn("kimi", provider_usage.REPORTING_PROVIDERS)

    def test_a_run_without_records_reports_nothing(self) -> None:
        agent_id = "agt_kimi_quiet"
        (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
        meta = {"agent_id": agent_id, "provider": "kimi", "started_at": 2_000.0}
        self.assertIsNone(agents._ingest_kimi_usage(agent_id, meta, "session_abc"))


class ResumeSafetyTests(unittest.TestCase):
    def test_native_changes_are_never_replayed_reads_and_mac_mcp_calls_are(self) -> None:
        marked = []
        with patch.object(workflow_checkpoints, "mark_checkpoint_unknown",
                          side_effect=lambda agent_id, reason, **kw: marked.append(kw.get("tool"))), \
                patch.object(workflow_checkpoints, "_record_provider_cursor"):
            for event in KIMI_RUN:
                workflow_checkpoints.note_provider_event("agt", "kimi", event)
            for event in GROK_RUN:
                workflow_checkpoints.note_provider_event("agt", "grok", event)
            self.assertEqual([], marked)
            workflow_checkpoints.note_provider_event("agt", "kimi", {"role": "assistant", "tool_calls": [
                {"id": "x", "function": {"name": "Bash", "arguments": "{}"}}]})
            workflow_checkpoints.note_provider_event("agt", "grok", {"type": "tool_call", "toolName": "run_terminal_command"})
            workflow_checkpoints.note_provider_event("agt", "grok", {"type": "tool_call", "toolName": "a_new_tool"})
        self.assertEqual(["Bash", "run_terminal_command", "a_new_tool"], marked)


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists(), "macOS Seatbelt only")
class BoundaryTests(OwnerHomes):
    def _profile(self, access_mode: str, workspace: Path) -> Path:
        agent_id = f"agt_boundary_{access_mode}"
        (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
        meta = {"provider": "grok", "binary": "/bin/sh", "access_mode": access_mode,
                "scope": {"access_mode": access_mode, "path_roots": [str(workspace)]}}
        cmd, profile = agents._provider_process_command(agent_id, meta, ["/bin/sh", "-c", "echo x > out.txt"])
        self.assertEqual(["/usr/bin/sandbox-exec", "-f", str(profile)], cmd[:3])
        return profile

    def test_the_boundary_refuses_writes_in_read_only_and_allows_the_workspace_otherwise(self) -> None:
        workspace = self.tmp / "work"
        workspace.mkdir()
        for mode, allowed in (("read_only", False), ("workspace_write", True)):
            profile = self._profile(mode, workspace)
            proc = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile), "/bin/sh", "-c", "echo x > out.txt"],
                                  cwd=workspace, capture_output=True, text=True, timeout=20)
            self.assertEqual(allowed, (workspace / "out.txt").exists(), (mode, proc.stderr))
            (workspace / "out.txt").unlink(missing_ok=True)
        # The owner's real CLI home stays out of reach either way.
        secret = self.grok_home / "auth.json"
        secret.write_text("owner sign-in")
        proc = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile), "/bin/cat", str(secret)],
                              capture_output=True, text=True, timeout=20)
        self.assertNotEqual(0, proc.returncode)
        self.assertNotIn("owner sign-in", proc.stdout)

    def test_full_access_runs_without_a_boundary(self) -> None:
        meta = {"provider": "kimi", "binary": "/bin/sh", "access_mode": "full", "scope": {"access_mode": "full"}}
        cmd, profile = agents._provider_process_command("agt_full", meta, ["kimi"])
        self.assertEqual((["kimi"], None), (cmd, profile))


class SurfaceTests(unittest.TestCase):
    def test_privacy_inventory_and_panel_know_the_providers(self) -> None:
        from mcp_server import chatgpt_panel, privacy_inventory
        self.assertIn("kimi", chatgpt_panel.PANEL_PROVIDERS)
        self.assertIn("grok", chatgpt_panel.PANEL_PROVIDERS)
        self.assertIn("kimi", privacy_inventory._PROVIDER_VENDORS)
        self.assertIn("grok", privacy_inventory._PROVIDER_VENDORS)

    def test_settings_app_has_a_switch_for_each(self) -> None:
        root = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"
        store = (root / "SettingsStore.swift").read_text(encoding="utf-8")
        view = (root / "SettingsView.swift").read_text(encoding="utf-8")
        for provider in ("kimi", "grok"):
            self.assertIn(f'"{provider}": .init(enabled: {provider}Enabled', store)
            self.assertIn(f'providerRow(id: "{provider}"', view)


if __name__ == "__main__":
    unittest.main()
