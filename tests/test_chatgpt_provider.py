import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server.tools_agents as agents
from mcp_server.tools_agents import (
    _apply_provider_event,
    _build_provider_command,
    _chatgpt_default_project,
    _chatgpt_effort,
    _extract_chatgpt,
    _find_binary,
    agent_catalog,
    provider_overview,
)


class ChatGPTProviderTests(unittest.TestCase):
    def test_default_project_is_local_config_only(self):
        with tempfile.TemporaryDirectory() as td:
            settings = str(Path(td) / "settings.json")
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": settings}, clear=False):
                os.environ.pop("CHATGPT_SUBAGENT_PROJECT", None)
                self.assertIsNone(_chatgpt_default_project())
                with patch.dict(os.environ, {"CHATGPT_SUBAGENT_PROJECT": "subagents"}, clear=False):
                    self.assertEqual("subagents", _chatgpt_default_project())
                Path(settings).write_text(json.dumps({
                    "subagents": {"providers": {"chatgpt": {"default_project": "Settings Project"}}}
                }))
                self.assertEqual("Settings Project", _chatgpt_default_project())

    def test_effort_aliases(self):
        self.assertEqual("low", _chatgpt_effort("low"))
        self.assertEqual("medium", _chatgpt_effort("medium"))
        self.assertEqual("high", _chatgpt_effort("high"))
        self.assertEqual("extra-high", _chatgpt_effort("xhigh"))
        self.assertEqual("extra-high", _chatgpt_effort("max"))
        self.assertIsNone(_chatgpt_effort(None))

    def test_new_command_uses_project_model_effort_and_stream(self):
        meta = {
            "provider": "chatgpt",
            "binary": "/tmp/chatgpt",
            "cwd": "/tmp",
            "model": "GPT-5.6 Sol",
            "reasoning": "high",
            "resume_session_id": None,
            "access_mode": "read_only",
            "project": "subagents",
            "timeout_s": 240,
        }
        cmd = _build_provider_command(meta, "PROMPT", Path("/tmp/result.txt"))
        self.assertEqual(["/tmp/chatgpt", "project", "subagents", "new"], cmd[:4])
        self.assertIn("--json-stream", cmd)
        self.assertIn("GPT-5.6 Sol", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("high", cmd)
        self.assertEqual("PROMPT", cmd[-1])

    def test_new_command_without_local_project_uses_plain_new(self):
        meta = {
            "provider": "chatgpt", "binary": "/tmp/chatgpt", "cwd": "/tmp",
            "model": None, "reasoning": None, "resume_session_id": None,
            "access_mode": "read_only", "project": None, "timeout_s": 120,
        }
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(Path(td) / "settings.json")}, clear=False):
                os.environ.pop("CHATGPT_SUBAGENT_PROJECT", None)
                cmd = _build_provider_command(meta, "PROMPT", Path("/tmp/result.txt"))
        self.assertEqual(["/tmp/chatgpt", "new"], cmd[:2])

    def test_resume_command_uses_conversation_session(self):
        meta = {
            "provider": "chatgpt", "binary": "/tmp/chatgpt", "cwd": "/tmp",
            "model": "GPT-5.6 Sol", "reasoning": "medium",
            "resume_session_id": "6aa845f7-3490-83eb-a5db-efe617534352",
            "access_mode": "read_only", "project": "subagents", "timeout_s": 120,
        }
        cmd = _build_provider_command(meta, "FOLLOW", Path("/tmp/result.txt"))
        self.assertEqual(
            ["/tmp/chatgpt", "resume", "6aa845f7-3490-83eb-a5db-efe617534352"],
            cmd[:3],
        )
        self.assertNotIn("project", cmd)
        self.assertIn("medium", cmd)

    def test_extract_chatgpt_final_and_session(self):
        events = [
            {"type": "job_started", "jobId": "job-1"},
            {"type": "status", "text": "Thinking"},
            {"type": "tool_start", "toolId": "t1", "text": "Checking"},
            {"type": "tool_end", "toolId": "t1", "text": "Called tool"},
            {"type": "assistant_delta", "text": "almost"},
            {"type": "final", "text": "DONE", "sessionId": "session-123"},
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stdout.log"
            path.write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")
            result, session_id, usage = _extract_chatgpt(path)
        self.assertEqual("DONE", result)
        self.assertEqual("session-123", session_id)
        self.assertIsNone(usage)

    def test_disabled_chatgpt_is_hidden_from_catalog(self):
        with tempfile.TemporaryDirectory() as td:
            settings = Path(td) / "settings.json"
            settings.write_text(json.dumps({
                "subagents": {"providers": {
                    "opencode": {"enabled": True},
                    "codex": {"enabled": True},
                    "chatgpt": {"enabled": False},
                }}
            }))
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}):
                with patch.object(agents, "_opencode_models", return_value=[]), \
                     patch.object(agents, "_codex_known_models", return_value=([], None, None)), \
                     patch.object(agents, "_find_binary", return_value=None):
                    catalog = agent_catalog(None)
                    self.assertNotIn("chatgpt", catalog["providers"])
                    self.assertEqual({}, agent_catalog(None, provider="chatgpt")["providers"])

    def test_provider_overview_reports_disabled_but_detected_provider(self):
        with tempfile.TemporaryDirectory() as td:
            settings = Path(td) / "settings.json"
            binary = Path(td) / "chatgpt-web"
            binary.write_text("#!/bin/sh\necho 0.3.2\n")
            binary.chmod(0o755)
            settings.write_text(json.dumps({
                "subagents": {"providers": {
                    "chatgpt": {"enabled": False, "binary_path": str(binary)}
                }}
            }))
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}):
                self.assertEqual(str(binary), _find_binary("chatgpt"))
                row = next(item for item in provider_overview()["providers"] if item["id"] == "chatgpt")
                self.assertFalse(row["enabled"])
                self.assertTrue(row["detected"])

    def test_chatgpt_events_normalize_progress_and_tools(self):
        now = time.time()
        meta = {
            "provider": "chatgpt", "status": "running", "phase": "provider_starting",
            "started_at": now, "spawn_requested_at": now, "last_activity_at": now,
            "step_count": 0, "tool_call_count": 0,
        }
        for event in [
            {"type": "job_started"},
            {"type": "status", "text": "Thinking"},
            {"type": "tool_update", "text": "Checking System Uptime"},
            {"type": "tool_start", "toolId": "t1", "text": "Checking System Uptime"},
            {"type": "tool_end", "toolId": "t1", "text": "Called tool"},
            {"type": "assistant_delta", "text": "done"},
            {"type": "final", "text": "done", "sessionId": "session-xyz"},
        ]:
            _apply_provider_event(meta, event, time.time())
        self.assertEqual(1, meta["step_count"])
        self.assertEqual(1, meta["tool_call_count"])
        self.assertEqual("Checking System Uptime", meta["last_tool"])
        self.assertEqual("session-xyz", meta["session_id"])
        self.assertEqual("finalizing", meta["phase"])
        self.assertEqual("final", meta["last_event_type"])


if __name__ == "__main__":
    unittest.main()
