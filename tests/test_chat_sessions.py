"""Mac MCP Chat (ACP): a fake ACP agent stands in for Copilot; the host, transcript and routes are real."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import chat_sessions
from mcp_server.chat_routes import create_chat_routes
from mcp_server.scoped_auth import ScopedCredentialStore

FAKE_AGENT = textwrap.dedent('''\
    import json, os, queue, sys, threading, time
    log = open(LOG_PATH, "a")
    inbox = queue.Queue()
    replies = {}
    cancelled = threading.Event()
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
    def update(sid, upd):
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": upd}})
    def reader():
        for line in sys.stdin:
            msg = json.loads(line)
            if "method" not in msg:
                replies[msg["id"]] = msg; continue
            if msg["method"] == "session/cancel":
                cancelled.set(); continue
            inbox.put(msg)
        inbox.put(None)
    threading.Thread(target=reader, daemon=True).start()
    def prompt(msg):
        sid = msg["params"]["sessionId"]
        text = msg["params"]["prompt"][0]["text"]
        cancelled.clear()
        if text == "slow":
            while not cancelled.is_set():
                update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "tick "}})
                time.sleep(0.05)
            return {"stopReason": "cancelled"}
        if text.startswith("write"):
            update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Create hello.txt", "kind": "edit",
                         "status": "pending", "rawInput": {"path": "hello.txt", "content": "hi"},
                         "locations": [{"path": "hello.txt"}]})
            send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
                "sessionId": sid, "toolCall": {"toolCallId": "t1", "title": "Create hello.txt"},
                "options": [{"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "reject_once", "name": "Deny", "kind": "reject_once"}]}})
            while 900 not in replies:
                time.sleep(0.02)
            outcome = replies.pop(900)["result"]["outcome"]
            log.write(json.dumps({"permission": outcome}) + "\\n"); log.flush()
            ok = outcome.get("optionId") == "allow_once"
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                         "status": "completed" if ok else "failed",
                         "content": [{"type": "content", "content": {"type": "text", "text": "wrote" if ok else "no"}}]})
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}})
            return {"stopReason": "end_turn", "usage": {"inputTokens": 50, "outputTokens": 5}}
        if text == "startup":
            for call, why in (("s1", "MCP server `mac-mcp` was not started, because the Codex config already defines an MCP server with this name."),
                              ("s2", "connection refused")):
                update(sid, {"sessionUpdate": "tool_call", "toolCallId": call, "title": "mcp__mac-mcp__startup",
                             "kind": "other", "status": "pending"})
                update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": call, "status": "failed",
                             "rawOutput": {"message": why}})
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ok"}})
            return {"stopReason": "end_turn"}
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Info: Disabled tools: task"}})
        update(sid, {"sessionUpdate": "session_info_update", "title": "Greeting"})
        update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hel"}})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "lo"}})
        update(sid, {"sessionUpdate": "usage_update", "used": 1200, "size": 128000})
        return {"stopReason": "end_turn", "usage": {"inputTokens": 100, "outputTokens": 2}}
    config = {"model": "opus", "effort": "high"}
    def options():
        return [{"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": config["model"],
                 "options": [{"value": "opus", "name": "Opus"}, {"value": "sonnet", "name": "Sonnet"}]},
                {"id": "effort", "name": "Effort", "category": "thought_level", "type": "select",
                 "currentValue": config["effort"],
                 "options": [{"group": "g", "name": "Levels", "options": [{"value": "low", "name": "Low"},
                                                                         {"value": "high", "name": "High"}]}]}]
    while True:
        msg = inbox.get()
        if msg is None:
            break
        method, params = msg["method"], msg.get("params") or {}
        log.write(json.dumps({"method": method, "params": params}) + "\\n"); log.flush()
        if method == "initialize":
            result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
        elif method == "session/new":
            if os.path.exists(LOG_PATH + ".needauth"):
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "Authentication required"}})
                continue
            result = {"sessionId": "sess-1", "configOptions": options()}
        elif method == "session/set_config_option":
            config[params["configId"]] = params["value"]
            result = {"configOptions": options()}
        elif method == "session/load":
            update(params["sessionId"], {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old"}})
            update(params["sessionId"], {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "old"}})
            result = {"configOptions": options()}
        elif method == "session/prompt":
            threading.Thread(target=lambda m=msg: send({"jsonrpc": "2.0", "id": m["id"], "result": prompt(m)}),
                             daemon=True).start()
            continue
        else:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}); continue
        send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
''')


class FriendlyTitleTest(unittest.TestCase):
    def test_mcp_tool_names_read_as_words(self):
        title = chat_sessions._friendly_title
        self.assertEqual(title({"agent_title": "mcp__mac-mcp__search_files"}, ""), "Searched files")  # Claude
        self.assertEqual(title({"agent_title": "mcp.mac-mcp.write_file"}, ""), "Wrote a file")  # Codex
        self.assertEqual(title({"agent_title": "mac-mcp_read_file"}, ""), "Read a file")  # OpenCode
        self.assertEqual(title({"agent_title": "ToolSearch"}, ""), "Looked for the right tool")
        self.assertEqual(title({"agent_title": "mac-mcp_write_file", "intent": "Save the summary"}, ""), "Save the summary")
        self.assertEqual(title({"agent_title": "Viewing …/notes.md", "kind": "read", "files": ["/p/notes.md"]}, ""),
                         "Read notes.md")

    def test_tool_source_names_the_server(self):
        source = chat_sessions._tool_source
        self.assertEqual(source("mcp__mac-mcp__search_files"), ("mac-mcp", "search_files"))
        self.assertEqual(source("mac-mcp_write_file"), ("mac-mcp", "write_file"))
        self.assertEqual(source("mcp__claude_ai_Google_Sheets__get_values"), ("claude_ai_Google_Sheets", "get_values"))
        self.assertEqual(source("Run the tests"), ("", ""))


class ChatSideEffectTest(unittest.TestCase):
    def test_chat_side_effects_need_no_delegated_workflow(self):
        from mcp_server import workflow_checkpoints as wc
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MAC_MCP_STATE_DIR": tmp}):
            self.assertIsNone(wc.begin_side_effect("chat_0123456789ab", tool="write_file", family="files",
                                                   capabilities=["local_write"], destructive=False,
                                                   arguments={"path": "x"}))
            with self.assertRaises(wc.CheckpointUnknownError):  # delegated agents still need their checkpoint
                wc.begin_side_effect("agt_0123456789", tool="write_file", family="files",
                                     capabilities=["local_write"], destructive=False, arguments={"path": "x"})


class ChatHostTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        agent = root / "fake_copilot"
        agent.write_text(f"#!{sys.executable}\nLOG_PATH = {str(root / 'agent.jsonl')!r}\n" + FAKE_AGENT)
        agent.chmod(0o755)
        self.log = root / "agent.jsonl"
        settings = root / "settings.json"
        settings.write_text(json.dumps({"subagents": {"providers": {"copilot": {"enabled": True}}}}))
        self.settings = settings
        self.store = ScopedCredentialStore(root / "scoped.sqlite3")
        env = {"MAC_MCP_STATE_DIR": str(root / "state"), "MAC_MCP_SETTINGS_PATH": str(settings),
               "MAC_MCP_COPILOT_BINARY": str(agent)}
        patches = [mock.patch.dict(os.environ, env),
                   mock.patch.object(chat_sessions, "get_scoped_credential_store", return_value=self.store),
                   mock.patch.object(chat_sessions, "_keychain_lookup", return_value=None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        chat_sessions.reset_host_for_tests()
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    def agent_log(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    async def wait_idle(self, host, chat_id, timeout=20.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            chat = host.read(chat_id)["chat"]
            if not chat["running"]:
                return host.read(chat_id)
            await asyncio.sleep(0.05)
        self.fail("chat did not finish")

    def test_create_refuses_bad_project_and_disabled_agent(self):
        host = chat_sessions.get_host()
        with self.assertRaises(chat_sessions.ChatError):
            host.create(str(self.project / "missing"), "copilot")
        with self.assertRaises(chat_sessions.ChatError):
            host.create(str(Path.home()), "copilot")
        with self.assertRaises(chat_sessions.ChatError):
            host.create(str(self.project), "claude")
        self.settings.write_text(json.dumps({"subagents": {"providers": {"copilot": {"enabled": False}}}}))
        with self.assertRaises(chat_sessions.ChatError) as caught:
            host.create(str(self.project), "copilot")
        self.assertEqual(caught.exception.code, "agent_disabled")
        self.assertEqual(host.list()["agents"], [])

    def test_prompt_streams_reply_and_scopes_mac_mcp(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot", model="openrouter/some/model:free")["chat"]["id"]
            await host.prompt(chat_id, "hello there")
            state = await self.wait_idle(host, chat_id)
            await host.shutdown()
            return chat_id, state

        chat_id, state = self.run_async(scenario())
        kinds = [item["type"] for item in state["items"]]
        self.assertEqual(kinds, ["user", "thought", "assistant", "turn"])
        self.assertEqual(state["items"][2]["text"], "Hello")
        turn = state["items"][3]  # what the CLI reported: time worked, model and tokens
        self.assertEqual((turn["stop"], turn["model"], turn["input_tokens"], turn["output_tokens"]),
                         ("end_turn", "Opus", 100, 2))
        self.assertGreaterEqual(turn["duration_s"], 0)
        self.assertNotIn("turn_started_at", state["chat"])
        self.assertEqual(state["chat"]["title"], "Greeting")
        self.assertEqual(state["chat"]["context"], {"used": 1200, "size": 128000})
        self.assertEqual(state["chat"]["usage"]["input_tokens"], 100)
        self.assertNotIn("acp_session_id", state["chat"])
        new = next(entry for entry in self.agent_log() if entry["method"] == "session/new")
        self.assertEqual(new["params"]["cwd"], str(self.project.resolve()))
        server = new["params"]["mcpServers"][0]
        self.assertEqual(server["type"], "http")
        self.assertTrue(server["url"].endswith("/mcp"))
        token = server["headers"][0]["value"].removeprefix("Bearer ")
        # The chat's credential ended with its agent process, and it was limited to the project folder.
        self.assertIsNone(self.store.resolve(token))
        saved = json.loads((chat_sessions.chats_root() / chat_id / "transcript.json").read_text())
        self.assertEqual([item["type"] for item in saved], kinds)

    def test_credential_is_scoped_to_project_while_running(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "hello")
            await self.wait_idle(host, chat_id)
            new = next(entry for entry in self.agent_log() if entry["method"] == "session/new")
            token = new["params"]["mcpServers"][0]["headers"][0]["value"].removeprefix("Bearer ")
            credential = self.store.resolve(token)
            await host.shutdown()
            return credential

        credential = self.run_async(scenario())
        self.assertIsNotNone(credential)
        self.assertEqual(credential.scope.path_roots, (str(self.project.resolve()),))
        self.assertRegex(credential.agent_id, r"^chat_[0-9a-f]{12}$")  # a valid workflow id: side effects are checkpointed

    def test_permission_prompt_waits_for_owner(self):
        async def scenario(choice):
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "write a file")
            for _ in range(400):
                pending = [item for item in host.read(chat_id)["items"]
                           if item["type"] == "permission" and item["status"] == "pending"]
                if pending:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(host.read(chat_id)["chat"]["status"], "waiting")
            with self.assertRaises(chat_sessions.ChatError):
                host.answer(chat_id, pending[0]["id"], "made_up")
            host.answer(chat_id, pending[0]["id"], choice)
            state = await self.wait_idle(host, chat_id)
            await host.shutdown()
            return state

        state = self.run_async(scenario("allow_once"))
        tool = next(item for item in state["items"] if item["type"] == "tool")
        permission = next(item for item in state["items"] if item["type"] == "permission")
        self.assertEqual((tool["status"], tool["output"], tool["files"]), ("done", "wrote", ["hello.txt"]))
        self.assertEqual(tool["title"], "Created hello.txt")
        self.assertEqual((permission["status"], permission["choice"]), ("answered", "allow_once"))
        self.assertEqual(permission["files"], ["hello.txt"])
        self.assertEqual([option["id"] for option in permission["options"]], ["allow_once", "reject_once"])
        self.assertEqual(state["items"][-2]["text"], "done")
        self.assertIn({"permission": {"outcome": "selected", "optionId": "allow_once"}}, self.agent_log())

    def test_cancel_stops_turn_and_answers_open_permission(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "slow")
            await asyncio.sleep(0.6)
            with self.assertRaises(chat_sessions.ChatError) as busy:
                await host.prompt(chat_id, "again")
            self.assertEqual(busy.exception.code, "busy")
            await host.cancel(chat_id)
            state = host.read(chat_id)
            await host.prompt(chat_id, "write it")
            for _ in range(400):
                if any(item["type"] == "permission" for item in host.read(chat_id)["items"]):
                    break
                await asyncio.sleep(0.02)
            await host.cancel(chat_id)
            after = host.read(chat_id)
            await host.shutdown()
            return state, after

        state, after = self.run_async(scenario())
        self.assertFalse(state["chat"]["running"])
        self.assertEqual(state["items"][-2], {**state["items"][-2], "type": "notice", "text": "Stopped."})
        self.assertEqual((state["items"][-1]["type"], state["items"][-1]["stop"]), ("turn", "cancelled"))
        self.assertIn("tick", state["items"][1]["text"])
        permission = next(item for item in after["items"] if item["type"] == "permission")
        self.assertEqual(permission["status"], "cancelled")
        self.assertIn({"permission": {"outcome": "cancelled"}}, self.agent_log())

    def test_reopened_chat_loads_agent_session_without_duplicating_history(self):
        async def first():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "hello")
            await self.wait_idle(host, chat_id)
            await host.shutdown()
            return chat_id

        chat_id = self.run_async(first())
        chat_sessions.reset_host_for_tests()  # the server restarted

        async def second():
            host = chat_sessions.get_host()
            self.assertEqual(host.list()["projects"][0]["chats"][0]["id"], chat_id)
            before = host.read(chat_id)["cursor"]
            await host.prompt(chat_id, "hello again")
            state = await self.wait_idle(host, chat_id)
            fresh = host.read(chat_id, after=before)
            await host.shutdown()
            return state, fresh

        state, fresh = self.run_async(second())
        load = next(entry for entry in self.agent_log() if entry["method"] == "session/load")
        self.assertEqual(load["params"]["sessionId"], "sess-1")
        self.assertEqual([item["type"] for item in state["items"]],
                         ["user", "thought", "assistant", "turn", "user", "thought", "assistant", "turn"])
        self.assertNotIn("old", [item.get("text") for item in state["items"]])
        self.assertEqual([item["type"] for item in fresh["items"]], ["user", "thought", "assistant", "turn"])

    def test_agent_options_are_read_set_and_kept_across_restart(self):
        async def first():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            host.connect(chat_id)
            for _ in range(200):
                if host.read(chat_id)["chat"]["config"]:
                    break
                await asyncio.sleep(0.02)
            config = host.read(chat_id)["chat"]["config"]
            with self.assertRaises(chat_sessions.ChatError):
                await host.configure(chat_id, "model", "gpt-made-up")
            changed = await host.configure(chat_id, "model", "sonnet")
            await host.shutdown()
            return chat_id, config, changed

        chat_id, config, changed = self.run_async(first())
        self.assertEqual([(entry["id"], entry["category"], entry["current"]) for entry in config],
                         [("model", "model", "opus"), ("effort", "thought_level", "high")])
        self.assertEqual([choice["value"] for choice in config[1]["options"]], ["low", "high"])
        self.assertEqual(config[1]["options"][0]["group"], "Levels")
        self.assertEqual(changed["chat"]["config"][0]["current"], "sonnet")
        chat_sessions.reset_host_for_tests()  # the agent restarted and forgot the pick

        async def second():
            host = chat_sessions.get_host()
            await host.prompt(chat_id, "hello")
            state = await self.wait_idle(host, chat_id)
            await host.shutdown()
            return state

        state = self.run_async(second())
        sets = [entry["params"] for entry in self.agent_log() if entry["method"] == "session/set_config_option"]
        self.assertEqual(len(sets), 2)  # chosen once, then set again after session/load
        self.assertEqual(state["chat"]["config"][0]["current"], "sonnet")

    def test_signed_out_agent_says_how_to_sign_in(self):
        Path(str(self.log) + ".needauth").write_text("1")

        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            host.connect(chat_id)
            for _ in range(200):
                chat = host.read(chat_id)["chat"]
                if chat.get("agent_error"):
                    break
                await asyncio.sleep(0.02)
            await host.prompt(chat_id, "hello")
            state = await self.wait_idle(host, chat_id)
            await host.shutdown()
            return chat, state

        chat, state = self.run_async(scenario())
        self.assertIn("copilot login", chat["agent_error"])
        self.assertEqual(state["items"][-2]["tone"], "error")
        self.assertIn("Sign in to GitHub Copilot", state["items"][-2]["text"])

    def test_mcp_startup_reports_are_not_steps(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "startup")
            state = await self.wait_idle(host, chat_id)
            await host.shutdown()
            return state

        state = self.run_async(scenario())
        self.assertEqual([item["type"] for item in state["items"]], ["user", "notice", "assistant", "turn"])
        self.assertIn("connection refused", state["items"][1]["text"])  # a skipped server says nothing; a failed one does

    def test_routes_need_the_dashboard_token(self):
        def guard(request):
            from starlette.responses import JSONResponse
            if request.headers.get("authorization") != "Bearer good":
                return JSONResponse({"detail": "no"}, status_code=401)
            return None

        client = TestClient(Starlette(routes=create_chat_routes(guard)))
        self.assertEqual(client.get("/dashboard/api/chats").status_code, 401)
        listed = client.get("/dashboard/api/chats", headers={"Authorization": "Bearer good"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["agents"][0]["id"], "copilot")
        made = client.post("/dashboard/api/chats", headers={"Authorization": "Bearer good"},
                           json={"project": str(self.project), "agent": "copilot"})
        self.assertEqual(made.status_code, 200)
        chat_id = made.json()["chat"]["id"]
        missing = client.get("/dashboard/api/chats/chat_000000000000", headers={"Authorization": "Bearer good"})
        self.assertEqual(missing.status_code, 404)
        empty = client.post(f"/dashboard/api/chats/{chat_id}/prompt", headers={"Authorization": "Bearer good"},
                            json={"text": "  "})
        self.assertEqual((empty.status_code, empty.json()["error"]), (400, "empty"))


if __name__ == "__main__":
    unittest.main()
