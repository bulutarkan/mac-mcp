"""GitHub Copilot as a delegated-agent provider: headless JSON runs, BYOK or a GitHub token, only Mac MCP's server."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import agent_timeline
from mcp_server import extra_harness_providers as extra
from mcp_server import harness_providers as harness
from mcp_server import tools_agents as agents
from mcp_server import workflow_checkpoints

FIXTURE = Path(__file__).parent / "fixtures" / "copilot_events.jsonl"
CLEAN_ENV = {key: value for key, value in os.environ.items()
             if not key.startswith("COPILOT_") and key not in {"OPENROUTER_API_KEY", "OPENAI_API_KEY",
                                                                "ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}}


def no_keychain(_name: str):
    return None


def events():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


class RegistrationTests(unittest.TestCase):
    def test_known_named_and_spawnable(self) -> None:
        self.assertEqual("GitHub Copilot", harness.LABELS["copilot"])
        self.assertIn("copilot", agents._PROVIDER_NAMES)
        self.assertIn("copilot", agents._USAGE_REPORTING_PROVIDERS)
        self.assertEqual("copilot", extra.BINARIES["copilot"])


class EnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "copilot-home"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def env(self, model: str, **owner_env: str):
        with patch.dict(os.environ, {**CLEAN_ENV, **owner_env}, clear=True):
            return extra.copilot_env(self.home, no_keychain, model)

    def test_openrouter_model_runs_on_the_owners_key_without_github(self) -> None:
        env = self.env("openrouter/poolside/laguna-s-2.1:free", OPENROUTER_API_KEY="or-key", GH_TOKEN="gh")
        self.assertEqual("https://openrouter.ai/api/v1", env["COPILOT_PROVIDER_BASE_URL"])
        self.assertEqual("openai", env["COPILOT_PROVIDER_TYPE"])
        self.assertEqual("or-key", env["COPILOT_PROVIDER_API_KEY"])
        self.assertEqual("poolside/laguna-s-2.1:free", env["COPILOT_MODEL"])
        self.assertNotIn("COPILOT_GITHUB_TOKEN", env)  # BYOK needs no GitHub account
        self.assertEqual(str(self.home), env["COPILOT_HOME"])
        self.assertEqual("false", env["COPILOT_AUTO_UPDATE"])

    def test_anthropic_and_openai_prefixes(self) -> None:
        env = self.env("anthropic/claude-sonnet-4", ANTHROPIC_API_KEY="an")
        self.assertEqual(("anthropic", "https://api.anthropic.com", "an"),
                         (env["COPILOT_PROVIDER_TYPE"], env["COPILOT_PROVIDER_BASE_URL"], env["COPILOT_PROVIDER_API_KEY"]))
        self.assertEqual("https://api.openai.com/v1", self.env("openai/gpt-5", OPENAI_API_KEY="oa")["COPILOT_PROVIDER_BASE_URL"])

    def test_a_plain_model_uses_the_github_token(self) -> None:
        env = self.env("claude-sonnet-4.5", GH_TOKEN="gh-token")
        self.assertEqual("gh-token", env["COPILOT_GITHUB_TOKEN"])
        self.assertEqual("claude-sonnet-4.5", env["COPILOT_MODEL"])
        self.assertNotIn("COPILOT_PROVIDER_BASE_URL", env)

    def test_the_owners_own_provider_setup_is_passed_on(self) -> None:
        env = self.env("my-local-model", COPILOT_PROVIDER_BASE_URL="http://localhost:11434/v1",
                       COPILOT_PROVIDER_TYPE="openai", OPENROUTER_API_KEY="unrelated")
        self.assertEqual("http://localhost:11434/v1", env["COPILOT_PROVIDER_BASE_URL"])
        self.assertEqual("my-local-model", env["COPILOT_MODEL"])
        self.assertNotIn("COPILOT_PROVIDER_API_KEY", env)

    def test_no_unrelated_keys_reach_the_agent(self) -> None:
        with patch.dict(os.environ, {**CLEAN_ENV, "OPENROUTER_API_KEY": "or", "OPENAI_API_KEY": "oa",
                                     "ANTHROPIC_API_KEY": "an"}, clear=True):
            env = extra.provider_env("copilot", self.home, Path(self.temp.name), no_keychain,
                                     model="openrouter/x/y:free")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("OPENROUTER_API_KEY", env)  # only as COPILOT_PROVIDER_API_KEY
        self.assertEqual("or", env["COPILOT_PROVIDER_API_KEY"])

    def test_sign_in_state(self) -> None:
        with patch.dict(os.environ, CLEAN_ENV, clear=True):
            ready, why = extra.sign_in_state("copilot", no_keychain)
        self.assertFalse(ready)
        self.assertIn("openrouter/<model>", why)
        with patch.dict(os.environ, {**CLEAN_ENV, "OPENROUTER_API_KEY": "or"}, clear=True):
            self.assertTrue(extra.sign_in_state("copilot", no_keychain)[0])

    def test_any_model_may_be_named(self) -> None:
        with patch.dict(os.environ, CLEAN_ENV, clear=True):
            self.assertEqual(([], None), extra.models("copilot", "/bin/copilot", {}))


class HomeAndCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_private_home_holds_only_mac_mcp(self) -> None:
        home = extra.prepare_home("copilot", self.state, "http://127.0.0.1:8765/mcp", "scoped-token")
        config = json.loads((home / "mcp-config.json").read_text())
        self.assertEqual(["mac-mcp"], list(config["mcpServers"]))
        server = config["mcpServers"]["mac-mcp"]
        self.assertEqual(("http", "http://127.0.0.1:8765/mcp", "Bearer scoped-token"),
                         (server["type"], server["url"], server["headers"]["Authorization"]))
        self.assertEqual(0o600, (home / "mcp-config.json").stat().st_mode & 0o777)
        extra.remove_credentials("copilot", self.state)
        self.assertFalse((home / "mcp-config.json").exists())

    def test_command_is_headless_json_with_github_extras_off(self) -> None:
        meta = {"provider": "copilot", "binary": "/opt/homebrew/bin/copilot", "model": "openrouter/x/y:free",
                "reasoning": "high", "resume_session_id": "0aabb489-275e-4117-a1a4-293bf8ca4dd7",
                "cwd": "/tmp/work", "access_mode": "read_only", "extra_roots": ["/tmp/work", "/tmp/data"]}
        cmd = extra.command(meta, "do the thing")
        self.assertEqual(["/opt/homebrew/bin/copilot", "--prompt=do the thing", "--output-format", "json"], cmd[:4])
        for flag in ("--allow-all-tools", "--no-ask-user", "--disable-builtin-mcps", "--no-auto-update",
                     "--disable-client-telemetry"):
            self.assertIn(flag, cmd)
        excluded = cmd[cmd.index("--excluded-tools") + 1: cmd.index("--excluded-tools") + 1 + len(extra.COPILOT_EXCLUDED_TOOLS)]
        self.assertEqual(list(extra.COPILOT_EXCLUDED_TOOLS), excluded)
        self.assertIn("--secret-env-vars=COPILOT_PROVIDER_API_KEY,COPILOT_GITHUB_TOKEN", cmd)
        self.assertEqual("high", cmd[cmd.index("--reasoning-effort") + 1])
        self.assertEqual(meta["resume_session_id"], cmd[cmd.index("--resume") + 1])
        self.assertEqual(["--add-dir", "/tmp/data"], cmd[cmd.index("--add-dir"):cmd.index("--add-dir") + 2])
        self.assertNotIn("--allow-all-paths", cmd)  # restricted: the Seatbelt boundary and the scope decide
        self.assertNotIn("--model", cmd)  # COPILOT_MODEL carries it without the provider prefix
        full = extra.command({**meta, "access_mode": "full"}, "x")
        self.assertIn("--allow-all-paths", full)
        self.assertIn("--allow-all-urls", full)


class StreamTests(unittest.TestCase):
    def test_progress_from_the_real_stream(self) -> None:
        meta = {"provider": "copilot"}
        unknown = []
        for event in events():
            harness.apply_event(meta, event, 1.0, lambda m, kind: unknown.append(kind))
        self.assertEqual([], unknown)
        self.assertEqual(2, meta["tool_call_count"])
        self.assertEqual("bash", meta["last_tool"])
        self.assertEqual(3, meta["step_count"])
        self.assertEqual("0aabb489-275e-4117-a1a4-293bf8ca4dd7", meta["session_id"])
        self.assertNotIn("stop_reason", meta)

    def test_extract_answer_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stdout.log"
            path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            final, session, usage = harness.extract("copilot", path)
            self.assertIn("macOS 26.5.1", final)
            self.assertEqual("0aabb489-275e-4117-a1a4-293bf8ca4dd7", session)
            self.assertIsNone(usage)  # counted from the session journal instead
            self.assertIsNone(harness.stop_reason_failure("copilot", path))
            failing = Path(temp) / "failed.log"
            failing.write_text(json.dumps({"type": "result", "sessionId": "s", "exitCode": 1}) + "\n", encoding="utf-8")
            self.assertEqual("exit_code_1", harness.stop_reason_failure("copilot", failing))
            cut = Path(temp) / "cut.log"
            cut.write_text("\n".join(FIXTURE.read_text(encoding="utf-8").splitlines()[:10]) + "\n", encoding="utf-8")
            self.assertEqual("missing_terminal_event", harness.stop_reason_failure("copilot", cut))

    def test_tool_calls_tell_mac_mcp_tools_apart(self) -> None:
        calls = [call for event in events() for call in extra.tool_calls("copilot", event)]
        self.assertEqual([("mac-mcp-mac_snapshot", True), ("bash", False)], calls)
        self.assertTrue(extra.turn_finished("copilot", events()[-1]))

    def test_a_run_that_used_bash_is_not_replayed_automatically(self) -> None:
        self.assertIn("view", extra.READ_ONLY_TOOLS["copilot"])
        self.assertNotIn("bash", extra.READ_ONLY_TOOLS["copilot"])
        self.assertNotIn("edit", extra.READ_ONLY_TOOLS["copilot"])

    def test_timeline_names_the_mac_mcp_tool_the_command_and_the_answer(self) -> None:
        steps = agent_timeline.from_events(FIXTURE.read_text(encoding="utf-8").splitlines())["steps"]
        kinds = [step["kind"] for step in steps]
        self.assertIn("command", kinds)
        command = next(step for step in steps if step["kind"] == "command")
        self.assertEqual(("sw_vers", "done"), (command["detail"], command["status"]))
        self.assertTrue(any(step["kind"] != "command" and "snapshot" in json.dumps(step).lower() for step in steps))
        self.assertTrue(any(step["kind"] == "message" and "26.5.1" in step.get("text", "") for step in steps))
        failed = agent_timeline.from_events([json.dumps({"type": "result", "sessionId": "s", "exitCode": 2})])["steps"]
        self.assertEqual("error", failed[-1]["kind"])


class UsageJournalTests(unittest.TestCase):
    def test_the_journal_is_read_from_copilots_own_private_home(self) -> None:
        state = Path("/tmp/state")
        self.assertEqual(state / "copilot-home", harness.private_home(state, "copilot"))
        self.assertEqual(state / "kimi-home", harness.private_home(state, "kimi"))
        self.assertEqual(state / "grok-home", harness.private_home(state, "grok"))
        with tempfile.TemporaryDirectory() as temp:
            home = extra.prepare_home("copilot", Path(temp), "http://127.0.0.1:8765/mcp", "t")
            self.assertEqual(harness.private_home(Path(temp), "copilot"), home)

    def test_records_come_from_the_session_journal_once_each(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            session = "0aabb489-275e-4117-a1a4-293bf8ca4dd7"
            journal = home / "session-state" / session / "events.jsonl"
            journal.parent.mkdir(parents=True)
            lines = [
                {"type": "session.usage_record", "id": "u1", "timestamp": "2026-10-23T09:00:00.000Z",
                 "data": {"usage": {"model": "x/y:free", "inputTokens": 100, "outputTokens": 10,
                                    "cacheReadTokens": 40, "cacheWriteTokens": 0, "reasoningTokens": 0}}},
                {"type": "assistant.message", "id": "m1", "timestamp": "2026-10-23T09:00:01.000Z", "data": {}},
                {"type": "session.usage_record", "id": "u2", "timestamp": "2026-10-23T09:05:00.000Z",
                 "data": {"usage": {"model": "x/y:free", "inputTokens": 200, "outputTokens": 20}}},
            ]
            journal.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            records = extra.copilot_usage_records(home, session, 0)
            self.assertEqual(["u1", "u2"], [r["event_id"] for r in records])
            self.assertEqual((100, 10, 40, 110), (records[0]["input"], records[0]["output"],
                                                  records[0]["cache_read"], records[0]["total"]))
            from datetime import datetime
            since = datetime.fromisoformat("2026-10-23T09:01:00+00:00").timestamp()
            self.assertEqual(["u2"], [r["event_id"] for r in extra.copilot_usage_records(home, session, since)])
            self.assertEqual([], extra.copilot_usage_records(home, "../escape", 0))
            self.assertEqual([], extra.copilot_usage_records(home, None, 0))


class CompatTests(unittest.TestCase):
    def test_probe_names_the_flags_mac_mcp_uses(self) -> None:
        from mcp_server import provider_compat

        flags = provider_compat.REQUIRED_OPTIONS["copilot"][0][1]
        for flag in ("--prompt", "--output-format", "--allow-all-tools", "--excluded-tools", "--resume"):
            self.assertIn(flag, flags)


if __name__ == "__main__":
    unittest.main()
