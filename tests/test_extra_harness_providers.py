"""pi, Kilo Code, Antigravity, Cline and Goose as agent providers: contracts, isolation and event parsing."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import harness_providers as harness
from mcp_server import extra_harness_providers as extra
from mcp_server import provider_compat


class ProviderContracts(unittest.TestCase):
    def test_registered_binary_and_contract(self):
        for provider in extra.LABELS:
            with self.subTest(provider=provider):
                self.assertIn(provider, harness.PROVIDERS)
                self.assertEqual(extra.LABELS[provider], harness.LABELS[provider])
                self.assertIn(provider, provider_compat.REQUIRED_OPTIONS)
                self.assertTrue(extra.binary_candidates(provider))

    def test_antigravity_runs_with_full_access_only(self):
        # agy reads its sign-in with /usr/bin/security; allowing that inside the boundary would
        # let the agent read the owner's other Keychain secrets.
        from mcp_server import tools_agents as agents
        self.assertIsNone(agents._validate_provider_access_mode("antigravity", "full"))
        for mode in ("read_only", "workspace_write"):
            with self.subTest(mode=mode):
                with self.assertRaises(agents.HTTPException) as error:
                    agents._validate_provider_access_mode("antigravity", mode)
                self.assertEqual(409, error.exception.status_code)
                self.assertIn("/usr/bin/security", error.exception.detail)
                info = agents._access_mode_info("antigravity", mode)
                self.assertFalse(info["enforced"])
                self.assertEqual("unsupported", info["boundary"])
        self.assertIn("/usr/bin/security", agents._RESTRICTED_ESCAPE_EXECUTABLES)

    def test_antigravity_tools_are_named_like_the_other_agents(self):
        info = {"name": "call_mcp_tool", "parameters": {"ServerName": "mac-mcp", "ToolName": "read_file"}}
        self.assertEqual("mcp__mac-mcp__read_file", extra.agy_tool_name(info))
        self.assertEqual("run_command", extra.agy_tool_name({"name": "run_command"}))
        command = harness.command({"provider": "antigravity", "binary": "agy", "cwd": "/w",
                                   "private_home": "/state/antigravity-home"}, "x")
        self.assertEqual(["--add-dir", "/state/antigravity-home"], command[-2:])
        self.assertIn("--disable-slash-commands", command)

    def test_headless_cli_commands_and_resumes(self):
        for provider in extra.LABELS:
            with self.subTest(provider=provider):
                meta = {"provider": provider, "binary": "/test/" + extra.BINARIES[provider],
                        "cwd": "/tmp/work", "agent_id": "agt1", "access_mode": "read_only",
                        "private_home": "/tmp/harness-home"}
                command = harness.command(meta, "Hello")
                self.assertEqual("/test/" + extra.BINARIES[provider], command[0])
                self.assertTrue(any(part.endswith("Hello") for part in command), command)
                self.assertNotIn("--api-key", command)
                resumed = harness.command({**meta, "resume_session_id": "resume-1"}, "Next")
                self.assertEqual(provider not in extra.NO_RESUME, "resume-1" in resumed)
                if provider in ("kilo", "cline", "goose"):
                    self.assertIn("--json" if provider == "cline" else "--format" if provider == "kilo" else "--output-format", command)

    def test_private_settings_contain_only_scoped_mcp_and_no_owner_extensions(self):
        for provider in extra.LABELS:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                state = Path(temp)
                with patch.object(extra, "owner_home", return_value=state / "owner"):
                    private = harness.prepare_home(provider, state, "http://127.0.0.1:8877/mcp", "secret-test-token")
                self.assertTrue(private.is_dir())
                matches = list(private.rglob("*"))
                containing = []
                for path in matches:
                    if path.is_file():
                        self.assertEqual(0o600, path.stat().st_mode & 0o777)
                        if "secret-test-token" in path.read_text(encoding="utf-8", errors="replace"):
                            containing.append(path)
                # Only in the agent's private folder; for Antigravity a workspace it adds.
                self.assertTrue(containing, provider)
                self.assertTrue(all(state in path.parents for path in containing), provider)
                harness.remove_credentials(provider, state)
                self.assertFalse(any("secret-test-token" in path.read_text(encoding="utf-8", errors="replace")
                                     for path in matches if path.is_file()), provider)

    def test_pi_owner_auth_only_copy_and_removed_after_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = root / "owner"
            owner.mkdir()
            (owner / "auth.json").write_text('{"openrouter":"example-secret"}')
            (owner / "skills.json").write_text('{"evil":"not allowed"}')
            with patch.object(extra, "owner_home", return_value=owner):
                home = extra.prepare_home("pi", root, "http://127.0.0.1:8877/mcp", "scoped")
            self.assertEqual('{"openrouter":"example-secret"}', (home / "agent" / "auth.json").read_text())
            self.assertFalse((home / "agent" / "skills.json").exists())
            extra.remove_credentials("pi", root)
            self.assertFalse((home / "agent" / "auth.json").exists())

    def test_env_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"TEST_USER_SECRET": "forbidden"}, clear=False):
                with patch.object(extra, "owner_home", return_value=root / "missing"):
                    for provider in extra.LABELS:
                        home = extra.prepare_home(provider, root / provider, "http://127.0.0.1:8877/mcp", "a")
                        env = extra.provider_env(provider, home, root / provider, lambda _: None)
                        self.assertNotIn("TEST_USER_SECRET", env)
                        self.assertEqual(str(Path.home()) if provider == "antigravity" else str(root / provider / "home"), env["HOME"])
                        self.assertNotIn("OPENAI_API_KEY", env)
                        self.assertNotIn("ANTHROPIC_API_KEY", env)
                        self.assertNotIn("MAC_MCP_AGENT_TOKEN", env)


class EventParsing(unittest.TestCase):
    def _extract(self, provider, events):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.jsonl"
            log.write_text("--- provider attempt 1 ---\n" + "".join(json.dumps(e) + "\n" for e in events))
            return harness.extract(provider, log), harness.stop_reason_failure(provider, log)

    def test_pi(self):
        events = [{"type": "session", "id": "pi-sid"},
                  {"type": "tool_execution_start", "toolName": "mcp__mac_mcp__read_file"},
                  {"type": "turn_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}],
                                                  "usage": {"input": 12, "output": 7}, "stopReason": "stop"}},
                  {"type": "agent_end", "messages": []}]
        meta = {"provider": "pi"}
        for e in events:
            harness.apply_event(meta, e, 1, lambda m, t: self.fail("unexpected " + t))
        (text, sid, usage), reason = self._extract("pi", events)
        self.assertEqual(("Done.", "pi-sid"), (text, sid))
        self.assertEqual(1, meta["tool_call_count"])
        self.assertEqual(12, usage["input"])
        self.assertIsNone(reason)

    def test_kilo(self):
        events = [{"type": "tool_use", "sessionID": "kilo-sid", "part": {"type": "tool", "tool": "bash"}},
                  {"type": "text", "sessionID": "kilo-sid", "part": {"type": "text", "text": "OK"}},
                  {"type": "step_finish", "sessionID": "kilo-sid", "part": {
                      "type": "step-finish", "reason": "stop",
                      "tokens": {"input": 10, "output": 2, "cache": {"read": 4}}}}]
        meta = {"provider": "kilo"}
        for ev in events:
            harness.apply_event(meta, ev, 1, lambda m, t: self.fail(t))
        (text, session, usage), reason = self._extract("kilo", events)
        self.assertEqual(("OK", "kilo-sid"), (text, session))
        self.assertEqual(1, meta["tool_call_count"])
        self.assertEqual(10, usage["input"])
        self.assertIsNone(reason)

    def test_cline(self):
        events = [
            {"type": "hook_event", "taskId": "cline-sid"},
            {"type": "agent_event", "event": {"type": "content_start", "contentType": "tool", "toolName": "run_commands",
                                              "toolCallId": "c1", "input": {"commands": ["ls"]}}},
            {"type": "agent_event", "event": {"type": "content_end", "contentType": "tool", "toolName": "run_commands",
                                              "toolCallId": "c1", "output": [{"query": "ls", "result": "x", "success": False}]}},
            {"type": "agent_event", "event": {"type": "content_start", "contentType": "tool", "toolName": "mac-mcp__read_file",
                                              "toolCallId": "c2", "input": {"path": "/etc/hosts"}}},
            {"type": "agent_event", "event": {"type": "content_end", "contentType": "tool", "toolName": "mac-mcp__read_file",
                                              "toolCallId": "c2", "output": {"content": [{"type": "text", "text": "##"}]}}},
            {"type": "agent_event", "event": {"type": "iteration_end", "toolCallCount": 2}},
            {"type": "run_result", "finishReason": "completed", "iterations": 2,
             "text": "Cline done", "usage": {"inputTokens": 45, "outputTokens": 13}},
        ]
        meta = {"provider": "cline"}
        for ev in events:
            harness.apply_event(meta, ev, 1, lambda m, t: self.fail(t))
        (text, sid, usage), reason = self._extract("cline", events)
        self.assertEqual(("Cline done", None), (text, sid))  # no headless resume, so no session to offer
        self.assertEqual((2, 1, "mac-mcp__read_file"), (meta["tool_call_count"], meta["failed_tool_calls"], meta["last_tool"]))
        self.assertEqual(13, usage["output"])
        from mcp_server import agent_timeline
        steps = agent_timeline.from_events([json.dumps(e) for e in events])["steps"]
        self.assertEqual([("command", "ls", "failed"), ("tool", "Mac MCP · read_file", "done")],
                         [(s["kind"], s["detail"], s["status"]) for s in steps[:2]])
        self.assertEqual("Cline done", steps[2]["text"])
        self.assertIsNone(reason)
        (_, _, _), bad_reason = self._extract("cline", [{"type": "run_result", "finishReason": "failed"}])
        self.assertEqual("failed", bad_reason)

    def test_goose(self):
        events = [{"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolRequest", "toolCall": {"name": "developer__shell"}},
            {"type": "text", "text": "Goose done"}]}},
            {"type": "complete", "input_tokens": 25, "output_tokens": 8}]
        meta = {"provider": "goose"}
        for ev in events:
            harness.apply_event(meta, ev, 1, lambda m, t: self.fail(t))
        (text, sid, usage), reason = self._extract("goose", events)
        self.assertEqual(("Goose done", None), (text, sid))
        self.assertEqual(1, meta["tool_call_count"])
        self.assertEqual("developer__shell", meta["last_tool"])
        self.assertEqual(25, usage["input"])
        self.assertIsNone(reason)

    def test_antigravity(self):
        events = [
            {"event": "init", "conversation_id": "agy-sid", "init": {"tools": ["run_command"]}},
            {"event": "step_update", "step_update": {"conversation_id": "agy-sid", "step_index": 2,
                "step_type": "tool", "state": "ACTIVE", "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"command": "sw_vers"}}}},
            {"event": "step_update", "step_update": {"conversation_id": "agy-sid", "step_index": 2,
                "step_type": "tool", "state": "DONE", "tool_name": "run_command"}},
            {"event": "result", "result": {"conversation_id": "agy-sid", "status": "SUCCESS",
                "response": "Agy done", "usage": {"input_tokens": 5, "output_tokens": 2, "thinking_tokens": 1}}},
        ]
        meta = {"provider": "antigravity"}
        for e in events:
            harness.apply_event(meta, e, 1, lambda m, t: self.fail("Unexpected: " + t))
        (text, sid, usage), reason = self._extract("antigravity", events)
        self.assertEqual(("Agy done", "agy-sid"), (text, sid))
        self.assertEqual(1, meta["tool_call_count"])
        self.assertEqual("run_command", meta["last_tool"])
        self.assertEqual(2, usage["output"])
        self.assertEqual(1, usage["reasoning"])
        self.assertIsNone(reason)
        _, missing = self._extract("antigravity", events[:-1])
        self.assertEqual("missing_terminal_event", missing)
        _, failure = self._extract("antigravity", [
            {"event": "result", "result": {"conversation_id": "agy-sid", "status": "ERROR", "response": ""}},
        ])
        self.assertEqual("ERROR", failure)
        from mcp_server import agent_timeline
        timeline = agent_timeline.from_events([json.dumps(e) for e in events])
        self.assertTrue(any(s["kind"] in ("tool", "command") for s in timeline["steps"]))


class UsageRecording(unittest.TestCase):
    def test_each_provider_records_under_its_own_name(self):
        from mcp_server import tools_agents as agents, provider_usage
        records = []

        class Store:
            def ingest(self, record):
                records.append(record)

            def diagnostic_increment(self, name):
                records.append(name)

        events = {
            "antigravity": {"event": "result", "result": {"status": "SUCCESS", "usage": {
                "input_tokens": 26704, "output_tokens": 684, "thinking_tokens": 596, "total_tokens": 27388}}},
            "pi": {"type": "turn_end", "message": {"role": "assistant", "timestamp": 17, "usage": {
                "input": 2480, "output": 298, "cacheRead": 0, "reasoning": 261, "totalTokens": 2778}}},
            "kilo": {"type": "step_finish", "part": {"id": "prt_1", "tokens": {"input": 10, "output": 2, "cache": {"read": 4}}}},
            "cline": {"type": "run_result", "usage": {"inputTokens": 86690, "outputTokens": 609, "cacheReadTokens": 54912}},
            "goose": {"type": "complete", "total_tokens": 115108, "input_tokens": 109780, "output_tokens": 5328},
        }
        with patch.object(agents, "get_provider_usage_store", return_value=Store()), \
                patch.object(agents, "_accumulate_agent_usage"):
            for provider, event in events.items():
                agents._ingest_provider_usage_event("agt_x", {"provider": provider, "session_id": "s"}, event, 1.0)
                agents._ingest_provider_usage_event("agt_x", {"provider": provider}, {"type": "noise"}, 1.0)
        self.assertEqual(list(events), [record.provider for record in records])
        totals = {record.provider: record.total_tokens for record in records}
        self.assertEqual({"antigravity": 27388, "pi": 2778, "kilo": 12, "cline": 87299, "goose": 115108}, totals)
        self.assertTrue(set(events) <= provider_usage.REPORTING_PROVIDERS)


class ResumeSafety(unittest.TestCase):
    """A native tool that may have changed something stops automatic replay; reads and Mac MCP's own calls do not."""

    def test_each_provider_marks_native_changes_only(self):
        from mcp_server import workflow_checkpoints
        agy_mcp = {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE", "tool_name": "call_mcp_tool",
                   "tool_info": {"name": "call_mcp_tool", "parameters": {"ServerName": "mac-mcp", "ToolName": "run_command"}}}}

        def agy(name):
            return {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE", "tool_name": name,
                                                            "tool_info": {"name": name}}}

        def goose(name):
            return {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "toolRequest", "id": "1", "toolCall": {"value": {"name": name}}}]}}

        def cline(name):
            return {"type": "agent_event", "event": {"type": "content_start", "contentType": "tool", "toolName": name}}

        safe = [("antigravity", agy_mcp), ("antigravity", agy("view_file")),
                ("pi", {"type": "tool_execution_start", "toolName": "mcp__mac_mcp__read_file"}),
                ("pi", {"type": "tool_execution_start", "toolName": "read"}),
                ("kilo", {"type": "tool_use", "part": {"type": "tool", "tool": "mac-mcp_read_file"}}),
                ("kilo", {"type": "tool_use", "part": {"type": "tool", "tool": "grep"}}),
                ("cline", cline("mac-mcp__read_file")), ("cline", cline("read_files")),
                ("goose", goose("mac-mcp__read_file"))]
        unsafe = [("antigravity", agy("run_command")), ("antigravity", agy("write_to_file")),
                  ("pi", {"type": "tool_execution_start", "toolName": "bash"}),
                  ("kilo", {"type": "tool_use", "part": {"type": "tool", "tool": "edit"}}),
                  ("cline", cline("run_commands")), ("goose", goose("shell")), ("goose", goose("a_new_tool"))]
        marked = []
        with patch.object(workflow_checkpoints, "mark_checkpoint_unknown",
                          side_effect=lambda agent_id, reason, **kw: marked.append(kw.get("tool"))), \
                patch.object(workflow_checkpoints, "_record_provider_cursor"):
            for provider, event in safe:
                workflow_checkpoints.note_provider_event("agt", provider, event)
            self.assertEqual([], marked)
            for provider, event in unsafe:
                workflow_checkpoints.note_provider_event("agt", provider, event)
        self.assertEqual(["run_command", "write_to_file", "bash", "edit", "run_commands", "shell", "a_new_tool"], marked)
        self.assertEqual("mcp_tool_event", workflow_checkpoints._provider_cursor_kind("antigravity", agy_mcp))
        self.assertEqual("native_tool_event", workflow_checkpoints._provider_cursor_kind("goose", goose("shell")))
        self.assertEqual("turn_completed", workflow_checkpoints._provider_cursor_kind("cline", {"type": "run_result"}))


