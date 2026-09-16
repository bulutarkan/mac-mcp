from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError

import mcp_server.tools_agents as agents
import mcp_server.workflow_checkpoints as workflows
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import PolicyContext


def _record_verified_effect(
    agent_id: str, *, tool: str, family: str, capabilities: list[str], destructive: bool,
    arguments: dict, result: dict, event_id: str | None = None,
):
    intent = workflows.begin_side_effect(
        agent_id, tool=tool, family=family, capabilities=capabilities, destructive=destructive,
        arguments=arguments, event_id=event_id,
    )
    return workflows.record_side_effect_outcome(
        agent_id, tool=tool, family=family, capabilities=capabilities, destructive=destructive,
        arguments=arguments, result=result, event_id=event_id, intent_id=intent["intent_id"],
    )


class WorkflowCheckpointStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.state)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def _create(self, agent_id: str = "agt_checkpoint01") -> tuple[str, dict]:
        input_hash = workflows.workflow_input_hash(
            prompt="submit once", provider="codex", cwd="/tmp", access_mode="workspace_write",
            scope={"access_mode": "workspace_write"}, role="coder",
        )
        checkpoint = workflows.create_workflow(
            agent_id=agent_id, input_hash=input_hash, provider="codex",
        )
        workflows.update_provider_state(agent_id, session_id="sess-checkpoint-1")
        return input_hash, checkpoint

    def test_verified_receipt_survives_restart_and_resumes_generation(self) -> None:
        input_hash, checkpoint = self._create()
        receipt = _record_verified_effect(
            "agt_checkpoint01", tool="write_file", family="files",
            capabilities=["local_write"], destructive=True,
            arguments={"path": "/tmp/important.txt", "content": "TOP_SECRET_SENTINEL"},
            result={"ok": True, "path": "/tmp/important.txt"}, event_id="evt-1",
        )
        self.assertTrue(receipt["verified"])
        workflows.note_provider_event("agt_checkpoint01", "codex", {"type": "turn.started"})
        workflows.note_provider_event(
            "agt_checkpoint01", "codex",
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "write_file"}},
        )
        workflows.mark_terminal("agt_checkpoint01", "failed")

        public = workflows.public_state("agt_checkpoint01")
        self.assertTrue(public["resumable"])
        self.assertEqual(1, public["side_effect_receipt_count"])
        self.assertEqual("interrupted", public["checkpoint_state"])

        prepared = workflows.prepare_resume(
            "agt_checkpoint01", expected_input_hash=input_hash, session_id="sess-checkpoint-1",
        )
        prompt = workflows.resume_prompt(prepared)
        self.assertIn(receipt["receipt_id"], prompt)
        self.assertIn("Do not replay completed side effects", prompt)
        self.assertIn("Last durable provider cursor", prompt)
        self.assertIn("kind=mcp_tool_completed", prompt)
        self.assertNotIn("TOP_SECRET_SENTINEL", prompt)

        child = "agt_checkpoint02"
        workflows.bind_resumed_agent(
            workflow_id=prepared["workflow_id"], parent_agent_id="agt_checkpoint01", agent_id=child,
            input_hash=input_hash, resume_generation=prepared["resume_generation"],
            resume_token=prepared["resume_token"], session_id="sess-checkpoint-1",
        )
        resumed = workflows.workflow_for_agent(child)
        self.assertEqual(1, resumed["resume_generation"])
        self.assertEqual(child, resumed["current_agent_id"])
        self.assertEqual(1, resumed["receipt_count"])
        self.assertEqual("running", resumed["state"])

    def test_corrupt_checkpoint_is_unknown_and_never_resumed(self) -> None:
        input_hash, checkpoint = self._create("agt_corrupt01")
        workflows.mark_terminal("agt_corrupt01", "failed")
        path = workflows.workflow_root() / f"{checkpoint['workflow_id']}.json"
        raw = json.loads(path.read_text())
        raw["payload"]["receipt_count"] = 999
        path.write_text(json.dumps(raw), encoding="utf-8")
        public = workflows.public_state("agt_corrupt01")
        self.assertEqual("unknown", public["checkpoint_state"])
        self.assertFalse(public["resumable"])
        with self.assertRaises(workflows.CheckpointIntegrityError):
            workflows.prepare_resume(
                "agt_corrupt01", expected_input_hash=input_hash, session_id="sess-checkpoint-1",
            )

    def test_resume_rejects_input_hash_mismatch(self) -> None:
        _, _ = self._create("agt_hashmismatch")
        workflows.mark_terminal("agt_hashmismatch", "failed")
        with self.assertRaises(workflows.CheckpointUnknownError):
            workflows.prepare_resume(
                "agt_hashmismatch", expected_input_hash="0" * 64, session_id="sess-checkpoint-1",
            )

    def test_provider_native_activity_makes_resume_outcome_unknown(self) -> None:
        input_hash, _ = self._create("agt_native01")
        workflows.note_provider_event(
            "agt_native01", "codex",
            {"type": "item.started", "item": {"type": "command_execution", "command": "touch /tmp/x"}},
        )
        workflows.mark_terminal("agt_native01", "failed")
        public = workflows.public_state("agt_native01")
        self.assertEqual("unknown", public["checkpoint_safety"])
        self.assertEqual("unverified_provider_native_activity", public["checkpoint_reason"])
        checkpoint = workflows.workflow_for_agent("agt_native01")
        raw = (workflows.workflow_root() / f"{checkpoint['workflow_id']}.json").read_text()
        self.assertNotIn("touch /tmp/x", raw)
        self.assertEqual("native_command_started", checkpoint["checkpoint_cursor"]["kind"])
        with self.assertRaises(workflows.CheckpointUnknownError):
            workflows.prepare_resume(
                "agt_native01", expected_input_hash=input_hash, session_id="sess-checkpoint-1",
            )

    def test_mcp_routed_provider_activity_does_not_taint_checkpoint(self) -> None:
        self._create("agt_mcp01")
        workflows.note_provider_event(
            "agt_mcp01", "opencode",
            {"type": "tool_use", "part": {"tool": "mac-mcp_write_file"}},
        )
        workflows.note_provider_event(
            "agt_mcp01", "codex",
            {"type": "item.started", "item": {"type": "mcp_tool_call", "server": "mac-mcp", "tool": "write_file"}},
        )
        state = workflows.workflow_for_agent("agt_mcp01")
        self.assertEqual("verified", state["safety"])
        self.assertEqual(2, state["provider_event_count"])
        self.assertEqual("mcp_tool_started", state["checkpoint_cursor"]["kind"])
        self.assertEqual(2, state["checkpoint_cursor"]["seq"])

    def test_explicit_failed_side_effect_result_becomes_unknown(self) -> None:
        self._create("agt_failedresult")
        arguments = {"actions": [{"type": "click"}]}
        intent = workflows.begin_side_effect(
            "agt_failedresult", tool="browser_act", family="browser",
            capabilities=["ui_action", "external_side_effect"], destructive=True, arguments=arguments,
        )
        receipt = workflows.record_side_effect_outcome(
            "agt_failedresult", tool="browser_act", family="browser",
            capabilities=["ui_action", "external_side_effect"], destructive=True,
            arguments=arguments, result={"ok": False, "error": "timeout"}, intent_id=intent["intent_id"],
        )
        self.assertFalse(receipt["verified"])
        state = workflows.workflow_for_agent("agt_failedresult")
        self.assertEqual("unknown", state["safety"])
        self.assertEqual("side_effect_result_not_verified", state["unknown_reason"])

    def test_crash_after_intent_before_result_is_outcome_unknown(self) -> None:
        input_hash, checkpoint = self._create("agt_pending01")
        secret = "PENDING_SECRET_SENTINEL"
        intent = workflows.begin_side_effect(
            "agt_pending01", tool="write_file", family="files", capabilities=["local_write"],
            destructive=True, arguments={"path": "/tmp/pending", "content": secret}, event_id="evt-pending",
        )
        self.assertTrue(intent["intent_id"].startswith("intent_"))
        in_flight = workflows.workflow_for_agent("agt_pending01")
        self.assertEqual("pending", in_flight["safety"])
        self.assertEqual(1, len(in_flight["pending_effects"]))
        raw = (workflows.workflow_root() / f"{checkpoint['workflow_id']}.json").read_text()
        self.assertNotIn(secret, raw)
        self.assertNotIn("/tmp/pending", raw)

        # Simulate daemon/worker death before the tool result can be committed as a receipt.
        workflows.mark_terminal("agt_pending01", "failed")
        public = workflows.public_state("agt_pending01")
        self.assertEqual("unknown", public["checkpoint_safety"])
        self.assertEqual("side_effect_in_flight", public["checkpoint_reason"])
        self.assertEqual(1, public["pending_side_effect_count"])
        self.assertFalse(public["resumable"])
        with self.assertRaises(workflows.CheckpointUnknownError):
            workflows.prepare_resume(
                "agt_pending01", expected_input_hash=input_hash, session_id="sess-checkpoint-1",
            )

    def test_checkpoint_files_are_owner_only(self) -> None:
        _, checkpoint = self._create("agt_modes01")
        root = workflows.workflow_root()
        path = root / f"{checkpoint['workflow_id']}.json"
        mapping = root / "agents" / "agt_modes01.json"
        self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE((root / "agents").stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(mapping.stat().st_mode))


