from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_agents as agents
from mcp_server import tools_browser_agent as browser_agent
from mcp_server import tools_jobs, tools_ui
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import PolicyContext
from mcp_server.security import load_settings
from mcp_server.tool_cancellation import (
    ToolCancellationContext,
    ToolCancelledError,
    cancellable_sleep,
    reset_tool_cancellation,
    set_tool_cancellation,
)
from mcp_server.tools_terminal import run_command
import mcp_server.workflow_checkpoints as workflows


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class CancellationPrimitiveTests(unittest.TestCase):
    def test_long_sync_worker_cancel_prevents_late_sentinel_and_telemetry_is_cancelled(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                sentinel = root / "late.txt"
                started = threading.Event()
                telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3", max_events=100)
                context = PolicyContext(profile="trusted", actor="authenticated")
                mcp = ObservedFastMCP(
                    name="cancel-sync-test", telemetry=telemetry,
                    policy_context_provider=lambda: context,
                )

                @mcp.tool(name="process_list", structured_output=False)
                def fake_process_list(filter: str | None = None):
                    started.set()
                    for _ in range(100):
                        cancellable_sleep(0.02)
                    sentinel.write_text("late", encoding="utf-8")
                    return {"ok": True}

                task = asyncio.create_task(mcp.call_tool("process_list", {}))
                self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0.15)
                self.assertFalse(sentinel.exists())
                events = telemetry.query_events(tool="process_list", limit=5)
                self.assertTrue(events)
                self.assertEqual("cancelled", events[0]["status"])

        asyncio.run(run())

    def test_run_command_cancellation_kills_shell_and_child_process_group(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                settings = replace(load_settings(), workdir=root)
                shell_pid_path = root / "shell.pid"
                child_pid_path = root / "child.pid"
                command = (
                    f"echo $$ > {shell_pid_path}; "
                    f"sleep 30 & child=$!; echo $child > {child_pid_path}; wait $child"
                )
                cancellation = ToolCancellationContext(cleanup_wait_s=1.5)
                token = set_tool_cancellation(cancellation)
                try:
                    task = asyncio.create_task(asyncio.to_thread(run_command, settings, command, 30))
                    self.assertTrue(await asyncio.to_thread(
                        _wait_until, lambda: shell_pid_path.exists() and child_pid_path.exists(), 2.0, 0.02
                    ))
                    shell_pid = int(shell_pid_path.read_text().strip())
                    child_pid = int(child_pid_path.read_text().strip())
                    self.assertTrue(_pid_alive(shell_pid))
                    self.assertTrue(_pid_alive(child_pid))
                    cancellation.cancel("test_cancel")
                    with self.assertRaises(ToolCancelledError):
                        await asyncio.wait_for(task, timeout=3)
                    self.assertTrue(await asyncio.to_thread(
                        _wait_until, lambda: not _pid_alive(shell_pid) and not _pid_alive(child_pid), 2.0, 0.03
                    ))
                finally:
                    reset_tool_cancellation(token)

        asyncio.run(run())

    def test_parallel_command_cancellation_stops_owned_background_jobs(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td, patch.object(tools_jobs, "JOBS_DIR", Path(td) / "jobs"):
                root = Path(td)
                settings = replace(load_settings(), workdir=root)
                cancellation = ToolCancellationContext(cleanup_wait_s=1.5)
                token = set_tool_cancellation(cancellation)
                try:
                    task = asyncio.create_task(asyncio.to_thread(
                        tools_jobs.run_commands_parallel,
                        settings,
                        ["sleep 30", "sleep 30"],
                        str(root),
                        30,
                        False,
                    ))
                    self.assertTrue(await asyncio.to_thread(
                        _wait_until,
                        lambda: tools_jobs.JOBS_DIR.exists() and len(list(tools_jobs.JOBS_DIR.glob("*/meta.json"))) >= 2,
                        2.0,
                        0.02,
                    ))
                    cancellation.cancel("test_cancel")
                    with self.assertRaises(ToolCancelledError):
                        await asyncio.wait_for(task, timeout=4)
                    meta_paths = list(tools_jobs.JOBS_DIR.glob("*/meta.json"))
                    self.assertEqual(2, len(meta_paths))
                    self.assertTrue(await asyncio.to_thread(
                        _wait_until,
                        lambda: all(
                            tools_jobs._read_meta(path.parent.name).get("status") in {"killed", "failed", "completed"}
                            and bool(tools_jobs._read_meta(path.parent.name).get("ended_at"))
                            for path in meta_paths
                        ),
                        3.0, 0.03,
                    ))
                    metas = [tools_jobs._read_meta(path.parent.name) for path in meta_paths]
                    for meta in metas:
                        pid = int(meta.get("pid") or 0)
                        if pid:
                            self.assertTrue(await asyncio.to_thread(_wait_until, lambda p=pid: not _pid_alive(p), 2.0, 0.03))
                finally:
                    reset_tool_cancellation(token)

        asyncio.run(run())

    def test_browser_wait_cancel_is_prompt_and_tab_lease_exits(self) -> None:
        cancellation = ToolCancellationContext()
        token = set_tool_cancellation(cancellation)
        try:
            timer = threading.Timer(0.12, lambda: cancellation.cancel("browser_cancel"))
            timer.start()
            started = time.monotonic()
            with patch.object(browser_agent, "_run_json_js", return_value={"matched": False}):
                with self.assertRaises(ToolCancelledError):
                    browser_agent._wait_action(
                        settings=None,
                        browser="Safari",
                        action={"type": "wait", "for": "selector", "selector": "#never", "timeout_s": 5, "poll_ms": 50},
                        window_index=1,
                        tab_index=1,
                        initial_url="https://example.com",
                        tab_handle="tab_test",
                    )
            timer.join(timeout=1)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            reset_tool_cancellation(token)

        exited = {"value": False}
        @contextmanager
        def fake_lease(*args, **kwargs):
            class Target:
                browser = "Safari"
                window_index = 1
                tab_index = 1
                tab_handle = "tab_test"
            try:
                yield Target()
            finally:
                exited["value"] = True
        with patch.object(browser_agent, "_norm_browser", return_value="Safari"), \
             patch.object(browser_agent, "_require_stable_handle_for_mutation"), \
             patch.object(browser_agent, "_ensure_visual_companion"), \
             patch.object(browser_agent, "_tab_lease", side_effect=fake_lease), \
             patch.object(browser_agent, "_browser_act_locked", side_effect=ToolCancelledError("client_cancelled")):
            with self.assertRaises(ToolCancelledError):
                browser_agent.browser_act(None, "Safari", [{"type": "wait", "for": "selector"}], tab_handle="tab_test")
        self.assertTrue(exited["value"])

    def test_native_cancel_restores_focus_before_propagating(self) -> None:
        cancellation = ToolCancellationContext()
        token = set_tool_cancellation(cancellation)
        restored = {"count": 0}
        try:
            target = {
                "app": "TextEdit", "pid": 123, "app_handle": "app_test",
                "window_handle": None, "window_index": 1,
            }
            focus_context = {
                "pid": 999, "window_index": 1, "window_handle": "win_prev",
                "window_count": 1,
            }
            def cancel_action(*args, **kwargs):
                cancellation.cancel("native_cancel")
                raise ToolCancelledError("native_cancel")
            def restore(*args, **kwargs):
                restored["count"] += 1
                return True, "previous focus restored", True
            with patch.object(tools_ui, "_resolve_action_native_target", return_value=(target, None)), \
                 patch.object(tools_ui, "_capture_focus_context", return_value=(focus_context, None)), \
                 patch.object(tools_ui, "_action_requires_foreground", return_value=True), \
                 patch.object(tools_ui, "_focus_transition_needed", return_value=True), \
                 patch.object(tools_ui, "_perform_action", side_effect=cancel_action), \
                 patch.object(tools_ui, "_post_action_focus_decision", return_value=("restore", None)), \
                 patch.object(tools_ui, "_restore_focus_context", side_effect=restore):
                with self.assertRaises(ToolCancelledError):
                    tools_ui.act_ui(
                        None,
                        actions=[{"type": "key", "key": "a"}],
                        app="TextEdit",
                        preserve_focus=True,
                        state_mode="none",
                    )
            self.assertEqual(1, restored["count"])
        finally:
            reset_tool_cancellation(token)


class CancellationOutcomeSafetyTests(unittest.TestCase):
    def test_outcome_unknown_blocks_automatic_provider_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"):
            agent_id = "agt_cancel_retry_guard"
            adir = agents.AGENTS_DIR / agent_id
            adir.mkdir(parents=True)
            input_hash = workflows.workflow_input_hash(
                prompt="perform once", provider="opencode", cwd=td,
                access_mode="workspace_write", scope={"access_mode": "workspace_write"}, role=None,
            )
            checkpoint = workflows.create_workflow(
                agent_id=agent_id, input_hash=input_hash, provider="opencode"
            )
            for name, value in {
                "prompt.txt": "perform once",
                "effective_prompt.txt": "perform once",
                "stdout.log": "",
                "stderr.log": "",
                "worker.log": "",
                "result.txt": "",
            }.items():
                (adir / name).write_text(value, encoding="utf-8")
            now = time.time()
            agents._write_meta(agent_id, {
                "agent_id": agent_id, "provider": "opencode", "binary": "/tmp/opencode",
                "cwd": td, "status": "starting", "phase": "starting",
                "started_at": now, "spawn_requested_at": now, "last_activity_at": now,
                "timeout_s": 1200, "idle_timeout_s": None, "retries": 1, "retry_count": 0,
                "result_style": "concise", "access_mode": "workspace_write",
                "permission_profile": "trusted", "capability_profile": "legacy",
                "scope": {"access_mode": "workspace_write", "path_roots": [td]},
                "attempt": 1, "workflow_id": checkpoint["workflow_id"],
                "workflow_input_hash": input_hash, "resume_generation": 0,
                "session_id": "ses-cancel-guard", "resume_session_id": None,
                "tool_call_count": 0, "step_count": 0,
            })
            workflows.update_provider_state(agent_id, session_id="ses-cancel-guard")
            calls: list[int] = []

            def fake_attempt(_agent_id, _meta, _prompt, attempt_index):
                calls.append(attempt_index)
                if attempt_index == 0:
                    workflows.mark_checkpoint_unknown(
                        agent_id, "client_cancelled_outcome_unknown",
                        tool="write_file", event_type="client_cancelled",
                    )
                    return 1, "timeout"
                raise AssertionError("automatic retry must not run after cancellation outcome becomes unknown")

            with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
                rc = agents._worker(agent_id)
            saved = agents._read_meta(agent_id)
            self.assertEqual(1, rc)
            self.assertEqual([0], calls)
            self.assertEqual("outcome_unknown", saved["retry_blocked_reason"])
            self.assertIn("outcome unknown", saved["note"])
            self.assertEqual("unknown", workflows.workflow_for_agent(agent_id)["safety"])

    def test_noncancellable_side_effect_cancel_marks_unknown_and_blocks_resume(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td, patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
            ):
                root = Path(td)
                sentinel = root / "late-side-effect.txt"
                started = threading.Event()
                input_hash = workflows.workflow_input_hash(
                    prompt="write once", provider="codex", cwd=td,
                    access_mode="workspace_write", scope={}, role=None,
                )
                workflows.create_workflow(agent_id="agt_cancel_unknown", input_hash=input_hash, provider="codex")
                workflows.update_provider_state("agt_cancel_unknown", session_id="sess-cancel-1")
                telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3", max_events=100)
                context = PolicyContext(
                    profile="trusted", actor="agent:agt_cancel_unknown", agent_id="agt_cancel_unknown"
                )
                mcp = ObservedFastMCP(
                    name="cancel-unknown-test", telemetry=telemetry,
                    policy_context_provider=lambda: context,
                )

                @mcp.tool(name="write_file", structured_output=False)
                def fake_write_file(path: str, content: str):
                    started.set()
                    # Intentionally non-cooperative to model an opaque native call.
                    time.sleep(2.2)
                    sentinel.write_text(content, encoding="utf-8")
                    return {"ok": True, "path": path}

                task = asyncio.create_task(mcp.call_tool(
                    "write_file", {"path": str(sentinel), "content": "late"}
                ))
                self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                state = workflows.workflow_for_agent("agt_cancel_unknown")
                self.assertEqual("unknown", state["safety"])
                self.assertEqual("client_cancelled_outcome_unknown", state["unknown_reason"])
                self.assertEqual([], state["pending_effects"])
                public = workflows.public_state("agt_cancel_unknown")
                self.assertFalse(public["resumable"])
                with self.assertRaises(workflows.CheckpointUnknownError):
                    workflows.prepare_resume(
                        "agt_cancel_unknown", expected_input_hash=input_hash, session_id="sess-cancel-1"
                    )
                events = telemetry.query_events(tool="write_file", limit=5)
                self.assertTrue(events)
                self.assertEqual("outcome_unknown", events[0]["status"])
                # Let the opaque worker finish so the test leaves no thread behind.
                await asyncio.sleep(0.9)
                self.assertTrue(sentinel.exists())

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
