from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import runtime_settings
from mcp_server import tools_agents as agents

_SETTINGS_DIR = tempfile.TemporaryDirectory(prefix="mac-mcp-cumulative-settings-")
_SETTINGS_ENV = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(Path(_SETTINGS_DIR.name) / "settings.json")})


def setUpModule() -> None:
    _SETTINGS_ENV.start()
    runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})


def tearDownModule() -> None:
    _SETTINGS_ENV.stop()
    runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})
    _SETTINGS_DIR.cleanup()


def meta(provider: str, **extra) -> dict:
    row = {
        "provider": provider, "status": "running", "phase": "provider_starting",
        "started_at": time.time(), "last_activity_at": time.time(), "step_count": 0,
        "tool_call_count": 0, "session_id": None, "resume_session_id": None,
        "requested_model": "m", "model": "m",
    }
    row.update(extra)
    return row


def step(part_id: str, total: int) -> dict:
    return {
        "type": "step_finish", "sessionID": "ses_1",
        "part": {"id": part_id, "sessionID": "ses_1",
                 "tokens": {"input": total - 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}, "total": total}},
    }


class CumulativeUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.agents_root = self.root / "agents"
        patchers = [
            patch.object(agents, "AGENTS_DIR", self.agents_root),
            patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.root / "state")}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, agent_id: str, row: dict) -> None:
        (self.agents_root / agent_id).mkdir(parents=True)
        (self.agents_root / agent_id / "meta.json").write_text(json.dumps(row), encoding="utf-8")

    def tokens(self, agent_id: str) -> int:
        return agents._read_meta(agent_id)["usage_cumulative"]["total_tokens"]

    def test_codex_retries_add_up_and_a_replayed_turn_counts_once(self) -> None:
        self.write("agt_codex", meta("codex"))
        completed = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}
        agents._record_provider_event("agt_codex", json.dumps({"type": "thread.started", "thread_id": "t1"}))
        agents._record_provider_event("agt_codex", json.dumps({"type": "turn.started"}))
        agents._record_provider_event("agt_codex", json.dumps(completed))
        agents._record_provider_event("agt_codex", json.dumps(completed))  # replay
        self.assertEqual(12, self.tokens("agt_codex"))

        def retry(row: dict) -> None:
            agents._prepare_codex_usage_attempt(row, "agt_codex", 1)
            row["retry_count"] = 1

        agents._update_meta("agt_codex", retry)
        agents._record_provider_event("agt_codex", json.dumps({"type": "turn.started"}))
        agents._record_provider_event("agt_codex", json.dumps(completed))
        self.assertEqual(24, self.tokens("agt_codex"))
        self.assertEqual(12, agents._usage_total_tokens(agents._read_meta("agt_codex")["usage"]),
                         "meta.usage still shows only the latest turn")

    def test_every_opencode_step_counts(self) -> None:
        self.write("agt_open", meta("opencode"))
        for part_id, total in (("prt_1", 100), ("prt_2", 50), ("prt_2", 50), ("prt_3", 7)):
            agents._record_provider_event("agt_open", json.dumps(step(part_id, total)))
        self.assertEqual(157, self.tokens("agt_open"))

    def test_team_budget_sums_cumulative_usage_and_flags_unknown_usage(self) -> None:
        self.write("agt_a", meta("codex", status="completed", usage_cumulative={"total_tokens": 300, "events": 3}))
        self.write("agt_b", meta("chatgpt", status="completed", tool_call_count=4))
        self.write("agt_c", meta("opencode", status="completed", step_count=2))
        self.write("agt_d", meta("codex", status="running"))
        team = {"agent_ids": ["agt_a", "agt_b", "agt_c", "agt_d"], "created_at": time.time(),
                "admission_token_budget": 1000, "tasks": []}
        budget = agents._team_budget_snapshot(team)
        self.assertEqual(300, budget["total_tokens_used"])
        self.assertFalse(budget["total_tokens_complete"])
        self.assertEqual(["agt_b", "agt_c"], budget["token_usage_unknown_agents"],
                         "ChatGPT and a finished agent that reported nothing are unknown, a running one is not")
        self.assertEqual(700, budget["total_tokens_remaining"])

    def test_complete_when_every_agent_reports(self) -> None:
        self.write("agt_a", meta("codex", status="completed", usage_cumulative={"total_tokens": 5}))
        budget = agents._team_budget_snapshot({"agent_ids": ["agt_a"], "created_at": time.time(), "tasks": []})
        self.assertTrue(budget["total_tokens_complete"])
        self.assertEqual([], budget["token_usage_unknown_agents"])


if __name__ == "__main__":
    unittest.main()