class ObservedReceiptTests(unittest.TestCase):
    def test_successful_mutating_mcp_call_writes_verified_receipt(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td, patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
            ):
                input_hash = workflows.workflow_input_hash(
                    prompt="write once", provider="codex", cwd=td, access_mode="workspace_write",
                    scope={}, role=None,
                )
                workflows.create_workflow(agent_id="agt_receipt01", input_hash=input_hash, provider="codex")
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                context = PolicyContext(profile="trusted", actor="agent:agt_receipt01", agent_id="agt_receipt01")
                mcp = ObservedFastMCP(name="receipt-test", telemetry=telemetry, policy_context_provider=lambda: context)

                @mcp.tool(name="write_file")
                def write_file(path: str, content: str) -> dict:
                    return {"ok": True, "path": path, "bytes": len(content)}

                await mcp.call_tool("write_file", {"path": "/tmp/a", "content": "secret-value"})
                checkpoint = workflows.workflow_for_agent("agt_receipt01")
                self.assertEqual(1, checkpoint["receipt_count"])
                self.assertEqual([], checkpoint["pending_effects"])
                self.assertEqual("verified", checkpoint["safety"])
                self.assertEqual("write_file", checkpoint["receipts"][0]["tool"])
                raw = (workflows.workflow_root() / f"{checkpoint['workflow_id']}.json").read_text()
                self.assertNotIn("secret-value", raw)
                self.assertNotIn("/tmp/a", raw)

        asyncio.run(run())

    def test_mutating_mcp_exception_marks_checkpoint_unknown(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td, patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
            ):
                input_hash = workflows.workflow_input_hash(
                    prompt="write once", provider="codex", cwd=td, access_mode="workspace_write",
                    scope={}, role=None,
                )
                workflows.create_workflow(agent_id="agt_receipt02", input_hash=input_hash, provider="codex")
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                context = PolicyContext(profile="trusted", actor="agent:agt_receipt02", agent_id="agt_receipt02")
                mcp = ObservedFastMCP(name="receipt-error-test", telemetry=telemetry, policy_context_provider=lambda: context)

                @mcp.tool(name="write_file")
                def write_file(path: str, content: str) -> dict:
                    raise RuntimeError("disk disappeared after write")

                with self.assertRaises(ToolError):
                    await mcp.call_tool("write_file", {"path": "/tmp/a", "content": "x"})
                checkpoint = workflows.workflow_for_agent("agt_receipt02")
                self.assertEqual("unknown", checkpoint["safety"])
                self.assertEqual([], checkpoint["pending_effects"])
                self.assertEqual("side_effect_call_raised", checkpoint["unknown_reason"])

        asyncio.run(run())

    def test_mutating_agent_without_durable_workflow_is_not_executed(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td, patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
            ):
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                context = PolicyContext(profile="trusted", actor="agent:agt_legacy01", agent_id="agt_legacy01")
                mcp = ObservedFastMCP(name="receipt-missing-test", telemetry=telemetry, policy_context_provider=lambda: context)
                executed = {"count": 0}

                @mcp.tool(name="write_file")
                def write_file(path: str, content: str) -> dict:
                    executed["count"] += 1
                    return {"ok": True}

                with self.assertRaises(ToolError) as ctx:
                    await mcp.call_tool("write_file", {"path": "/tmp/nope", "content": "x"})
                self.assertIn("resume_outcome_unknown", str(ctx.exception))
                self.assertIn("action was not executed", str(ctx.exception))
                self.assertEqual(0, executed["count"])

        asyncio.run(run())


