from __future__ import annotations

import json
import unittest

from mcp_server import agent_timeline


def lines(*events) -> list:
    return [json.dumps(event) for event in events]


class CodexTimelineTests(unittest.TestCase):
    def test_codex_items_become_readable_steps(self) -> None:
        tool = {"id": "item_2", "type": "mcp_tool_call", "server": "mac-mcp-agent", "tool": "tool_invoke",
                "arguments": {"tool_name": "get_system_info", "arguments": {"verbose": True},
                              "description": "Read CPU, RAM and disk usage."},
                "result": {"content": [{"type": "text", "text": "{\"ok\": true, \"cpu\": \"Apple M3\"}"}]},
                "error": None, "status": "completed"}
        timeline = agent_timeline.from_events(lines(
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning",
                                                "text": "**Checking the Mac**\n\nI should read system info."}},
            {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "Let me look."}},
            {"type": "item.started", "item": {**tool, "result": None, "status": "in_progress"}},
            {"type": "item.completed", "item": tool},
            {"type": "item.completed", "item": {"id": "item_3", "type": "command_execution",
                                                "command": "/bin/zsh -lc 'df -h /'", "aggregated_output": "Filesystem\n",
                                                "exit_code": 1, "status": "failed"}},
            {"type": "item.completed", "item": {"id": "item_4", "type": "file_change", "status": "completed",
                                                "changes": [{"path": "/tmp/a.txt", "kind": "add"}]}},
            {"type": "item.completed", "item": {"id": "item_5", "type": "agent_message",
                                                "text": "Done.\n\nTASK_RESULT_ENVELOPE_V1\n{\"schema_version\":1}"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20, "reasoning_output_tokens": 5}},
        ))
        steps = timeline["steps"]
        self.assertEqual(["thinking", "message", "tool", "command", "files", "message"], [s["kind"] for s in steps])
        self.assertEqual(("Checking the Mac", "I should read system info."), (steps[0]["title"], steps[0]["text"]))
        self.assertEqual(("Read CPU, RAM and disk usage.", "Mac MCP · get_system_info", "done"),
                         (steps[2]["title"], steps[2]["detail"], steps[2]["status"]))  # started+completed merged
        self.assertEqual({"verbose": "true"}, steps[2]["input"])
        self.assertEqual({"ok": True, "cpu": "Apple M3"}, json.loads(steps[2]["output"]))
        self.assertEqual(("df -h /", "failed", 1), (steps[3]["detail"], steps[3]["status"], steps[3]["exit_code"]))
        self.assertEqual("Changed 1 file", steps[4]["title"])
        self.assertEqual("Done.", steps[5]["text"])  # the machine-readable block is not shown
        self.assertEqual({"input_tokens": 100, "output_tokens": 20, "reasoning_tokens": 5}, timeline["usage"])


class ClaudeTimelineTests(unittest.TestCase):
    def test_claude_thinking_tools_and_results(self) -> None:
        timeline = agent_timeline.from_events(lines(
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "Need the git log."},
                {"type": "text", "text": "Checking commits."},
                {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "git log -3", "description": "Show recent commits"}},
                {"type": "tool_use", "id": "tu2", "name": "mcp__mac-mcp__read_file", "input": {"path": "/tmp/x", "description": "Read the notes"}},
                {"type": "tool_use", "id": "tu3", "name": "Edit", "input": {"file_path": "/tmp/y.py"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": "abc123 Fix", "is_error": False},
                {"type": "tool_result", "tool_use_id": "tu2", "content": [{"type": "text", "text": "notes"}], "is_error": True},
            ]}},
            {"type": "result", "result": "ok", "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
             "total_cost_usd": 0.012},
        ))
        steps = timeline["steps"]
        self.assertEqual(["thinking", "message", "command", "tool", "files"], [s["kind"] for s in steps])
        self.assertEqual(("Show recent commits", "git log -3", "abc123 Fix", "done"),
                         (steps[2]["title"], steps[2]["detail"], steps[2]["output"], steps[2]["status"]))
        self.assertEqual(("Read the notes", "failed", "notes"), (steps[3]["title"], steps[3]["status"], steps[3]["output"]))
        self.assertEqual("running", steps[4]["status"])  # no result came back for the edit
        self.assertEqual({"input_tokens": 100, "output_tokens": 5, "cost_usd": 0.012}, timeline["usage"])


class OpenCodeTimelineTests(unittest.TestCase):
    def test_opencode_parts(self) -> None:
        timeline = agent_timeline.from_events(lines(
            {"type": "step_start", "part": {}},
            {"type": "reasoning", "part": {"text": "Plan it."}},
            {"type": "tool_use", "part": {"callID": "c1", "tool": "bash",
                                          "state": {"status": "completed", "title": "List files",
                                                    "input": {"command": "ls"}, "output": "a\nb"}}},
            {"type": "tool_use", "part": {"callID": "c2", "tool": "read",
                                          "state": {"status": "error", "input": {"filePath": "/x"}, "error": "missing"}}},
            {"type": "text", "part": {"text": "All set."}},
            {"type": "step_finish", "part": {"reason": "tool-calls", "cost": 0.01,
                                             "tokens": {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 90}}}},
            {"type": "step_finish", "part": {"reason": "stop", "cost": 0,
                                             "tokens": {"input": 5, "output": 3, "reasoning": 0, "cache": {"read": 0}}}},
            "not json",
        ))
        self.assertEqual({"input_tokens": 105, "output_tokens": 5, "reasoning_tokens": 1, "cost_usd": 0.01}, timeline["usage"])
        steps = timeline["steps"]
        self.assertEqual(["thinking", "command", "tool", "message"], [s["kind"] for s in steps])
        self.assertEqual(("List files", "ls", "done"), (steps[1]["title"], steps[1]["detail"], steps[1]["status"]))
        self.assertEqual(("Read a file", "failed", "missing"), (steps[2]["title"], steps[2]["status"], steps[2]["output"]))

    def test_provider_errors_become_the_reason(self) -> None:
        steps = agent_timeline.from_events(lines(
            {"type": "error", "sessionID": "s", "error": {"name": "APIError", "data": {
                "message": "An active OpenCode Go subscription is required to use Go models.", "statusCode": 403}}},
        ))["steps"]
        self.assertEqual(("error", "An active OpenCode Go subscription is required to use Go models."),
                         (steps[0]["kind"], steps[0]["text"]))
        self.assertEqual(steps[0]["text"], agent_timeline.failure_reason(steps))
        self.assertIsNone(agent_timeline.failure_reason([{"kind": "message", "text": "hi"}]))

    def test_limits(self) -> None:
        many = [{"type": "item.completed", "item": {"id": f"i{n}", "type": "agent_message", "text": "x" * 9000}}
                for n in range(agent_timeline.MAX_STEPS + 20)]
        steps = agent_timeline.from_events(lines(*many))["steps"]
        self.assertEqual(agent_timeline.MAX_STEPS, len(steps))
        self.assertLessEqual(len(steps[0]["text"]), agent_timeline.MAX_TEXT)


if __name__ == "__main__":
    unittest.main()
