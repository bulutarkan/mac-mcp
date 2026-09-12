from __future__ import annotations

import asyncio
import gc
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent

from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.steering import (
    SteeringIdentity,
    SteeringManager,
    attach_steering,
    describe_target,
    preemption_error,
    steering_identity_from_context,
)


class FakeSession:
    pass


class FakeMeta:
    def __init__(self, **extras):
        self.model_extra = dict(extras)

    def model_dump(self, **_kwargs):
        return dict(self.model_extra)


def fake_context(
    session: FakeSession,
    *,
    openai_session: str | None = None,
    openai_subject: str = "subject-a",
    client_id: str | None = None,
):
    extras = {}
    if openai_session is not None:
        extras["openai/session"] = openai_session
        extras["openai/subject"] = openai_subject
    return SimpleNamespace(
        session=session,
        client_id=client_id,
        request_context=SimpleNamespace(meta=FakeMeta(**extras)),
    )


def transport_identity(session: FakeSession) -> SteeringIdentity:
    return SteeringIdentity(
        key=f"transport:{id(session)}",
        source="transport",
        transport_session=session,
    )


class SteeringIdentityTests(unittest.TestCase):
    def test_openai_conversation_identity_survives_transport_changes(self) -> None:
        a = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-1"))
        b = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-1"))
        c = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-2"))
        self.assertEqual(a.key, b.key)
        self.assertEqual(a.source, "openai_session")
        self.assertNotEqual(a.key, c.key)
        self.assertNotIn("conversation-1", a.key)

    def test_openai_session_beats_generic_client_id(self) -> None:
        a = steering_identity_from_context(
            fake_context(FakeSession(), openai_session="conversation-a", client_id="shared-client")
        )
        b = steering_identity_from_context(
            fake_context(FakeSession(), openai_session="conversation-b", client_id="shared-client")
        )
        self.assertNotEqual(a.key, b.key)
        self.assertEqual(a.source, "openai_session")

    def test_generic_client_id_and_transport_fallbacks(self) -> None:
        a = steering_identity_from_context(fake_context(FakeSession(), client_id="client-a"))
        b = steering_identity_from_context(fake_context(FakeSession(), client_id="client-a"))
        self.assertEqual(a.key, b.key)
        self.assertEqual(a.source, "client_id")

        session = FakeSession()
        transport = steering_identity_from_context(fake_context(session))
        self.assertEqual(transport.source, "transport")
        self.assertIs(transport.transport_session, session)


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

    def test_logical_identity_merges_fresh_transports_and_isolates_other_conversation(self) -> None:
        manager = SteeringManager()
        a1 = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-a"))
        a2 = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-a"))
        b = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-b"))
        a_id = manager.session_id_for(a1)
        self.assertEqual(a_id, manager.session_id_for(a2))
        b_id = manager.session_id_for(b)
        self.assertNotEqual(a_id, b_id)
        self.assertEqual(len(manager.sessions()), 2)

        manager.enqueue(a_id, "only A should see this")
        self.assertEqual(manager.prepare_call(b, tool="read_file", arguments={}), [])
        pending = manager.prepare_call(a2, tool="read_file", arguments={})
        self.assertEqual(pending[0]["text"], "only A should see this")
        self.assertEqual(manager.recent()[0]["status"], "preempted")

    def test_versioned_lifecycle_queued_delivered_acknowledged(self) -> None:
        manager = SteeringManager()
        identity = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-lifecycle"))
        manager.prepare_call(identity, tool="read_file", arguments={"path": "/tmp/a"})
        initial = manager.sessions()[0]
        self.assertEqual(1, initial["schema_version"])
        self.assertEqual("idle", initial["activity_state"])
        self.assertEqual("ready", initial["lifecycle_state"])
        self.assertEqual(0, initial["pending_instruction_count"])

        sid = initial["session_id"]
        manager.begin_call(identity, "evt_live", tool="read_file", arguments={"path": "/tmp/a"})
        message = manager.enqueue(sid, "change direction")
        queued = manager.sessions()[0]
        self.assertEqual("working", queued["activity_state"])
        self.assertEqual("queued", queued["lifecycle_state"])
        self.assertEqual(1, queued["pending_instruction_count"])
        self.assertEqual("queued", message["lifecycle_state"])

        delivered = manager.finish_call(identity, "evt_live", delivered=True)
        self.assertEqual(message["id"], delivered[0]["id"])
        snapshot = manager.sessions()[0]
        self.assertEqual("idle", snapshot["activity_state"])
        self.assertEqual("delivered", snapshot["lifecycle_state"])
        self.assertEqual(0, snapshot["pending_instruction_count"])
        self.assertEqual(1, snapshot["awaiting_acknowledgement_count"])

        manager.prepare_call(identity, tool="read_file", arguments={"path": "/tmp/b"})
        acknowledged = manager.sessions()[0]
        self.assertEqual("acknowledged", acknowledged["lifecycle_state"])
        self.assertEqual(0, acknowledged["awaiting_acknowledgement_count"])
        history = [row for row in manager.recent() if row["id"] == message["id"]]
        self.assertEqual(["acknowledged", "delivered", "queued"], [row["lifecycle_state"] for row in history[:3]])

    def test_failed_delivery_is_explicit_and_retries_on_next_call(self) -> None:
        manager = SteeringManager()
        identity = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-failure"))
        manager.prepare_call(identity, tool="run_command", arguments={"command": "false"})
        sid = manager.session_id_for(identity)
        manager.begin_call(identity, "evt_fail", tool="run_command", arguments={"command": "false"})
        message = manager.enqueue(sid, "do something else")
        self.assertEqual([], manager.finish_call(identity, "evt_fail", delivered=False))
        failed = manager.sessions()[0]
        self.assertEqual("failed", failed["lifecycle_state"])
        self.assertEqual("tool_failed_before_steering_delivery", failed["last_error"])
        self.assertEqual(1, failed["pending_instruction_count"])

        pending = manager.prepare_call(identity, tool="read_file", arguments={"path": "/tmp/retry"})
        self.assertEqual(message["id"], pending[0]["id"])
        retried = manager.sessions()[0]
        self.assertEqual("delivered", retried["lifecycle_state"])
        self.assertIsNone(retried["last_error"])

    def test_illegal_lifecycle_transition_is_rejected(self) -> None:
        manager = SteeringManager()
        identity = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-illegal"))
        manager.session_id_for(identity)
        with manager._lock:
            state = manager._sessions[identity.key]
            with self.assertRaisesRegex(RuntimeError, "illegal_steering_transition:ready->acknowledged"):
                manager._transition_locked(state, "acknowledged")

    def test_ttl_expiry_emits_explicit_terminal_session_event(self) -> None:
        manager = SteeringManager(session_ttl_s=60)
        identity = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-expired"))
        manager.session_id_for(identity)
        with manager._lock:
            manager._sessions[identity.key]["last_activity_at"] -= 61
        self.assertEqual([], manager.sessions())
        event = manager.recent()[0]
        self.assertEqual("session", event["kind"])
        self.assertEqual("session_expired", event["status"])
        self.assertEqual("expired", event["lifecycle_state"])
        self.assertEqual(1, event["schema_version"])

    def test_active_delivery_keeps_logical_session_idle_after_call(self) -> None:
        manager = SteeringManager()
        identity = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-a"))
        manager.prepare_call(identity, tool="read_file", arguments={"path": "/tmp/x"})
        sid = manager.session_id_for(identity)
        manager.begin_call(identity, "evt_test", tool="read_file", arguments={"path": "/tmp/x"})
        self.assertEqual(manager.sessions()[0]["state"], "working")
        queued = manager.enqueue(sid, "change direction")
        delivered = manager.finish_call(identity, "evt_test", delivered=True)
        self.assertEqual(delivered[0]["id"], queued["id"])
        self.assertEqual(manager.sessions()[0]["state"], "idle")
        self.assertEqual(manager.sessions()[0]["queued"], 0)

    def test_failed_tool_preserves_pending_for_next_preemption(self) -> None:
        manager = SteeringManager()
        identity = steering_identity_from_context(fake_context(FakeSession(), openai_session="conversation-a"))
        manager.prepare_call(identity, tool="run_command", arguments={"command": "false"})
        sid = manager.session_id_for(identity)
        manager.begin_call(identity, "evt_fail", tool="run_command", arguments={"command": "false"})
        manager.enqueue(sid, "do something else")
        self.assertEqual(manager.finish_call(identity, "evt_fail", delivered=False), [])
        self.assertEqual(manager.sessions()[0]["queued"], 1)
        pending = manager.prepare_call(identity, tool="read_file", arguments={"path": "/tmp/a"})
        self.assertEqual(pending[0]["text"], "do something else")

    def test_transport_fallback_cleans_up_when_session_dies(self) -> None:
        manager = SteeringManager()
        session = FakeSession()
        identity = transport_identity(session)
        sid = manager.session_id_for(identity)
        manager.enqueue(sid, "pending")
        del identity
        del session
        gc.collect()
        self.assertEqual(manager.sessions(), [])
        terminal = manager.recent()[0]
        self.assertEqual(terminal["status"], "session_ended")
        self.assertEqual(terminal["kind"], "session")
        self.assertEqual(terminal["lifecycle_state"], "disconnected")
        self.assertEqual(terminal["schema_version"], 1)
        with self.assertRaises(KeyError):
            manager.enqueue(sid, "too late")


    def test_session_ttl_uses_persisted_minutes_and_rejects_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({"steering": {"session_ttl_minutes": 120}}))
            with patch.dict(
                os.environ,
                {"MAC_MCP_SETTINGS_PATH": str(path), "MAC_MCP_STEERING_SESSION_TTL_S": ""},
            ):
                manager = SteeringManager()
        self.assertEqual(manager.session_ttl_minutes, 120)
        self.assertEqual(manager.set_session_ttl_minutes(1), 1)
        self.assertEqual(manager.session_ttl_minutes, 1)
        with self.assertRaises(ValueError):
            manager.set_session_ttl_minutes(0)

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

    def test_attach_preserves_original_content_and_tuple(self) -> None:
        original = {"ok": True, "value": 42}
        result = attach_steering(original, [{"id": "st_1", "text": "hello", "created_at": 1.0}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["_mac_mcp_steering"]["messages"][0]["text"], "hello")

        original_content = [TextContent(type="text", text='{"ok":true}')]
        structured = {"ok": True, "value": 42}
        tuple_result = attach_steering(
            (original_content, structured),
            [{"id": "st_tuple", "text": "new direction", "created_at": 2.0}],
        )
        self.assertIsInstance(tuple_result, tuple)
        self.assertEqual(tuple_result[1]["ok"], True)
        self.assertEqual(len(tuple_result[0]), 2)


class ObservedFastMCPSteeringTests(unittest.TestCase):
    @staticmethod
    def bind_context(
        mcp: ObservedFastMCP,
        session: FakeSession,
        *,
        openai_session: str = "conversation-test",
    ) -> None:
        context = fake_context(session, openai_session=openai_session)
        mcp.get_context = lambda: context  # type: ignore[method-assign]

    def test_live_message_reaches_response_and_not_telemetry(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="steering-test", telemetry=telemetry, steering=steering)
                self.bind_context(mcp, FakeSession())

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
                sid = sessions[0]["session_id"]
                steering.enqueue(sid, "vazgeçtim, başka dosyaya bak")
                result = await task
                payload = json.loads(result[-1].text)
                self.assertEqual(
                    payload["_mac_mcp_steering"]["messages"][0]["text"],
                    "vazgeçtim, başka dosyaya bak",
                )
                events = telemetry.query_events(limit=10)
                self.assertNotIn("_mac_mcp_steering", json.dumps(events[0].get("result"), ensure_ascii=False))
                self.assertEqual(steering.sessions()[0]["state"], "idle")

        asyncio.run(run())

    def test_fresh_transport_same_conversation_preempts_before_execution(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="preempt-test", telemetry=telemetry, steering=steering)
                executions = 0

                @mcp.tool(name="read_file")
                def fake_read_file(path: str):
                    nonlocal executions
                    executions += 1
                    return {"ok": True, "path": path, "executions": executions}

                self.bind_context(mcp, FakeSession(), openai_session="conversation-a")
                await mcp.call_tool("read_file", {"path": "/tmp/first"})
                self.assertEqual(executions, 1)
                sid = steering.sessions()[0]["session_id"]
                steering.enqueue(sid, "do not run the next tool")

                # Simulate ChatGPT opening a brand-new transport session for the
                # next call while keeping the same conversation metadata.
                self.bind_context(mcp, FakeSession(), openai_session="conversation-a")
                with self.assertRaises(ToolError) as caught:
                    await mcp.call_tool("read_file", {"path": "/tmp/second"})
                self.assertEqual(executions, 1)
                self.assertIn("do not run the next tool", str(caught.exception))
                self.assertEqual(len(steering.sessions()), 1)

                self.bind_context(mcp, FakeSession(), openai_session="conversation-a")
                await mcp.call_tool("read_file", {"path": "/tmp/third"})
                self.assertEqual(executions, 2)

        asyncio.run(run())

    def test_sync_tool_keeps_event_loop_responsive_for_live_steering(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="sync-steering-test", telemetry=telemetry, steering=steering)
                self.bind_context(mcp, FakeSession())

                @mcp.tool(name="read_file", structured_output=False)
                def fake_read_file(path: str):
                    time.sleep(0.16)
                    return {"ok": True, "path": path}

                task = asyncio.create_task(mcp.call_tool("read_file", {"path": "/tmp/sync.txt"}))
                await asyncio.sleep(0.035)
                sessions = steering.sessions()
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
                self.bind_context(mcp, FakeSession())

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
