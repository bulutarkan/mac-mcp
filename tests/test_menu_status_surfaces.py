"""Menu-bar popover and shared status labels (#61-#65).

The popover keeps normal session history calm; server, endpoint and safety wording
comes from one shared mapping that both the popover and Settings use.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests._menu_sources import feature_files

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"

HARNESS = r'''
import Foundation

func semantics(_ active: String, known: Bool = true, normalized: Bool = false) -> SecuritySemanticsEnvelope {
    let json = #"{"active_profile":"\#(active)","configured_profile":"developer","profile_was_normalized":\#(normalized),"normalized_from_profile":"developer","known_profile":\#(known),"capability_enforcement":"server","approval_contract":"separate","ask_confirmation_is_automatic_gate":false,"supported_approval_sources":["none"],"profiles":[{"name":"trusted","active":true,"capability_enforcement":"server","allowed_capabilities":["read"],"denied_capabilities":[],"destructive_families":[],"access_mode_ceiling":"full","approval_behavior":{"source":"none","automatic_confirmation":false,"summary":"s"}}]}"#
    return try! JSONDecoder().decode(SecuritySemanticsEnvelope.self, from: Data(json.utf8))
}

@main
struct StatusDisplayHarness {
    static func main() {
        let labels = [DashboardConnectionState.connected, .degraded, .disconnected, .connecting].map(\.display.label)
        precondition(labels == ["Connected", "Degraded", "Disconnected", "Connecting"], "\(labels)")
        precondition(DashboardConnectionState.degraded.display.tone == .warning)

        let local = EndpointDisplay.make(mode: "none", cloudflareRunning: false, ngrokRunning: false)
        precondition(local.label == "Local only")
        precondition(EndpointDisplay.make(mode: "cloudflare", cloudflareRunning: true, ngrokRunning: false).label == "Cloudflare · tunnel process detected")
        precondition(EndpointDisplay.make(mode: "cloudflare", cloudflareRunning: false, ngrokRunning: false).label == "Cloudflare · tunnel not detected")
        precondition(EndpointDisplay.make(mode: "ngrok", cloudflareRunning: false, ngrokRunning: true).label == "ngrok · tunnel process detected")
        precondition(EndpointDisplay.make(mode: "ngrok", cloudflareRunning: true, ngrokRunning: false).label == "ngrok · tunnel not detected")
        precondition(EndpointDisplay.make(mode: "custom", cloudflareRunning: true, ngrokRunning: true).label == "Custom URL configured · not checked")
        precondition(EndpointDisplay.needsReview(mode: "cloudflare", cloudflareRunning: false, ngrokRunning: false))
        precondition(!EndpointDisplay.needsReview(mode: "custom", cloudflareRunning: false, ngrokRunning: false))
        for mode in ["none", "cloudflare", "ngrok", "custom"] {
            for running in [true, false] {
                let text = EndpointDisplay.make(mode: mode, cloudflareRunning: running, ngrokRunning: running)
                let words = (text.label + " " + text.explanation).lowercased()
                precondition(!words.contains("online") && !words.contains("reachable"), "no reachability claims: \(words)")
            }
        }

        precondition(SafetyDisplay.make(semantics("trusted"), serverRunning: true).label == "Trusted")
        precondition(SafetyDisplay.make(semantics("trusted"), serverRunning: true).tone == .neutral)
        precondition(SafetyDisplay.make(nil, serverRunning: true).tone == .warning)
        precondition(SafetyDisplay.make(semantics("power_user", known: false), serverRunning: true).tone == .warning)
        precondition(SafetyDisplay.make(semantics("trusted", normalized: true), serverRunning: true).tone == .warning)
        precondition(SafetyDisplay.profileName("read_only") == "Read Only")
        print("STATUS_DISPLAY_PASS")
    }
}
'''


class MenuPopoverStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.menu = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        cls.body = cls.menu[cls.menu.index("    var body: some View {"):cls.menu.index("    private var displayAgents")]

    def test_first_viewport_order_is_health_actions_summary_work(self) -> None:
        # #201: header, callouts, chips, rows (running agents first), work sections, footer.
        order = ["header", "actionBar", "chipsRow", "agentCard", "summaryCard(proxy)", "activityCard",
                 "steeringCard", "footer"]
        positions = [self.body.index(name) for name in order]
        self.assertEqual(sorted(positions), positions)
        self.assertNotIn("attentionBanner", self.body)
        self.assertNotIn("securityCard", self.body)

    def test_scroll_regions_are_the_popover_and_the_agent_list_only(self) -> None:
        # The owner asked for the agent list to scroll inside itself, about five rows tall (#201).
        self.assertEqual(2, len(re.findall(r"\bScrollView\s*[({]", self.menu)))
        card = self.menu[self.menu.index("private var agentCard"):self.menu.index("private func compactAgentRow")]
        self.assertEqual(1, len(re.findall(r"\bScrollView\s*[({]", card)))
        self.assertNotIn("LazyVStack", self.menu)

    def test_header_uses_shared_connection_status_without_session_history_override(self) -> None:
        self.assertIn("state.connectionState.display", self.menu)
        self.assertIn("return connectionDisplay", self.menu)
        self.assertNotIn('label: "Needs attention"', self.menu)
        self.assertNotIn("sessionNeedsAttentionCount", self.menu)

    def test_secondary_actions_live_in_a_labeled_more_menu(self) -> None:
        more = self.menu[self.menu.index("private var moreMenu"):self.menu.index("    // MARK: Summary")]
        for action in ("state.stopServer()", "state.retryConnection()", "state.openDashboard()",
                       "openSettings(.connections)", "openSettings(.permissions)", "state.checkForUpdates()", "state.quitApp()"):
            self.assertIn(action, more)
        self.assertIn('title: "More"', more)
        self.assertIn('Button("Restart Server") { state.restartServer() }', more)
        self.assertIn('.accessibilityLabel("Open Settings")', self.menu)

    def test_summary_rows_route_to_settings_and_never_count_approvals(self) -> None:
        summary = self.menu[self.menu.index("private func summaryCard"):self.menu.index("private var agentsSummaryValue")]
        for title in ('title: "Agents"', 'title: "Endpoint"', 'title: "Safety"'):
            self.assertIn(title, summary)
        # Sessions are a chip and a section of their own (#201), not a fourth summary row.
        self.assertNotIn('title: "Sessions"', summary)
        self.assertIn("openSettings(.connections)", summary)
        self.assertIn("openSettings(.permissions)", summary)
        self.assertIn("SafetyDisplay.make(state.securitySemantics", self.menu)
        self.assertNotIn("pending approval", self.menu.lower())

    def test_agents_show_two_rows_and_expand_inline(self) -> None:
        card = self.menu[self.menu.index("private var agentCard"):self.menu.index("private func compactAgentRow")]
        self.assertIn("activeAgents.prefix(2)", card)
        # The header's chevron opens and closes the list; no "Show fewer" link at its bottom.
        self.assertIn("Button { showAllAgents.toggle() }", card)
        self.assertIn('Image(systemName: "chevron.down")', card)
        self.assertNotIn("Show fewer", card)
        self.assertIn("displayAgents.prefix(Self.agentListLimit)", card)
        self.assertIn("private static let agentListLimit = 20", self.menu)
        self.assertIn(".frame(height: min(agentListHeight, Self.agentListVisibleHeight))", card)

    def test_recent_activity_is_collapsed_with_a_route_to_the_full_list(self) -> None:
        self.assertIn("@MacMCPState private var showActivity = false", self.menu)
        self.assertIn("@MacMCPState private var showSessions = false", self.menu)
        card = self.menu[self.menu.index("private var activityCard"):self.menu.index("private func activityEmptyText")]
        # No running count in the title: it changed with every call and flickered.
        self.assertIn('Label("Recent activity", systemImage: "waveform.path.ecg")', card)
        self.assertIn("Full activity in Dashboard", card)

    def test_one_surface_with_tiles_chips_and_a_small_restart(self) -> None:
        # #201: no boxes in the popover body, a small restart beside the status, chips at a glance.
        self.assertNotIn("GroupBox", self.body)
        header = self.menu[self.menu.index("private var header"):self.menu.index("private var chipsRow")]
        self.assertIn('.accessibilityLabel("Restart Server")', header)
        self.assertNotIn(".frame(maxWidth: .infinity)", header)
        chips = self.menu[self.menu.index("private var chipsRow"):self.menu.index("private var successTint")]
        for symbol in ('"bolt.fill"', '"checkmark.seal.fill"', '"cpu"', '"rectangle.stack"'):
            self.assertIn(symbol, chips)
        for label in ('Label("Recent activity", systemImage: "waveform.path.ecg")', 'Label("Sessions", systemImage: "rectangle.stack")'):
            self.assertIn(label, self.menu)
        self.assertEqual(2, self.menu.count(".disclosureGroupStyle(MenuDisclosureStyle())"))

    def test_a_stopped_server_gets_a_prominent_start(self) -> None:
        bar = self.menu[self.menu.index("private var actionBar"):self.menu.index("/// Settings and More close the menu")]
        self.assertIn("let stopped = !state.serverRunning && !state.startingAfterLogin", bar)
        start = bar[bar.index("if stopped {"):bar.index("} else if")]
        self.assertIn("state.startServer()", start)
        self.assertIn(".buttonStyle(.borderedProminent)", start)

    def test_settings_window_opens_on_requested_pane(self) -> None:
        self.assertIn("SettingsWindowController.shared.show(state: state, settings: settings, section: section)", self.menu)

    def test_menu_bar_icon_is_reserved_for_activity(self) -> None:
        app = (SOURCES / "MacMCPMenuApp.swift").read_text(encoding="utf-8")
        self.assertIn('return state.pulse ? "cpu.fill" : "cpu"', app)
        self.assertIn("state.activeAgents > 0", app)
        self.assertNotIn("sessionNeedsAttentionCount", app)
        self.assertNotIn("exclamationmark.triangle.fill", app)

    def test_ended_and_idle_sessions_use_neutral_history_words(self) -> None:
        grouping = (SOURCES / "AppModels+Steering.swift").read_text(encoding="utf-8")
        settings = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn("let ended: [SteeringRecent]", grouping)
        self.assertNotIn("needsAttention", grouping)
        self.assertNotIn("terminalAttention", grouping)
        self.assertIn('sessionSectionHeader("Ended"', self.menu)
        self.assertIn('case .disconnected: return "Ended"', self.menu)
        self.assertIn('case .expired: return "Ended after idle"', self.menu)
        self.assertNotIn('sessionSectionHeader("Needs Attention"', self.menu)
        self.assertIn("idle and ended sessions stay visible as neutral history", settings)


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class StatusDisplayMappingTests(unittest.TestCase):
    def test_shared_labels_distinguish_configuration_from_observation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "StatusDisplayHarness.swift"
            binary = Path(td) / "status-display"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            cmd = [
                "xcrun", "swiftc", "-parse-as-library",
                *map(str, feature_files("AppState")),
                str(SOURCES / "AgentNotificationController.swift"),
                str(SOURCES / "SettingsStore.swift"),
                str(SOURCES / "ToolActivityBubbleController.swift"),
                str(SOURCES / "KeychainStore.swift"),
                str(SOURCES / "StatusDisplay.swift"),
                str(harness),
                "-framework", "SwiftUI", "-framework", "AppKit", "-framework", "Security",
                "-framework", "UserNotifications", "-framework", "Combine",
                "-o", str(binary),
            ]
            built = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertIn("STATUS_DISPLAY_PASS", run.stdout)

    def test_general_settings_and_menu_bar_label_use_the_same_mapping(self) -> None:
        settings = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        app = (SOURCES / "MacMCPMenuApp.swift").read_text(encoding="utf-8")
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("EndpointDisplay.make(", settings)
        self.assertIn("state.connectionState.display", app)
        self.assertIn('"${SCRIPT_DIR}/Sources/StatusDisplay.swift"', build)


if __name__ == "__main__":
    unittest.main()
