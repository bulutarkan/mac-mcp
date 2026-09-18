from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mcp_server.tools_agents as agents


class ChatGPTTurnBudgetTests(unittest.TestCase):
    def test_rate_limit_classifier_covers_real_web_messages(self) -> None:
        self.assertEqual("requesting_too_fast", agents._chatgpt_rate_limit_reason_text("You're making requests too quickly"))
        self.assertEqual("requesting_too_fast", agents._chatgpt_rate_limit_reason_text("You’re making requests too fast"))
        self.assertEqual("temporarily_limited", agents._chatgpt_rate_limit_reason_text("temporarily limited access to your conversations"))
        self.assertEqual("too_many_requests", agents._chatgpt_rate_limit_reason_text("HTTP 429 Too Many Requests"))
        self.assertIsNone(agents._chatgpt_rate_limit_reason_text("normal provider error"))

    def test_rate_limit_detection_is_scoped_to_current_attempt_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "stdout.log"
            err = Path(td) / "stderr.log"
            out.write_text("", encoding="utf-8")
            err.write_text("You're making requests too quickly\n", encoding="utf-8")
            old_offset = err.stat().st_size
            with err.open("a", encoding="utf-8") as handle:
                handle.write("ordinary provider failure\n")
            self.assertIsNone(agents._chatgpt_rate_limit_reason(out, err, 0, old_offset))
            self.assertEqual("requesting_too_fast", agents._chatgpt_rate_limit_reason(out, err, 0, 0))

    def test_exponential_cooldown_is_bounded(self) -> None:
        env = {
            "CHATGPT_PROVIDER_RATE_LIMIT_BACKOFF_S": "10",
            "CHATGPT_PROVIDER_RATE_LIMIT_BACKOFF_CAP_S": "40",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(10, agents._chatgpt_cooldown_seconds(1))
            self.assertEqual(20, agents._chatgpt_cooldown_seconds(2))
            self.assertEqual(40, agents._chatgpt_cooldown_seconds(3))
            self.assertEqual(40, agents._chatgpt_cooldown_seconds(8))

    def test_turn_budget_waits_for_tool_then_hard_checkpoints(self) -> None:
        base = {
            "provider": "chatgpt",
            "turn_started_at": 100.0,
            "turn_budget_s": 900,
            "hard_tool_budget_s": 1200,
            "checkpoint_pending": False,
        }
        self.assertIsNone(agents._chatgpt_turn_budget_action(base, now=999.0))
        self.assertEqual("turn_budget", agents._chatgpt_turn_budget_action(base, now=1000.0))

        using_tool = {**base, "active_tool_started_at": 700.0}
        self.assertEqual("wait_for_tool", agents._chatgpt_turn_budget_action(using_tool, now=1000.0))
        self.assertEqual("hard_tool_budget", agents._chatgpt_turn_budget_action(using_tool, now=1900.0))
        self.assertIsNone(agents._chatgpt_turn_budget_action({**using_tool, "checkpoint_pending": True}, now=1900.0))

    def test_checkpoint_command_targets_live_job_and_preserves_effort(self) -> None:
        meta = {
            "provider": "chatgpt",
            "binary": "/tmp/chatgpt",
            "provider_job_id": "job-live-1",
            "session_id": "session-fallback",
            "model": "GPT-5.6 Sol",
            "reasoning": "xhigh",
        }
        cmd = agents._chatgpt_checkpoint_command(meta, "turn_budget")
        self.assertEqual(["/tmp/chatgpt", "interrupt", "job-live-1"], cmd[:3])
        self.assertIn("--model", cmd)
        self.assertIn("GPT-5.6 Sol", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("extra-high", cmd)
        self.assertIn("Do not repeat completed work", cmd[-1])
        self.assertIn("duplicate external side effects", cmd[-1])

    def test_checkpoint_request_marks_pending_then_waits_for_interrupted_event(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.object(agents, "AGENTS_DIR", Path(td)):
            agent_id = "agt_checkpoint"
            adir = Path(td) / agent_id
            adir.mkdir()
            meta = {
                "agent_id": agent_id, "provider": "chatgpt", "status": "running", "phase": "reasoning",
                "binary": "/tmp/chatgpt", "provider_job_id": "job-live", "model": "GPT-5.6 Sol",
                "reasoning": "high", "checkpoint_pending": False, "checkpoint_count": 0,
                "active_tool_started_at": None,
            }
            (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            proc = SimpleNamespace(returncode=0, stdout="Interrupt requested", stderr="")
            with patch.object(agents.subprocess, "run", return_value=proc) as run:
                self.assertTrue(agents._request_chatgpt_checkpoint(agent_id, meta, "turn_budget"))
            saved = agents._read_meta(agent_id)
            self.assertTrue(saved["checkpoint_pending"])
            self.assertEqual(1, saved["checkpoint_count"])
            self.assertEqual("checkpointing", saved["phase"])
            self.assertEqual("interrupt", run.call_args.args[0][1])
            self.assertEqual("job-live", run.call_args.args[0][2])

    def test_chatgpt_events_track_turns_tools_and_checkpoint_resume(self) -> None:
        meta = {
            "provider": "chatgpt", "status": "running", "phase": "provider_starting",
            "first_event_at": None, "last_activity_at": 0.0, "step_count": 0,
            "tool_call_count": 0, "turn_count": 0, "checkpoint_pending": False,
            "checkpoint_waiting_for_tool": False,
        }
        agents._apply_provider_event(meta, {"type": "job_started", "jobId": "job-1"}, 100.0)
        self.assertEqual("job-1", meta["provider_job_id"])
        self.assertEqual(1, meta["turn_count"])
        self.assertEqual(100.0, meta["turn_started_at"])

        agents._apply_provider_event(meta, {"type": "tool_start", "toolId": "tool-1", "text": "Search"}, 110.0)
        self.assertEqual(110.0, meta["active_tool_started_at"])
        agents._apply_provider_event(meta, {"type": "tool_end", "toolId": "tool-1"}, 125.25)
        self.assertEqual(15250, meta["last_tool_duration_ms"])
        self.assertIsNone(meta["active_tool_started_at"])

        meta["checkpoint_pending"] = True
        meta["checkpoint_waiting_for_tool"] = True
        agents._apply_provider_event(meta, {"type": "interrupted", "jobId": "job-1"}, 130.0)
        self.assertEqual(2, meta["turn_count"])
        self.assertEqual(130.0, meta["turn_started_at"])
        self.assertFalse(meta["checkpoint_pending"])
        self.assertFalse(meta["checkpoint_waiting_for_tool"])
        self.assertEqual("reasoning", meta["phase"])

    def test_shared_throttle_gate_staggers_followup_starts(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.object(agents, "AGENTS_DIR", Path(td)), patch.dict(
            os.environ,
            {
                "CHATGPT_PROVIDER_REDUCED_CONCURRENCY_S": "300",
                "CHATGPT_PROVIDER_POST_THROTTLE_SPACING_S": "15",
            },
            clear=False,
        ):
            state = agents._record_chatgpt_shared_throttle(130.0, "requesting_too_fast", now=100.0)
            self.assertEqual(1, state["throttle_count"])
            self.assertEqual(30.0, agents._reserve_chatgpt_provider_start(now=100.0))
            self.assertEqual(45.0, agents._reserve_chatgpt_provider_start(now=100.0))
            self.assertEqual(0.0, agents._reserve_chatgpt_provider_start(now=500.0))

    def test_session_recovery_from_provider_job_index(self) -> None:
        proc = SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"id": "job-a", "sessionId": "sess-a"}, {"id": "job-b", "sessionId": "sess-b"}]),
            stderr="",
        )
        meta = {"provider_job_id": "job-b", "binary": "/tmp/chatgpt"}
        with patch.object(agents.subprocess, "run", return_value=proc):
            self.assertEqual("sess-b", agents._chatgpt_session_for_job(meta))

    def test_worker_rate_limit_retry_resumes_instead_of_replaying_original_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent_id = "agt_budgetretry"
            adir = root / agent_id
            adir.mkdir()
            original = "submit the form only once, then verify confirmation"
            effective = original + "\n\nscoped instructions"
            for name, text in {
                "prompt.txt": original,
                "effective_prompt.txt": effective,
                "stdout.log": "",
                "stderr.log": "",
                "worker.log": "",
                "result.txt": "",
            }.items():
                (adir / name).write_text(text, encoding="utf-8")
            meta = {
                "agent_id": agent_id, "provider": "chatgpt", "binary": "/tmp/chatgpt",
                "cwd": td, "model": "GPT-5.6 Sol", "reasoning": "high", "status": "starting",
                "phase": "starting", "started_at": 100.0, "spawn_requested_at": 100.0,
                "last_activity_at": 100.0, "retries": 1, "retry_count": 0,
                "result_style": "concise", "resume_session_id": None, "session_id": None,
                "provider_job_id": "job-rate-limited", "permission_profile": "read_only",
                "scope": {"access_mode": "read_only"}, "scoped_mcp": False,
                "tool_call_count": 0, "step_count": 0, "checkpoint_count": 0,
                "checkpoint_pending": False, "checkpoint_waiting_for_tool": False,
                "throttle_count": 0, "turn_count": 1, "turn_started_at": 100.0,
                "turn_budget_s": 900, "hard_tool_budget_s": 1200,
            }
            (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            calls = []
            def fake_attempt(_agent_id, latest, prompt, attempt_index):
                calls.append((attempt_index, prompt, latest.get("resume_session_id")))
                return (4, None) if attempt_index == 0 else (0, None)

            with patch.object(agents, "AGENTS_DIR", root), \
                 patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt), \
                 patch.object(agents, "_chatgpt_rate_limit_reason", side_effect=["requesting_too_fast"]), \
                 patch.object(agents, "_chatgpt_cooldown_seconds", return_value=1), \
                 patch.object(agents, "_record_chatgpt_shared_throttle", return_value={}), \
                 patch.object(agents, "_wait_chatgpt_provider_gate", return_value=True), \
                 patch.object(agents, "_chatgpt_session_for_job", return_value="sess-resume"):
                rc = agents._worker(agent_id)
                saved = agents._read_meta(agent_id)

            self.assertEqual(0, rc)
            self.assertEqual(effective, calls[0][1])
            self.assertEqual("sess-resume", calls[1][2])
            self.assertNotEqual(effective, calls[1][1])
            self.assertIn("Do not repeat completed work", calls[1][1])
            self.assertEqual(1, saved["throttle_count"])
            self.assertEqual("requesting_too_fast", saved["last_throttle_reason"])
            self.assertEqual("completed", saved["status"])

    def test_chatgpt_spawn_defaults_to_high_reasoning_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
             os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"), \
             patch.object(agents, "provider_enabled", return_value=True), \
             patch.object(agents, "_find_binary", return_value="/tmp/chatgpt"), \
             patch.object(agents, "_base_env", return_value={}), \
             patch.object(agents.subprocess, "Popen", return_value=SimpleNamespace(pid=43210)), \
             patch.object(agents.threading, "Thread", return_value=SimpleNamespace(start=lambda: None)):
            scope = agents.ResourceScope.from_dict({"access_mode": "full", "path_roots": [td]})
            spawned = agents._spawn_internal(
                settings=None, provider="chatgpt", prompt="inspect only", model=None, reasoning=None,
                cwd=td, timeout_s=1200, title="budget default", result_style="concise",
                access_mode="full", scope=scope, permission_profile="trusted",
            )
            saved = agents._read_meta(spawned["agent_id"])
        self.assertEqual("high", saved["reasoning"])
        self.assertEqual(900, saved["turn_budget_s"])
        self.assertEqual(1200, saved["hard_tool_budget_s"])

    def test_public_meta_exposes_resilience_telemetry(self) -> None:
        meta = {
            "provider": "chatgpt", "status": "running", "started_at": 10.0, "last_activity_at": 20.0,
            "turn_started_at": 15.0, "turn_count": 3, "turn_budget_s": 900, "hard_tool_budget_s": 1200,
            "checkpoint_count": 2, "checkpoint_pending": False, "throttle_count": 1,
            "last_throttled_at": 18.0, "last_throttle_reason": "requesting_too_fast", "cooldown_until": 108.0,
        }
        with patch.object(agents, "_now", return_value=25.0):
            public = agents._public_meta("agt-public", meta)
        self.assertEqual(3, public["turn_count"])
        self.assertEqual(10000, public["turn_elapsed_ms"])
        self.assertEqual(2, public["checkpoint_count"])
        self.assertEqual(1, public["throttle_count"])
        self.assertEqual("requesting_too_fast", public["last_throttle_reason"])


if __name__ == "__main__":
    unittest.main()
