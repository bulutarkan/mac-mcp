from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.steering import SteeringManager, attach_steering, describe_target


class SteeringManagerTests(unittest.TestCase):
    def test_labels_hide_event_ids_and_surface_context(self) -> None:
        label, detail = describe_target("browser_do", {"browser": "Safari", "url": "https://www.booking.com/hotel/tr/example.html"})
        self.assertEqual(label, "Safari · booking.com")
        self.assertEqual(detail, "browser_do")

        label, _ = describe_target("read_file", {"path": "/Users/test/Projects/mac-mcp/mcp_server/main.py"})
        self.assertIn("mcp_server/main.py", label)

    def test_attach_mirrors_steering_into_wrapped_structured_result(self) -> None:
        from mcp.types import TextContent

        original_content = [TextContent(type="text", text='{"ok":true}')]
        structured = {"result": {"ok": True, "value": 42}}
        result = attach_steering(
            (original_content, structured),
            [{"id": "st_wrapped", "text": "connector-visible", "created_at": 3.0}],
        )
        self.assertEqual(result[1]["result"]["ok"], True)
        steering = result[1]["result"]["_mac_mcp_steering"]
        self.assertEqual(steering["messages"][0]["text"], "connector-visible")

    def test_close_is_race_safe_for_late_messages(self) -> None:
        manager = SteeringManager()
        manager.open_target("evt_test", tool="read_file", arguments={"path": "/tmp/x"})
        queued = manager.enqueue("evt_test", "change direction")
        delivered = manager.close_target("evt_test", delivered=True)
        self.assertEqual(delivered[0]["id"], queued["id"])
        self.assertEqual(manager.recent()[0]["status"], "delivered")
        with self.assertRaises(KeyError):
            manager.enqueue("evt_test", "too late")

    def test_attach_preserves_original_content(self) -> None:
        original = {"ok": True, "value": 42}
        result = attach_steering(original, [{"id": "st_1", "text": "hello", "created_at": 1.0}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["value"], 42)
        self.assertEqual(result["_mac_mcp_steering"]["messages"][0]["text"], "hello")

    def test_attach_preserves_fastmcp_structured_output_tuple(self) -> None:
        from mcp.types import TextContent

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
    def test_live_message_reaches_only_top_level_response_and_not_telemetry(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="steering-test", telemetry=telemetry, steering=steering)

                @mcp.tool(name="read_file", structured_output=False)
                async def fake_read_file(path: str):
                    await asyncio.sleep(0.12)
                    return {"ok": True, "path": path, "content": "original"}

                task = asyncio.create_task(mcp.call_tool("read_file", {"path": "/tmp/example.txt"}))
                for _ in range(30):
                    targets = steering.active_targets()
                    if targets:
                        break
                    await asyncio.sleep(0.005)
                self.assertEqual(len(targets), 1)
                event_id = targets[0]["event_id"]
                steering.enqueue(event_id, "vazgeçtim, başka dosyaya bak")
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
                self.assertEqual(steering.active_targets(), [])

        asyncio.run(run())

    def test_sync_tool_keeps_event_loop_responsive_for_live_steering(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="sync-steering-test", telemetry=telemetry, steering=steering)

                @mcp.tool(name="read_file", structured_output=False)
                def fake_read_file(path: str):
                    time.sleep(0.16)
                    return {"ok": True, "path": path}

                task = asyncio.create_task(mcp.call_tool("read_file", {"path": "/tmp/sync.txt"}))
                await asyncio.sleep(0.035)
                targets = steering.active_targets()
                self.assertEqual(len(targets), 1, "sync tool blocked the event loop; steering target vanished before UI could act")
                steering.enqueue(targets[0]["event_id"], "change course while sync tool is running")
                result = await task
                payload = json.loads(result[-1].text)
                self.assertEqual(
                    payload["_mac_mcp_steering"]["messages"][0]["text"],
                    "change course while sync tool is running",
                )

        asyncio.run(run())

    def test_nested_call_does_not_create_second_visible_flow(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                steering = SteeringManager()
                mcp = ObservedFastMCP(name="nested-test", telemetry=telemetry, steering=steering)

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
                max_visible = 0
                for _ in range(30):
                    max_visible = max(max_visible, len(steering.active_targets()))
                    await asyncio.sleep(0.005)
                await task
                self.assertEqual(max_visible, 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
