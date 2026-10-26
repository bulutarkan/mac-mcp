"""#123: the menu popup tells connecting from stopped, keeps work it last saw, and says live news.

- Start is offered only when a finished check found the server stopped; while the first check
  runs the popup says it is connecting.
- A failed activity read keeps the last good activity, marked not current; the first read shows
  a loading state.
- The header line uses the same live state as the menu bar icon (MenuLiveStatus): a waiting
  question, agents or a conversation at work; idle says just "Ready".
- Counts are not repeated, extra active teams show as "+N", one scroll region, the failed
  filter is its own control, disclosures tell VoiceOver expanded/collapsed, ⌘R refreshes, and
  the conversation retention setting lives only in Settings.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests._menu_sources import feature_files, feature_source

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"

STATUS_HARNESS = r'''
@main
struct MenuLiveStatusHarness {
    static func main() {
        // The app just opened: nothing has answered yet. Not stopped, so no Start.
        let launch = MenuLiveStatus(serverAnswered: false, checking: true, startingAfterLogin: false)
        precondition(launch.server == .checking && launch.subtitle == "Connecting…", launch.subtitle)
        precondition(launch.iconLook == .stopped, "the icon stays dimmed until Mac MCP answers")
        let login = MenuLiveStatus(serverAnswered: false, checking: true, startingAfterLogin: true)
        precondition(login.server == .starting && login.subtitle == "Starting…")
        // A finished check found nothing answering: the only stopped state.
        let stopped = MenuLiveStatus(serverAnswered: false, checking: false, startingAfterLogin: false)
        precondition(stopped.server == .stopped && stopped.subtitle == "Not running")
        // A refresh of a running server is still running.
        precondition(MenuLiveStatus(serverAnswered: true, checking: true, startingAfterLogin: false).server == .running)

        // Idle says only "Ready".
        let idle = MenuLiveStatus(serverAnswered: true, checking: false, startingAfterLogin: false)
        precondition(idle.subtitle == "Ready" && idle.iconLook == .idle)

        // A waiting question is the news, ahead of work.
        let asking = MenuLiveStatus(serverAnswered: true, checking: false, startingAfterLogin: false,
                                    questionsWaiting: 1, askedBy: "Claude Code", agentsWorking: 2)
        precondition(asking.subtitle == "Claude Code is asking you something", asking.subtitle)
        precondition(asking.iconLook == .needsYou)
        let unnamed = MenuLiveStatus(serverAnswered: true, checking: false, startingAfterLogin: false,
                                     questionsWaiting: 1, askedBy: "  ")
        precondition(unnamed.subtitle == "An agent is asking you something", unnamed.subtitle)
        let several = MenuLiveStatus(serverAnswered: true, checking: false, startingAfterLogin: false, questionsWaiting: 3)
        precondition(several.subtitle == "3 questions are waiting for you")

        // Work: delegated agents and an active AI app conversation, as the icon counts them.
        let agents = MenuLiveStatus(serverAnswered: true, checking: false, startingAfterLogin: false, agentsWorking: 1)
        precondition(agents.subtitle == "1 agent working" && agents.iconLook == .working)
        let chat = MenuLiveStatus(serverAnswered: true, checking: false, startingAfterLogin: false, conversationsActive: 1)
        precondition(chat.subtitle == "1 conversation active" && chat.iconLook == .working, chat.subtitle)
        let both = MenuLiveStatus(serverAnswered: true, checking: false, startingAfterLogin: false,
                                  agentsWorking: 2, conversationsActive: 3)
        precondition(both.subtitle == "2 agents working · 3 conversations active", both.subtitle)

        // Paused shows in the icon (and the header pill); the line still carries the work.
        let paused = MenuLiveStatus(serverAnswered: true, checking: false, startingAfterLogin: false,
                                    agentsWorking: 1, paused: true)
        precondition(paused.iconLook == .paused && paused.subtitle == "1 agent working")
        // Nothing live is claimed while the server is not answering.
        let downWithStale = MenuLiveStatus(serverAnswered: false, checking: false, startingAfterLogin: false,
                                           questionsWaiting: 1, agentsWorking: 2)
        precondition(downWithStale.iconLook == .stopped && downWithStale.subtitle == "Not running")
        print("MENU_LIVE_STATUS_PASS")
    }
}
'''

ACTIVITY_HARNESS = r'''
import Foundation

@main
struct ActivityStaleHarness {
    @MainActor static func main() async {
        let env = ProcessInfo.processInfo.environment
        let port = env["FAKE_PORT"]!
        func mode(_ name: String) async {
            _ = try? await URLSession.shared.data(from: URL(string: "http://127.0.0.1:\(port)/__mode/\(name)")!)
        }
        let app = AppState(startBackgroundTasks: false)
        precondition(app.settings.serverPort == Int(port)!, "settings came from the test file")

        // Before the first answer: connecting, not stopped, and the activity is loading.
        precondition(app.connectionState == .connecting)
        precondition(app.liveStatus(questionsWaiting: 0, askedBy: nil).server == .checking)
        precondition(!app.activityLoaded && !app.activityStale)

        await mode("ok")
        await app.refresh()
        precondition(app.connectionState == .connected, "\(app.connectionState) \(app.connectionIssue ?? "")")
        precondition(app.activityLoaded && !app.activityStale)
        precondition(app.activeEvents.map(\.id) == ["ev-live"])
        precondition(app.activeBrowserEvents.count == 1)

        // The activity endpoint fails: the browser work it showed stays, marked not current.
        await mode("events_fail")
        await app.refresh()
        precondition(app.activityStale)
        precondition(app.activeEvents.map(\.id) == ["ev-live"], "an activity error must not hide active work")
        precondition(app.activeBrowserEvents.count == 1)
        precondition(app.recentEvents.map(\.id) == ["ev-live"])
        precondition(app.serverRunning && app.connectionState == .degraded)
        precondition(app.liveStatus(questionsWaiting: 0, askedBy: nil).server == .running)

        // The server answers with an error: the same.
        await mode("summary_fail")
        await app.refresh()
        precondition(app.activityStale && app.activeEvents.map(\.id) == ["ev-live"])
        precondition(app.connectionState == .degraded)

        // A good read clears the mark.
        await mode("ok")
        await app.refresh()
        precondition(!app.activityStale && app.connectionState == .connected)

        // Nothing answers at all: the server is verified stopped and nothing is shown as live.
        app.settings.serverPort = Int(env["DEAD_PORT"]!)!
        await app.refresh()
        precondition(app.connectionState == .disconnected && !app.serverRunning)
        precondition(app.liveStatus(questionsWaiting: 0, askedBy: nil).server == .stopped)
        precondition(app.activeEvents.isEmpty && app.activityStale)
        print("ACTIVITY_STALE_PASS")
    }
}
'''

SEMANTICS = {
    "active_profile": "trusted", "configured_profile": "trusted", "profile_was_normalized": False,
    "normalized_from_profile": "trusted", "known_profile": True, "capability_enforcement": "server",
    "approval_contract": "separate", "ask_confirmation_is_automatic_gate": False,
    "supported_approval_sources": ["none"],
    "profiles": [{"name": "trusted", "active": True, "capability_enforcement": "server",
                  "allowed_capabilities": ["read"], "denied_capabilities": [], "destructive_families": [],
                  "access_mode_ceiling": "full",
                  "approval_behavior": {"source": "none", "automatic_confirmation": False, "summary": "s"}}],
}


class _FakeDashboard(BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path.startswith("/__mode/"):
            type(self).mode = path.rsplit("/", 1)[1]
            return self._send(200, {"ok": True})
        mode = type(self).mode
        event = {"event_id": "ev-live", "timestamp": time.time(), "source": "mcp", "tool": "browser_act",
                 "status": "running", "duration_ms": 0,
                 "browser_context": {"browser": "chrome", "tab_handle": "t1", "site": "example.test", "action": "click"}}
        if path == "/dashboard/api/summary":
            if mode == "summary_fail":
                return self._send(500, {"error": "boom"})
            return self._send(200, {"version": "test", "total_calls": 1, "success_rate": 100.0, "active_agents": 0})
        if path == "/dashboard/api/events":
            if mode == "events_fail":
                return self._send(500, {"error": "boom"})
            return self._send(200, {"events": [event], "active": [event]})
        if path == "/dashboard/api/agents":
            return self._send(200, {"agents": [], "teams": []})
        if path == "/dashboard/api/steering":
            return self._send(200, {"schema_version": 1, "sessions": [], "recent": []})
        if path == "/dashboard/api/security/semantics":
            return self._send(200, SEMANTICS)
        return self._send(404, {"error": "not found"})


def _swift_build(sources: list, harness: str, out_dir: Path, name: str) -> tuple:
    source = out_dir / f"{name}.swift"
    binary = out_dir / name
    source.write_text(textwrap.dedent(harness), encoding="utf-8")
    cmd = ["xcrun", "swiftc", "-parse-as-library", *map(str, sources), str(source),
           "-framework", "SwiftUI", "-framework", "AppKit", "-framework", "Security",
           "-framework", "UserNotifications", "-framework", "Combine", "-o", str(binary)]
    built = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=240)
    return built, binary


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class MenuLiveStatusModelTests(unittest.TestCase):
    def test_connecting_is_not_stopped_and_the_line_carries_only_news(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            built, binary = _swift_build([SOURCES / "MenuLiveStatus.swift"], STATUS_HARNESS, Path(td), "live-status")
            self.assertEqual(0, built.returncode, built.stderr[-3000:])
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(0, ran.returncode, ran.stderr[-2000:])
            self.assertIn("MENU_LIVE_STATUS_PASS", ran.stdout)


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class ActivityKeepsLastGoodTests(unittest.TestCase):
    def test_an_api_error_keeps_active_work_marked_stale(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDashboard)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()  # nothing listens here: connection refused
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                settings = tmp / "settings.json"
                settings.write_text(json.dumps({"server": {"port": server.server_address[1]}}), encoding="utf-8")
                sources = [*feature_files("AppState"), SOURCES / "AgentNotificationController.swift",
                           SOURCES / "SettingsStore.swift", SOURCES / "ToolActivityBubbleController.swift",
                           SOURCES / "KeychainStore.swift"]
                built, binary = _swift_build(sources, ACTIVITY_HARNESS, tmp, "activity-stale")
                self.assertEqual(0, built.returncode, built.stderr[-3000:])
                env = {
                    **os.environ,
                    "FAKE_PORT": str(server.server_address[1]),
                    "DEAD_PORT": str(dead_port),
                    "MAC_MCP_SETTINGS_PATH": str(settings),
                    "MAC_MCP_STATE_DIR": str(tmp / "state"),
                    "MAC_MCP_DASHBOARD_TOKEN_FILE": str(tmp / "state" / "dashboard-token"),
                    "MAC_MCP_PENDING_STEERING_STATE_FILE": str(tmp / "state" / "pending-steering.json"),
                    "MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE": "com.bulutarkan.mac-mcp.popup-states-test",
                    "MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT": "test",
                    "MAC_MCP_DECISIONS_KEYCHAIN_SERVICE": "com.bulutarkan.mac-mcp.harness-test",
                    "MAC_MCP_DECISIONS_KEYCHAIN_ACCOUNT": "test",
                }
                ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=env)
                self.assertEqual(0, ran.returncode, ran.stderr[-3000:])
                self.assertIn("ACTIVITY_STALE_PASS", ran.stdout)
        finally:
            server.shutdown()
            server.server_close()


class PopupWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.menu = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        cls.app = (SOURCES / "MacMCPMenuApp.swift").read_text(encoding="utf-8")
        cls.state = feature_source("AppState")
        cls.settings = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")

    def section(self, start: str, end: str) -> str:
        begin = self.menu.index(start)
        return self.menu[begin:self.menu.index(end, begin)]

    def test_start_only_for_a_verified_stop_and_icon_and_header_share_one_state(self) -> None:
        card = self.section("private var connectionCard", "private var setupCard")
        self.assertIn("let stopped = liveStatus.server == .stopped", card)
        self.assertIn("private var headerSubtitle: String { liveStatus.subtitle }", self.menu)
        self.assertIn("state.liveStatus(questionsWaiting: questionsWaiting, askedBy: questionAskedBy)", self.menu)
        self.assertIn("state.liveStatus(questionsWaiting: prompts.queue.count", self.app)
        self.assertIn("switch live.iconLook", self.app)
        self.assertIn("prompts: OwnerPromptController.shared.model", self.app)
        # The popup reads the queue without re-rendering on the question window's countdown.
        self.assertIn(".onReceive(prompts.$queue)", self.menu)
        self.assertNotIn("@ObservedObject var prompts", self.menu)

    def test_activity_error_keeps_the_last_good_list(self) -> None:
        refresh = self.state[self.state.index("func refresh(background: Bool = false) async"):
                             self.state.index("private func settingsFailureState")]
        events = refresh[refresh.index("if let eventsEnvelope = try await eventsFetch"):refresh.index("let agentsEnvelope")]
        self.assertNotIn("setIfChanged(\\.activeEvents, [])", events)
        self.assertIn("setIfChanged(\\.activityStale, true)", events)
        self.assertIn("if state == .disconnected { setIfChanged(\\.activeEvents, []) }", refresh)
        card = self.section("private var activityCard", "private func activityEmptyText")
        self.assertIn('"Showing the last known activity · not current"', card)
        self.assertIn('Text("Loading activity…")', card)
        self.assertLess(card.index('Text("Loading activity…")'), card.index('"No tool calls recorded in the last hour."'))

    def test_counts_once_and_extra_teams_counted(self) -> None:
        chips = self.section("private var chipsRow", "private var successTint")
        self.assertNotIn('"cpu"', chips)
        self.assertNotIn('"rectangle.stack"', chips)
        card = self.section("private var agentCard", "private func compactAgentRow")
        self.assertIn("activeTeams.prefix(Self.teamRowLimit)", card)
        self.assertIn("moreTeamsRow(activeTeams.count - Self.teamRowLimit)", card)
        self.assertIn('Text("+\\(extra)")', self.menu)

    def test_one_scroll_region_and_accessible_disclosures(self) -> None:
        self.assertEqual(1, len(re.findall(r"\bScrollView\s*[({]", self.menu)))
        style = self.menu[self.menu.index("private struct MenuDisclosureStyle"):self.menu.index("private struct MenuChip")]
        self.assertIn('.accessibilityValue(configuration.isExpanded ? "Expanded" : "Collapsed")', style)
        self.assertIn("accessory", style)
        agents = self.section("private var agentCard", "private func compactAgentRow")
        self.assertIn('.accessibilityValue(showAllAgents ? "Expanded" : "Collapsed")', agents)
        # The failed filter is handed to the style as its own control, outside the toggle's label.
        card = self.section("private var activityCard", "private func activityEmptyText")
        label = card[card.index("} label: {"):]
        self.assertNotIn("showFailedOnly.toggle()", label)
        self.assertIn(".disclosureGroupStyle(MenuDisclosureStyle(accessory: failedFilter))", card)

    def test_refresh_shortcut_and_retention_only_in_settings(self) -> None:
        self.assertIn('.keyboardShortcut("r", modifiers: .command)', self.menu)
        self.assertNotIn("Keep idle", self.menu)
        self.assertNotIn("sessionMinutesText", self.menu)
        self.assertNotIn("updateSteeringRetention", self.menu)
        # Settings' control now reaches the running server too, so "applies live" is true.
        apply = self.settings[self.settings.index("private func applySessionRetention"):]
        apply = apply[:apply.index("\n    }\n")]
        self.assertIn("await state.updateSteeringRetention(minutes: minutes)", apply)
        self.assertIn('"Saved · applies when Mac MCP next starts"', apply)


if __name__ == "__main__":
    unittest.main()
