"""#114: secrets masked before agent and job records reach the disk, Mac MCP Chat in the privacy inventory,
chat retention and private-mode chats.

Synthetic secrets are compared with assertTrue/assertFalse so a failure never prints them.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server import chat_sessions, data_guard, privacy_inventory, runtime_settings
from mcp_server import tools_agents as agents
from mcp_server import tools_jobs
from mcp_server.scoped_auth import ScopedCredentialStore
from tests.test_chat_sessions import FAKE_AGENT

# Built at run time so no secret-shaped literal sits in the source.
KEY = "sk-" + "Synth114" + "x" * 24
PASSWORD = "hunter" + "2Synth114pw"
SECRET_LINE = f"api_key={KEY}"


def _has_secret(text: str) -> bool:
    return KEY in text or PASSWORD in text


class RedactorTests(unittest.TestCase):
    def test_text_and_json_lines_are_masked_and_json_still_parses(self) -> None:
        masked = data_guard.redact_log_line(f"export OPENAI_API_KEY={KEY}\n")
        self.assertFalse(_has_secret(masked))
        self.assertIn("[REDACTED]", masked)
        self.assertTrue(masked.endswith("\n"))
        event = json.dumps({"type": "text", "part": {"text": f"see https://x.test/?token={KEY}&a=1 done"}}) + "\n"
        line = data_guard.redact_log_line(event)
        self.assertFalse(_has_secret(line))
        parsed = json.loads(line)  # a provider's event stays readable for the extractors
        self.assertIn("[REDACTED]", parsed["part"]["text"])
        self.assertEqual("text", parsed["type"])
        plain = json.dumps({"type": "step_finish", "part": {"tokens": {"total": 3}}}) + "\n"
        self.assertEqual(plain, data_guard.redact_log_line(plain))  # nothing to mask: untouched

    def test_private_key_over_several_lines_is_one_masked_line(self) -> None:
        redact = data_guard.LogLineRedactor()
        lines = ["before\n", "-----BEGIN OPENSSH PRIVATE KEY-----\n", "b3BlbnNzaC1rZXktdjEAAAAA" + "Q" * 40 + "\n",
                 "c2VjcmV0" + "Z" * 40 + "\n", "-----END OPENSSH PRIVATE KEY-----\n", "after\n"]
        out = "".join(redact(line) for line in lines)
        self.assertEqual("before\n[PRIVATE KEY REDACTED]\nafter\n", out)


class JobOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-114-jobs-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = patch.object(tools_jobs, "JOBS_DIR", self.root / "jobs")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = MagicMock(max_output_chars=20_000, workdir=str(self.root), allow_shell=True)

    def test_secret_in_job_output_is_masked_on_disk_and_in_get_job_output(self) -> None:
        job_dir = self.root / "jobs" / "job114"
        job_dir.mkdir(parents=True)
        tools_jobs._write_meta("job114", {"job_id": "job114", "status": "running", "started_at": time.time()})
        stream = io.StringIO(f"building\n{SECRET_LINE}\nPassword: {PASSWORD}\ndone\n")
        tools_jobs._drain_stream("job114", stream, job_dir / "stdout.log", "stdout.log")
        stored = (job_dir / "stdout.log").read_text(encoding="utf-8")
        self.assertFalse(_has_secret(stored))
        self.assertIn("[REDACTED]", stored)
        self.assertIn("building", stored)
        output = tools_jobs.get_job_output(self.settings, "job114", stream="stdout")
        self.assertFalse(_has_secret(output["stdout"]))
        self.assertIn("[REDACTED]", output["stdout"])

    def test_output_kept_before_114_is_masked_when_read(self) -> None:
        job_dir = self.root / "jobs" / "jobold"
        job_dir.mkdir(parents=True)
        tools_jobs._write_meta("jobold", {"job_id": "jobold", "status": "completed", "started_at": time.time()})
        (job_dir / "stdout.log").write_text(SECRET_LINE + "\n", encoding="utf-8")
        output = tools_jobs.get_job_output(self.settings, "jobold", tail_lines=10)
        self.assertFalse(_has_secret(output["stdout"]))
        self.assertIn("[REDACTED]", output["stdout"])

    def test_a_real_job_stores_its_output_masked(self) -> None:
        settings = MagicMock(max_output_chars=20_000, workdir=str(self.root), allow_shell=True,
                             shell_enabled=True)
        with patch.object(tools_jobs, "require_shell_enabled"):
            started = tools_jobs.start_background_job(settings, f"echo '{SECRET_LINE}'", cwd=str(self.root))
            waited = tools_jobs.wait_jobs(settings, [started["job_id"]], timeout_s=30, return_output=True)
        self.assertTrue(waited["completed"])
        stored = (self.root / "jobs" / started["job_id"] / "stdout.log").read_text(encoding="utf-8")
        self.assertFalse(_has_secret(stored))
        self.assertIn("[REDACTED]", stored)
        self.assertFalse(_has_secret(waited["jobs"][0]["stdout"]))
        self.assertFalse(_has_secret(json.dumps(tools_jobs.get_job_status(settings, started["job_id"]))))


class AgentRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-114-agents-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for patcher in (patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.root / "state")}),
                        patch.object(agents, "AGENTS_DIR", self.root / "agents"),
                        patch.object(agents, "TEAMS_DIR", self.root / "teams")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fixture(self, agent_id: str, prompt: str) -> Path:
        adir = self.root / "agents" / agent_id
        adir.mkdir(parents=True)
        input_hash = agents.workflow_input_hash(prompt=prompt, provider="opencode", cwd=str(self.root),
                                                access_mode="read_only",
                                                scope={"access_mode": "read_only", "path_roots": [str(self.root)]},
                                                role=None)
        checkpoint = agents.create_workflow(agent_id=agent_id, input_hash=input_hash, provider="opencode")
        for name in ("stdout.log", "stderr.log", "worker.log", "result.txt"):
            (adir / name).write_text("", encoding="utf-8")
        (adir / "prompt.txt").write_text(data_guard.redact_sensitive_text(prompt), encoding="utf-8")
        (adir / "effective_prompt.txt").write_text(prompt, encoding="utf-8")
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id, "provider": "opencode", "binary": "/tmp/opencode", "cwd": str(self.root),
            "team_id": None, "team_task_id": None, "status": "starting", "phase": "starting", "started_at": now,
            "spawn_requested_at": now, "last_activity_at": now, "timeout_s": 1200, "idle_timeout_s": None,
            "retries": 0, "retry_count": 0, "result_style": "concise", "result_contract_version": 1,
            "result_contract_status": "pending", "access_mode": "read_only", "permission_profile": "read_only",
            "capability_profile": "legacy", "scope": {"access_mode": "read_only", "path_roots": [str(self.root)]},
            "attempt": 1, "workflow_id": checkpoint["workflow_id"], "workflow_input_hash": input_hash,
            "resume_generation": 0, "session_id": None, "resume_session_id": None, "tool_call_count": 0,
            "step_count": 0,
        })
        return adir

    def test_provider_streams_are_masked_before_the_log_and_the_event_record(self) -> None:
        adir = self.root / "agents" / "agt_stream"
        adir.mkdir(parents=True)
        events = []
        stdout = io.StringIO(json.dumps({"type": "text", "part": {"text": f"found {SECRET_LINE}"}}) + "\n")
        stderr = io.StringIO(f"warning: password={PASSWORD}\n")
        with patch.object(agents, "_record_provider_event", side_effect=lambda _id, line: events.append(line)):
            agents._capture_provider_stream("agt_stream", stdout, adir / "stdout.log", True)
            agents._capture_provider_stream("agt_stream", stderr, adir / "stderr.log", False)
        for name in ("stdout.log", "stderr.log"):
            text = (adir / name).read_text(encoding="utf-8")
            self.assertFalse(_has_secret(text), name)
            self.assertIn("[REDACTED]", text)
        self.assertEqual(1, len(events))
        self.assertFalse(_has_secret(events[0]))
        json.loads(events[0])

    def test_worker_sends_the_full_prompt_but_keeps_results_and_prompt_masked(self) -> None:
        prompt = f"Deploy with {SECRET_LINE} and report back."
        adir = self._fixture("agt_secret", prompt)
        sent = []
        report = f"Done. I used {SECRET_LINE}; the admin password: {PASSWORD} works."

        def fake_attempt(_agent_id, _meta, attempt_prompt, _index):
            sent.append(attempt_prompt)
            (adir / "stdout.log").write_text(
                json.dumps({"type": "text", "part": {"text": report}}) + "\n"
                + json.dumps({"type": "step_finish", "part": {"reason": "stop", "tokens": {"total": 12}}}) + "\n",
                encoding="utf-8")
            return 0, None

        with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
            rc = agents._worker("agt_secret")
        self.assertEqual(0, rc)
        self.assertTrue(len(sent) == 1 and KEY in sent[0])  # the provider got the task as written
        for name in ("effective_prompt.txt", "prompt.txt", "result.txt", "result.full.txt", "meta.json"):
            path = adir / name
            if path.exists():
                self.assertFalse(_has_secret(path.read_text(encoding="utf-8")), name)
        for path in adir.iterdir():  # the result envelope, wherever it is kept
            # stdout.log was written here by the fake, around the capture the stream test covers.
            if path.is_file() and path.name != "stdout.log":
                self.assertFalse(_has_secret(path.read_text(encoding="utf-8", errors="replace")), path.name)
        self.assertIn("[REDACTED]", (adir / "result.txt").read_text(encoding="utf-8"))
        shown = agents.get_agent(None, "agt_secret", include_logs=True, result_mode="full")
        self.assertFalse(_has_secret(json.dumps(shown)))
        self.assertIn("[REDACTED]", shown["result_full"]["text"])

    def test_get_agent_masks_logs_written_before_114(self) -> None:
        adir = self._fixture("agt_old", "plain task")
        (adir / "stdout.log").write_text(SECRET_LINE + "\n", encoding="utf-8")
        (adir / "worker.log").write_text(f"password={PASSWORD}\n", encoding="utf-8")
        shown = agents.get_agent(None, "agt_old", include_logs=True)
        self.assertFalse(_has_secret(json.dumps(shown["logs"])))
        self.assertIn("[REDACTED]", shown["logs"]["stdout"])

    def test_spawn_saves_the_prompt_and_title_masked(self) -> None:
        settings_path = self.root / "settings.json"
        settings_path.write_text(json.dumps({"subagents": {"providers": {"opencode": {"enabled": True}}}}))
        raw_at_start = []

        def fake_worker(agent_id, _log, _nonce=None):
            raw_at_start.append((agents.AGENTS_DIR / agent_id / "effective_prompt.txt").read_text(encoding="utf-8"))
            raise OSError("no worker in tests")

        with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings_path)}), \
             patch.object(agents, "_find_binary", return_value="/usr/bin/true"), \
             patch.object(agents, "_validate_provider_model_selection", return_value={"validated": True}), \
             patch.object(agents, "_spawn_worker_process", side_effect=fake_worker):
            with self.assertRaises(HTTPException):
                agents.spawn_agent(MagicMock(), provider="opencode", prompt=f"Use {SECRET_LINE} to deploy",
                                   cwd=str(self.root), access_mode="read_only", git_isolation="off")
        self.assertTrue(len(raw_at_start) == 1 and KEY in raw_at_start[0])  # the worker would read it whole
        (agent_dir,) = [path for path in (self.root / "agents").iterdir() if path.name.startswith("agt_")]
        for name in ("prompt.txt", "effective_prompt.txt", "meta.json"):
            text = (agent_dir / name).read_text(encoding="utf-8")
            self.assertFalse(_has_secret(text), name)
        self.assertIn("[REDACTED]", (agent_dir / "prompt.txt").read_text(encoding="utf-8"))


class ChatRetentionSettingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-114-settings-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.settings = self.root / "settings.json"
        patcher = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(self.settings)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_is_90_days_and_odd_values_snap_to_a_choice(self) -> None:
        self.assertEqual(90, runtime_settings.chat_retention_days())
        self.settings.write_text(json.dumps({"privacy": {"chat_retention_days": 29}}))
        self.assertEqual(30, runtime_settings.chat_retention_days())
        self.settings.write_text(json.dumps({"privacy": {"chat_retention_days": "x"}}))
        self.assertEqual(90, runtime_settings.chat_retention_days())

    def test_route_validates_merges_and_keeps_unrelated_fields(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        from mcp_server import dashboard_routes

        self.settings.write_text(json.dumps({"privacy": {"private_mode": True, "excluded_sites": ["bank.test"]},
                                             "subagents": {"retention_days": 14}}))
        token = "t" * 40
        routes = dashboard_routes.create_dashboard_routes(MagicMock(), MagicMock(), token, MagicMock(), MagicMock())
        client = TestClient(Starlette(routes=routes))
        headers = {"Authorization": f"Bearer {token}"}
        with patch.object(dashboard_routes, "_dashboard_guard", return_value=None), \
             patch.object(chat_sessions.ChatHost, "expire", new=mock.AsyncMock(return_value=[])) as expire:
            bad = client.post("/dashboard/api/privacy/settings", json={"chat_retention_days": 45}, headers=headers)
            self.assertEqual(400, bad.status_code)
            good = client.post("/dashboard/api/privacy/settings", json={"chat_retention_days": 30}, headers=headers)
            self.assertEqual(200, good.status_code, good.text)
            self.assertEqual(30, good.json()["chat_retention_days"])
            expire.assert_awaited()  # applies right away
            overview = client.get("/dashboard/api/privacy", headers=headers).json()
        saved = json.loads(self.settings.read_text())
        self.assertEqual(30, saved["privacy"]["chat_retention_days"])
        self.assertTrue(saved["privacy"]["private_mode"])
        self.assertEqual(["bank.test"], saved["privacy"]["excluded_sites"])
        self.assertEqual(14, saved["subagents"]["retention_days"])
        self.assertEqual(30, overview["chat_retention_days"])
        self.assertEqual([7, 30, 90, 180, 365], overview["chat_retention_choices"])
        chats = next(row for row in overview["stores"] if row["key"] == "chats")
        self.assertEqual("30 days after the last use", chats["kept"])
        jobs = next(row for row in overview["stores"] if row["key"] == "jobs")
        self.assertTrue(jobs["kept"].startswith(f"{tools_jobs.job_retention()['days']} days after they finish"))


class ChatStoreTests(unittest.TestCase):
    """The privacy inventory and bulk delete see chats, and only the chats."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-114-store-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = self.root / "state"
        patcher = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.state),
                                          "MAC_MCP_SETTINGS_PATH": str(self.root / "settings.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        chats = self.state / "chats"
        for chat_id in ("chat_0123456789ab", "chat_ba9876543210"):
            (chats / chat_id).mkdir(parents=True)
            (chats / chat_id / "transcript.json").write_text("[]")
        (chats / "agents" / "antigravity-1.4.3").mkdir(parents=True)  # a download, not your data
        (chats / "agents" / "antigravity-1.4.3" / "server.par").write_bytes(b"x" * 50_000)
        (chats / "copilot-home").mkdir()

    def test_inventory_lists_chats_and_bulk_delete_moves_only_chat_folders(self) -> None:
        self.assertIn("chats", privacy_inventory.DELETABLE)
        row = next(store for store in privacy_inventory.stores() if store["key"] == "chats")
        self.assertTrue(row["exists"] and row["deletable"])
        self.assertLess(row["bytes"], 1000)  # the download beside them is not counted
        self.assertEqual(2, row["files"])
        trash = self.root / "Trash"
        result = privacy_inventory.trash_stores(list(privacy_inventory.DELETABLE), trash_root=trash, stamp="t")
        moved = sorted(Path(item["path"]).name for item in result["moved"] if item["store"] == "chats")
        self.assertEqual(["chat_0123456789ab", "chat_ba9876543210"], moved)
        self.assertEqual([], result["failed"])
        left = sorted(path.name for path in (self.state / "chats").iterdir())
        self.assertEqual(["agents", "copilot-home"], left)
        self.assertTrue((trash / "Mac MCP data t" / "chats" / "chat_0123456789ab").is_dir())


class ChatLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        agent = root / "fake_copilot"
        agent.write_text(f"#!{sys.executable}\nLOG_PATH = {str(root / 'agent.jsonl')!r}\n" + FAKE_AGENT)
        agent.chmod(0o755)
        self.settings = root / "settings.json"
        self.write_settings()
        self.state = root / "state"
        store = ScopedCredentialStore(root / "scoped.sqlite3")
        patches = [mock.patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.state),
                                                "MAC_MCP_SETTINGS_PATH": str(self.settings),
                                                "MAC_MCP_COPILOT_BINARY": str(agent)}),
                   mock.patch.object(chat_sessions, "get_scoped_credential_store", return_value=store),
                   mock.patch.object(chat_sessions, "_keychain_lookup", return_value=None)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        chat_sessions.reset_host_for_tests()
        self.addCleanup(chat_sessions.reset_host_for_tests)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def write_settings(self, **privacy) -> None:
        self.settings.write_text(json.dumps({"subagents": {"providers": {"copilot": {"enabled": True}}},
                                             "privacy": privacy}))
        from mcp_server import privacy_policy
        privacy_policy._cache.update({"at": -1.0, "value": None})

    async def wait_idle(self, host, chat_id):
        for _ in range(400):
            if not host.read(chat_id)["chat"]["running"]:
                return
            await asyncio.sleep(0.05)
        self.fail("chat did not finish")

    def test_private_mode_chat_is_never_written_and_normal_chat_is(self) -> None:
        async def scenario():
            host = chat_sessions.get_host()
            self.write_settings(private_mode=True)
            private_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(private_id, "hello")
            await self.wait_idle(host, private_id)
            await asyncio.sleep(0.7)  # past the coalesced save
            private_state = host.read(private_id)
            self.write_settings(private_mode=False)
            normal_id = host.create(str(self.project), "copilot")["chat"]["id"]
            await host.prompt(normal_id, "hello")
            await self.wait_idle(host, normal_id)
            await host.shutdown()
            return private_id, private_state, normal_id

        private_id, private_state, normal_id = self.run_async(scenario())
        self.assertTrue(private_state["chat"]["private"])
        self.assertIn("assistant", [item["type"] for item in private_state["items"]])  # it worked, in memory
        self.assertFalse((self.state / "chats" / private_id).exists())
        self.assertTrue((self.state / "chats" / normal_id / "transcript.json").is_file())
        chat_sessions.reset_host_for_tests()  # the server restarted
        listed = [chat["id"] for project in chat_sessions.get_host().list(agents=False)["projects"]
                  for chat in project["chats"]]
        self.assertEqual([normal_id], listed)

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    def _old_chat(self, chat_id: str, days_ago: float, items=None) -> None:
        folder = self.state / "chats" / chat_id
        folder.mkdir(parents=True)
        at = time.time() - days_ago * 86400
        (folder / "chat.json").write_text(json.dumps({
            "id": chat_id, "title": "t", "project": str(self.project), "agent": "copilot", "model": "",
            "created_at": at, "updated_at": at, "status": "idle", "rev": 1}))
        (folder / "transcript.json").write_text(json.dumps(items if items is not None else [
            {"id": "u1", "type": "user", "text": "hi", "rev": 1, "at": at}]))

    def test_expired_chats_go_at_start_and_recent_ones_stay(self) -> None:
        self._old_chat("chat_000000000001", 100)
        self._old_chat("chat_000000000002", 10)
        host = chat_sessions.get_host()
        removed = self.run_async(host.expire())
        self.assertEqual([], removed)  # the load at start already removed the old one
        self.assertFalse((self.state / "chats" / "chat_000000000001").exists())
        self.assertTrue((self.state / "chats" / "chat_000000000002").exists())

    def test_sweep_removes_an_idle_chat_but_never_a_working_one(self) -> None:
        self._old_chat("chat_000000000003", 20)
        self._old_chat("chat_000000000004", 20)
        host = chat_sessions.get_host()
        host.list(agents=False)  # loaded while the retention is 90 days
        self.write_settings(chat_retention_days=7)

        async def scenario():
            working = host._chat("chat_000000000004")
            working.turn = asyncio.get_running_loop().create_future()  # an answer in progress
            removed = await host.expire()
            working.turn.cancel()
            return removed

        removed = self.run_async(scenario())
        self.assertEqual(["chat_000000000003"], removed)
        self.assertFalse((self.state / "chats" / "chat_000000000003").exists())
        self.assertTrue((self.state / "chats" / "chat_000000000004").exists())
        ids = [chat["id"] for project in host.list(agents=False)["projects"] for chat in project["chats"]]
        self.assertEqual(["chat_000000000004"], ids)


if __name__ == "__main__":
    unittest.main()
