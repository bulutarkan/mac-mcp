from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server.tools_agents as agents


FAKE_CHATGPT = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
root = pathlib.Path(os.environ["FAKE_CHATGPT_ROOT"])
flag = root / "interrupt.flag"
log = root / "calls.log"
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args and args[0] == "interrupt":
    flag.write_text("1", encoding="utf-8")
    print("Interrupt requested")
    raise SystemExit(0)
print(json.dumps({"type":"job_started","jobId":"job-integration"}), flush=True)
print(json.dumps({"type":"status","text":"Thinking"}), flush=True)
start = time.time()
while time.time() - start < 8:
    if flag.exists():
        print(json.dumps({"type":"interrupting","jobId":"job-integration"}), flush=True)
        print(json.dumps({"type":"interrupted","jobId":"job-integration"}), flush=True)
        print(json.dumps({"type":"assistant_delta","jobId":"job-integration","text":"Done after checkpoint"}), flush=True)
        print(json.dumps({"type":"final","jobId":"job-integration","text":"Done after checkpoint"}), flush=True)
        raise SystemExit(0)
    time.sleep(0.05)
raise SystemExit(7)
'''


class ChatGPTCheckpointIntegrationTests(unittest.TestCase):
    def test_soft_budget_interrupts_live_job_and_finishes_same_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "chatgpt"
            binary.write_text(FAKE_CHATGPT, encoding="utf-8")
            binary.chmod(0o755)
            agent_id = "agt_integration"
            adir = root / "agents" / agent_id
            adir.mkdir(parents=True)
            for name in ("stdout.log", "stderr.log", "result.txt", "worker.log"):
                (adir / name).write_text("", encoding="utf-8")
            meta = {
                "agent_id": agent_id,
                "provider": "chatgpt",
                "binary": str(binary),
                "status": "running",
                "phase": "worker_starting",
                "model": None,
                "reasoning": "high",
                "project": None,
                "cwd": td,
                "access_mode": "read_only",
                "permission_profile": "read_only",
                "scope": {"access_mode":"read_only", "path_roots":[td]},
                "scoped_mcp": False,
                "timeout_s": 10,
                "idle_timeout_s": None,
                "retry_count": 0,
                "turn_count": 0,
                "turn_started_at": None,
                "turn_budget_s": 1,
                "hard_tool_budget_s": 3,
                "checkpoint_count": 0,
                "checkpoint_pending": False,
                "checkpoint_waiting_for_tool": False,
                "active_tool_started_at": None,
                "tool_call_count": 0,
                "step_count": 0,
                "last_activity_at": agents._now(),
            }
            (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            env = {**os.environ, "FAKE_CHATGPT_ROOT": str(root)}
            with patch.object(agents, "AGENTS_DIR", root / "agents"), \
                 patch.object(agents, "_chatgpt_env", return_value=env), \
                 patch.object(agents, "_provider_env", return_value=(env, None)):
                code, reason = agents._run_provider_attempt(agent_id, meta, "original task", 0)
                saved = agents._read_meta(agent_id)
            self.assertEqual(0, code)
            self.assertIsNone(reason)
            self.assertEqual(1, saved["checkpoint_count"])
            self.assertFalse(saved["checkpoint_pending"])
            self.assertGreaterEqual(saved["turn_count"], 2)
            calls = [json.loads(line) for line in (root / "calls.log").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(2, len(calls))
            self.assertEqual("interrupt", calls[1][0])
            self.assertEqual("job-integration", calls[1][1])
            self.assertIn("Do not repeat completed work", calls[1][-1])


if __name__ == "__main__":
    unittest.main()