class AgentDurableResumeTests(unittest.TestCase):
    def test_automatic_retry_stops_after_first_verified_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"):
            agent_id = "agt_retryreceipt"
            adir = Path(td) / "agents" / agent_id
            adir.mkdir(parents=True)
            input_hash = workflows.workflow_input_hash(
                prompt="write marker once", provider="opencode", cwd=td, access_mode="workspace_write",
                scope={"access_mode": "workspace_write"}, role=None,
            )
            checkpoint = workflows.create_workflow(agent_id=agent_id, input_hash=input_hash, provider="opencode")
            for name, value in {
                "prompt.txt": "write marker once",
                "effective_prompt.txt": "write marker once",
                "stdout.log": "",
                "stderr.log": "",
                "worker.log": "",
                "result.txt": "",
            }.items():
                (adir / name).write_text(value, encoding="utf-8")
            meta = {
                "agent_id": agent_id, "provider": "opencode", "binary": "/tmp/opencode",
                "cwd": td, "status": "starting", "phase": "starting",
                "started_at": 100.0, "spawn_requested_at": 100.0, "last_activity_at": 100.0,
                "timeout_s": 1200, "idle_timeout_s": None, "retries": 1, "retry_count": 0,
                "result_style": "concise", "access_mode": "workspace_write",
                "permission_profile": "trusted", "capability_profile": "legacy",
                "scope": {"access_mode": "workspace_write", "path_roots": [td]},
                "attempt": 1, "workflow_id": checkpoint["workflow_id"],
                "workflow_input_hash": input_hash, "resume_generation": 0,
                "session_id": "ses-retry-receipt", "resume_session_id": None,
                "tool_call_count": 0, "step_count": 0,
            }
            (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            workflows.update_provider_state(agent_id, session_id="ses-retry-receipt")
            calls: list[int] = []

            def fake_attempt(_agent_id, _meta, _prompt, attempt_index):
                calls.append(attempt_index)
                if attempt_index == 0:
                    _record_verified_effect(
                        agent_id, tool="write_file", family="files", capabilities=["local_write"],
                        destructive=True, arguments={"path": "/tmp/marker"}, result={"ok": True},
                    )
                    return 4, None
                raise AssertionError("second provider attempt must not run after a verified side effect")

            with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
                rc = agents._worker(agent_id)
            saved = agents._read_meta(agent_id)
            self.assertEqual(1, rc)
            self.assertEqual([0], calls)
            self.assertEqual("failed", saved["status"])
            self.assertIn("Automatic replay stopped", saved["note"])
            self.assertEqual(1, workflows.workflow_for_agent(agent_id)["receipt_count"])

    def test_injected_worker_crash_resumes_same_session_without_replaying_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"), \
             patch.object(agents, "provider_enabled", return_value=True), \
             patch.object(agents, "_find_binary", return_value="/tmp/codex"), \
             patch.object(agents, "_base_env", return_value={}), \
             patch.object(agents.subprocess, "Popen", return_value=SimpleNamespace(pid=999999)), \
             patch.object(agents.threading, "Thread", return_value=SimpleNamespace(start=lambda: None)):
            scope = agents.ResourceScope.from_dict({"access_mode": "workspace_write", "path_roots": [td]})
            parent = agents._spawn_internal(
                settings=None, provider="codex", prompt="create marker once", model=None, reasoning="high",
                cwd=td, timeout_s=1200, title="crash resume", result_style="concise",
                access_mode="workspace_write", scope=scope, permission_profile="trusted",
            )
            parent_id = parent["agent_id"]
            agents._update_meta(parent_id, lambda current: current.update({"session_id": "sess-durable-1"}))
            workflows.update_provider_state(parent_id, session_id="sess-durable-1")
            receipt = _record_verified_effect(
                parent_id, tool="write_file", family="files", capabilities=["local_write"], destructive=True,
                arguments={"path": "/tmp/marker", "content": "done"}, result={"ok": True},
            )

            # Injected crash: worker pid is dead, so normalization must checkpoint interruption.
            parent_meta = agents._normalize(parent_id, agents._read_meta(parent_id))
            self.assertEqual("failed", parent_meta["status"])
            self.assertTrue(workflows.public_state(parent_id)["resumable"])

            with self.assertRaises(HTTPException) as retry_ctx:
                agents.agent_action(None, action="retry", agent_id=parent_id)
            self.assertEqual(409, retry_ctx.exception.status_code)
            self.assertIn("retry_replay_unsafe", str(retry_ctx.exception.detail))

            resumed = agents.agent_action(None, action="resume", agent_id=parent_id)
            child_id = resumed["agent_id"]
            child_meta = agents._read_meta(child_id)
            self.assertEqual("sess-durable-1", child_meta["resume_session_id"])
            self.assertEqual(1, child_meta["resume_generation"])
            resume_text = (Path(td) / "agents" / child_id / "prompt.txt").read_text()
            self.assertIn(receipt["receipt_id"], resume_text)
            self.assertIn("Do not replay completed side effects", resume_text)
            self.assertNotEqual("create marker once", resume_text)
            checkpoint = workflows.workflow_for_agent(child_id)
            self.assertEqual(child_id, checkpoint["current_agent_id"])
            self.assertEqual(1, checkpoint["receipt_count"])
            self.assertEqual(1, checkpoint["resume_generation"])

    def test_corrupt_checkpoint_agent_resume_returns_409_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"):
            agent_id = "agt_corruptresume"
            adir = Path(td) / "agents" / agent_id
            adir.mkdir(parents=True)
            input_hash = workflows.workflow_input_hash(
                prompt="x", provider="codex", cwd=td, access_mode="workspace_write", scope={}, role=None,
            )
            checkpoint = workflows.create_workflow(agent_id=agent_id, input_hash=input_hash, provider="codex")
            workflows.update_provider_state(agent_id, session_id="sess-corrupt")
            workflows.mark_terminal(agent_id, "failed")
            meta = {
                "agent_id": agent_id, "provider": "codex", "status": "failed", "phase": "failed",
                "session_id": "sess-corrupt", "workflow_input_hash": input_hash, "workflow_id": checkpoint["workflow_id"],
                "cwd": td, "access_mode": "workspace_write", "permission_profile": "trusted",
                "capability_profile": "legacy", "scope": {"access_mode": "workspace_write"},
                "attempt": 1, "started_at": 1.0, "last_activity_at": 1.0,
            }
            (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            (adir / "prompt.txt").write_text("x", encoding="utf-8")
            (adir / "stdout.log").write_text("", encoding="utf-8")
            path = workflows.workflow_root() / f"{checkpoint['workflow_id']}.json"
            raw = json.loads(path.read_text())
            raw["payload"]["state"] = "tampered"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(HTTPException) as ctx:
                agents.agent_action(None, action="resume", agent_id=agent_id)
            self.assertEqual(409, ctx.exception.status_code)
            self.assertIn("resume_outcome_unknown", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