class Timelines(unittest.TestCase):
    """Shapes taken from real runs of each CLI (agy 1.3.5, pi 1.2.0, Goose 1.54.0)."""

    def steps(self, events):
        from mcp_server import agent_timeline
        return agent_timeline.from_events([json.dumps(e) for e in events])

    def test_antigravity_mcp_calls_errors_and_streamed_text(self):
        def update(index, **fields):
            return {"event": "step_update", "step_update": {"conversation_id": "c", "step_index": index, **fields}}
        mcp = {"name": "call_mcp_tool", "parameters": {"Arguments": {"command": "sw_vers"}, "ServerName": "mac-mcp",
                                                        "ToolName": "run_command"}}
        timeline = self.steps([
            update(2, state="ACTIVE", step_type="tool", tool_name="view_file", tool_info={"name": "view_file"}),
            update(2, state="ERROR", step_type="tool", tool_name="view_file",
                   tool_info={"name": "view_file", "error": {"type": "TOOL_ERROR", "message": "no such file"}}),
            update(3, state="ACTIVE", step_type="tool", tool_name="call_mcp_tool", tool_info=mcp),
            update(3, state="DONE", step_type="tool", tool_name="call_mcp_tool", tool_info={**mcp, "output": "26.5.1"}),
            update(4, state="ACTIVE", step_type="tool", tool_name="run_command",
                   tool_info={"name": "run_command", "parameters": {"CommandLine": "ls"}}),
            update(4, state="DONE", step_type="tool", tool_name="run_command", tool_info={"name": "run_command", "output": "a"}),
            update(5, state="ACTIVE", step_type="agent_response", text_delta="Here are"),
            update(5, state="DONE", step_type="agent_response", text_delta=" the results."),
            {"event": "result", "result": {"status": "SUCCESS", "response": "Here are the results.",
                                           "usage": {"input_tokens": 10, "output_tokens": 3, "thinking_tokens": 1}}},
        ])
        steps = timeline["steps"]
        self.assertEqual(("tool", "Read a file", "failed", "no such file"),
                         (steps[0]["kind"], steps[0]["title"], steps[0]["status"], steps[0]["output"]))
        self.assertEqual(("Mac MCP · run_command", "done"), (steps[1]["detail"], steps[1]["status"]))
        self.assertEqual(("command", "ls"), (steps[2]["kind"], steps[2]["detail"]))
        self.assertEqual("Here are the results.", steps[3]["text"])
        self.assertEqual(4, len(steps))
        self.assertEqual(3, timeline["usage"]["output_tokens"])

    def test_pi_answer_leaves_out_thinking(self):
        timeline = self.steps([
            {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "mcp__mac_mcp__read_file", "args": {"path": "/etc/hosts"}},
            {"type": "tool_execution_end", "toolCallId": "t1", "result": {"content": [{"type": "text", "text": "##"}]}, "isError": False},
            {"type": "tool_execution_start", "toolCallId": "t2", "toolName": "bash", "args": {"command": "which mcp"}},
            {"type": "tool_execution_end", "toolCallId": "t2", "result": {"content": []}, "isError": True},
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "content": [
                {"type": "thinking", "thinking": "Now we need to craft JSON."}, {"type": "text", "text": "Done."}],
                "usage": {"input": 5, "output": 2, "cacheRead": 1}}},
        ])
        steps = timeline["steps"]
        self.assertEqual(("Mac MCP · read_file", "done"), (steps[0]["detail"], steps[0]["status"]))
        self.assertEqual(("command", "failed"), (steps[1]["kind"], steps[1]["status"]))
        self.assertEqual("Done.", steps[2]["text"])
        self.assertEqual({"input_tokens": 6, "output_tokens": 2}, timeline["usage"])

    def test_goose_text_pieces_make_one_message(self):
        def assistant(message_id, *content):
            return {"type": "message", "message": {"id": message_id, "role": "assistant", "content": list(content)}}
        request = {"type": "toolRequest", "id": "call-1",
                   "toolCall": {"status": "success", "value": {"name": "mac-mcp__read_file", "arguments": {"path": "x"}}}}
        response = {"type": "message", "message": {"id": "m2", "role": "user", "content": [{
            "type": "toolResponse", "id": "call-1", "toolResult": {"status": "success", "value": {
                "content": [{"type": "text", "text": "scope_denied"}], "isError": True}}}]}}
        events = [assistant("g1", {"type": "thinking", "thinking": "We"}), assistant("g1", request), response,
                  assistant("g2", {"type": "toolRequest", "id": "call-2", "toolCall": {"value": {
                      "name": "shell", "arguments": {"command": "sw_vers -productVersion"}}}}),
                  assistant("g3", {"type": "text", "text": "Mac"}), assistant("g3", {"type": "text", "text": "OS 26"}),
                  {"type": "complete", "input_tokens": 7, "output_tokens": 4}]
        timeline = self.steps(events)
        steps = timeline["steps"]
        self.assertEqual(("Mac MCP · read_file", "failed"), (steps[0]["detail"], steps[0]["status"]))
        self.assertEqual(("command", "sw_vers -productVersion"), (steps[1]["kind"], steps[1]["detail"]))
        self.assertEqual("MacOS 26", steps[2]["text"])
        self.assertEqual(3, len(steps))
        meta = {"provider": "goose"}
        for event in events:
            harness.apply_event(meta, event, 1, lambda m, t: self.fail(t))
        self.assertEqual((2, 1, "shell"), (meta["tool_call_count"], meta["failed_tool_calls"], meta["last_tool"]))
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "out.jsonl"
            log.write_text("".join(json.dumps(e) + "\n" for e in events))
            self.assertEqual("MacOS 26", harness.extract("goose", log)[0])

    def test_kilo_tokens_add_up_over_steps(self):
        meta = {"provider": "kilo"}
        for tokens in ({"input": 10, "output": 2, "cache": {"read": 4}}, {"input": 5, "output": 1, "reasoning": 3}):
            harness.apply_event(meta, {"type": "step_finish", "part": {"type": "step-finish", "tokens": tokens}},
                                1, lambda m, t: self.fail(t))
        self.assertEqual({"input": 15, "output": 3, "reasoning": 3, "cache": {"read": 4, "write": 0}}, meta["usage"])

    def test_failed_tools_are_counted(self):
        cases = {
            "pi": [{"type": "tool_execution_start", "toolName": "write"}, {"type": "tool_execution_end", "isError": True}],
            "kilo": [{"type": "tool_use", "part": {"type": "tool", "tool": "write", "state": {"status": "error"}}}],
            "cline": [{"type": "agent_event", "event": {"type": "content_start", "contentType": "tool", "toolName": "editor"}},
                      {"type": "agent_event", "event": {"type": "content_end", "contentType": "tool", "toolName": "editor",
                                                        "toolCallId": "e", "output": {"result": "", "error": "EPERM"}}}],
        }
        for provider, events in cases.items():
            meta = {"provider": provider}
            for event in events:
                harness.apply_event(meta, event, 1, lambda m, t: self.fail(t))
            self.assertEqual((1, 1), (meta["tool_call_count"], meta["failed_tool_calls"]), provider)
        from mcp_server import agent_timeline
        start = dict(cases["cline"][0]); start["event"] = {**start["event"], "toolCallId": "e", "input": {"path": "/w/a.txt"}}
        steps = agent_timeline.from_events([json.dumps(start), json.dumps(cases["cline"][1])])["steps"]
        self.assertEqual(("failed", "EPERM"), (steps[0]["status"], steps[0]["output"]))


if __name__ == "__main__":
    unittest.main()
