from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from mcp_server import dashboard_routes
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
DASHBOARD_TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"

HARNESS = r'''
import Foundation

func event(_ id: String, _ ts: Double, _ status: String = "success") -> [String: Any] {
    ["event_id": id, "timestamp": ts, "source": "mcp", "tool": "read_file", "status": status,
     "duration_ms": 3, "browser_context": ["browser": "Safari", "site": "example.com", "action": "Reading"]]
}

@main
struct ActivityStreamHarness {
    @MainActor
    static func main() {
        // Poll only until the stream is connected and seeded on that connection.
        precondition(AppState.shouldPollActivityEvents(streamConnected: false, seeded: false))
        precondition(AppState.shouldPollActivityEvents(streamConnected: false, seeded: true))
        precondition(AppState.shouldPollActivityEvents(streamConnected: true, seeded: false))
        precondition(!AppState.shouldPollActivityEvents(streamConnected: true, seeded: true))

        let now = 1_800_000_000.0
        let started = AppState.toolEvent(from: event("evt_a", now, "running"))!
        precondition(started.browserContext != nil, "stream events keep browser_context")
        var lists = AppState.applyActivityStreamEvent(kind: "call_started", event: started, active: [], recent: [], now: now)
        precondition(lists.active.map(\.eventID) == ["evt_a"] && lists.recent.isEmpty)

        let finished = AppState.toolEvent(from: event("evt_a", now + 1))!
        lists = AppState.applyActivityStreamEvent(kind: "call_finished", event: finished, active: lists.active, recent: lists.recent, now: now + 1)
        precondition(lists.active.isEmpty && lists.recent.map(\.eventID) == ["evt_a"])

        // A repeated finish does not duplicate; newest first; bounded and last hour only.
        lists = AppState.applyActivityStreamEvent(kind: "call_finished", event: finished, active: lists.active, recent: lists.recent, now: now + 1)
        precondition(lists.recent.count == 1)
        let old = AppState.toolEvent(from: event("evt_old", now - 4000))!
        var recent = [old]
        for index in 0..<25 {
            let item = AppState.toolEvent(from: event("evt_\(index)", now + Double(index)))!
            recent = AppState.applyActivityStreamEvent(kind: "call_finished", event: item, active: [], recent: recent, now: now + Double(index)).recent
        }
        precondition(recent.count == AppState.recentActivityLimit)
        precondition(recent.first?.eventID == "evt_24")
        precondition(!recent.contains { $0.eventID == "evt_old" })

        // Unknown kinds and undecodable payloads leave the lists alone.
        let same = AppState.applyActivityStreamEvent(kind: "keepalive", event: finished, active: [started], recent: recent, now: now)
        precondition(same.active.map(\.eventID) == ["evt_a"] && same.recent.count == recent.count)
        precondition(AppState.toolEvent(from: ["event_id": "x"]) == nil)
        print("ACTIVITY_STREAM_PASS")
    }
}
'''


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class MenuActivityStreamHarnessTests(unittest.TestCase):
    def test_stream_keeps_activity_lists_and_polling_stops_only_when_seeded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "ActivityStreamHarness.swift"
            binary = Path(td) / "activity-stream-harness"
            source.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            cmd = [
                "xcrun", "swiftc", "-parse-as-library",
                str(SOURCES / "AppState.swift"), str(SOURCES / "AgentNotificationController.swift"),
                str(SOURCES / "SettingsStore.swift"), str(SOURCES / "ToolActivityBubbleController.swift"),
                str(SOURCES / "KeychainStore.swift"), str(source),
                "-framework", "SwiftUI", "-framework", "AppKit", "-framework", "Security",
                "-framework", "UserNotifications", "-framework", "Combine",
                "-o", str(binary),
            ]
            built = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertIn("ACTIVITY_STREAM_PASS", run.stdout)

    def test_refresh_skips_events_fetch_only_through_the_poll_gate(self) -> None:
        source = (SOURCES / "AppState.swift").read_text(encoding="utf-8")
        refresh = source[source.index("func refresh() async"):source.index("private func fetchActivityEvents")]
        self.assertNotIn('fetch(base.appendingPathComponent("dashboard/api/events")', refresh)
        self.assertIn("fetchActivityEvents(base: base, poll: pollEvents)", refresh)
        stream = source[source.index("private func consumeToolActivityStream()"):source.index("private func handleToolActivityPayload")]
        self.assertIn("streamActivitySeeded = false", stream)
        self.assertGreaterEqual(stream.count("toolActivityStreamConnected = false"), 2)


class DashboardStreamContextTests(unittest.TestCase):
    def test_stream_events_carry_browser_context_like_the_events_api(self) -> None:
        async def run() -> list[dict]:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                routes = create_dashboard_routes(telemetry, load_settings(), DASHBOARD_TOKEN)
                endpoint = next(route.endpoint for route in routes if getattr(route, "path", "") == "/dashboard/events")

                async def receive():
                    await asyncio.sleep(3600)

                request = Request({
                    "type": "http", "method": "GET", "path": "/dashboard/events", "query_string": b"",
                    "headers": [(b"authorization", f"Bearer {DASHBOARD_TOKEN}".encode())],
                    "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765), "scheme": "http",
                }, receive)
                with patch.object(dashboard_routes, "browser_event_context",
                                  side_effect=lambda event: {"browser": "Safari", "site": "example.com"}
                                  if event.get("tool") == "browser_act" else None):
                    response = await endpoint(request)
                    chunks = response.body_iterator
                    hello = await chunks.__anext__()
                    event_id = telemetry.start_call("mcp", "browser_act", {"browser": "Safari"})
                    started = await asyncio.wait_for(chunks.__anext__(), 5)
                    telemetry.finish_call(event_id, result={"ok": True})
                    finished = await asyncio.wait_for(chunks.__anext__(), 5)
                    await chunks.aclose()
                return [json.loads(chunk.split("data: ", 1)[1]) for chunk in (hello, started, finished)]

        hello, started, finished = asyncio.run(run())
        self.assertEqual("connected", hello["kind"])
        self.assertEqual("call_started", started["kind"])
        self.assertEqual({"browser": "Safari", "site": "example.com"}, started["browser_context"])
        self.assertEqual("call_finished", finished["kind"])
        self.assertEqual("example.com", finished["browser_context"]["site"])


if __name__ == "__main__":
    unittest.main()
