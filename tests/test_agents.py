import json
import tempfile
import unittest
from pathlib import Path

from mcp_server.tools_agents import (
    _build_provider_command,
    _extract_opencode,
    _handoff_instruction,
)


class AgentDelegationTests(unittest.TestCase):
    def test_extract_opencode_returns_only_final_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stdout.log"
            events = [
                {"type": "step_start", "sessionID": "ses_test", "part": {}},
                {"type": "text", "sessionID": "ses_test", "part": {"text": "first draft"}},
                {"type": "step_finish", "sessionID": "ses_test", "part": {"reason": "tool", "tokens": {"total": 10}}},
                {"type": "step_start", "sessionID": "ses_test", "part": {}},
                {"type": "text", "sessionID": "ses_test", "part": {"text": "short final handoff"}},
                {"type": "step_finish", "sessionID": "ses_test", "part": {"reason": "stop", "tokens": {"total": 20}}},
            ]
            path.write_text("\n".join(json.dumps(event) for event in events))
            result, session_id, usage = _extract_opencode(path)
            self.assertEqual("short final handoff", result)
            self.assertEqual("ses_test", session_id)
            self.assertEqual(20, usage["total"])

    def test_opencode_command_includes_model_variant_and_session(self):
        meta = {
            "provider": "opencode", "binary": "/opt/homebrew/bin/opencode",
            "cwd": "/tmp", "model": "opencode/test-free", "reasoning": "high",
            "resume_session_id": "ses_123", "access_mode": "workspace_write",
        }
        cmd = _build_provider_command(meta, "PROMPT", Path("/tmp/result.txt"))
        self.assertIn("--model", cmd)
        self.assertIn("opencode/test-free", cmd)
        self.assertIn("--variant", cmd)
        self.assertIn("high", cmd)
        self.assertIn("--session", cmd)
        self.assertIn("ses_123", cmd)

    def test_codex_command_includes_reasoning_and_sandbox(self):
        meta = {
            "provider": "codex", "binary": "/opt/homebrew/bin/codex",
            "cwd": "/tmp", "model": "gpt-test", "reasoning": "low",
            "resume_session_id": None, "access_mode": "read_only",
        }
        cmd = _build_provider_command(meta, "PROMPT", Path("/tmp/result.txt"))
        self.assertIn("gpt-test", cmd)
        self.assertIn("read-only", cmd)
        self.assertTrue(any('model_reasoning_effort="low"' in item for item in cmd))
        self.assertTrue(any('approval_policy="never"' in item for item in cmd))

    def test_concise_handoff_instruction_discourages_process_narration(self):
        text = _handoff_instruction("concise")
        self.assertIn("concise handoff", text)
        self.assertIn("Do not narrate", text)


if __name__ == "__main__":
    unittest.main()
