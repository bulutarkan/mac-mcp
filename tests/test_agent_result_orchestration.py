from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_agents as agents
from mcp_server.agent_results import RESULT_ENVELOPE_MARKER


class TypedResultWorkerTests(unittest.TestCase):
    def _fixture(self, root: Path, agent_id: str) -> None:
        adir = root / "agents" / agent_id
        adir.mkdir(parents=True)
        input_hash = agents.workflow_input_hash(
            prompt="typed result fixture",
            provider="opencode",
            cwd=str(root),
            access_mode="read_only",
            scope={"access_mode": "read_only", "path_roots": [str(root)]},
            role=None,
        )
        checkpoint = agents.create_workflow(
            agent_id=agent_id,
            input_hash=input_hash,
            provider="opencode",
        )
        for name, value in {
            "prompt.txt": "typed result fixture",
            "effective_prompt.txt": "typed result fixture",
            "stdout.log": "",
            "stderr.log": "",
            "worker.log": "",
            "result.txt": "",
        }.items():
            (adir / name).write_text(value, encoding="utf-8")
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id,
            "provider": "opencode",
            "binary": "/tmp/opencode",
            "cwd": str(root),
            "team_id": None,
            "team_task_id": None,
            "status": "starting",
            "phase": "starting",
            "started_at": now,
            "spawn_requested_at": now,
            "last_activity_at": now,
            "timeout_s": 1200,
            "idle_timeout_s": None,
            "retries": 0,
            "retry_count": 0,
            "result_style": "concise",
            "result_contract_version": 1,
            "result_contract_status": "pending",
            "access_mode": "read_only",
            "permission_profile": "read_only",
            "capability_profile": "legacy",
            "scope": {"access_mode": "read_only", "path_roots": [str(root)]},
            "attempt": 1,
            "workflow_id": checkpoint["workflow_id"],
            "workflow_input_hash": input_hash,
            "resume_generation": 0,
            "session_id": None,
            "resume_session_id": None,
            "tool_call_count": 0,
            "step_count": 0,
        })

    @staticmethod
    def _opencode_stdout(text: str) -> str:
        return (
            json.dumps({"type": "text", "part": {"text": text}}, ensure_ascii=False)
            + "\n"
            + json.dumps({
                "type": "step_finish",
                "part": {"reason": "stop", "tokens": {"total": 12}},
            })
            + "\n"
        )

    def test_valid_marked_contract_completes_and_get_agent_returns_typed_result(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"), patch.object(
            agents, "TEAMS_DIR", Path(td) / "teams"
        ):
            root = Path(td)
            agent_id = "agt_typed_valid"
            self._fixture(root, agent_id)
            payload = {
                "schema_version": 1,
                "outcome": "success",
                "summary": "typed done",
                "claims": [{"id": "c1", "statement": "done"}],
                "evidence": [{"id": "e1", "ref": "test:1", "summary": "fixture", "claim_ids": ["c1"]}],
                "artifacts": [],
                "warnings": [],
                "confidence": 0.9,
                "errors": [],
            }
            handoff = RESULT_ENVELOPE_MARKER + "\n" + json.dumps(payload)
            def fake_attempt(*_args):
                (root / "agents" / agent_id / "stdout.log").write_text(
                    self._opencode_stdout(handoff), encoding="utf-8",
                )
                return 0, None
            with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
                rc = agents._worker(agent_id)

            saved = agents._read_meta(agent_id)
            result = agents.get_agent(None, agent_id)
            self.assertEqual(0, rc)
            self.assertEqual("completed", saved["status"])
            self.assertEqual("valid", saved["result_contract_status"])
            self.assertEqual("typed done", result["result"])
            self.assertEqual(1, result["result_envelope"]["schema_version"])
            self.assertEqual("test:1", result["result_envelope"]["evidence"][0]["ref"])
            self.assertNotIn("stdout", result)

    def test_invalid_marked_contract_cannot_be_success(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"), patch.object(
            agents, "TEAMS_DIR", Path(td) / "teams"
        ):
            root = Path(td)
            agent_id = "agt_typed_invalid"
            self._fixture(root, agent_id)
            bad = RESULT_ENVELOPE_MARKER + '\n{"schema_version":1,"outcome":"success","summary":'
            def fake_attempt(*_args):
                (root / "agents" / agent_id / "stdout.log").write_text(
                    self._opencode_stdout(bad), encoding="utf-8",
                )
                return 0, None
            with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
                rc = agents._worker(agent_id)

            saved = agents._read_meta(agent_id)
            envelope = agents._read_result_envelope(agent_id, meta=saved, allow_legacy=False)
            self.assertEqual(1, rc)
            self.assertEqual("failed", saved["status"])
            self.assertEqual("invalid", saved["result_contract_status"])
            self.assertEqual("invalid_json", saved["result_contract_error"]["code"])
            self.assertEqual("failure", envelope["outcome"])
            self.assertIn("invalid typed result contract", saved["note"].lower())

    def test_reviewer_marker_and_typed_gate_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"), patch.object(
            agents, "TEAMS_DIR", Path(td) / "teams"
        ):
            root = Path(td)
            agent_id = "agt_gate_mismatch"
            self._fixture(root, agent_id)
            payload = {
                "schema_version": 1,
                "outcome": "success",
                "summary": "review",
                "claims": [],
                "evidence": [],
                "artifacts": [],
                "warnings": [],
                "confidence": 0.9,
                "errors": [],
                "quality_gate": {"decision": "fail", "feedback": "typed fail"},
            }
            handoff = "QUALITY_GATE: PASS\n" + RESULT_ENVELOPE_MARKER + "\n" + json.dumps(payload)
            def fake_attempt(*_args):
                (root / "agents" / agent_id / "stdout.log").write_text(
                    self._opencode_stdout(handoff), encoding="utf-8",
                )
                return 0, None
            with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
                rc = agents._worker(agent_id)
            saved = agents._read_meta(agent_id)
            self.assertEqual(1, rc)
            self.assertEqual("failed", saved["status"])
            self.assertEqual("invalid", saved["result_contract_status"])
            self.assertEqual("quality_gate_mismatch", saved["result_contract_error"]["code"])

    def test_plain_text_provider_uses_explicit_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"), patch.object(
            agents, "TEAMS_DIR", Path(td) / "teams"
        ):
            root = Path(td)
            agent_id = "agt_typed_legacy"
            self._fixture(root, agent_id)
            def fake_attempt(*_args):
                (root / "agents" / agent_id / "stdout.log").write_text(
                    self._opencode_stdout("legacy done"), encoding="utf-8",
                )
                return 0, None
            with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
                rc = agents._worker(agent_id)
            saved = agents._read_meta(agent_id)
            envelope = agents._read_result_envelope(agent_id, meta=saved, allow_legacy=False)
            self.assertEqual(0, rc)
            self.assertEqual("completed", saved["status"])
            self.assertEqual("legacy_fallback", saved["result_contract_status"])
            self.assertEqual("legacy done", envelope["summary"])


class ParentTypedResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_agents = agents.AGENTS_DIR
        self.old_teams = agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        agents.AGENTS_DIR.mkdir(parents=True)
        agents.TEAMS_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        agents.AGENTS_DIR = self.old_agents
        agents.TEAMS_DIR = self.old_teams
        self.temp.cleanup()

    def _agent(self, agent_id: str, task_id: str, envelope: dict, *, status: str = "completed") -> None:
        adir = agents.AGENTS_DIR / agent_id
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "result.txt").write_text("RAW_PROVIDER_TEXT_SHOULD_NOT_BE_PARENT_CONTEXT", encoding="utf-8")
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id,
            "team_id": "team-typed",
            "team_task_id": task_id,
            "title": task_id,
            "provider": "opencode",
            "access_mode": "read_only",
            "permission_profile": "trusted",
            "status": status,
            "phase": status,
            "started_at": now - 1,
            "spawn_requested_at": now - 1,
            "last_activity_at": now,
            "updated_at": now,
            "ended_at": now if status in agents.TERMINAL_STATUSES else None,
            "retry_count": 0,
            "retries": 0,
            "result_style": "concise",
            "result_contract_version": 1,
            "result_contract_status": envelope.get("contract_status", "valid"),
        })
        agents._write_result_envelope(agent_id, envelope)

    def test_dependency_prompt_uses_deterministic_fan_in_not_raw_provider_text(self) -> None:
        base = {
            "schema_version": 1, "contract_status": "valid", "outcome": "success",
            "summary": "structured child",
            "claims": [{"id": "c1", "statement": "x", "key": "answer", "value": 1}],
            "evidence": [{"id": "e1", "ref": "ref:shared", "summary": "proof", "claim_ids": ["c1"]}],
            "artifacts": [], "warnings": [], "confidence": 0.9, "errors": [],
            "provenance": {"agent_id": "source"}, "quality_gate": None,
            "truncation": {"truncated": False, "omitted": {}},
        }
        self._agent("agt-a", "a", base)
        self._agent("agt-b", "b", base)
        task_map = {
            "a": {"id": "a", "title": "A", "latest_agent_id": "agt-a"},
            "b": {"id": "b", "title": "B", "latest_agent_id": "agt-b"},
            "c": {"id": "c", "title": "C", "prompt": "combine", "depends_on": ["a", "b"]},
        }
        prompt = agents._team_task_prompt(
            {"team_id": "team-typed"}, task_map["c"], task_map,
        )
        self.assertIn("deterministic fan-in", prompt)
        self.assertIn('"source_task_ids": ["a", "b"]', prompt)
        self.assertNotIn("RAW_PROVIDER_TEXT_SHOULD_NOT_BE_PARENT_CONTEXT", prompt)

    def test_team_summary_exposes_fan_in_and_typed_partial_failure(self) -> None:
        partial = {
            "schema_version": 1, "contract_status": "valid", "outcome": "partial_failure",
            "summary": "partial result", "claims": [], "evidence": [],
            "artifacts": [{"id": "a1", "ref": "/tmp/a", "description": "a"}],
            "warnings": ["one caveat"], "confidence": 0.6,
            "errors": [{"code": "subtask", "message": "one subtask failed"}],
            "provenance": {"agent_id": "agt-a"}, "quality_gate": None,
            "truncation": {"truncated": False, "omitted": {}},
        }
        self._agent("agt-a", "a", partial)
        now = time.time()
        team = {
            "team_id": "team-typed", "title": "typed", "provider": "opencode",
            "access_mode": "read_only", "permission_profile": "trusted", "scope": None,
            "created_at": now - 1, "updated_at": now, "agent_ids": ["agt-a"],
            "scheduler_version": 1, "cancelled": False, "max_parallel": 1,
            "max_revisions": 0, "retries": 0, "team_retry_count": 0,
            "team_retry_reservations": [], "budget_exhausted_reason": None,
            "tasks": [{
                "id": "a", "title": "A", "role": None, "state": "completed",
                "depends_on": [], "review_of": None, "revision_count": 0,
                "max_revisions": 0, "gate_attempts": 0, "failure_reason": None,
                "active_agent_id": None, "latest_agent_id": "agt-a",
                "agent_ids": ["agt-a"], "resource_claims": [],
            }],
        }
        summary = agents._team_summary("team-typed", team)
        self.assertFalse(summary["success"])
        self.assertEqual("partial_failure", summary["outcome"])
        self.assertEqual(1, summary["typed_partial_count"])
        self.assertEqual("partial_failure", summary["tasks"][0]["result_outcome"])
        self.assertEqual("/tmp/a", summary["result_fan_in"]["artifacts"][0]["ref"])

    def test_wait_agents_bounded_result_reports_omissions(self) -> None:
        huge = {
            "schema_version": 1, "contract_status": "valid", "outcome": "success",
            "summary": "summary",
            "claims": [{"id": f"c{i}", "statement": "x" * 400} for i in range(20)],
            "evidence": [{"id": f"e{i}", "ref": f"ref:{i}", "summary": "y" * 300, "claim_ids": []} for i in range(20)],
            "artifacts": [], "warnings": [], "confidence": 0.8, "errors": [],
            "provenance": {"agent_id": "agt-big"}, "quality_gate": None,
            "truncation": {"truncated": False, "omitted": {}},
        }
        self._agent("agt-big", "big", huge)
        result = agents.wait_agents(None, agent_ids=["agt-big"], mode="all", timeout_s=0)
        row = result["agents"][0]
        self.assertTrue(row["result_truncation"]["truncated"])
        self.assertTrue(row["result_truncation"]["omitted"])
        self.assertEqual("summary", row["result"])
        self.assertNotIn("RAW_PROVIDER_TEXT_SHOULD_NOT_BE_PARENT_CONTEXT", json.dumps(row))


if __name__ == "__main__":
    unittest.main()
