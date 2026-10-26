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
    log.write(json.dumps({"argv": sys.argv[1:]}) + "\\n"); log.flush()
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
        log.write(json.dumps({"prompt_text": text}) + "\\n"); log.flush()
        if text == "run tests":
            update(sid, {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "npm test", "kind": "execute",
                         "status": "in_progress", "rawInput": {"command": "npm test"},
                         "_meta": {"terminal_info": {"terminal_id": "c1", "cwd": "/p"}}})
            for chunk in ("\\x1b[32mPASS\\x1b[0m a\\r\\n", "progress 10%\\rprogress 100%\\n"):
                update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                             "_meta": {"terminal_output": {"terminal_id": "c1", "data": chunk}}})
                time.sleep(0.05)
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "failed",
                         "content": [{"type": "terminal", "terminalId": "c1"}],
                         "_meta": {"terminal_exit": {"terminal_id": "c1", "exit_code": 1, "signal": None}}})
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "one failed"}})
            return {"stopReason": "end_turn"}
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
                         "content": [{"type": "content", "content": {"type": "text", "text": "wrote" if ok else "no"}},
                                     {"type": "diff", "path": "hello.txt", "oldText": "a\\nb\\n", "newText": "a\\nc\\nd\\n"}]})
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
        update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": [
            {"name": "compact", "description": "Summarize the conversation", "input": None},
            {"name": "review", "description": "Review changes", "input": {"hint": "what to review"}}]})
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
            log.write(json.dumps({"client_caps": params.get("clientCapabilities")}) + "\\n"); log.flush()
            result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True}},
                      "authMethods": [{"id": "oauth-personal", "name": "Log in with Google"}]}
        elif method == "authenticate":
            if os.path.exists(LOG_PATH + ".needauth") and params.get("methodId") == "oauth-personal":
                os.remove(LOG_PATH + ".needauth")
            result = {}
        elif method == "session/new":
            if os.path.exists(LOG_PATH + ".needauth"):
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "Authentication required"}})
                continue
            result = {"sessionId": "sess-1"}
            if not os.path.exists(LOG_PATH + ".nooptions"):  # like Copilot, which offers no model over ACP
                result["configOptions"] = options()
        elif method == "session/set_config_option":
            config[params["configId"]] = params["value"]
            result = {"configOptions": options()}
        elif method == "session/load":
            update(params["sessionId"], {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old"}})
            update(params["sessionId"], {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "old"}})
            result = {} if os.path.exists(LOG_PATH + ".nooptions") else {"configOptions": options()}
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
        new = next(entry for entry in self.agent_log() if entry.get("method") == "session/new")
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
            new = next(entry for entry in self.agent_log() if entry.get("method") == "session/new")
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
        self.assertEqual(tool["diffs"][0], {"path": "hello.txt", "added": 2, "removed": 1, "created": False,
                                            "lines": ["@@ -1,2 +1,3 @@", " a", "-b", "+c", "+d"], "truncated": False})
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
            queued = await host.prompt(chat_id, "again")  # written while it works: waits its turn
            self.assertTrue(queued["queued"])
            await host.cancel(chat_id)
            state = host.read(chat_id)
            self.assertEqual([entry["text"] for entry in state["chat"]["queue"]], ["again"])  # Stop sends nothing on
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
        load = next(entry for entry in self.agent_log() if entry.get("method") == "session/load")
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
        sets = [entry["params"] for entry in self.agent_log() if entry.get("method") == "session/set_config_option"]
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

    def test_agent_without_a_chosen_sign_in_is_told_which_one_and_opens(self):
        Path(str(self.log) + ".needauth").write_text("1")

        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "hello")
            state = await self.wait_idle(host, chat_id)
            await host.shutdown()
            return state

        with mock.patch.dict(chat_sessions.AGENTS["copilot"], {"auth_method": "oauth-personal"}):
            state = self.run_async(scenario())
        methods = [entry.get("method") for entry in self.agent_log() if entry.get("method")]
        self.assertEqual(methods[:4], ["initialize", "session/new", "authenticate", "session/new"])
        self.assertFalse(state["chat"].get("agent_error"))
        self.assertEqual([item["type"] for item in state["items"] if item["type"] == "assistant"], ["assistant"])

    def test_downloaded_agent_is_checked_before_it_is_unpacked(self):
        import hashlib
        import zipfile
        archive = Path(self.tmp.name) / "server.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            info = zipfile.ZipInfo("agy_acp_server.par")
            info.external_attr = 0o100755 << 16
            bundle.writestr(info, "#!/bin/sh\n")
        spec = {"version": "9.9.9", "url": archive.as_uri(), "cmd": "agy_acp_server.par",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
        with mock.patch.dict(chat_sessions.AGENTS["antigravity"], {"download": spec}):
            with mock.patch.dict(spec, {"sha256": "0" * 64}):
                with self.assertRaises(chat_sessions.ChatError):
                    chat_sessions._ensure_download("antigravity")
            self.assertFalse(chat_sessions._download_dir("antigravity").exists())
            chat_sessions._ensure_download("antigravity")
            server = chat_sessions._download_dir("antigravity") / "agy_acp_server.par"
            self.assertTrue(os.access(server, os.X_OK))
            with mock.patch.object(chat_sessions, "_binary", return_value="/opt/agy"):
                self.assertEqual(chat_sessions._command("antigravity"), [str(server)])
        evil = Path(self.tmp.name) / "evil.zip"
        with zipfile.ZipFile(evil, "w") as bundle:
            bundle.writestr("../outside.txt", "x")
        bad = dict(spec, version="6.6.6", url=evil.as_uri(), sha256=hashlib.sha256(evil.read_bytes()).hexdigest())
        with mock.patch.dict(chat_sessions.AGENTS["antigravity"], {"download": bad}):
            with self.assertRaises(chat_sessions.ChatError):
                chat_sessions._ensure_download("antigravity")
        self.assertFalse((chat_sessions.chats_root() / "outside.txt").exists())

    def test_messages_written_while_it_works_go_out_in_order(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "slow")
            for _ in range(500):  # the agent is answering
                if any(entry.get("prompt_text") == "slow" for entry in self.agent_log()):
                    break
                await asyncio.sleep(0.02)
            first = (await host.prompt(chat_id, "hello one"))["chat"]["queue"][0]["id"]
            await host.prompt(chat_id, "hello two")
            dropped = (await host.prompt(chat_id, "hello three"))["chat"]["queue"][-1]["id"]
            await host.queued(chat_id, dropped, "remove")
            await host.queued(chat_id, first, "send_now")  # stops "slow", sends "hello one", then "hello two"
            for _ in range(500):
                state = host.read(chat_id)
                if not state["chat"]["running"] and not state["chat"].get("queue"):
                    break
                await asyncio.sleep(0.02)
            await host.shutdown()
            return state

        state = self.run_async(scenario())
        sent = [entry["prompt_text"] for entry in self.agent_log() if "prompt_text" in entry]
        self.assertEqual(sent, ["slow", "hello one", "hello two"])
        self.assertEqual([item["text"] for item in state["items"] if item["type"] == "user"],
                         ["slow", "hello one", "hello two"])
        self.assertEqual([item["stop"] for item in state["items"] if item["type"] == "turn"],
                         ["cancelled", "end_turn", "end_turn"])

    def test_stop_while_the_agent_is_still_starting_stops_at_once(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "slow")
            started = asyncio.get_running_loop().time()
            await host.cancel(chat_id)
            took = asyncio.get_running_loop().time() - started
            state = host.read(chat_id)
            await host.shutdown()
            return took, state

        took, state = self.run_async(scenario())
        self.assertLess(took, 3)
        self.assertFalse(state["chat"]["running"])
        self.assertEqual([(item["type"], item.get("text"), item.get("stop")) for item in state["items"][1:]],
                         [("notice", "Stopped.", None), ("turn", None, "cancelled")])

    def test_command_output_streams_into_its_step_as_a_terminal(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "run tests")
            state = await self.wait_idle(host, chat_id)
            await host.shutdown()
            return state

        state = self.run_async(scenario())
        caps = next(entry["client_caps"] for entry in self.agent_log() if "client_caps" in entry)
        self.assertEqual((caps["terminal"], caps["_meta"]), (False, {"terminal_output": True}))
        step = next(item for item in state["items"] if item["type"] == "tool")
        self.assertEqual(step["terminal"], {"output": "PASS a\nprogress 100%\n", "exit_code": 1, "cwd": "/p", "ended": True})
        self.assertEqual(step["status"], "failed")

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

    def test_old_empty_chats_are_removed_at_start(self):
        host = chat_sessions.get_host()
        empty = host.create(str(self.project), "copilot")["chat"]["id"]
        fresh = host.create(str(self.project), "copilot")["chat"]["id"]
        meta_path = chat_sessions.chats_root() / empty / "chat.json"
        meta = json.loads(meta_path.read_text())
        meta["updated_at"] = 1000.0  # long ago
        meta_path.write_text(json.dumps(meta))
        chat_sessions.reset_host_for_tests()
        listed = [chat["id"] for project in chat_sessions.get_host().list()["projects"] for chat in project["chats"]]
        self.assertEqual(listed, [fresh])  # the new empty one stays: the owner may be about to write
        self.assertFalse((chat_sessions.chats_root() / empty).exists())

    def test_delete_only_if_empty_keeps_a_chat_with_messages(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "hello")
            await self.wait_idle(host, chat_id)
            kept = await host.delete(chat_id, only_if_empty=True)
            still = host.read(chat_id)["ok"]
            empty = host.create(str(self.project), "copilot")["chat"]["id"]
            gone = await host.delete(empty, only_if_empty=True)
            await host.shutdown()
            return kept, still, gone, empty

        kept, still, gone, empty = self.run_async(scenario())
        self.assertEqual((kept.get("kept"), still), (True, True))
        self.assertNotIn("kept", gone)
        self.assertFalse((chat_sessions.chats_root() / empty).exists())

    def test_event_stream_pushes_changes_as_they_happen(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            stop = asyncio.Event()

            async def disconnected():
                return stop.is_set()

            batches = []

            async def listen():
                async for batch in host.stream(chat_id, 0, disconnected):
                    batches.append(batch)
                    if batch is not None and any(item["type"] == "turn" for item in batch["items"]):
                        stop.set()

            with mock.patch.object(chat_sessions, "STREAM_KEEPALIVE_S", 0.2):
                listener = asyncio.create_task(listen())
                await asyncio.sleep(0.5)  # quiet: keep-alives only
                await host.prompt(chat_id, "hello")
                await asyncio.wait_for(listener, 15)
            await host.shutdown()
            return batches

        batches = self.run_async(scenario())
        self.assertIn(None, batches[:3])  # keep-alive while nothing happened
        updates = [batch for batch in batches if batch is not None]
        cursors = [batch["cursor"] for batch in updates]
        self.assertEqual(cursors, sorted(cursors))
        seen = [item["type"] for batch in updates for item in batch["items"]]
        for kind in ("user", "thought", "assistant", "turn"):
            self.assertIn(kind, seen)
        merged = {}
        for batch in updates:
            for item in batch["items"]:
                merged[item["id"]] = item
        self.assertIn("Hello", [item.get("text") for item in merged.values()])  # chunks arrive merged, nothing lost

    def test_attachments_go_to_the_agent_as_image_and_file_link(self):
        import base64
        picture = self.project / "shot.png"
        picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pixels")
        notes = self.project / "notes.txt"
        notes.write_text("hello")

        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            with self.assertRaises(chat_sessions.ChatError):
                await host.prompt(chat_id, "look", [str(self.project / "missing.png")])
            with self.assertRaises(chat_sessions.ChatError):
                await host.prompt(chat_id, "look", [str(notes)] * 11)
            await host.prompt(chat_id, "", [str(picture), str(notes)])
            state = await self.wait_idle(host, chat_id)
            await host.shutdown()
            return state

        state = self.run_async(scenario())
        user = state["items"][0]
        self.assertEqual([(file["name"], file["kind"]) for file in user["attachments"]],
                         [("shot.png", "image"), ("notes.txt", "file")])
        sent = next(entry for entry in self.agent_log() if entry.get("method") == "session/prompt")["params"]["prompt"]
        self.assertEqual([block["type"] for block in sent], ["text", "image", "resource_link"])
        self.assertEqual(base64.b64decode(sent[1]["data"]), picture.read_bytes())
        self.assertEqual((sent[1]["mimeType"], sent[2]["uri"]), ("image/png", notes.resolve().as_uri()))

    def test_images_become_links_for_agents_without_image_support(self):
        picture = self.project / "shot.png"
        picture.write_bytes(b"png")
        blocks = chat_sessions._prompt_blocks("see", chat_sessions._attachments([str(picture)]), {})
        self.assertEqual([block["type"] for block in blocks], ["text", "resource_link"])

    def test_chat_says_whether_its_agent_takes_pictures_once_it_knows(self):
        # The app notes "Goes as a file link" on an image only from what the agent itself said at its start.
        async def scenario():
            host = chat_sessions.get_host()
            created = host.create(str(self.project), "copilot")["chat"]
            await host.prompt(created["id"], "hello there")
            state = await self.wait_idle(host, created["id"])
            chat = host._chat(created["id"])
            chat.prompt_caps = {"audio": True}
            without = chat.public()["takes_images"]
            await host.shutdown()
            return created, state, without

        created, state, without = self.run_async(scenario())
        self.assertIsNone(created["takes_images"])  # not started yet: nothing claimed
        self.assertIs(state["chat"]["takes_images"], True)  # the fake agent says promptCapabilities.image
        self.assertIs(without, False)
        self.assertNotIn("takes_images", json.loads((chat_sessions.chats_root() / created["id"] / "chat.json").read_text()))

    def test_rename_keeps_the_owner_title_and_commands_are_listed(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            with self.assertRaises(chat_sessions.ChatError):
                host.rename(chat_id, "   ")
            host.rename(chat_id, "  Launch   plan ")
            await host.prompt(chat_id, "hello")  # the agent then offers its own title, "Greeting"
            state = await self.wait_idle(host, chat_id)
            await host.shutdown()
            return state

        state = self.run_async(scenario())
        self.assertEqual(state["chat"]["title"], "Launch plan")
        self.assertEqual(state["chat"]["commands"], [
            {"name": "compact", "description": "Summarize the conversation", "hint": ""},
            {"name": "review", "description": "Review changes", "hint": "what to review"}])

    def test_usage_counts_chat_turns_apart_from_agents(self):
        async def scenario():
            host = chat_sessions.get_host()
            chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(chat_id, "hello")
            await self.wait_idle(host, chat_id)
            await host.prompt(chat_id, "hello again")
            await self.wait_idle(host, chat_id)
            usage = host.usage(30)
            await host.shutdown()
            return usage

        usage = self.run_async(scenario())
        self.assertEqual((usage["totals"]["turns"], usage["totals"]["input_tokens"], usage["totals"]["output_tokens"]),
                         (2, 200, 4))
        self.assertEqual(usage["agents"][0]["agent"], "copilot")
        self.assertEqual(usage["agents"][0]["chats"], 1)
        self.assertEqual(sum(day["turns"] for day in usage["daily"]), 2)
        self.assertEqual(sum(day["agents"].get("copilot", 0) for day in usage["daily"]), 204)

    def test_copilot_model_and_thinking_restart_the_agent_with_flags(self):
        Path(str(self.log) + ".nooptions").write_text("1")
        with mock.patch.object(chat_sessions, "copilot_models", return_value=["claude-sonnet-5.5", "gpt-6-luna"]), \
                mock.patch.object(chat_sessions, "copilot_efforts", return_value=["low", "high"]):
            async def scenario():
                host = chat_sessions.get_host()
                chat_id = host.create(str(self.project), "copilot")["chat"]["id"]
                host.connect(chat_id)
                for _ in range(200):
                    if host.read(chat_id)["chat"]["config"]:
                        break
                    await asyncio.sleep(0.02)
                config = json.loads(json.dumps(host.read(chat_id)["chat"]["config"]))  # as first offered
                await host.configure(chat_id, "model", "gpt-6-luna")
                await host.configure(chat_id, "reasoning_effort", "high")
                for _ in range(200):
                    chat = host.read(chat_id)["chat"]
                    if chat["agent_alive"] and not chat["connecting"]:
                        break
                    await asyncio.sleep(0.02)
                await host.prompt(chat_id, "hello")
                state = await self.wait_idle(host, chat_id)
                await host.shutdown()
                return config, state

            config, state = self.run_async(scenario())
        by_id = {option["id"]: option for option in config}
        self.assertEqual(by_id["model"]["current"], "auto")
        self.assertEqual([choice["value"] for choice in by_id["model"]["options"]], ["auto", "claude-sonnet-5.5", "gpt-6-luna"])
        self.assertEqual(by_id["reasoning_effort"]["category"], "thought_level")
        starts = [entry["argv"] for entry in self.agent_log() if "argv" in entry]
        self.assertEqual(starts[-1][:4], ["--model", "gpt-6-luna", "--reasoning-effort", "high"])
        self.assertEqual({option["id"]: option["current"] for option in state["chat"]["config"]}["model"], "gpt-6-luna")

    def test_codex_chat_gets_its_own_mac_mcp_and_no_chatgpt_apps(self):
        fake_codex = Path(self.tmp.name) / "codex"
        fake_codex.write_text("#!/bin/sh\necho \"$@\"\n")
        fake_codex.chmod(0o755)
        import subprocess
        with mock.patch("mcp_server.tools_agents._owner_codex_has_mac_mcp", return_value=True), \
                mock.patch.dict(os.environ, {"MAC_MCP_CODEX_BINARY": str(fake_codex), "MAC_MCP_PORT": "8765"}):
            env = chat_sessions._agent_env("codex", "", "mcs_token")
        self.assertEqual(env["MAC_MCP_CHAT_TOKEN"], "mcs_token")
        self.assertEqual(json.loads(env["CODEX_CONFIG"]), {"features": {"apps": False}})
        started = subprocess.run([env["CODEX_PATH"], "app-server", "--listen", "stdio://"], capture_output=True, text=True)
        self.assertEqual(started.stdout.split(), [
            "app-server", "-c", "features.apps=false",
            "-c", 'mcp_servers.mac-mcp-chat.url="http://127.0.0.1:8765/mcp"',
            "-c", 'mcp_servers.mac-mcp-chat.bearer_token_env_var="MAC_MCP_CHAT_TOKEN"',
            "-c", "mcp_servers.mac-mcp.enabled=false", "--listen", "stdio://"])
        other = subprocess.run([env["CODEX_PATH"], "--version"], capture_output=True, text=True)
        self.assertEqual(other.stdout.split(), ["--version"])
        self.assertEqual(chat_sessions.AGENTS["codex"]["mcp_name"], "mac-mcp-chat")
        self.assertEqual(chat_sessions._tool_source("mcp.mac-mcp-chat.write_file"), ("mac-mcp", "write_file"))

    def test_goose_without_a_provider_of_its_own_starts_on_openrouter(self):
        goose_home = Path(self.tmp.name) / "goose-config"
        goose_home.mkdir()
        environ = {key: value for key, value in os.environ.items()
                   if key not in ("GOOSE_PROVIDER", "GOOSE_MODEL", "OPENROUTER_API_KEY")}
        with mock.patch.object(chat_sessions.harness, "owner_home", return_value=goose_home), \
                mock.patch.object(chat_sessions, "_keychain_lookup", return_value="or-key"), \
                mock.patch.dict(os.environ, environ, clear=True):
            env = chat_sessions._agent_env("goose", "", "mcs_token")
            self.assertEqual((env["GOOSE_PROVIDER"], env["GOOSE_MODEL"]), ("openrouter", chat_sessions.GOOSE_START_MODEL))
            self.assertTrue(env["OPENROUTER_API_KEY"] == "or-key")  # never print a key in a failure
            (goose_home / "config.yaml").write_text("GOOSE_PROVIDER: anthropic\n")  # the owner's own setup wins
            env = chat_sessions._agent_env("goose", "", "mcs_token")
            self.assertNotIn("GOOSE_PROVIDER", env)
            self.assertNotIn("OPENROUTER_API_KEY", env)

    def test_sign_in_problems_are_recognised(self):
        from mcp_server.acp_client import AcpError
        self.assertTrue(chat_sessions._needs_sign_in(AcpError("Internal error", -32603,
                                                             "Failed to resolve provider: Configuration value not found: GOOSE_PROVIDER")))
        self.assertTrue(chat_sessions._needs_sign_in(AcpError("Authentication required", -32000)))
        self.assertFalse(chat_sessions._needs_sign_in(AcpError("Session not found", -32602)))

    def test_list_stream_shows_a_chat_working_in_the_background(self):
        async def scenario():
            host = chat_sessions.get_host()
            busy = host.create(str(self.project), "copilot")["chat"]["id"]
            host.create(str(self.project), "copilot")  # the one the owner looks at
            stop = asyncio.Event()

            async def disconnected():
                return stop.is_set()

            seen = []

            async def listen():
                async for listing in host.list_stream(disconnected):
                    if listing is None:
                        continue
                    state = {chat["id"]: chat["running"] for project in listing["projects"] for chat in project["chats"]}
                    seen.append(state.get(busy))
                    if seen[-1] is False and True in seen:
                        stop.set()

            listener = asyncio.create_task(listen())
            await asyncio.sleep(0.1)
            await host.prompt(busy, "slow")
            await asyncio.sleep(1.5)
            await host.cancel(busy)
            await asyncio.wait_for(listener, 10)
            await host.shutdown()
            return seen

        seen = self.run_async(scenario())
        self.assertIn(True, seen)
        self.assertEqual(seen[-1], False)
        self.assertNotIn("agents", chat_sessions.get_host().list(agents=False))

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
        self.assertEqual(client.get(f"/dashboard/api/chats/{chat_id}/events").status_code, 401)
        self.assertEqual(client.get("/dashboard/api/chats/chat_000000000000/events",
                                    headers={"Authorization": "Bearer good"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
