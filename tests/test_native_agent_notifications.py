from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "menu_app/Sources/SettingsStore.swift"
STATE = ROOT / "menu_app/Sources/AppState.swift"
VIEW = ROOT / "menu_app/Sources/SettingsView.swift"
CONTROLLER = ROOT / "menu_app/Sources/AgentNotificationController.swift"
BUILD = ROOT / "menu_app/build_app.sh"
DASHBOARD = ROOT / "mcp_server/dashboard/dashboard.js"
DASHBOARD_CSS = ROOT / "mcp_server/dashboard/dashboard.css"


class NativeAgentNotificationContractTests(unittest.TestCase):
    def test_settings_are_opt_in_and_default_off(self):
        source = SETTINGS.read_text(encoding="utf-8")
        self.assertIn("struct Notifications: Codable", source)
        self.assertIn("agent_completion: Bool", source)
        self.assertIn("Notifications(agent_completion: false)", source)
        self.assertIn("agentCompletionNotificationsEnabled = false", source)

    def test_permission_is_requested_only_from_explicit_enable_flow(self):
        controller = CONTROLLER.read_text(encoding="utf-8")
        state = STATE.read_text(encoding="utf-8")
        self.assertIn("func requestAuthorization() async -> Bool", controller)
        self.assertIn("func setAgentCompletionNotificationsEnabled(_ enabled: Bool)", state)
        self.assertIn("requestAuthorization()", state)
        init_block = state.split("init(startBackgroundTasks: Bool = true)", 1)[1].split("deinit", 1)[0]
        self.assertNotIn("requestAuthorization()", init_block)
        self.assertIn("refreshAgentNotificationAuthorization(reconcilePreference: true)", init_block)

    def test_notification_payload_and_coalescing_are_sanitized(self):
        source = CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("AgentNotificationSanitizer.label", source)
        self.assertIn('if let teamID = agent.teamID, !teamID.isEmpty { continue }', source)
        self.assertIn("previousTeamStatuses[team.id]", source)
        self.assertIn("target_kind", source)
        self.assertIn("target_id", source)
        self.assertNotIn("prompt", source.lower())
        self.assertNotIn("result_preview", source)

    def test_settings_ui_explains_coalescing_and_privacy(self):
        source = VIEW.read_text(encoding="utf-8")
        self.assertIn('GroupBox("Agent Notifications")', source)
        self.assertIn("Completion & attention alerts", source)
        self.assertIn("Teams are coalesced into one terminal notification", source)
        self.assertIn("never prompts, results, file paths, URLs, or secrets", source)

    def test_native_build_links_user_notifications(self):
        source = BUILD.read_text(encoding="utf-8")
        self.assertIn("-framework UserNotifications", source)
        self.assertIn("AgentNotificationController.swift", source)

    def test_notification_click_deep_links_dashboard_agent_or_team(self):
        state = STATE.read_text(encoding="utf-8")
        dashboard = DASHBOARD.read_text(encoding="utf-8")
        css = DASHBOARD_CSS.read_text(encoding="utf-8")
        self.assertIn('URLQueryItem(name: "focus_agent"', state)
        self.assertIn('URLQueryItem(name: "focus_team"', state)
        self.assertIn('initialQuery.get("focus_agent")', dashboard)
        self.assertIn('initialQuery.get("focus_team")', dashboard)
        self.assertIn(".agent-card.is-focus", css)


if __name__ == "__main__":
    unittest.main()
