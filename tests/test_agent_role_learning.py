from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mcp_server.tools_agents as agents
from mcp_server.policy_scope import ResourceScope
from mcp_server import tools_lessons as lessons


class AgentRoleLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-role-agent-")
        self.root = Path(self.temp.name)
        self.agents_root = self.root / "agents"
        self.lesson_root = self.root / "lessons"
        self.env = patch.dict(os.environ, {"MAC_MCP_LESSON_DIR": str(self.lesson_root)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        with agents._WORKERS_LOCK:
            agents._WORKERS.clear()
        self.temp.cleanup()

    def active_reviewer_lesson(self) -> str:
        created = lessons.lesson_record(
            role="reviewer",
            trigger_context="reviewing Python retry and idempotency logic",
            mistake_pattern="flagging a duplicate without checking idempotency evidence",
            preferred_action="verify client instruction key and side-effect evidence before reporting a duplicate",
            confidence=0.8,
            evidence_refs=["user:correction"],
        )
        lesson_id = created["lesson"]["lesson_id"]
        lessons.lesson_feedback(lesson_id, "approve")
        return lesson_id

    def fake_spawn(self, *, role: str | None, provenance_class: str, prompt: str):
        scope = ResourceScope.from_dict({"access_mode": "read_only", "path_roots": [str(self.root)]})
        fake_proc = SimpleNamespace(pid=43210)
        fake_thread = SimpleNamespace(start=lambda: None)
        with patch.object(agents, "AGENTS_DIR", self.agents_root), \
             patch.object(agents, "_find_binary", return_value="/tmp/codex"), \
             patch.object(agents, "_base_env", return_value={}), \
             patch.object(agents.subprocess, "Popen", return_value=fake_proc), \
             patch.object(agents.threading, "Thread", return_value=fake_thread):
            spawned = agents._spawn_internal(
                settings=None,
                provider="codex",
                prompt=prompt,
                model="gpt-test",
                reasoning="high",
                cwd=str(self.root),
                timeout_s=600,
                title="Role-learning test",
                result_style="concise",
                access_mode="read_only",
                scope=scope,
                permission_profile="read_only",
                role=role,
                provenance_class=provenance_class,
            )
            agent_id = spawned["agent_id"]
            meta = agents._read_meta(agent_id)
            effective = (self.agents_root / agent_id / "effective_prompt.txt").read_text(encoding="utf-8")
        return spawned, meta, effective

    def test_trusted_reviewer_gets_relevant_active_lesson(self) -> None:
        lesson_id = self.active_reviewer_lesson()
        spawned, meta, effective = self.fake_spawn(
            role="reviewer",
            provenance_class="local",
            prompt="Review the Python retry path for duplicate side effects and idempotency mistakes.",
        )
        self.assertEqual("reviewer", spawned["role"])
        self.assertEqual([lesson_id], meta["injected_lesson_ids"])
        self.assertGreater(meta["lesson_context_chars"], 0)
        self.assertIn("Prior reviewer lessons", effective)
        self.assertIn("verify client instruction key", effective)
        self.assertIn("MAC_MCP_LESSON_CANDIDATE", effective)

    def test_untrusted_spawn_does_not_receive_trusted_lessons(self) -> None:
        lesson_id = self.active_reviewer_lesson()
        _, meta, effective = self.fake_spawn(
            role="reviewer",
            provenance_class="tainted_untrusted_web",
            prompt="Review the Python retry path for duplicate side effects and idempotency mistakes.",
        )
        self.assertNotIn(lesson_id, meta["injected_lesson_ids"])
        self.assertEqual([], meta["injected_lesson_ids"])
        self.assertEqual(0, meta["lesson_context_chars"])
        self.assertNotIn("Prior reviewer lessons", effective)
        self.assertIn("MAC_MCP_LESSON_CANDIDATE", effective)
        self.assertEqual("tainted_untrusted_web", meta["provenance_class"])

    def test_agent_without_role_keeps_old_prompt_shape(self) -> None:
        _, meta, effective = self.fake_spawn(
            role=None,
            provenance_class="local",
            prompt="Inspect the repository status.",
        )
        self.assertIsNone(meta["role"])
        self.assertEqual([], meta["injected_lesson_ids"])
        self.assertNotIn("Role-learning mode", effective)
        self.assertNotIn("Prior coder lessons", effective)
        self.assertNotIn("MAC_MCP_LESSON_CANDIDATE", effective)

    def test_worker_extracts_candidate_and_keeps_handoff_clean(self) -> None:
        agent_id = "agt_roleworker"
        adir = self.agents_root / agent_id
        adir.mkdir(parents=True)
        candidate = (
            'MAC_MCP_LESSON_CANDIDATE {"trigger_context":"reviewing retries","mistake_pattern":"false duplicate positive",'
            '"preferred_action":"check idempotency evidence before reporting","confidence":0.55}'
        )
        effective_prompt = "Review retries.\n\nRole-learning mode."
        for name, value in {
            "prompt.txt": "Review retries.",
            "effective_prompt.txt": effective_prompt,
            "stdout.log": "",
            "stderr.log": "",
            "worker.log": "",
            "result.txt": "",
        }.items():
            (adir / name).write_text(value, encoding="utf-8")
        meta = {
            "agent_id": agent_id,
            "provider": "opencode",
            "binary": "/tmp/opencode",
            "model": "opencode/test",
            "reasoning": "high",
            "cwd": str(self.root),
            "access_mode": "read_only",
            "permission_profile": "read_only",
            "capability_profile": "read_only",
            "scope": {"access_mode": "read_only", "path_roots": [str(self.root)]},
            "status": "starting",
            "phase": "starting",
            "started_at": 100.0,
            "spawn_requested_at": 100.0,
            "last_activity_at": 100.0,
            "timeout_s": 600,
            "idle_timeout_s": None,
            "retries": 0,
            "retry_count": 0,
            "result_style": "concise",
            "role": "reviewer",
            "provenance_class": "local",
            "injected_lesson_ids": [],
            "lesson_context_chars": 0,
            "lesson_candidate_ids": [],
            "attempt": 1,
            "team_id": None,
            "project": None,
            "resume_session_id": None,
            "session_id": None,
            "tool_call_count": 0,
            "step_count": 0,
        }
        (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        def fake_attempt(_agent_id, _meta, _prompt, _attempt_index):
            events = [
                {"type": "step_start", "sessionID": "ses_role", "part": {}},
                {"type": "text", "sessionID": "ses_role", "part": {"text": "Verified final handoff\n" + candidate}},
                {"type": "step_finish", "sessionID": "ses_role", "part": {"reason": "stop", "tokens": {"total": 12}}},
            ]
            (adir / "stdout.log").write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            return 0, None

        with patch.object(agents, "AGENTS_DIR", self.agents_root), \
             patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
            rc = agents._worker(agent_id)
            saved = agents._read_meta(agent_id)

        self.assertEqual(0, rc)
        self.assertEqual("Verified final handoff", (adir / "result.txt").read_text(encoding="utf-8"))
        self.assertEqual(1, len(saved["lesson_candidate_ids"]))
        candidate_row = lessons.lesson_search(role="reviewer", state="candidate")["results"][0]
        self.assertEqual(saved["lesson_candidate_ids"][0], candidate_row["lesson_id"])
        self.assertEqual("local", candidate_row["provenance_class"])
        self.assertIn(f"agent:{agent_id}", candidate_row["evidence_refs"])
        self.assertIn("session:ses_role", candidate_row["evidence_refs"])

    def test_retry_and_followup_preserve_role_and_provenance(self) -> None:
        agent_id = "agt_roleparent"
        adir = self.agents_root / agent_id
        adir.mkdir(parents=True)
        (adir / "prompt.txt").write_text("Review retry logic", encoding="utf-8")
        meta = {
            "agent_id": agent_id, "provider": "opencode", "status": "completed", "phase": "completed",
            "title": "review", "model": "opencode/test", "reasoning": "high", "cwd": str(self.root),
            "timeout_s": 600, "result_style": "concise", "access_mode": "read_only",
            "scope": {"access_mode": "read_only", "path_roots": [str(self.root)]},
            "permission_profile": "read_only", "capability_profile": "read_only",
            "role": "reviewer", "provenance_class": "tainted_untrusted_web",
            "attempt": 1, "idle_timeout_s": None, "retries": 0, "project": None,
            "session_id": "ses_parent", "started_at": 1.0, "last_activity_at": 1.0,
        }
        (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        with patch.object(agents, "AGENTS_DIR", self.agents_root), patch.object(agents, "_spawn_internal", return_value={"ok": True}) as spawn:
            agents._agent_action_single(None, agent_id, "retry")
            retry_kwargs = spawn.call_args.kwargs
            self.assertEqual("reviewer", retry_kwargs["role"])
            self.assertEqual("tainted_untrusted_web", retry_kwargs["provenance_class"])
            spawn.reset_mock()
            agents._agent_action_single(None, agent_id, "message", message="Review again")
            followup_kwargs = spawn.call_args.kwargs
            self.assertEqual("reviewer", followup_kwargs["role"])
            self.assertEqual("tainted_untrusted_web", followup_kwargs["provenance_class"])


if __name__ == "__main__":
    unittest.main()
