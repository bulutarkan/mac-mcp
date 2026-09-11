from __future__ import annotations

import asyncio
import gc
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent

from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.steering import SteeringManager, attach_steering, describe_target, preemption_error


class FakeSession:
    pass


class SteeringManagerTests(unittest.TestCase):
    def test_labels_hide_internal_ids_and_surface_context(self) -> None:
        label, detail = describe_target(
            "browser_do",
            {"browser": "Safari", "url": "https://www.booking.com/hotel/tr/example.html"},
        )
        self.assertEqual(label, "Safari · booking.com")
        self.assertEqual(detail, "browser_do")

        label, _ = describe_target("read_file", {"path": "/Users/test/Projects/mac-mcp/mcp_server/main.py"})
        self.assertIn("mcp_server/main.py", label)

    def test_session_identity_is_stable_and_isolated(self) -> None:
        manager = SteeringManager()
        a = FakeSession()
        b = FakeSession()
        a_id = manager.session_id_for(a)
        self.assertEqual(a_id, manager.session_id_for(a))
        b_id = manager.session_id_for(b)
        self.assertNotEqual(a_id, b_id)

        manager.enqueue(a_id, "only A should see this")
        rows = {row["session_id"]: row for row in manager.sessions()}
        self.assertEqual(rows[a_id]["queued"], 1)
        self.assertEqual(rows[b_id]["queued"], 0)
        self.assertEqual(manager.prepare_call(b, tool="read_file", arguments={}), [])
        pending = manager.prepare_call(a, tool="read_file", arguments={})
        self.assertEqual(pending[0]["text"], "only A should see this")
        self.assertEqual(manager.recent()[0]["status"], "preempted")

    def test_active_delivery_keeps_session_idle_after_call(self) -> None:
        manager = SteeringManager()
        session = FakeSession()
        manager.prepare_call(session, tool="read_file", arguments={"path": "/tmp/x"})
        sid = manager.session_id_for(session)
        manager.begin_call(session, "evt_test", tool="read_file", arguments={"path": "/tmp/x"})
        self.assertEqual(manager.sessions()[0]["state"], "working")
        queued = manager.enqueue(sid, "change direction")
        delivered = manager.finish_call(session, "evt_test", delivered=True)
        self.assertEqual(delivered[0]["id"], queued["id"])
        row = manager.sessions()[0]
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["queued"], 0)
        self.assertEqual(manager.recent()[0]["status"], "delivered")

    def test_failed_tool_preserves_pending_for_next_preemption(self) -> None:
        manager = SteeringManager()
        session = FakeSession()
        manager.prepare_call(session, tool="run_command", arguments={"command": "false"})
        sid = manager.session_id_for(session)
        manager.begin_call(session, "evt_fail", tool="run_command", arguments={"command": "false"})
        manager.enqueue(sid, "do something else")
        self.assertEqual(manager.finish_call(session, "evt_fail", delivered=False), [])
        self.assertEqual(manager.sessions()[0]["queued"], 1)
        pending = manager.prepare_call(session, tool="read_file", arguments={"path": "/tmp/a"})
        self.assertEqual(pending[0]["text"], "do something else")

    def test_session_cleanup_marks_undelivered_messages(self) -> None:
        manager = SteeringManager()
        session = FakeSession()
        sid = manager.session_id_for(session)
        manager.enqueue(sid, "pending")
        del session
        gc.collect()
        self.assertEqual(manager.sessions(), [])
        recent = manager.recent()
        self.assertEqual(recent[0]["status"], "session_ended")
        self.assertEqual(recent[0]["session_id"], sid)
        with self.assertRaises(KeyError):
            manager.enqueue(sid, "too late")

    def test_preemption_error_says_tool_was_not_executed(self) -> None:
        text = preemption_error(
            "delete_path",
            [{"id": "st_1", "text": "do not delete", "created_at": 1.0}],
        )
        self.assertIn("mac_mcp_steering_preempted", text)
        self.assertIn("This tool was NOT executed", text)
        self.assertIn("do not delete", text)

    def test_attach_mirrors_steering_into_wrapped_structured_result(self) -> None:
        original_content = [TextContent(type="text", text='{"ok":true}')]
        structured = {"result": {"ok": True, "value": 42}}
        result = attach_steering(
            (original_content, structured),
            [{"id": "st_wrapped", "text": "connector-visible", "created_at": 3.0}],
        )
        self.assertEqual(result[1]["result"]["ok"], True)
        steering = result[1]["result"]["_mac_mcp_steering"]
        self.assertEqual(steering["messages"][0]["text"], "connector-visible")

    def test_attach_preserves_original_content(self) -> None:
        original = {"ok": True, "value": 42}
        result = attach_steering(original, [{"id": "st_1", "text": "hello", "created_at": 1.0}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["value"], 42)
        self.assertEqual(result["_mac_mcp_steering"]["messages"][0]["text"], "hello")

    def test_attach_preserves_fastmcp_structured_output_tuple(self) -> None:
        original_content = [TextContent(type="text", text='{"ok":true}')]
        structured = {"ok": True, "value": 42}
        result = attach_steering(
            (original_content, structured),
            [{"id": "st_tuple", "text": "new direction", "created_at": 2.0}],
        )
        self.assertIsInstance(result, tuple)
        self.assertEqual(result[1]["ok"], structured["ok"])
        self.assertEqual(len(result[0]), 2)
        payload = json.loads(result[0][-1].text)
        self.assertEqual(payload["_mac_mcp_steering"]["messages"][0]["text"], "new direction")


class ObservedFastMCPSteeringTests(unittest.TestCase):
    @staticmethod
    def bind_session(mcp: ObservedFastMCP, session: FakeSession) -> None:
        mcp.get_context = lambda: SimpleNamespace(session=session)  # type: ignore[method-assign]

    def test_live_message_reaches_response_and_not_telemetry(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="steering-test", telemetry=telemetry, steering=steering)
                session = FakeSession()
                self.bind_session(mcp, session)

                @mcp.tool(name="read_file", structured_output=False)
                async def fake_read_file(path: str):
                    await asyncio.sleep(0.12)
                    return {"ok": True, "path": path, "content": "original"}

                task = asyncio.create_task(mcp.call_tool("read_file", {"path": "/tmp/example.txt"}))
                for _ in range(30):
                    sessions = steering.sessions()
                    if sessions and sessions[0]["state"] == "working":
                        break
                    await asyncio.sleep(0.005)
                self.assertEqual(len(sessions), 1)
                sid = sessions[0]["session_id"]
                steering.enqueue(sid, "vazgeçtim, başka dosyaya bak")
                result = await task

                self.assertEqual(len(result), 2)
                payload = json.loads(result[-1].text)
                self.assertEqual(
                    payload["_mac_mcp_steering"]["messages"][0]["text"],
                    "vazgeçtim, başka dosyaya bak",
                )
                events = telemetry.query_events(limit=10)
                self.assertEqual(len(events), 1)
                self.assertNotIn("_mac_mcp_steering", json.dumps(events[0].get("result"), ensure_ascii=False))
                self.assertEqual(steering.sessions()[0]["state"], "idle")

        asyncio.run(run())

    def test_idle_message_preempts_next_tool_before_execution(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="preempt-test", telemetry=telemetry, steering=steering)
                session = FakeSession()
                self.bind_session(mcp, session)
                executions = 0

                @mcp.tool(name="read_file")
                def fake_read_file(path: str):
                    nonlocal executions
                    executions += 1
                    return {"ok": True, "path": path, "executions": executions}

                first = await mcp.call_tool("read_file", {"path": "/tmp/first"})
                self.assertEqual(executions, 1)
                sid = steering.sessions()[0]["session_id"]
                steering.enqueue(sid, "do not run the next tool")

                with self.assertRaises(ToolError) as caught:
                    await mcp.call_tool("read_file", {"path": "/tmp/second"})
                self.assertEqual(executions, 1)
                self.assertIn("mac_mcp_steering_preempted", str(caught.exception))
                self.assertIn("do not run the next tool", str(caught.exception))
                self.assertEqual(steering.recent()[0]["status"], "preempted")

                await mcp.call_tool("read_file", {"path": "/tmp/third"})
                self.assertEqual(executions, 2)
                self.assertTrue(first)

        asyncio.run(run())

    def test_sync_tool_keeps_event_loop_responsive_for_live_steering(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="sync-steering-test", telemetry=telemetry, steering=steering)
                session = FakeSession()
                self.bind_session(mcp, session)

                @mcp.tool(name="read_file", structured_output=False)
                def fake_read_file(path: str):
                    time.sleep(0.16)
                    return {"ok": True, "path": path}

                task = asyncio.create_task(mcp.call_tool("read_file", {"path": "/tmp/sync.txt"}))
                await asyncio.sleep(0.035)
                sessions = steering.sessions()
                self.assertEqual(len(sessions), 1, "sync tool blocked the event loop")
                self.assertEqual(sessions[0]["state"], "working")
                steering.enqueue(sessions[0]["session_id"], "change course while sync tool is running")
                result = await task
                payload = json.loads(result[-1].text)
                self.assertEqual(
                    payload["_mac_mcp_steering"]["messages"][0]["text"],
                    "change course while sync tool is running",
                )

        asyncio.run(run())

    def test_nested_call_does_not_create_second_visible_session_or_active_call(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="nested-test", telemetry=telemetry, steering=steering)
                session = FakeSession()
                self.bind_session(mcp, session)

                @mcp.tool(name="read_file", structured_output=False)
                async def fake_read_file(path: str):
                    await asyncio.sleep(0.12)
                    return {"ok": True, "path": path}

                @mcp.tool(name="tool_invoke", structured_output=False)
                async def fake_tool_invoke(tool_name: str, arguments: dict | None = None):
                    inner = await mcp.call_tool(tool_name, arguments or {})
                    return {"ok": True, "inner_blocks": len(inner)}

                task = asyncio.create_task(
                    mcp.call_tool("tool_invoke", {"tool_name": "read_file", "arguments": {"path": "/tmp/a"}})
                )
                max_sessions = 0
                max_active_calls = 0
                for _ in range(30):
                    rows = steering.sessions()
                    max_sessions = max(max_sessions, len(rows))
                    if rows:
                        max_active_calls = max(max_active_calls, rows[0]["active_calls"])
                    await asyncio.sleep(0.005)
                await task
                self.assertEqual(max_sessions, 1)
                self.assertEqual(max_active_calls, 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
