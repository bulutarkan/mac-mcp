import AppKit
import CoreImage
import CoreImage.CIFilterBuiltins
import Foundation
import SwiftUI
import UniformTypeIdentifiers

enum SettingsSection: String, CaseIterable, Identifiable {
    case start
    case general
    case agents
    case scheduled
    case activity
    case usage
    case permissions
    case privacy
    case apps
    case mcpServers
    case macs
    case connections
    case voice
    case advanced
    case help

    var id: String { rawValue }

    var title: String {
        switch self {
        case .start: return "Get Started"
        case .general: return "General"
        case .agents: return "Agents"
        case .scheduled: return "Automation"
        case .activity: return "Activity & Notifications"
        case .usage: return "Usage"
        case .permissions: return "Permissions & Safety"
        case .privacy: return "Privacy"
        case .apps: return "Apps"
        case .mcpServers: return "MCP Servers"
        case .macs: return "Devices"
        case .connections: return "Connections"
        case .voice: return "Voice"
        case .advanced: return "Advanced"
        case .help: return "Help & Diagnostics"
        }
    }

    var symbol: String {
        switch self {
        case .start: return "flag.checkered"
        case .general: return "slider.horizontal.3"
        case .agents: return "cpu"
        case .scheduled: return "bolt.badge.clock"
        case .activity: return "bell.badge"
        case .usage: return "chart.bar.xaxis"
        case .permissions: return "checkmark.shield"
        case .privacy: return "hand.raised"
        case .apps: return "square.grid.2x2"
        case .mcpServers: return "point.3.connected.trianglepath.dotted"
        case .macs: return "laptopcomputer.and.iphone"
        case .connections: return "network"
        case .voice: return "waveform.and.mic"
        case .advanced: return "wrench.and.screwdriver"
        case .help: return "lifepreserver"
        }
    }

    /// Its words, the labels it shows and what people call them (#125).
    var searchTerms: String {
        SettingsSearchIndex.searchTerms(for: rawValue)
    }

    /// The sidebar group this section sits in (#250).
    var group: SettingsSectionGroup {
        switch self {
        case .start, .general: return .overview
        case .agents, .scheduled, .activity, .usage: return .agents
        case .permissions, .privacy: return .safety
        case .apps, .mcpServers, .macs, .connections, .voice: return .connect
        case .advanced, .help: return .support
        }
    }
}

/// A few plain headings so fifteen sidebar destinations scan quickly (#250). The first
/// group has no heading, like the top of System Settings.
enum SettingsSectionGroup: String, CaseIterable, Identifiable {
    case overview
    case agents
    case safety
    case connect
    case support

    var id: String { rawValue }

    var title: String? {
        switch self {
        case .overview: return nil
        case .agents: return "Agents"
        case .safety: return "Safety"
        case .connect: return "Connect"
        case .support: return "Support"
        }
    }
}

private enum ConnectionsTab: String, SettingsTab {
    case browser
    case mobile
    case clients

    var title: String {
        switch self {
        case .browser: return "Browser"
        case .mobile: return "Mobile"
        case .clients: return "AI Clients"
        }
    }
    var symbol: String {
        switch self {
        case .browser: return "safari"
        case .mobile: return "iphone"
        case .clients: return "link"
        }
    }
}

/// Clients `mac-mcp connect-config` knows; the CLI renders the setup, so the app never builds a snippet itself.
private let connectClients: [(id: String, title: String)] = [
    ("chatgpt", "ChatGPT"), ("claude-code", "Claude Code"), ("codex", "Codex"),
    ("opencode", "OpenCode"), ("claude-desktop", "Claude Desktop"), ("other", "Another AI app"),
]

private enum UsageAggregation: String, CaseIterable, Identifiable {
    case daily = "Daily"
    case weekly = "Weekly"
    case cumulative = "Cumulative"

    var id: String { rawValue }
}

private enum UsageActorFilter: String, CaseIterable, Identifiable {
    case all = "All"
    case primary = "Primary"
    case subagents = "Subagents"

    var id: String { rawValue }
    var apiValue: String {
        switch self {
        case .all: return "all"
        case .primary: return "primary"
        case .subagents: return "scoped_subagent"
        }
    }
}


private enum SettingsFeedbackScope: String, Equatable {
    case agents
    case decisions
    case endpoint
    case runtime
    case sessions
    case voice
}

/// The Voice pane's two typed fields.
private enum VoiceField: Hashable {
    case name
    case rate
}

private struct SettingsSaveFeedback: Equatable {
    let scope: SettingsFeedbackScope
    let message: String
    let isError: Bool
}

struct SettingsView: View {
    @ObservedObject var state: AppState
    @ObservedObject var settings: SettingsStore
    @StateObject private var audio = AudioDeviceStore()
    @StateObject private var diagnostics = DiagnosticsCenter()
    @ObservedObject private var navigation = SettingsNavigation.shared
    @ObservedObject private var tabs = SettingsTabs.shared
    @MacMCPState private var selection: SettingsSection
    @MacMCPState private var notice = ""
    /// This Mac's shortcuts, for the ones agents may run with commands off (#195).
    @MacMCPState private var macShortcutNames: [String]?
    @MacMCPState private var shortcutFilter = ""
    @MacMCPState private var permissionDetailsShown = false
    /// What agents can do in each app, from the server's adapter support matrix (#262).
    @MacMCPState private var appSupport: [[String: Any]]?
    /// #19: why the app list could not be read, kept apart from an empty list.
    @MacMCPState private var appSupportProblem: String?
    @MacMCPState private var confirmClearUsage = false
    @MacMCPState private var memoryClearPreview: MemoryClearResult?
    @MacMCPState private var reviewingMemory = false
    @MacMCPState private var groqKey = ""
    /// Voice name and rate as typed, before Return or leaving the field saves them; nil when not edited.
    @MacMCPState private var voiceNameDraft: String?
    @MacMCPState private var ttsRateDraft: String?
    @MacMCPState private var voiceNameProblem: String?
    @MacMCPState private var ttsRateProblem: String?
    @FocusState private var voiceField: VoiceField?
    @MacMCPState private var decisionsKey = ""
    @MacMCPState private var cloudflareToken = ""
    @MacMCPState private var settingsSearch = ""
    @AppStorage("settings.connections.tab") private var connectionsTab: ConnectionsTab = .browser
    @MacMCPState private var connectClient = "claude-code"
    @MacMCPState private var connectOutput = ""
    @MacMCPState private var manualSetupExpanded = false
    @MacMCPState private var connectFailed = false
    @MacMCPState private var connectLoading = false
    @MacMCPState private var usageAggregation: UsageAggregation = .daily
    @MacMCPState private var usageActor: UsageActorFilter = .all
    @MacMCPState private var providerUsagePeriod: ProviderUsagePeriod = .year
    @MacMCPState private var saveFeedback: SettingsSaveFeedback?
    @MacMCPState private var showUpdateDetails = false
    @FocusState private var searchFocused: Bool

    init(state: AppState, settings: SettingsStore, initialSection: SettingsSection = .general) {
        self.state = state
        self.settings = settings
        _selection = MacMCPState(initialValue: initialSection)
    }

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            detail
                // Every group in every pane: the Usage tabs' title and box.
                .groupBoxStyle(SettingsGroupStyle())
                // A pane that replaces another rises in slightly; with Reduce Motion it only fades.
                .animation(AppMotion.pane, value: selection)
        }
        .frame(minWidth: 820, idealWidth: 920, minHeight: 560, idealHeight: 640)
        .background(.regularMaterial)
        .onAppear {
            navigation.lastSection = selection
            applyNavigationRequest()
        }
        .onChange(of: navigation.requestedSection) { _ in applyNavigationRequest() }
        .onChange(of: selection) { section in navigation.lastSection = section }
        .task {
            audio.refresh()
            state.refreshCloudflareCredentialState()
            await state.refreshProviders()
            await state.refreshMobileDevices()
            await state.refreshUsage(actorClass: usageActor.apiValue)
            await state.refreshProviderUsage(days: providerUsagePeriod.days)
            await state.refreshAgentNotificationAuthorization()
        }
        .task(id: selection) {
            if selection == .voice {
                settings.refreshVoiceConsent()
            }
            if selection == .agents {
                await state.refreshTeamHistory(limit: max(10, state.teamHistory?.teams.count ?? 0))
            }
            if selection == .usage {
                await state.refreshUsage(actorClass: usageActor.apiValue)
                await state.refreshProviderUsage(days: providerUsagePeriod.days)
            }
            if selection == .general, state.updateCheckInfo == nil, !state.updateTransactionActive {
                state.checkForUpdates()
            }
            guard selection == .connections else { return }
            while !Task.isCancelled {
                await state.refreshMobileDevices()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    private var filteredSections: [SettingsSection] {
        SettingsSection.allCases.filter {
            SettingsSearchIndex.matches(query: settingsSearch, title: $0.title, terms: $0.searchTerms)
        }
    }

    private var trimmedSettingsSearch: String {
        settingsSearch.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var startPane: some View {
        let checklist = state.onboardingChecklist
        return ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader(
                    "Get Started",
                    subtitle: "Three steps from install to a first request that works. Everything stays on this Mac unless you turn on remote access."
                )

                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(checklist.isComplete ? "All set" : "\(checklist.completed) of \(checklist.steps.count) done")
                            .font(SettingsType.title)
                        Spacer()
                        if checklist.isComplete {
                            Label("Ready for your first request", systemImage: "checkmark.seal.fill")
                                .font(SettingsType.note).foregroundStyle(.green)
                        }
                    }
                    UsageShareMeter(share: Double(checklist.completed) / Double(max(1, checklist.steps.count)),
                                    color: .green, height: 6)
                        .accessibilityLabel("\(checklist.completed) of \(checklist.steps.count) steps done")
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(Array(checklist.steps.enumerated()), id: \.element.id) { index, step in
                            if index > 0 { Divider() }
                            onboardingRow(step, number: index + 1)
                        }
                    }
                    .padding(.horizontal, 14).padding(.vertical, 12)
                    .usageListBox()
                    .padding(.top, 4)
                }

                GroupBox("Optional") {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(checklist.optional) { step in
                            onboardingRow(step)
                            Divider()
                        }
                        onboardingOptionalRow(
                            symbol: "safari",
                            title: "Browser companions",
                            detail: browserCompanionsSummary,
                            actionTitle: "Open Browser"
                        ) {
                            connectionsTab = .browser
                            selection = .connections
                            notice = ""
                        }
                        Divider()
                        onboardingOptionalRow(
                            symbol: "globe",
                            title: "Remote access for ChatGPT",
                            detail: (state.onboardingStatus?.publicEndpointMode ?? "none") == "none"
                                ? "ChatGPT connects from the internet, so it needs a public HTTPS endpoint: Cloudflare Tunnel, ngrok or your own. It stays off until you set it up."
                                : "Remote access is set up, so ChatGPT can reach this Mac over HTTPS.",
                            actionTitle: "Open Connections"
                        ) {
                            connectionsTab = .clients
                            selection = .connections
                            notice = ""
                        }
                    }
                    .padding(.vertical, 4)
                }

                HStack {
                    Button("Open Setup & Tour") { state.showTour() }
                    Spacer()
                    if !checklist.isComplete {
                        Button(settings.onboardingDismissed ? "Show Get Started in the Menu" : "Hide Get Started") {
                            state.setOnboardingDismissed(!settings.onboardingDismissed)
                        }
                    }
                }
            }
            .padding(22)
        }
        .task { await state.refreshOnboarding() }
    }

    /// A step: its number until it is done (then a green check), what it is, and the button that does it.
    private func onboardingRow(_ step: OnboardingStep, number: Int? = nil) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Group {
                if step.done {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 20, weight: .semibold))
                        .foregroundStyle(Color.green)
                } else if let number {
                    Text("\(number)")
                        .font(.system(size: 12, weight: .semibold)).monospacedDigit()
                        .frame(width: 22, height: 22)
                        .overlay(Circle().strokeBorder(Color.secondary.opacity(0.6), lineWidth: 1.5))
                } else {
                    Image(systemName: "circle")
                        .font(.system(size: 20, weight: .regular))
                        .foregroundStyle(Color.secondary)
                }
            }
            .frame(width: 24)
            .accessibilityLabel(step.done ? "Done" : "To do")
            VStack(alignment: .leading, spacing: 3) {
                Text(step.title)
                    .font(SettingsType.title)
                Text(step.detail)
                    .font(SettingsType.note)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            Spacer()
            if !step.done {
                onboardingAction(step.id)
            }
        }
    }

    @ViewBuilder
    private func onboardingAction(_ id: OnboardingStepID) -> some View {
        switch id {
        case .server:
            Button("Start Server") { state.startServer() }
        case .client:
            Button("Client Setup") {
                connectionsTab = .clients
                selection = .connections
                notice = ""
            }
        case .firstRequest:
            Button("Copy Request") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(OnboardingChecklist.firstRequestPrompt, forType: .string)
                notice = "Copied. Paste it into your AI app."
            }
        case .appControl, .screenCapture:
            Button("Allow…") {
                selection = .help
                notice = ""
            }
        }
    }

    /// "Safari is on · Chrome is not connected" once known; the pitch until then.
    private var browserCompanionsSummary: String {
        guard state.chromeCompanion.checked else {
            return "Let agents work in Safari and Chrome tabs in the background instead of taking over your screen."
        }
        let safari = state.safariExtensionEnabled ? "Safari is on" : "Safari is off"
        let chrome = state.chromeCompanion.connected
            ? (state.chromeCompanion.needsReload ? "Chrome needs a reload" : "Chrome is connected")
            : (state.chromeInstalled ? "Chrome is not connected" : "Chrome is not installed")
        return "\(safari) · \(chrome). Agents use them to work in browser tabs without taking over your screen."
    }

    private func onboardingOptionalRow(
        symbol: String, title: String, detail: String, actionTitle: String, action: @escaping () -> Void
    ) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .regular))
                .foregroundStyle(Color.secondary)
                .frame(width: 24)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(SettingsType.title)
                Text(detail)
                    .font(SettingsType.note)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            Button(actionTitle, action: action)
        }
    }

    private var generalPane: some View {
        let server = state.connectionState.display
        let endpoint = endpointDisplay
        return ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader(
                    "General",
                    subtitle: "Status of this Mac MCP instance, remote access, and anything that needs your attention."
                )

                VStack(alignment: .leading, spacing: 8) {
                    Text("Overview").font(SettingsType.title)
                    UsageFactsRow(facts: [
                        UsageFact(value: server.label, title: "Server", note: server.explanation,
                                  warning: server.tone == .error || server.tone == .warning),
                        UsageFact(value: state.version == "—" ? "Unknown" : "v\(state.version)", title: "Version",
                                  note: "The Mac MCP installed on this Mac."),
                        UsageFact(value: String(state.activeAgents), title: "Active agents",
                                  note: state.activeAgents == 0 ? "None working right now." : "Delegated agents working right now."),
                    ])
                }

                GroupBox("Remote Access") {
                    HStack(spacing: 12) {
                        Image(systemName: endpoint.symbol)
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(endpoint.tone == .neutral ? Color.secondary : endpoint.tone.color)
                            .frame(width: 34)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(endpoint.label)
                                .font(SettingsType.title)
                            Text(endpoint.explanation)
                                .font(SettingsType.note)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                        Button("Open Connections") {
                            selection = .connections
                            notice = ""
                        }
                    }
                    .padding(.top, 5)
                }

                GroupBox("Startup") {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: "power")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(Color.accentColor)
                            .frame(width: 34)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 3) {
                            Text("Start Mac MCP when you log in")
                                .font(SettingsType.title)
                            Text("If the server was running when you restarted or logged out, it starts again with the menu bar app. A server you stopped stays stopped.")
                                .font(SettingsType.note)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                        if state.startAtLoginChanging {
                            ProgressView()
                                .controlSize(.small)
                        } else {
                            Toggle("Start Mac MCP when you log in", isOn: startAtLoginBinding)
                                .labelsHidden()
                                .toggleStyle(.switch)
                        }
                    }
                    .padding(.top, 5)
                }

                GroupBox("Issues") {
                    VStack(alignment: .leading, spacing: 10) {
                        // Remote access has its own row above; it is not said twice.
                        let issues = generalIssues.filter { $0.id != "endpoint" }
                        if issues.isEmpty && state.connectionState == .connecting {
                            // #19: not checked yet is not "nothing to do".
                            HStack(spacing: 10) {
                                ProgressView().controlSize(.small).frame(width: 22)
                                Text("Checking…").font(SettingsType.title)
                                Spacer()
                            }
                            .accessibilityElement(children: .combine)
                        } else if issues.isEmpty {
                            HStack(spacing: 10) {
                                Image(systemName: "checkmark.seal.fill")
                                    .font(.system(size: 17, weight: .semibold))
                                    .foregroundStyle(.green)
                                    .frame(width: 22)
                                    .accessibilityHidden(true)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("All caught up")
                                        .font(SettingsType.title)
                                    Text(generalIssues.contains { $0.id == "endpoint" }
                                         ? "Nothing else needs your attention." : "Nothing needs your attention.")
                                        .font(SettingsType.note)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                            }
                            .accessibilityElement(children: .combine)
                        } else {
                            ForEach(Array(issues.enumerated()), id: \.element.id) { index, issue in
                                if index > 0 { Divider() }
                                generalIssueRow(issue)
                            }
                        }
                    }
                    .padding(.top, 5)
                }

                updateCard

                if let action = state.actionNotice {
                    Label(action.message, systemImage: action.symbolName)
                        .font(SettingsType.note)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Spacer(minLength: 0)
            }
            .padding(22)
        }
    }

    private var activityPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader(
                    "Activity & Notifications",
                    subtitle: "Live activity while Mac MCP works, and alerts when delegated agents finish."
                )

                GroupBox("Tool Activity") {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(alignment: .center, spacing: 12) {
                            Image(systemName: "bubble.left.and.text.bubble.right.fill")
                                .font(.system(size: 18, weight: .semibold))
                                .foregroundStyle(Color.accentColor)
                                .frame(width: 34)
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Explain active tool work")
                                    .font(SettingsType.title)
                                Text("Show a short, human-readable intent while an MCP tool is running.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            if liveActivityStyle != "off" {
                                Button("Preview") {
                                    if liveActivityStyle == "island" {
                                        LiveIslandController.shared.preview()
                                    } else {
                                        ToolActivityBubbleController.shared.preview(
                                            tool: "run_command",
                                            description: "Checking RAM & Disk Health"
                                        )
                                    }
                                }
                                .controlSize(.small)
                            }
                        }

                        Divider()

                        VStack(alignment: .leading, spacing: 8) {
                            Text("Live activity")
                                .font(SettingsType.body.weight(.medium))
                            HStack(spacing: 10) {
                                liveActivityChoice("off", title: "Off", symbol: "nosign")
                                liveActivityChoice("bubble", title: "Bubble", symbol: "bubble.left.and.text.bubble.right")
                                liveActivityChoice("island", title: "Notch island", symbol: "macbook")
                            }
                            .accessibilityElement(children: .contain)
                            .accessibilityLabel("Live activity")
                            Text(liveActivityDetail)
                                .font(SettingsType.note)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }

                        Divider()

                        HStack(alignment: .top, spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Agent cursor on the desktop")
                                    .font(SettingsType.body.weight(.medium))
                                Text("Shows where an agent clicks or types in another app's window while it works in the background. It floats above all windows on whichever desktop you are looking at, so you see every step, and it never appears in screenshots.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            Toggle("Agent cursor on the desktop", isOn: desktopCursorBinding)
                                .labelsHidden()
                                .toggleStyle(.switch)
                        }

                        Divider()

                        HStack(alignment: .top, spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Glow on the agent's browser tab")
                                    .font(SettingsType.body.weight(.medium))
                                Text("When an agent works in a Safari or Chrome tab you are not looking at, that tab glows briefly in the tab bar each time it acts. Nothing shows on the tab you are on.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            Toggle("Glow on the agent's browser tab", isOn: browserTabPulseBinding)
                                .labelsHidden()
                                .toggleStyle(.switch)
                        }

                        Divider()

                        HStack(alignment: .top, spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Require Tool Descriptions")
                                    .font(SettingsType.body.weight(.medium))
                                Text("Adds a short required description field to MCP tool schemas. Turn this off to remove the field entirely.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            Toggle("Require Tool Descriptions", isOn: requireToolDescriptionsBinding)
                                .labelsHidden()
                                .toggleStyle(.switch)
                        }

                        if settings.requireToolDescriptions {
                            Label(
                                "Reconnect MCP clients after changing this setting so they refresh their tool schemas.",
                                systemImage: "arrow.triangle.2.circlepath"
                            )
                            .font(SettingsType.note)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.top, 5)
                }

                GroupBox("Agent Notifications") {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: "bell.badge.fill")
                                .font(.system(size: 18, weight: .semibold))
                                .foregroundStyle(Color.accentColor)
                                .frame(width: 34)
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Completion & attention alerts")
                                    .font(SettingsType.title)
                                Text("Notify when standalone delegated agents or whole agent teams finish while you work elsewhere.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            if state.agentNotificationPermissionChanging {
                                ProgressView()
                                    .controlSize(.small)
                            } else {
                                Toggle("Completion & attention alerts", isOn: agentCompletionNotificationsBinding)
                                    .labelsHidden()
                                    .toggleStyle(.switch)
                            }
                        }

                        Divider()

                        HStack(spacing: 6) {
                            Circle()
                                .fill(state.agentNotificationAuthorizationState == .denied ? Color.orange
                                      : state.agentNotificationAuthorizationState == .authorized ? Color.green
                                      : Color.secondary.opacity(0.5))
                                .frame(width: 7, height: 7)
                            Text("macOS permission: \(state.agentNotificationAuthorizationState.displayName)")
                                .font(SettingsType.note)
                                .foregroundStyle(state.agentNotificationAuthorizationState == .denied ? Color.orange : Color.secondary)
                        }
                        .accessibilityElement(children: .combine)

                        Text("Teams are coalesced into one terminal notification. Alerts contain only a sanitized agent/team label — never prompts, results, file paths, URLs, or secrets.")
                            .font(SettingsType.note)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)

                        if state.agentNotificationAuthorizationState == .denied {
                            Label(
                                "Notification permission is denied in macOS. Re-enable it in System Settings before turning this on.",
                                systemImage: "bell.slash"
                            )
                            .font(SettingsType.note)
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.top, 5)
                }

                Spacer(minLength: 0)
            }
            .padding(22)
        }
    }

    /// One of the three ways live activity can show, as a card with its symbol; the chosen one is outlined.
    private func liveActivityChoice(_ tag: String, title: String, symbol: String) -> some View {
        let selected = liveActivityStyle == tag
        return Button {
            liveActivityStyleBinding.wrappedValue = tag
        } label: {
            VStack(spacing: 6) {
                Image(systemName: symbol)
                    .font(.system(size: 20, weight: .regular))
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                    .frame(height: 26)
                Text(title).font(SettingsType.note.weight(selected ? .semibold : .regular))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 10)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(selected ? Color.accentColor.opacity(0.1) : Color.primary.opacity(0.04)))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(selected ? Color.accentColor : Color.primary.opacity(0.08), lineWidth: selected ? 1.5 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var endpointDisplay: StatusDisplay {
        EndpointDisplay.make(
            mode: settings.publicEndpointMode,
            cloudflareRunning: state.cloudflareRunning,
            ngrokRunning: state.ngrokRunning
        )
    }

    private struct GeneralIssue: Identifiable {
        let id: String
        let title: String
        let detail: String
        let symbol: String
        let tone: StatusTone
        var actionTitle: String? = nil
        var action: (() -> Void)? = nil
    }

    /// Problems the user can act on now, most urgent first.
    private var generalIssues: [GeneralIssue] {
        var issues: [GeneralIssue] = []
        let server = state.connectionState.display
        switch state.connectionState {
        case .disconnected:
            if state.serverRunning {
                issues.append(GeneralIssue(id: "server", title: "Server is not answering", detail: server.explanation,
                                           symbol: server.symbol, tone: .error, actionTitle: "Retry",
                                           action: { Task { await state.retryConnection() } }))
            } else {
                issues.append(GeneralIssue(id: "server", title: "Server is not running", detail: server.explanation,
                                           symbol: server.symbol, tone: .error, actionTitle: "Start Server",
                                           action: { state.startServer() }))
            }
        case .degraded:
            issues.append(GeneralIssue(id: "server", title: "Some server data is unavailable",
                                       detail: state.connectionBannerDetail.isEmpty ? server.explanation : state.connectionBannerDetail,
                                       symbol: server.symbol, tone: .warning, actionTitle: "Retry",
                                       action: { Task { await state.retryConnection() } }))
        case .connected, .connecting:
            break
        }
        if state.connectionState == .connected || state.connectionState == .degraded {
            let safety = SafetyDisplay.make(state.securitySemantics, serverRunning: state.serverRunning)
            if safety.tone == .warning {
                issues.append(GeneralIssue(id: "safety", title: "Review the permission profile", detail: safety.explanation,
                                           symbol: safety.symbol, tone: .warning, actionTitle: "Review Safety",
                                           action: { selection = .permissions; notice = "" }))
            }
        }
        if EndpointDisplay.needsReview(mode: settings.publicEndpointMode,
                                       cloudflareRunning: state.cloudflareRunning,
                                       ngrokRunning: state.ngrokRunning) {
            let endpoint = endpointDisplay
            issues.append(GeneralIssue(id: "endpoint", title: endpoint.label, detail: endpoint.explanation,
                                       symbol: endpoint.symbol, tone: .warning, actionTitle: "Open Connections",
                                       action: { selection = .connections; notice = "" }))
        }
        if !settings.settingsLoadIssue.isEmpty {
            issues.append(GeneralIssue(id: "settings", title: "Settings file needs repair", detail: settings.settingsLoadIssue,
                                       symbol: "exclamationmark.shield", tone: .warning, actionTitle: "Open Agents",
                                       action: { selection = .agents; notice = "" }))
        }
        if let progress = state.updateProgress, !state.updateTransactionActive,
           progress.isInProgress || ["failed", "recovery_failed"].contains(progress.normalizedStatus) {
            issues.append(GeneralIssue(id: "update", title: progress.statusTitle, detail: progress.statusDetail,
                                       symbol: updateStatusSymbol(progress.statusKind), tone: .error,
                                       actionTitle: state.canInstallUpdate ? state.updateActionTitle : nil,
                                       action: state.canInstallUpdate ? { state.installUpdate() } : nil))
        }
        if settings.agentCompletionNotificationsEnabled && state.agentNotificationAuthorizationState == .denied {
            issues.append(GeneralIssue(id: "notifications", title: "Agent alerts are blocked by macOS",
                                       detail: "Allow notifications for Mac MCP in System Settings.",
                                       symbol: "bell.slash", tone: .warning, actionTitle: "Review",
                                       action: { selection = .activity; notice = "" }))
        }
        return issues
    }

    private func generalIssueRow(_ issue: GeneralIssue) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: issue.symbol)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(issue.tone.color)
                .frame(width: 22)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(issue.title)
                    .font(SettingsType.title)
                Text(issue.detail)
                    .font(SettingsType.note)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            if let title = issue.actionTitle, let action = issue.action {
                Button(title, action: action)
                    .controlSize(.small)
            }
        }
    }

    private func applyNavigationRequest() {
        guard let section = navigation.requestedSection else { return }
        navigation.requestedSection = nil
        if !filteredSections.contains(section) { settingsSearch = "" }
        selection = section
        notice = ""
    }

    private func overviewMetric(title: String, value: String, symbol: String, accent: Color) -> some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(accent)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(SettingsType.meta)
                    .foregroundStyle(.secondary)
                Text(value)
                    .font(SettingsType.title)
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
        }
        .padding(11)
        .frame(maxWidth: .infinity)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.55), in: RoundedRectangle(cornerRadius: 9))
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Settings")
                .font(.title3.weight(.semibold))
                .padding(.horizontal, 14)
                .padding(.top, 16)
                .padding(.bottom, 8)

            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                TextField("Search Settings", text: $settingsSearch)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .focused($searchFocused)
                    .accessibilityLabel("Search Settings")
                    .accessibilityHint("Filters the settings sections. Press Command-F to jump here.")
                if !settingsSearch.isEmpty {
                    Button { settingsSearch = "" } label: {
                        Image(systemName: "xmark.circle.fill").font(.system(size: 12)).foregroundStyle(.tertiary)
                    }
                    .buttonStyle(.plain)
                    .help("Clear Search")
                    .accessibilityLabel("Clear Search")
                }
            }
            .padding(.horizontal, 9)
            .frame(height: 30)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(0.06)))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(searchFocused ? Color.accentColor.opacity(0.75) : Color.primary.opacity(0.08),
                                  lineWidth: searchFocused ? 2 : 1)
            )
            .contentShape(Rectangle())
            .onTapGesture { searchFocused = true }
            .help("Search Settings (⌘F)")
            .padding(.horizontal, 10)
            .padding(.bottom, 6)
                .background {
                    // ⌘F focuses search while the Settings window is key.
                    Button("Find in Settings") { searchFocused = true }
                        .keyboardShortcut("f", modifiers: .command)
                        .opacity(0)
                        .frame(width: 0, height: 0)
                        .accessibilityHidden(true)
                }
                .onChange(of: settingsSearch) { _ in
                    // Never leave a pane on screen that the search no longer lists: show the best match,
                    // on the tab its words name (#125); the open pane stays while it matches as well.
                    guard let hit = SettingsSearchIndex.target(
                        query: settingsSearch,
                        sections: SettingsSection.allCases.map { ($0.rawValue, $0.title) },
                        current: selection.rawValue
                    ), let section = SettingsSection(rawValue: hit.section) else { return }
                    selection = section
                    if let tab = hit.tab { openSearchTab(tab) }
                }

            if filteredSections.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("No settings found for “\(trimmedSettingsSearch)”")
                        .font(SettingsType.note)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Button("Clear Search") { settingsSearch = "" }
                        .controlSize(.small)
                }
                .padding(.horizontal, 14)
                .padding(.top, 4)
            }

            // Scrolls on its own, so every destination stays reachable in the smallest window,
            // and brings the open section into view when a link opens one further down.
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(SettingsSectionGroup.allCases) { group in
                            let items = filteredSections.filter { $0.group == group }
                            if !items.isEmpty {
                                if let title = group.title {
                                    Text(title.uppercased())
                                        .font(.system(size: 10, weight: .semibold))
                                        .foregroundStyle(.tertiary)
                                        .tracking(0.5)
                                        .padding(.horizontal, 17)
                                        .padding(.top, 10)
                                        .padding(.bottom, 2)
                                        .accessibilityLabel(title)
                                        .accessibilityAddTraits(.isHeader)
                                }
                                ForEach(items) { item in
                                    sidebarRow(item)
                                        .id(item)
                                }
                            }
                        }
                    }
                    .padding(.bottom, 8)
                }
                // On a new window the rows are not laid out yet when onAppear runs.
                .onAppear { DispatchQueue.main.async { proxy.scrollTo(selection) } }
                .onChange(of: selection) { item in proxy.scrollTo(item) }
                // A link to the section already open changes no selection; the window is reused.
                .onChange(of: navigation.requestedSection) { section in
                    if let section { DispatchQueue.main.async { proxy.scrollTo(section) } }
                }
            }

            if !notice.isEmpty {
                Text(notice)
                    .font(SettingsType.meta)
                    .foregroundStyle(notice.hasPrefix("Could not") ? Color.red : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 14)
                    .padding(.bottom, 14)
            }
        }
        .frame(width: 224)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.38))
    }

    /// The tab a search names, by the tab ids SettingsSearchIndex uses.
    private func openSearchTab(_ tab: String) {
        switch tab {
        case "browser": connectionsTab = .browser
        case "mobile": connectionsTab = .mobile
        case "ai-clients": connectionsTab = .clients
        case "agents-general": tabs.agents = .general
        case "protection": tabs.privacy = .protection
        case "scheduled-tasks": AutomationNavigation.shared.tab = .tasks
        case "recipes": AutomationNavigation.shared.tab = .recipes
        default: tabs.select(link: tab)
        }
    }

    private func sidebarRow(_ item: SettingsSection) -> some View {
        Button {
            selection = item
            notice = ""
        } label: {
            HStack(spacing: 9) {
                Image(systemName: item.symbol)
                    .frame(width: 17)
                    .accessibilityHidden(true)
                Text(item.title)
                Spacer()
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(
            selection == item ? Color.accentColor.opacity(0.16) : Color.clear,
            in: RoundedRectangle(cornerRadius: 8, style: .continuous)
        )
        .accessibilityLabel(item.title)
        .accessibilityAddTraits(selection == item ? .isSelected : [])
        .padding(.horizontal, 7)
    }

    @ViewBuilder
    private var detail: some View {
        if filteredSections.isEmpty {
            VStack(spacing: 10) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 28))
                    .foregroundStyle(.secondary)
                Text("No settings found for “\(trimmedSettingsSearch)”")
                    .font(.headline)
                Text("Try a shorter or different word, such as port, tunnel or notifications.")
                    .font(SettingsType.note)
                    .foregroundStyle(.secondary)
                Button("Clear Search") { settingsSearch = "" }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            selectedPane
                .id(selection)
                .transition(AppMotion.paneChange)
        }
    }

    @ViewBuilder
    private var selectedPane: some View {
        switch selection {
        case .start: startPane
        case .general: generalPane
        case .agents: subagentsPane
        case .scheduled: AutomationPane(state: state)
        case .activity: activityPane
        case .usage: usagePane
        case .permissions: permissionsPane
        case .privacy: PrivacyPane(state: state)
        case .apps: appsPane
        case .mcpServers: McpServersPane(state: state)
        case .macs: MacsPane(state: state)
        case .connections: connectionsPane
        case .voice: voicePane
        case .advanced: advancedPane
        case .help:
            HelpDiagnosticsPane(state: state, settings: settings, center: diagnostics) { pane in
                selection = pane == "advanced" ? .advanced : .connections
                notice = ""
            }
        }
    }

    private var subagentsPane: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 13) {
                paneHeader(
                    "Agents",
                    subtitle: "Choose delegated providers, models, and the default agent Mac MCP uses for new work.",
                    refresh: true
                )
                SettingsTabPicker(selection: $tabs.agents, label: "Agents section")
            }
            .padding(.horizontal, 22)
            .padding(.top, 22)
            .padding(.bottom, 12)
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    switch tabs.agents {
                    case .general:
                        agentsStatusRows
                        defaultAgentCard
                        agentLimitsGroup
                    case .teams:
                        agentsStatusRows
                        TeamPresetsSection(
                            settings: settings,
                            state: state,
                            providers: state.providerStatuses,
                            catalogs: state.providerCatalogs,
                            phase: providersPhase
                        ) {
                            tabs.agents = .providers
                        }
                    case .providers:
                        agentsStatusRows
                        providersList
                    case .skills:
                        SkillsSection(state: state)
                    case .history:
                        TeamHistorySection(
                            history: state.teamHistory,
                            loading: state.teamHistoryLoading,
                            issue: state.teamHistoryIssue,
                            loadMessages: { await state.teamMessages(teamID: $0, after: $1) }
                        ) {
                            let shown = state.teamHistory?.teams.count ?? 0
                            Task { await state.refreshTeamHistory(limit: shown + 10) }
                        }
                    }
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .id(tabs.agents)  // each tab opens at its top
        }
    }

    /// Whether provider settings could be read and saved; shown on the tabs that change them.
    @ViewBuilder
    private var agentsStatusRows: some View {
        if !settings.settingsLoadIssue.isEmpty {
            Label(settings.settingsLoadIssue, systemImage: "exclamationmark.shield")
                .font(SettingsType.note.weight(.medium))
                .foregroundStyle(settings.providerSettingsLocked ? Color.orange : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }

        settingsDataStateRow(state.providerSettingsState) {
            Task { await state.refreshProviders() }
        }
        saveFeedbackRow(.agents)
    }

    @ViewBuilder
    private var providersList: some View {
        Text("Choose which AI tools on this Mac can take work from Mac MCP. Each one has to be installed and signed in first.")
            .font(SettingsType.note)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)

        VStack(spacing: 0) {
            providerRow(id: "opencode", title: "OpenCode", subtitle: "Uses the OpenCode command-line tool")
            Divider()
            providerRow(id: "codex", title: "Codex", subtitle: "Uses the Codex command-line tool")
            Divider()
            providerRow(id: "claude", title: "Claude Code", subtitle: "Uses Claude Code, with your Claude sign-in")
            Divider()
            providerRow(id: "kimi", title: "Kimi Code", subtitle: "Uses Kimi Code, with your kimi login or its model providers")
            Divider()
            providerRow(id: "grok", title: "Grok Build", subtitle: "Uses Grok Build, with your grok login or xAI key")
            Divider()
            providerRow(id: "antigravity", title: "Antigravity", subtitle: "Uses Antigravity, with your Google sign-in")
            Divider()
            providerRow(id: "pi", title: "pi", subtitle: "Uses the pi coding agent, with your pi login or API keys")
            Divider()
            providerRow(id: "cline", title: "Cline", subtitle: "Uses Cline, with your Cline sign-in or API keys")
            Divider()
            providerRow(id: "kilo", title: "Kilo Code", subtitle: "Uses Kilo Code, with your Kilo sign-in or API keys")
            Divider()
            providerRow(id: "goose", title: "Goose", subtitle: "Uses Goose, with its provider and key")
            Divider()
            providerRow(id: "copilot", title: "GitHub Copilot",
                        subtitle: "Uses GitHub Copilot, with a GitHub token or your own key (model openrouter/…)")
        }
        .usageListBox()

        Text("A tool that is off is hidden from AI apps and never started. Changes apply to new requests right away.")
            .font(SettingsType.note)
            .foregroundStyle(.tertiary)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.top, 2)
    }

    private var usageRetentionDays: Int {
        state.usagePrivacy?.retentionDays ?? state.usageSummary?.retentionDays ?? 365
    }

    private var usageMeteringEnabled: Bool {
        state.usagePrivacy?.enabled ?? state.usageSummary?.meteringEnabled ?? true
    }

    /// Recording, retention and memory: settings people change rarely, on their own tab.
    private var usageStorageTab: some View {
        VStack(alignment: .leading, spacing: 0) {
            usageDataSettings
            memorySettings.padding(.top, 28)
        }
        .task { await state.refreshMemory() }
        .confirmationDialog("Delete all stored usage data?", isPresented: $confirmClearUsage) {
            Button("Delete Usage Data", role: .destructive) {
                Task { await state.clearUsage() }
            }
        } message: {
            Text("This removes tool and provider usage history from this Mac. It cannot be undone.")
        }
        .confirmationDialog(
            "Delete \(memoryClearPreview?.count ?? 0) memories?",
            isPresented: Binding(get: { memoryClearPreview != nil }, set: { if !$0 { memoryClearPreview = nil } })
        ) {
            Button("Delete \(memoryClearPreview?.count ?? 0) Memories", role: .destructive) {
                Task { _ = await state.clearMemory(confirm: true) }
            }
        } message: {
            Text("Every memory from \(memoryClearPreview?.oldest ?? "?") to \(memoryClearPreview?.newest ?? "?") will be removed from this Mac. Export them first if you may need them. This cannot be undone.")
        }
    }

    /// Tool and provider usage history: whether it is kept, for how long, and clearing it.
    private var usageDataSettings: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Usage history").font(.system(size: 13, weight: .semibold))
            VStack(spacing: 0) {
                UsageSettingRow(title: "Record usage",
                                note: usageRecordingNote) {
                    Toggle("Record usage", isOn: Binding(
                        get: { usageMeteringEnabled },
                        set: { value in Task { await state.updateUsagePrivacy(enabled: value) } }
                    ))
                    .toggleStyle(.switch)
                    .labelsHidden()
                }
                Divider()
                UsageSettingRow(title: "Keep for",
                                note: "Older days are removed on their own.") {
                    Picker("Keep for", selection: Binding(
                        get: { usageRetentionDays },
                        set: { value in Task { await state.updateUsagePrivacy(retentionDays: value) } }
                    )) {
                        Text("30 days").tag(30)
                        Text("90 days").tag(90)
                        Text("365 days").tag(365)
                    }
                    .labelsHidden()
                    .fixedSize()
                }
                Divider()
                UsageSettingRow(title: "Clear usage data",
                                note: "Removes the tool and provider usage history from this Mac.") {
                    Button("Clear Usage Data…", role: .destructive) { confirmClearUsage = true }
                }
            }
            .usageListBox()
            Text("Only daily per-tool counts, sizes and latency buckets are kept, never prompts, arguments or results.")
                .font(.system(size: 12)).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            if let notice = state.usageDataNotice {
                Text(notice).font(.system(size: 12)).foregroundStyle(.secondary)
            }
        }
    }

    /// "Recording since Oct 4." while it records; what turning it on would do while it does not.
    private var usageRecordingNote: String {
        guard usageMeteringEnabled else { return "Off: no new usage is kept. What is already here stays until it expires." }
        if let since = state.usageSummary?.availableSince.flatMap(usageDayDate) {
            return "Recording since \(since.formatted(.dateTime.month(.abbreviated).day().year()))."
        }
        return "Recording."
    }

    /// The memories agents keep: how many and how much, how long they stay, and reviewing, exporting or
    /// deleting them.
    private var memorySettings: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Memory").font(.system(size: 13, weight: .semibold))
            if let memory = state.memoryOverview {
                if memory.count == 0 {
                    Text("No memories are stored.").font(.system(size: 13)).foregroundStyle(.secondary)
                        .padding(.bottom, 4)
                } else {
                    UsageFactsRow(facts: [
                        UsageFact(value: UsageFormat.exact(memory.count), title: memory.count == 1 ? "Memory" : "Memories",
                                  note: "\(UsageFormat.exact(memory.importantCount)) high or critical."),
                        UsageFact(value: memorySpan(memory), title: "Saved between",
                                  note: "The oldest and the newest memory."),
                        UsageFact(value: UsageFormat.bytes(memory.bytesOnDisk), title: "On disk",
                                  note: "Markdown day files and a search index, owner-only."),
                    ])
                    .padding(.bottom, 6)
                }
            }
            VStack(spacing: 0) {
                UsageSettingRow(title: "Keep for",
                                note: "Memories older than this are removed on their own.") {
                    Picker("Keep for", selection: Binding(
                        get: { state.memoryOverview?.retentionDays ?? 0 },
                        set: { value in Task { await state.updateMemoryRetention(days: value) } }
                    )) {
                        Text("Until deleted").tag(0)
                        Text("90 days").tag(90)
                        Text("180 days").tag(180)
                        Text("1 year").tag(365)
                        Text("2 years").tag(730)
                    }
                    .labelsHidden()
                    .fixedSize()
                }
                Divider()
                UsageSettingRow(title: "Keep high and critical",
                                note: (state.memoryOverview?.retentionDays ?? 0) == 0
                                    ? "Applies once memories expire; now they stay until deleted."
                                    : "Important memories stay past the time above.") {
                    Toggle("Keep high and critical", isOn: Binding(
                        get: { state.memoryOverview?.keepImportant ?? true },
                        set: { value in Task { await state.updateMemoryRetention(keepImportant: value) } }
                    ))
                    .toggleStyle(.switch)
                    .labelsHidden()
                    .disabled((state.memoryOverview?.retentionDays ?? 0) == 0)
                }
                Divider()
                // Read, fix or delete one memory or lesson at a time (#60).
                UsageSettingRow(title: "Review memories and lessons",
                                note: "Read, fix or delete them one at a time.") {
                    Button("Review Memories and Lessons…") { reviewingMemory = true }
                }
                Divider()
                UsageSettingRow(title: "Export memories", note: "Saves them all as one JSON file.") {
                    Button("Export Memories…") { exportMemories() }
                }
                Divider()
                UsageSettingRow(title: "Delete all memories", note: "Asks first, and says how many will go.") {
                    Button("Delete All Memories…", role: .destructive) {
                        Task { memoryClearPreview = await state.clearMemory(confirm: false) }
                    }
                    .disabled((state.memoryOverview?.count ?? 0) == 0)
                }
            }
            .usageListBox()
            Text("Deleting removes the text from the day files and the search index; it is not a forensic disk wipe.")
                .font(.system(size: 12)).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            if let notice = state.memoryNotice {
                Text(notice).font(.system(size: 12)).foregroundStyle(.secondary)
            }
        }
        .sheet(isPresented: $reviewingMemory, onDismiss: { Task { await state.refreshMemory() } }) {
            MemoryReviewView().environmentObject(state)
        }
    }

    /// "Sep 7 – Oct 22" (with the years when they differ from this one).
    private func memorySpan(_ memory: MemoryOverview) -> String {
        let dates = [memory.oldest, memory.newest].map { $0.flatMap { usageDayDate(String($0.prefix(10))) } }
        guard let first = dates[0], let last = dates[1] else { return "\(memory.oldest ?? "?") – \(memory.newest ?? "?")" }
        let thisYear = Calendar.current.component(.year, from: Date())
        func short(_ date: Date) -> String {
            Calendar.current.component(.year, from: date) == thisYear
                ? date.formatted(.dateTime.month(.abbreviated).day())
                : date.formatted(.dateTime.month(.abbreviated).day().year())
        }
        return Calendar.current.isDate(first, inSameDayAs: last) ? short(first) : "\(short(first)) – \(short(last))"
    }

    private func exportMemories() {
        let panel = NSSavePanel()
        let stamp = ISO8601DateFormatter().string(from: Date()).prefix(10)
        panel.nameFieldStringValue = "mac-mcp-memories-\(stamp).json"
        panel.allowedContentTypes = [.json]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await state.exportMemory(to: url) }
    }

    private var usagePane: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 13) {
                HStack(alignment: .top) {
                    paneHeader(
                        "Usage",
                        subtitle: "What Mac MCP's tools and your delegated agents used, kept only on this Mac."
                    )
                    Spacer(minLength: 12)
                    if state.usageLoading {
                        ProgressView().controlSize(.small)
                    } else {
                        Button {
                            Task {
                                await state.refreshUsage(actorClass: usageActor.apiValue)
                                await state.refreshProviderUsage(days: providerUsagePeriod.days)
                            }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                        .help("Refresh usage")
                        .accessibilityLabel("Refresh usage")
                    }
                }
                SettingsTabPicker(selection: $tabs.usage, label: "Usage section")
            }
            .padding(.horizontal, 22)
            .padding(.top, 22)
            .padding(.bottom, 12)
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    switch tabs.usage {
                    case .tools:
                        if let issue = state.usageIssue {
                            Label(issue, systemImage: "exclamationmark.triangle")
                                .font(SettingsType.note)
                                .foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }

                        if let usage = state.usageSummary {
                            usageOverviewCard(usage)
                            usageActivityCard(usage)
                            if !usage.topTools.isEmpty {
                                Text("Top tools").font(.system(size: 13, weight: .semibold)).padding(.top, 14)
                                UsageTopToolsList(tools: usage.topTools,
                                                  total: usage.totals.inputTokens + usage.totals.outputTokens)
                            }
                            if let memorySkills = usage.memorySkills, !memorySkills.isEmpty {
                                Text("Memory and skills").font(.system(size: 13, weight: .semibold)).padding(.top, 14)
                                MemorySkillUsageView(usage: memorySkills)
                            }
                        } else if state.usageLoading {
                            GroupBox {
                                HStack {
                                    ProgressView().controlSize(.small)
                                    Text("Loading Usage…")
                                        .font(SettingsType.note)
                                        .foregroundStyle(.secondary)
                                    Spacer()
                                }
                                .padding(.vertical, 8)
                            }
                        } else {
                            GroupBox {
                                Text("Usage becomes available after the first measured MCP tool call.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.vertical, 8)
                            }
                        }
                    case .agents:
                        ProviderUsageSection(
                            summary: state.providerUsageSummary,
                            loading: state.providerUsageLoading,
                            issue: state.providerUsageIssue,
                            period: $providerUsagePeriod,
                            showTeams: { tabs.usage = .teams }
                        )
                        .onChange(of: providerUsagePeriod) { period in
                            Task {
                                await state.refreshProviderUsage(days: period.days)
                            }
                        }
                    case .teams:
                        TeamUsageSection(state: state)
                    case .chat:
                        ChatUsageSection(state: state)
                    case .data:
                        usageStorageTab
                    }
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .id(tabs.usage)  // each tab opens at its top
        }
    }

    /// Mac MCP's own tool traffic in a sentence; how it is measured lives in the info tooltip.
    private func usageOverviewCard(_ usage: UsageSummaryEnvelope) -> some View {
        let totals = usage.totals
        return HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text(UsageFormat.compact(totals.inputTokens + totals.outputTokens))
                        .font(.system(size: 34, weight: .semibold)).monospacedDigit()
                        .help("\(UsageFormat.exact(totals.inputTokens + totals.outputTokens)) tokens")
                    Text("tokens through Mac MCP tools").font(.system(size: 15, weight: .medium)).foregroundStyle(.secondary)
                }
                Text(usageActivityLine(usage)).font(.system(size: 13)).foregroundStyle(.secondary)
                HStack(spacing: 5) {
                    Text("An estimate of the text that went through Mac MCP tools. Not your AI app’s model tokens or bill.")
                    Image(systemName: "info.circle")
                        .help(usageMeasurementHelp(usage))
                        .accessibilityLabel("About tool data processed")
                }
                .font(.system(size: 12)).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
            Spacer(minLength: 12)
            Picker("Calls from", selection: $usageActor) {
                ForEach(UsageActorFilter.allCases) { item in
                    Text(item.rawValue).tag(item)
                }
            }
            .pickerStyle(.menu)
            .fixedSize()
            .help("Count every caller, only your primary assistant, or only delegated subagents")
            .onChange(of: usageActor) { value in
                Task { await state.refreshUsage(actorClass: value.apiValue) }
            }
        }
    }

    /// The days as bars split into results and arguments, then calls, speed and payload with what they mean.
    private func usageActivityCard(_ usage: UsageSummaryEnvelope) -> some View {
        let totals = usage.totals
        let succeeded = totals.calls > 0 ? Double(totals.successCount) / Double(totals.calls) : nil
        let buckets = usageBuckets(usage)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 14) {
                usageLegend("Results", totals.outputTokens, UsageColors.results)
                usageLegend("Arguments", totals.inputTokens, UsageColors.arguments)
                Spacer(minLength: 8)
                Picker("View", selection: $usageAggregation) {
                    ForEach(UsageAggregation.allCases) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .fixedSize()
            }
            .padding(.bottom, 14)
            UsageBarChart(buckets: buckets,
                          series: [UsageSeries(id: "results", name: "Results", color: UsageColors.results),
                                   UsageSeries(id: "arguments", name: "Arguments", color: UsageColors.arguments)],
                          spacing: usageAggregation == .weekly ? 14 : buckets.count > 120 ? 1 : buckets.count > 45 ? 2 : 4,
                          label: "Tool data processed, \(usageAggregation.rawValue.lowercased())")
            UsageFactsRow(facts: [
                UsageFact(value: UsageFormat.compact(totals.calls), title: "Tool calls",
                          note: succeeded.map { "\(usagePercentText($0)) succeeded, \(UsageFormat.exact(totals.errorCount)) failed." }
                              ?? "None yet.",
                          warning: (succeeded ?? 1) < 0.9),
                UsageFact(value: Self.capitalized(UsageFormat.duration(totals.p50LatencyMs, relation: totals.p50LatencyRelation)),
                          title: "Typical response",
                          note: "The slowest 5% took " + UsageFormat.duration(totals.p95LatencyMs, relation: totals.p95LatencyRelation) + ".",
                          help: "Median / slowest 5%: " + latencyPairText(
                              p50: totals.p50LatencyMs, p50Relation: totals.p50LatencyRelation,
                              p95: totals.p95LatencyMs, p95Relation: totals.p95LatencyRelation)),
                UsageFact(value: UsageFormat.bytes(totals.inputBytes + totals.outputBytes), title: "Payload",
                          note: "Including \(UsageFormat.count(totals.imageCount, "image")) and \(UsageFormat.bytes(totals.binaryBytes)) of other files."),
            ])
            .padding(.top, 20)
            if let diagnostics = usage.diagnostics,
               (diagnostics.queueDropped ?? 0) > 0 || (diagnostics.workerErrors ?? 0) > 0 {
                Label(
                    "Some calls were not measured: \(diagnostics.queueDropped ?? 0) dropped, \(diagnostics.workerErrors ?? 0) worker errors.",
                    systemImage: "exclamationmark.triangle"
                )
                .font(.system(size: 12))
                .foregroundStyle(.orange)
                .padding(.top, 8)
            }
        }
        .padding(.top, 8)
    }

    private static func capitalized(_ text: String) -> String { text.prefix(1).uppercased() + text.dropFirst() }

    private func usageLegend(_ title: String, _ value: Int, _ color: Color) -> some View {
        HStack(spacing: 5) {
            RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 8, height: 8)
            Text(title).foregroundStyle(.secondary)
            Text(UsageFormat.compact(value)).fontWeight(.medium).monospacedDigit()
        }
        .font(.system(size: 12))
        .help("\(UsageFormat.exact(value)) tokens")
        .accessibilityElement(children: .combine)
    }

    /// "18,726 tool calls on 22 active days since Oct 4. The busiest was Oct 17."
    private func usageActivityLine(_ usage: UsageSummaryEnvelope) -> String {
        let days = usageDays(usage).filter { $0.calls > 0 || $0.totalTokens > 0 }
        guard !days.isEmpty else { return "No tool calls in the last 12 months." }
        let calls = days.reduce(0) { $0 + $1.calls }
        var text = "\(UsageFormat.count(calls, "tool call")) on \(UsageFormat.count(days.count, "active day"))"
        if let first = usageDayDate(days[0].date) {
            text += " since \(first.formatted(.dateTime.month(.abbreviated).day()))"
        }
        text += "."
        if days.count > 1, let busiest = days.max(by: { $0.totalTokens < $1.totalTokens }),
           let date = usageDayDate(busiest.date) {
            text += " The busiest was \(date.formatted(.dateTime.month(.abbreviated).day()))."
        }
        return text
    }

    private func usageMeasurementHelp(_ usage: UsageSummaryEnvelope) -> String {
        var text = usage.metricScope + " Input = tool arguments; output = tool result. Metric: \(usage.tokenizerId)."
        if let since = ProviderUsageFormat.since(usage.availableSince) {
            text += " Measured since \(since)."
        }
        return text
    }

    private func usagePercentText(_ ratio: Double) -> String {
        let percent = ratio * 100
        if percent >= 99.95 { return "100%" }
        return String(format: "%.1f%%", percent)
    }

    private static let usageDayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()

    private func usageDayDate(_ key: String) -> Date? { Self.usageDayFormatter.date(from: key) }

    /// The last 365 days of daily rows, whatever the chart shows.
    private func usageDays(_ usage: UsageSummaryEnvelope) -> [UsageDay] {
        let calendar = Calendar(identifier: .gregorian)
        let today = calendar.startOfDay(for: Date())
        let start = calendar.date(byAdding: .day, value: -364, to: today) ?? today
        let startKey = Self.usageDayFormatter.string(from: start)
        return usage.daily.filter { $0.date >= startKey }.sorted { $0.date < $1.date }
    }

    /// The chart's bars from the first measured day (at most a year back) to today: one per day, one per
    /// week, or the running total.
    private func usageBuckets(_ usage: UsageSummaryEnvelope) -> [UsageBucket] {
        let calendar = Calendar(identifier: .gregorian)
        let today = calendar.startOfDay(for: Date())
        let yearAgo = calendar.date(byAdding: .day, value: -364, to: today) ?? today
        let firstMeasured = [usage.availableSince, usageDays(usage).first?.date].compactMap { $0 }.compactMap(usageDayDate).min()
        var start = max(yearAgo, firstMeasured ?? today)
        if let fortnight = calendar.date(byAdding: .day, value: -13, to: today), start > fortnight { start = fortnight }
        let byDate = Dictionary(usage.daily.map { ($0.date, $0) }, uniquingKeysWith: { first, _ in first })
        var days: [Date] = []
        var cursor = start
        while cursor <= today {
            days.append(cursor)
            cursor = calendar.date(byAdding: .day, value: 1, to: cursor) ?? today.addingTimeInterval(86_400)
        }
        let name: (Date) -> String = { calendar.isDate($0, inSameDayAs: today) ? "Today" : $0.formatted(.dateTime.month(.abbreviated).day()) }
        func detail(calls: Int, failed: Int) -> String? {
            calls > 0 ? "\(UsageFormat.count(calls, "call")), \(UsageFormat.exact(failed)) failed" : nil
        }
        switch usageAggregation {
        case .daily:
            return days.enumerated().map { index, date in
                let day = byDate[Self.usageDayFormatter.string(from: date)]
                return UsageBucket(id: index, title: name(date), axis: name(date),
                                   parts: ["results": day?.outputTokens ?? 0, "arguments": day?.inputTokens ?? 0],
                                   detail: detail(calls: day?.calls ?? 0, failed: day?.errorCount ?? 0))
            }
        case .weekly:
            var weeks: [(start: Date, calls: Int, failed: Int, results: Int, arguments: Int)] = []
            for date in days {
                let monday = calendar.date(from: calendar.dateComponents([.yearForWeekOfYear, .weekOfYear], from: date)) ?? date
                let day = byDate[Self.usageDayFormatter.string(from: date)]
                if weeks.last?.start != monday { weeks.append((max(monday, start), 0, 0, 0, 0)) }
                weeks[weeks.count - 1].calls += day?.calls ?? 0
                weeks[weeks.count - 1].failed += day?.errorCount ?? 0
                weeks[weeks.count - 1].results += day?.outputTokens ?? 0
                weeks[weeks.count - 1].arguments += day?.inputTokens ?? 0
            }
            return weeks.enumerated().map { index, week in
                let label = week.start.formatted(.dateTime.month(.abbreviated).day())
                return UsageBucket(id: index, title: "Week of \(label)", axis: label,
                                   parts: ["results": week.results, "arguments": week.arguments],
                                   detail: detail(calls: week.calls, failed: week.failed))
            }
        case .cumulative:
            var calls = 0, results = 0, arguments = 0
            return days.enumerated().map { index, date in
                let day = byDate[Self.usageDayFormatter.string(from: date)]
                calls += day?.calls ?? 0
                results += day?.outputTokens ?? 0
                arguments += day?.inputTokens ?? 0
                let isToday = calendar.isDate(date, inSameDayAs: today)
                return UsageBucket(id: index, title: isToday ? "Up to today" : "Up to \(name(date))", axis: name(date),
                                   parts: ["results": results, "arguments": arguments],
                                   detail: calls > 0 ? "\(UsageFormat.count(calls, "call")) in all" : nil)
            }
        }
    }

    private func compactNumber(_ value: Int) -> String {
        let absolute = Double(abs(value))
        if absolute >= 1_000_000_000 {
            return String(format: "%.1fB", Double(value) / 1_000_000_000)
        }
        if absolute >= 1_000_000 {
            return String(format: "%.1fM", Double(value) / 1_000_000)
        }
        if absolute >= 1_000 {
            return String(format: "%.1fK", Double(value) / 1_000)
        }
        return "\(value)"
    }

    private func byteText(_ value: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(max(0, value)), countStyle: .file)
    }

    private func latencyPairText(
        p50: Int?,
        p50Relation: String?,
        p95: Int?,
        p95Relation: String?
    ) -> String {
        "\(latencyText(p50, relation: p50Relation)) / \(latencyText(p95, relation: p95Relation))"
    }

    private func latencyText(_ value: Int?, relation: String?) -> String {
        guard let value else { return "—" }
        let prefix = relation == "gte" ? "≥" : (relation == "lt" ? "<" : "")
        if value >= 1000 {
            return prefix + String(format: "%.1fs", Double(value) / 1000)
        }
        return "\(prefix)\(value)ms"
    }

    private var browserPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                sectionLead(
                    "Agents in Safari and Chrome",
                    subtitle: "Turn these on so agents can work in your browser tabs in the background, and you can see which page they are using."
                )

                settingsDataStateRow(state.browserSettingsState) {
                    state.refreshSafariExtensionState()
                }

                VStack(spacing: 0) {
                    let chrome = state.chromeCompanion
                    let ready = chrome.connected && !chrome.needsReload
                    companionRow(
                        bundleID: "com.apple.Safari", symbol: "safari", title: "Safari Visual Companion",
                        status: state.safariExtensionEnabled ? "On" : state.safariExtensionStatus,
                        statusColor: state.safariExtensionEnabled ? .green : .secondary,
                        detail: state.safariExtensionEnabled
                            ? "Shows when Mac MCP is actively using a Safari page."
                            : (state.safariExtensionRegistered
                               ? "Enable the bundled extension in Safari, then allow website access."
                               : "Unsigned build: Safari developer setup is required.")
                    ) {
                        if state.safariExtensionEnabled {
                            Button { state.refreshSafariExtensionState() } label: { Image(systemName: "arrow.clockwise") }
                                .accessibilityLabel("Refresh Safari extension status")
                                .help("Refresh extension status")
                        } else if state.safariExtensionRegistered {
                            Button("Enable in Safari…") { state.openSafariExtensionPreferences() }
                        } else {
                            Button("Developer Setup…") { state.openSafariExtensionPreferences() }
                        }
                    }
                    Divider().padding(.leading, 58)
                    companionRow(
                        bundleID: "com.google.Chrome", symbol: "globe", title: "Chrome Visual Companion",
                        status: ready ? "Connected" + (chrome.version.map { " · \($0)" } ?? "")
                            : (chrome.connected ? "Needs a reload" : "Not connected"),
                        statusColor: ready ? .green : (chrome.connected ? UsageColors.warning : .secondary),
                        detail: chromeDetail(chrome)
                    ) {
                        if ready {
                            Button { Task { await state.refreshChromeCompanion() } } label: { Image(systemName: "arrow.clockwise") }
                                .accessibilityLabel("Refresh Chrome companion status")
                                .help("Refresh companion status")
                        } else if chrome.connected {
                            Button("Reload in Chrome…") { state.openChromeExtensionSetup() }
                        } else {
                            Button("Chrome Setup…") { state.openChromeExtensionSetup() }
                        }
                    }
                }
                .usageListBox()

                Spacer(minLength: 0)
            }
            .padding(22)
        }
        // Live while the pane is open: a companion turned on in Safari or Chrome shows up by itself.
        .task {
            state.refreshSafariExtensionState(quiet: true)
            while !Task.isCancelled {
                await state.refreshChromeCompanion()
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                state.refreshSafariExtensionState(quiet: true)
            }
        }
    }

    /// One browser companion: the browser's own icon, its state as a dot and a word, what that means, and the next step.
    private func companionRow<Control: View>(
        bundleID: String, symbol: String, title: String, status: String, statusColor: Color, detail: String,
        @ViewBuilder control: () -> Control
    ) -> some View {
        HStack(spacing: 12) {
            if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
                Image(nsImage: NSWorkspace.shared.icon(forFile: url.path)).resizable().frame(width: 32, height: 32)
                    .accessibilityHidden(true)
            } else {
                UsageSymbolTile(symbol: symbol)
            }
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(title).font(SettingsType.title)
                    HStack(spacing: 5) {
                        Circle().fill(statusColor).frame(width: 6, height: 6)
                        Text(status).font(SettingsType.meta).foregroundStyle(.secondary)
                    }
                }
                Text(detail).font(SettingsType.note).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            control()
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .accessibilityElement(children: .contain)
    }

    private func chromeDetail(_ chrome: ChromeCompanionStatus) -> String {
        if chrome.connected && chrome.needsReload {
            return "Older than this Mac MCP: reload it once at chrome://extensions."
        }
        if chrome.connected { return "Chrome tabs work in the background: page actions and capture without bringing Chrome forward." }
        if !chrome.checked {
            return state.serverRunning ? "Checking Chrome…" : "Start Mac MCP to see whether the Chrome companion is connected."
        }
        if !state.chromeInstalled { return "Google Chrome is not installed (Chrome Beta, Canary and Chromium work too). Safari works without it." }
        return "Chrome Setup loads the companion once; after that Chrome tabs work in the background."
    }

    private var permissionsPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader("Permissions & Safety", subtitle: "What your AI apps may do on this Mac, and when Mac MCP asks you first.")

                settingsDataStateRow(state.permissionsSettingsState) {
                    Task { await state.refresh() }
                }
                if let issue = state.permissionActionIssue {
                    inlineError(issue)
                }

                if let semantics = state.securitySemantics {
                    if let profile = semantics.profiles.first(where: { $0.name == semantics.activeProfile }) {
                        if semantics.profileWasNormalized == true,
                           let configured = semantics.normalizedFromProfile ?? semantics.configuredProfile {
                            Label(
                                "Configured \(profileDisplayName(configured)) is not a global server preset. "
                                + "Mac MCP is safely using \(profileDisplayName(semantics.activeProfile)).",
                                systemImage: "exclamationmark.shield"
                            )
                            .font(SettingsType.note.weight(.medium))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                        if let serverApproval = semantics.serverApproval, !serverApproval.configValid {
                            Label(
                                "Invalid Server Approval configuration. High-risk calls fail closed until repaired.",
                                systemImage: "exclamationmark.shield"
                            )
                            .font(SettingsType.note.weight(.medium))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                        }

                        // #246: the plain answer first; the policy terms live under Details.
                        GroupBox("What AI apps can do") {
                            VStack(alignment: .leading, spacing: 4) {
                                // The checked row is the answer; its sentence says what it allows.
                                ForEach(semantics.profiles) { item in
                                    if item.id != semantics.profiles.first?.id { Divider() }
                                    choiceRow(title: profileDisplayName(item.name),
                                              detail: SafetyDisplay.profileSummary(item.name) ?? item.approvalBehavior.summary,
                                              selected: item.active) {
                                        state.setPermissionProfile(item.name)
                                    }
                                    // #17: while a change is saved the rows wait, and say so.
                                    .disabled(state.permissionProfileChanging)
                                }
                                if state.permissionProfileChanging {
                                    HStack(spacing: 6) {
                                        ProgressView().controlSize(.mini)
                                        Text("Saving…").font(SettingsType.note).foregroundStyle(.secondary)
                                    }
                                    .accessibilityElement(children: .combine)
                                }
                            }
                            .padding(.top, 5)
                        }

                        if let serverApproval = semantics.serverApproval {
                            GroupBox("When Mac MCP asks you first") {
                                VStack(alignment: .leading, spacing: 8) {
                                    ForEach(serverApproval.availableProfiles) { item in
                                        if item.id != serverApproval.availableProfiles.first?.id { Divider() }
                                        choiceRow(title: profileDisplayName(item.name),
                                                  detail: SafetyDisplay.approvalSummary(item.name) ?? item.summary,
                                                  selected: item.name == serverApproval.activeProfile) {
                                            state.setServerApprovalProfile(item.name)
                                        }
                                        .disabled(state.serverApprovalProfileChanging)
                                    }
                                    Text("You answer on this Mac; someone connected from elsewhere can't answer for you. Your AI app may also ask you on its own, which is separate.")
                                        .font(SettingsType.note)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                .padding(.top, 5)
                            }
                        }

                        SavedLoginsCard(state: state)

                        DisclosureGroup("Details", isExpanded: $permissionDetailsShown) {
                            VStack(alignment: .leading, spacing: 12) {
                                GroupBox("Active Profile") {
                                    VStack(alignment: .leading, spacing: 10) {
                                        HStack {
                                            Text("Profile").foregroundStyle(.secondary)
                                            Spacer()
                                            Text(profileDisplayName(profile.name)).fontWeight(.semibold)
                                        }
                                        Divider()
                                        Label("Allowed Capabilities", systemImage: "lock.shield")
                                            .font(SettingsType.note.weight(.semibold))
                                        Text(profile.allowedCapabilities.map(capabilityDisplayName).joined(separator: " · "))
                                            .font(SettingsType.note).foregroundStyle(.secondary)
                                            .fixedSize(horizontal: false, vertical: true)
                                        HStack {
                                            Text("Destructive families").font(SettingsType.note).foregroundStyle(.secondary)
                                            Spacer()
                                            Text(destructiveFamiliesLabel(profile.destructiveFamilies)).font(SettingsType.note.weight(.medium))
                                        }
                                        HStack {
                                            Text("Agent access ceiling").font(SettingsType.note).foregroundStyle(.secondary)
                                            Spacer()
                                            Text(capabilityDisplayName(profile.accessModeCeiling)).font(SettingsType.note.weight(.medium))
                                        }
                                    }
                                    .padding(.top, 5)
                                }

                                GroupBox("Approval Behavior") {
                                    VStack(alignment: .leading, spacing: 9) {
                                        HStack {
                                            Text("Source").font(SettingsType.note).foregroundStyle(.secondary)
                                            Spacer()
                                            Text(approvalSourceDisplayName(profile.approvalBehavior.source))
                                                .font(SettingsType.note.weight(.semibold))
                                        }
                                        Text(profile.approvalBehavior.summary)
                                            .font(SettingsType.note).foregroundStyle(.secondary)
                                            .fixedSize(horizontal: false, vertical: true)
                                        Text("Capability allowed means the server permits it. Approval is separate; a confirmation prompt is not guaranteed.")
                                            .font(SettingsType.note.weight(.medium)).foregroundStyle(.orange)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                    .padding(.top, 5)
                                }

                                if let serverApproval = semantics.serverApproval {
                                    GroupBox("Server Approval") {
                                        VStack(alignment: .leading, spacing: 10) {
                                            HStack {
                                                Text("Risk profile").font(SettingsType.note).foregroundStyle(.secondary)
                                                Spacer()
                                                Text(profileDisplayName(serverApproval.activeProfile))
                                                    .font(SettingsType.note.weight(.semibold))
                                            }
                                            HStack {
                                                Text("Approval source").font(SettingsType.note).foregroundStyle(.secondary)
                                                Spacer()
                                                Text(approvalSourceDisplayName(serverApproval.source))
                                                    .font(SettingsType.note.weight(.medium))
                                            }
                                            HStack {
                                                Text("Headless behavior").font(SettingsType.note).foregroundStyle(.secondary)
                                                Spacer()
                                                Text(serverApproval.headlessBehavior.capitalized)
                                                    .font(SettingsType.note.weight(.medium))
                                            }
                                            HStack {
                                                Text("Timeout").font(SettingsType.note).foregroundStyle(.secondary)
                                                Spacer()
                                                Text("\(serverApproval.approvalTimeoutS)s · \(serverApproval.timeoutBehavior.capitalized)")
                                                    .font(SettingsType.note.weight(.medium))
                                            }
                                            HStack {
                                                Text("Remote sessions").font(SettingsType.note).foregroundStyle(.secondary)
                                                Spacer()
                                                Text("Approve on this Mac")
                                                    .font(SettingsType.note.weight(.medium))
                                            }
                                            Text(serverApproval.summary ?? "Risk-based server approval status unavailable.")
                                                .font(SettingsType.note).foregroundStyle(.secondary)
                                                .fixedSize(horizontal: false, vertical: true)

                                            Text(serverApproval.doublePromptGuidance)
                                                .font(SettingsType.meta.weight(.medium))
                                                .foregroundStyle(.orange)
                                                .fixedSize(horizontal: false, vertical: true)
                                            Text("Remote callers cannot approve locally on their behalf. Allow Once is exact-action and single-use.")
                                                .font(SettingsType.note)
                                                .foregroundStyle(.secondary)
                                                .fixedSize(horizontal: false, vertical: true)
                                        }
                                        .padding(.top, 5)
                                    }
                                }
                            }
                            .padding(.top, 8)
                        }
                        .font(SettingsType.note.weight(.semibold))
                    } else {
                        Text("Unknown permission profile: \(semantics.activeProfile). Calls fail closed until a known profile is configured.")
                            .font(SettingsType.note).foregroundStyle(.orange)
                    }
                } else {
                    SavedLoginsCard(state: state)
                    Text(state.serverRunning ? "Permission semantics unavailable." : "Start the server to inspect permission semantics.")
                        .font(SettingsType.note).foregroundStyle(.secondary)
                }

                Spacer(minLength: 0)
            }
            .padding(22)
        }
    }

    /// One choice of several, as System Settings lists them: its symbol, what it means, and a check on the chosen one.
    private func choiceRow(title: String, detail: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(alignment: .center, spacing: 12) {
                Image(systemName: Self.choiceSymbol(title))
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                    .frame(width: 28, height: 28)
                    .background(Circle().fill(selected ? Color.accentColor.opacity(0.14) : Color.primary.opacity(0.05)))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(SettingsType.title)
                    Text(detail)
                        .font(SettingsType.note)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 17))
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary.opacity(0.5))
                    .accessibilityHidden(true)
            }
            .padding(.vertical, 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    /// A symbol for each permission profile and approval level, by its display name.
    private static func choiceSymbol(_ title: String) -> String {
        switch title.lowercased() {
        case "trusted": return "checkmark.shield"
        case "standard": return "shield.lefthalf.filled"
        case "read only": return "eye"
        case "off": return "nosign"
        case "critical": return "exclamationmark.octagon"
        case "high risk": return "exclamationmark.triangle"
        default: return "shield"
        }
    }

    private var connectionsPane: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 13) {
                paneHeader(
                    "Connections",
                    subtitle: "Browser companions, mobile access, and the public endpoint used to reach this Mac."
                )

                SettingsTabPicker(selection: $connectionsTab, label: "Connections section")

                publicEndpointCard
            }
            .padding(.horizontal, 22)
            .padding(.top, 22)
            .padding(.bottom, 12)

            Divider()

            switch connectionsTab {
            case .browser: browserPane
            case .mobile: mobilePane
            case .clients: clientsPane
            }
        }
    }

    private var publicEndpointCard: some View {
        GroupBox("Public Endpoint") {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Mode")
                        .foregroundStyle(.secondary)
                        .frame(width: 92, alignment: .leading)
                    Picker("", selection: $settings.publicEndpointMode) {
                        Text("Local only").tag("none")
                        Text("ngrok").tag("ngrok")
                        Text("Cloudflare").tag("cloudflare")
                        Text("Custom HTTPS").tag("custom")
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .onChange(of: settings.publicEndpointMode) { _ in
                        settings.ngrokOnStart = settings.publicEndpointMode == "ngrok"
                        persistEndpointSettings()
                    }
                }

                if settings.publicEndpointMode == "custom" || settings.publicEndpointMode == "cloudflare" {
                    HStack {
                        Text("Public URL")
                            .foregroundStyle(.secondary)
                            .frame(width: 92, alignment: .leading)
                        TextField("https://example.com/mcp", text: $settings.publicURL)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit { persistEndpointSettings() }
                    }
                    if let issue = publicURLValidationMessage {
                        inlineError(issue, leadingInset: 92)
                    }
                }

                if settings.publicEndpointMode == "cloudflare" {
                    HStack {
                        Text("Credential")
                            .foregroundStyle(.secondary)
                            .frame(width: 92, alignment: .leading)
                        SecureField("Paste once; it is never written to settings.json", text: $cloudflareToken)
                            .textFieldStyle(.roundedBorder)
                        Button(state.cloudflareCredentialConfigured ? "Replace credential" : "Save credential") {
                            let token = cloudflareToken
                            cloudflareToken = ""
                            state.saveCloudflareCredential(token)
                        }
                        .disabled(
                            cloudflareToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                || state.busyAction != nil
                        )
                    }

                    HStack(spacing: 8) {
                        Text("").frame(width: 92)
                        Label(
                            state.cloudflareCredentialConfigured ? "Credential configured" : "Credential not configured",
                            systemImage: state.cloudflareCredentialConfigured ? "checkmark.shield.fill" : "exclamationmark.shield"
                        )
                        .foregroundStyle(state.cloudflareCredentialConfigured ? Color.secondary : Color.orange)
                        Spacer()
                    }
                    .font(SettingsType.note)

                    DisclosureGroup("Tunnel details") {
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Text("Named tunnel")
                                    .foregroundStyle(.secondary)
                                    .frame(width: 92, alignment: .leading)
                                TextField("Optional name or UUID", text: $settings.cloudflareTunnel)
                                    .textFieldStyle(.roundedBorder)
                                    .onSubmit { persistEndpointSettings() }
                            }
                            Text("The credential stays in an owner-only file and is never written into settings.json.")
                                .font(SettingsType.meta)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.top, 6)
                    }
                    .font(SettingsType.note)
                }

                saveFeedbackRow(.endpoint)
            }
            .padding(.top, 5)
        }
    }

    /// One Audio row: its name on the left, the control on the right.
    private func voiceRow<Name: View, Control: View>(
        @ViewBuilder name: () -> Name, @ViewBuilder control: () -> Control
    ) -> some View {
        HStack(spacing: 16) {
            name().font(SettingsType.body)
            Spacer(minLength: 12)
            control()
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
    }

    private var voicePane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader("Voice", subtitle: "Configure the experimental Ask User Voice tool and audio devices.")
                saveFeedbackRow(.voice)

                GroupBox("Voice Questions") {
                    VStack(alignment: .leading, spacing: 0) {
                        Toggle(isOn: $settings.voiceEnabled) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Ask User Voice").font(SettingsType.body)
                                Text("Off by default. Agents fall back to a text question (ask_user) when this is off or you decline a recording.")
                                    .font(SettingsType.note).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .toggleStyle(.switch)
                        .padding(.bottom, 10)
                        .onChange(of: settings.voiceEnabled) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }

                        Divider()

                        Toggle(isOn: $settings.voiceAskEveryTime) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Ask before every recording").font(SettingsType.body)
                                Text(settings.voiceAskEveryTime
                                     ? "A dialog asks you to Record, Always Allow or decline before anything is recorded or sent."
                                     : "Recordings are sent without asking because you chose Always Allow. Turn this on to be asked again.")
                                    .font(SettingsType.note).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .toggleStyle(.switch)
                        .padding(.vertical, 10)
                        .disabled(!settings.voiceEnabled)
                        .onChange(of: settings.voiceAskEveryTime) { _ in
                            // Values re-read from disk are not edits and are not written back.
                            if settings.voiceConsentEdited { persistSettings(scope: .voice, success: "Saved · applies live") }
                        }

                        Divider()

                        Label {
                            Text("Each voice question sends its text to Microsoft's online text-to-speech, and your recorded answer to Groq for transcription; Groq's own retention applies. The recording is deleted from this Mac afterwards and the transcript is not kept in Mac MCP's activity history.")
                                .fixedSize(horizontal: false, vertical: true)
                        } icon: {
                            Image(systemName: "network")
                        }
                        .font(SettingsType.note)
                        .foregroundStyle(.secondary)
                        .padding(.top, 10)
                    }
                }

                GroupBox("Transcription") {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(spacing: 6) {
                            Circle().fill(settings.hasGroqKey ? Color.green : UsageColors.warning).frame(width: 7, height: 7)
                            Text(settings.hasGroqKey ? "Groq key configured" : "Groq key required")
                                .font(SettingsType.body)
                            Spacer()
                            Text("Stored in your Keychain").font(SettingsType.meta).foregroundStyle(.secondary)
                        }
                        HStack {
                            SecureField(settings.hasGroqKey ? "Replace Groq API key" : "Groq API key", text: $groqKey)
                                .textFieldStyle(.roundedBorder)
                                .accessibilityLabel("Groq API key")
                            Button("Save") { saveGroqKey() }
                                .disabled(groqKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            if settings.hasGroqKey {
                                Button { removeGroqKey() } label: { Image(systemName: "trash") }
                                    .accessibilityLabel("Remove Groq API key")
                                    .help("Remove key")
                            }
                        }
                    }
                }

                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("Audio").font(SettingsType.title)
                        Spacer()
                        Button { audio.refresh() } label: { Image(systemName: "arrow.clockwise") }
                            .buttonStyle(.borderless)
                            .accessibilityLabel("Refresh audio devices")
                            .help("Refresh audio devices")
                    }
                    VStack(spacing: 0) {
                        // #21: the picker carries the name VoiceOver reads; the visible label is not read twice.
                        voiceRow {
                            Text("Input").foregroundStyle(.secondary).accessibilityHidden(true)
                        } control: {
                            Picker("Input device", selection: $settings.inputDevice) {
                                Text("Auto").tag("auto")
                                Text("Built-in Mic").tag("built-in")
                                ForEach(audio.inputs) { device in Text(device.name).tag(device.settingValue) }
                            }
                            .labelsHidden()
                            .frame(maxWidth: 260)
                        }
                        Divider().padding(.leading, 14)
                        voiceRow {
                            Text("Output").foregroundStyle(.secondary).accessibilityHidden(true)
                        } control: {
                            Picker("Output device", selection: $settings.outputDevice) {
                                Text("System").tag("system")
                                Text("Built-in Speakers").tag("built-in")
                                ForEach(audio.outputs) { device in Text(device.name).tag(device.settingValue) }
                            }
                            .labelsHidden()
                            .frame(maxWidth: 260)
                        }
                        Divider().padding(.leading, 14)
                        voiceRow {
                            Text("Language").foregroundStyle(.secondary).accessibilityHidden(true)
                        } control: {
                            Picker("Language", selection: $settings.language) {
                                Text("Auto").tag("auto")
                                Text("Turkish").tag("tr")
                                Text("English").tag("en")
                            }
                            .labelsHidden()
                            .frame(maxWidth: 160)
                        }
                        Divider().padding(.leading, 14)
                        voiceRow {
                            Text("Answer time").foregroundStyle(.secondary).accessibilityHidden(true)
                        } control: {
                            Stepper("\(settings.timeoutSeconds) s", value: $settings.timeoutSeconds, in: 5...180, step: 5)
                                .monospacedDigit()
                                .fixedSize()
                                .accessibilityLabel("Answer time in seconds")
                        }
                        Divider().padding(.leading, 14)
                        voiceRow {
                            Text("Voice").foregroundStyle(.secondary).accessibilityHidden(true)
                        } control: {
                            // #125: typed values are saved on Return or when the field is left, and only if
                            // Microsoft's text-to-speech would take them.
                            HStack {
                                TextField("tr-TR-AhmetNeural", text: Binding(
                                    get: { voiceNameDraft ?? settings.voiceName }, set: { voiceNameDraft = $0 }))
                                    .textFieldStyle(.roundedBorder)
                                    .frame(maxWidth: 220)
                                    .focused($voiceField, equals: .name)
                                    .onSubmit { commitVoiceName() }
                                    .help(VoiceSettingsFormat.voiceHint)
                                    .accessibilityLabel("Voice name")
                                Text("Rate").foregroundStyle(.secondary).accessibilityHidden(true)
                                TextField("-5%", text: Binding(
                                    get: { ttsRateDraft ?? settings.ttsRate }, set: { ttsRateDraft = $0 })).frame(width: 58)
                                    .textFieldStyle(.roundedBorder)
                                    .focused($voiceField, equals: .rate)
                                    .onSubmit { commitTTSRate() }
                                    .help(VoiceSettingsFormat.rateHint)
                                    .accessibilityLabel("Speech rate")
                            }
                        }
                        VStack(alignment: .leading, spacing: 3) {
                            Text("Voice is a name such as tr-TR-AhmetNeural or en-US-AriaNeural; rate is slower or faster than normal, such as -5% or +10%. Each is saved when you press Return or leave its field.")
                                .font(SettingsType.note).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            ForEach([voiceNameProblem, ttsRateProblem].compactMap { $0 }, id: \.self) { problem in
                                Label(problem, systemImage: "exclamationmark.triangle.fill")
                                    .font(SettingsType.note).foregroundStyle(.red)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 14).padding(.bottom, 10)
                    }
                    .usageListBox()
                }
                .onChange(of: settings.inputDevice) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }
                .onChange(of: settings.outputDevice) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }
                .onChange(of: settings.language) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }
                .onChange(of: settings.timeoutSeconds) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }
                .onChange(of: voiceField) { [voiceField] _ in
                    // Leaving a field commits it, the same as Return.
                    if voiceField == .name { commitVoiceName() }
                    if voiceField == .rate { commitTTSRate() }
                }
                .onDisappear {
                    commitVoiceName()
                    commitTTSRate()
                }

                Spacer(minLength: 0)
            }
            .padding(22)
        }
    }

    private var clientsPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                sectionLead(
                    "Connect an AI Client",
                    subtitle: "Mac MCP adds itself to the AI apps on this Mac: no address or key goes into their settings."
                )

                ConnectAppsBox(state: state)

                DisclosureGroup("Manual setup", isExpanded: $manualSetupExpanded) {
                GroupBox {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Picker("Client", selection: $connectClient) {
                                ForEach(connectClients, id: \.id) { client in
                                    Text(client.title).tag(client.id)
                                }
                            }
                            .frame(maxWidth: 260)
                            .onChange(of: connectClient) { _ in
                                connectOutput = ""
                                Task { await generateConnectConfig() }
                            }
                            Spacer()
                            Button {
                                Task { await generateConnectConfig() }
                            } label: {
                                if connectLoading {
                                    ProgressView().controlSize(.small)
                                } else {
                                    Label("Generate", systemImage: "arrow.clockwise")
                                }
                            }
                            .disabled(connectLoading)
                            Button {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(connectOutput, forType: .string)
                            } label: {
                                Label("Copy", systemImage: "doc.on.doc")
                            }
                            .disabled(connectOutput.isEmpty || connectFailed)
                        }

                        if connectOutput.isEmpty {
                            Text(connectLoading ? "Generating…" : "Choose a client to see its setup.")
                                .font(SettingsType.note)
                                .foregroundStyle(.secondary)
                        } else {
                            Text(connectOutput)
                                .font(.system(size: 11, design: .monospaced))
                                .foregroundStyle(connectFailed ? Color.orange : Color.primary)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(8)
                                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
                        }
                    }
                    .padding(.vertical, 2)
                } label: {
                    Label("Client Setup", systemImage: "link")
                }
                .padding(.top, 6)
                }
                .onChange(of: manualSetupExpanded) { expanded in
                    if expanded && connectOutput.isEmpty { Task { await generateConnectConfig() } }
                }

                Text("This connects an AI app to Mac MCP. Delegated agents that Mac MCP starts (Codex, OpenCode, Claude Code) are configured in Agents.")
                    .font(SettingsType.meta)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(22)
        }
    }

    private func generateConnectConfig() async {
        connectLoading = true
        let requested = connectClient
        let result = await state.runCLICommand(["connect-config", "--client", requested])
        guard requested == connectClient else { return }
        connectLoading = false
        connectFailed = result.code != 0
        let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        connectOutput = text.isEmpty ? "mac-mcp connect-config returned no output (exit \(result.code))." : text
    }

    private var mobilePane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                sectionLead(
                    "Mobile Access",
                    subtitle: "Pair an iPhone or iPad with this Mac MCP instance for a secure read-only dashboard."
                )

                settingsDataStateRow(state.mobileSettingsState) {
                    Task { await state.refreshMobileDevices() }
                }

                GroupBox("Pair a device") {
                    VStack(alignment: .leading, spacing: 12) {
                        if settings.publicEndpointMode == "none" {
                            Label(
                                "Choose a public endpoint above before pairing a phone.",
                                systemImage: "exclamationmark.triangle"
                            )
                            .font(SettingsType.note)
                            .foregroundStyle(.orange)
                        } else {
                            HStack {
                                Button {
                                    Task { await state.createMobilePairing() }
                                } label: {
                                    if state.mobilePairingLoading {
                                        ProgressView().controlSize(.small)
                                    } else {
                                        Label("Pair New Device", systemImage: "qrcode")
                                    }
                                }
                                .disabled(state.mobilePairingLoading)
                                Spacer()
                                Button {
                                    Task { await state.refreshMobileDevices() }
                                } label: {
                                    Image(systemName: "arrow.clockwise")
                                }
                                .help("Refresh connected devices")
                                .accessibilityLabel("Refresh connected devices")
                            }

                            if let issue = state.mobilePairingIssue {
                                inlineError(issue)
                            }

                            if let pairingURL = state.mobilePairingURL,
                               let image = qrImage(for: pairingURL) {
                                HStack(alignment: .top, spacing: 18) {
                                    Image(nsImage: image)
                                        .interpolation(.none)
                                        .resizable()
                                        .frame(width: 176, height: 176)
                                        .padding(10)
                                        .background(Color.white, in: RoundedRectangle(cornerRadius: 14))

                                    VStack(alignment: .leading, spacing: 9) {
                                        Text("Scan with Camera")
                                            .font(SettingsType.title)
                                        Text("Or pair from the Home Screen app with this one-time code:")
                                            .font(SettingsType.note)
                                            .foregroundStyle(.secondary)

                                        if let code = state.mobilePairingCode {
                                            Text(code)
                                                .font(.system(size: 22, weight: .semibold, design: .monospaced))
                                                .tracking(2)
                                                .textSelection(.enabled)
                                                .padding(.vertical, 5)
                                        }

                                        Text("The QR secret stays in the URL fragment; the manual code is short-lived and single-use.")
                                            .font(SettingsType.note)
                                            .foregroundStyle(.secondary)
                                            .fixedSize(horizontal: false, vertical: true)
                                        if let expiresAt = state.mobilePairingExpiresAt {
                                            Text("Expires " + Date(timeIntervalSince1970: expiresAt).formatted(date: .omitted, time: .shortened))
                                                .font(SettingsType.meta)
                                                .foregroundStyle(.secondary)
                                        }
                                        Text("After pairing, the device receives its own read-only session. Your MCP API key and local dashboard token are never sent to the phone.")
                                            .font(SettingsType.meta)
                                            .foregroundStyle(.secondary)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                }
                            }
                        }
                    }
                    .padding(.top, 5)
                }

                GroupBox("Connected Devices") {
                    VStack(alignment: .leading, spacing: 8) {
                        if state.mobileDevices.isEmpty {
                            switch state.mobileSettingsState.phase {
                            case .loading:
                                HStack(spacing: 7) {
                                    ProgressView().controlSize(.small)
                                    Text("Loading connected devices…")
                                }
                                .font(SettingsType.note)
                                .foregroundStyle(.secondary)
                                .padding(.vertical, 6)
                            case .error, .unavailable:
                                Text("Connected devices could not be loaded. Use Retry above.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.orange)
                                    .padding(.vertical, 6)
                            default:
                                Text("No paired mobile devices.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .padding(.vertical, 6)
                            }
                        } else {
                            ForEach(state.mobileDevices) { device in
                                HStack(spacing: 10) {
                                    Image(systemName: "iphone")
                                        .foregroundStyle(.secondary)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(device.deviceName)
                                            .font(SettingsType.body.weight(.medium))
                                        Text("Last seen " + Date(timeIntervalSince1970: device.lastSeenAt).formatted(date: .abbreviated, time: .shortened))
                                            .font(SettingsType.meta)
                                            .foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Text("Read-only")
                                        .font(SettingsType.meta.weight(.semibold))
                                        .padding(.horizontal, 6)
                                        .padding(.vertical, 2)
                                        .background(.quaternary, in: Capsule())
                                    Button("Revoke") {
                                        Task { await state.revokeMobileDevice(device.deviceID) }
                                    }
                                }
                                .padding(.vertical, 4)
                                if device.id != state.mobileDevices.last?.id {
                                    Divider()
                                }
                            }
                        }
                    }
                    .padding(.top, 5)
                }

                if let issue = state.mobileDeviceActionIssue {
                    inlineError(issue)
                }

                Text("The public /mobile page can be reached through the selected connector, but agent, session, and activity APIs require a paired device session.")
                    .font(SettingsType.meta)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                Spacer(minLength: 0)
            }
            .padding(22)
        }
    }

    private var advancedPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader(
                    "Advanced",
                    subtitle: "Developer and runtime controls. Most users should not need to change these."
                )

                GroupBox("Runtime") {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text("Port")
                                .foregroundStyle(.secondary)
                                .frame(width: 82, alignment: .leading)
                            TextField("8000", value: $settings.serverPort, format: .number.grouping(.never))
                                .frame(width: 100)
                                .onSubmit {
                                    persistRuntimeSettings()
                                    Task { await state.refresh() }
                                }
                            Spacer()
                            Text("Restart required")
                                .font(SettingsType.meta.weight(.medium))
                                .foregroundStyle(.secondary)
                        }

                        HStack {
                            Text("CLI path")
                                .foregroundStyle(.secondary)
                                .frame(width: 82, alignment: .leading)
                            TextField("Auto-detect", text: $settings.cliPath)
                                .font(.system(.caption, design: .monospaced))
                                .textFieldStyle(.roundedBorder)
                                .onSubmit { persistRuntimeSettings() }
                        }

                        if let issue = serverPortValidationMessage {
                            inlineError(issue)
                        }
                        if let issue = cliPathValidationMessage {
                            inlineError(issue)
                        }
                        saveFeedbackRow(.runtime)
                    }
                    .padding(.top, 5)
                }

                GroupBox("Session Lifecycle") {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Steering session visibility")
                                    .font(SettingsType.title)
                                Text("How long idle and ended sessions stay visible as neutral history in the control surface.")
                                    .font(SettingsType.meta)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Stepper(
                                "\(settings.steeringSessionMinutes) min",
                                value: $settings.steeringSessionMinutes,
                                in: 1...120,
                                step: 1
                            )
                            .frame(width: 150)
                            .onChange(of: settings.steeringSessionMinutes) { minutes in applySessionRetention(minutes) }
                        }
                        .padding(.top, 5)
                        saveFeedbackRow(.sessions)
                    }
                }

                decisionAccelerationBox

                Label(
                    "Changes here affect runtime behavior; public endpoint and mobile/browser connectivity now live under Connections.",
                    systemImage: "wrench.and.screwdriver"
                )
                .font(SettingsType.note)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

                Spacer(minLength: 0)
            }
            .padding(22)
        }
    }

    private var decisionAccelerationBox: some View {
        GroupBox("Decision Acceleration") {
            VStack(alignment: .leading, spacing: 12) {
                // #125: where it is used is the one control; Off turns it off.
                HStack(alignment: .center, spacing: 16) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Use OpenAI Decisions")
                            .font(SettingsType.title)
                        Text("When several buttons or fields look equally right, Mac MCP can ask OpenAI which one you meant: choose where, or Off to keep Mac MCP's own choice.")
                            .font(SettingsType.meta)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 12)
                    Picker("Use OpenAI Decisions", selection: decisionScopeBinding) {
                        ForEach(DecisionScope.allCases) { scope in Text(scope.title).tag(scope) }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .fixedSize()
                    .accessibilityLabel("Use OpenAI Decisions")
                }

                HStack {
                    Label(decisionKeyStatusText, systemImage: decisionKeyStatusSymbol)
                        .font(SettingsType.note)
                        .foregroundStyle(decisionKeyStatusColor)
                    Spacer()
                    if state.decisionVerifying {
                        ProgressView().controlSize(.small)
                    }
                }

                HStack {
                    SecureField(settings.hasDecisionsKey ? "Replace OpenAI API key" : "OpenAI API key", text: $decisionsKey)
                    Button("Save") { saveDecisionsKey() }
                        .disabled(decisionsKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Button("Test") { Task { await state.verifyDecisionsKey() } }
                        .disabled(!settings.hasDecisionsKey || state.decisionVerifying)
                        .help("Send one tiny Decisions request to check the key")
                    if settings.hasDecisionsKey {
                        Button { removeDecisionsKey() } label: { Image(systemName: "trash") }
                            .accessibilityLabel("Remove OpenAI API key")
                            .help("Remove key")
                    }
                }

                Text("Only used when the deterministic resolver finds several equally likely targets. The key is stored in Keychain, never in settings.json.")
                    .font(SettingsType.meta)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                if let issue = state.decisionIssue {
                    inlineError(issue)
                }
                saveFeedbackRow(.decisions)
            }
            .padding(.top, 5)
        }
        .task { await state.refreshDecisionAcceleration() }
    }

    private var decisionKeyStatus: String {
        guard settings.hasDecisionsKey else { return "missing" }
        return state.decisionAcceleration?.keyStatus ?? "unverified"
    }

    /// Where Decisions is used as the picker shows it: Off whenever it is turned off or its scope is off.
    private var decisionScope: DecisionScope {
        guard settings.decisionAccelerationEnabled else { return .off }
        return DecisionScope(rawValue: settings.decisionAccelerationScope) ?? .off
    }

    private var decisionScopeBinding: Binding<DecisionScope> {
        Binding(
            get: { decisionScope },
            set: { scope in
                guard scope != decisionScope else { return }
                settings.decisionAccelerationEnabled = scope != .off
                settings.decisionAccelerationScope = scope.rawValue
                persistSettings(scope: .decisions, success: "Saved · applies live")
                Task { await state.refreshDecisionAcceleration(reloadKey: true) }
            }
        )
    }

    private var decisionKeyStatusText: String {
        DecisionScope.statusText(scope: decisionScope, keyStatus: decisionKeyStatus,
                                 checkStatus: state.decisionAcceleration?.status)
    }

    private var decisionKeyStatusSymbol: String {
        if decisionScope == .off { return "pause.circle" }
        switch decisionKeyStatus {
        case "valid": return "checkmark.shield.fill"
        case "invalid": return "xmark.shield"
        case "missing": return "key"
        default: return "questionmark.circle"
        }
    }

    private var decisionKeyStatusColor: Color {
        if decisionScope == .off { return .secondary }
        switch decisionKeyStatus {
        case "valid": return .green
        case "invalid": return .red
        case "missing": return .orange
        default: return .secondary
        }
    }

    private var currentUpdateCommit: String {
        if let progress = state.updateProgress,
           progress.normalizedStatus == "completed",
           let value = progress.toShort ?? progress.toCommit.map({ String($0.prefix(8)) }) {
            return value
        }
        if let value = state.updateCheckInfo?.deployedShort { return value }
        if let progress = state.updateProgress {
            if let value = progress.fromShort { return value }
            if let commit = progress.fromCommit { return String(commit.prefix(8)) }
        }
        return "—"
    }

    private var currentUpdateVersion: String {
        if state.version != "—", !state.version.isEmpty { return "v\(state.version)" }
        if state.updateProgress?.normalizedStatus == "completed",
           let releaseVersion = state.updateProgress?.releaseVersion {
            return "v\(releaseVersion)"
        }
        return "—"
    }

    private var availableUpdateText: String {
        if let package = state.packageUpdate {
            if package.checkFailed == true { return "Couldn’t check right now" }
            return package.updateAvailable == true ? "A new update is ready" : "Up to date"
        }
        guard let info = state.updateCheckInfo else { return "Check for update" }
        if info.dirty { return "Blocked by local changes" }
        if info.updateAvailable {
            let version = info.releaseVersion.map { "v\($0)" } ?? "Verified release"
            return "\(version) · \(info.targetShort)"
        }
        if let ahead = info.unverifiedAhead, ahead > 0 {
            return "No newer stable release · \(ahead) dev commit(s) ahead"
        }
        return "Up to date · \(info.targetShort)"
    }

    /// One summary row; versions, commits and updater progress sit behind Details.
    private var updateCard: some View {
        GroupBox("Updates") {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    Image(systemName: updateSummarySymbol)
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(updateSummaryColor)
                        .frame(width: 22)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(updateSummaryTitle)
                            .font(SettingsType.title)
                            .lineLimit(2)
                        Text("Installed \(currentUpdateVersion)")
                            .font(SettingsType.meta)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        state.checkForUpdates()
                    } label: {
                        if state.updateCheckLoading {
                            HStack(spacing: 6) {
                                ProgressView().controlSize(.small)
                                Text("Checking…")
                            }
                        } else {
                            Text("Check Update")
                        }
                    }
                    .disabled(!state.canCheckForUpdates)

                    if state.canInstallUpdate {
                        Button(state.updateActionTitle) {
                            state.performUpdate()
                        }
                        .buttonStyle(.borderedProminent)
                    }
                }

                Divider()
                HStack(spacing: 12) {
                    Text("Let me know when an update is ready")
                        .font(SettingsType.body)
                    Spacer()
                    Toggle("Let me know when an update is ready", isOn: Binding(
                        get: { settings.updateAlertsEnabled },
                        set: { state.setUpdateAlertsEnabled($0) }
                    ))
                    .labelsHidden()
                    .toggleStyle(.switch)
                }

                DisclosureGroup(isExpanded: $showUpdateDetails) {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(alignment: .top, spacing: 18) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Current")
                                    .font(SettingsType.meta)
                                    .foregroundStyle(.secondary)
                                Text(currentUpdateVersion)
                                    .font(SettingsType.title)
                                Text(currentUpdateCommit)
                                    .font(.system(.caption2, design: .monospaced))
                                    .foregroundStyle(.secondary)
                            }
                            Divider().frame(height: 48)
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Available")
                                    .font(SettingsType.meta)
                                    .foregroundStyle(.secondary)
                                Text(availableUpdateText)
                                    .font(SettingsType.body.weight(.medium))
                                    .lineLimit(2)
                                if state.updateCheckLoading {
                                    Text("Checking verified release channel…")
                                        .font(SettingsType.meta)
                                        .foregroundStyle(.secondary)
                                } else if state.updateCheckInfo?.releaseVerified == true {
                                    Text("Verified release")
                                        .font(SettingsType.meta)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                        }

                        if let progress = state.updateProgress {
                            Divider()
                            VStack(alignment: .leading, spacing: 8) {
                                HStack(spacing: 7) {
                                    Image(systemName: updateStatusSymbol(progress.statusKind))
                                        .foregroundStyle(updateStatusColor(progress.statusKind))
                                    Text(progress.statusTitle)
                                        .font(SettingsType.title)
                                    Spacer()
                                    if state.updateTransactionActive {
                                        Text("Running")
                                            .font(SettingsType.meta.weight(.semibold))
                                            .foregroundStyle(.secondary)
                                            .padding(.horizontal, 7)
                                            .padding(.vertical, 3)
                                            .background(.quaternary, in: Capsule())
                                    }
                                }
                                Text(progress.statusDetail)
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)

                                if !progress.steps.isEmpty {
                                    VStack(spacing: 5) {
                                        ForEach(progress.steps) { step in
                                            HStack(spacing: 8) {
                                                updateStepIcon(step.state)
                                                    .frame(width: 15)
                                                Text(step.title)
                                                    .font(SettingsType.note)
                                                Spacer()
                                                Text(step.detail)
                                                    .font(SettingsType.meta)
                                                    .foregroundStyle(.secondary)
                                            }
                                        }
                                    }
                                    .padding(9)
                                    .background(Color(nsColor: .controlBackgroundColor).opacity(0.55), in: RoundedRectangle(cornerRadius: 8))
                                }
                            }
                        }

                        if state.updateTransactionActive {
                            Text("Updater transaction is active")
                                .font(SettingsType.meta)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Details")
                        .font(SettingsType.note)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.top, 5)
        }
    }

    private var updateSummaryTitle: String {
        if let progress = state.updateProgress,
           state.updateTransactionActive || progress.isInProgress
            || ["failed", "recovered", "recovery_failed", "rolling_back"].contains(progress.normalizedStatus) {
            return progress.statusTitle
        }
        if state.updateCheckLoading { return "Checking for updates…" }
        // The same words for every install; versions and commits stay under Details.
        if state.updateReady { return "A new update is ready" }
        if state.updateCheckInfo?.dirty != true, state.updateCheckInfo != nil || state.packageUpdate?.updateAvailable == false {
            return "Mac MCP is up to date"
        }
        return availableUpdateText
    }

    private var updateSummarySymbol: String {
        if let progress = state.updateProgress,
           state.updateTransactionActive || progress.isInProgress
            || ["failed", "recovered", "recovery_failed", "rolling_back"].contains(progress.normalizedStatus) {
            return updateStatusSymbol(progress.statusKind)
        }
        if state.updateReady { return "sparkles" }
        if state.packageUpdate?.updateAvailable == false, state.packageUpdate?.checkFailed != true { return "checkmark.circle.fill" }
        guard let info = state.updateCheckInfo else { return "arrow.triangle.2.circlepath" }
        if info.dirty { return "exclamationmark.triangle.fill" }
        return info.updateAvailable ? "arrow.down.circle.fill" : "checkmark.circle.fill"
    }

    private var updateSummaryColor: Color {
        if let progress = state.updateProgress,
           state.updateTransactionActive || progress.isInProgress
            || ["failed", "recovered", "recovery_failed", "rolling_back"].contains(progress.normalizedStatus) {
            return updateStatusColor(progress.statusKind)
        }
        if state.updateReady { return .accentColor }
        if state.packageUpdate?.updateAvailable == false, state.packageUpdate?.checkFailed != true { return .green }
        guard let info = state.updateCheckInfo else { return .secondary }
        if info.dirty { return .orange }
        return info.updateAvailable ? .accentColor : .green
    }

    private func updateStatusColor(_ kind: UpdateStatusKind) -> Color {
        switch kind {
        case .running: return .accentColor
        case .success: return .green
        case .warning: return .orange
        case .error: return .red
        case .recovery: return .orange
        }
    }

    private func updateStatusSymbol(_ kind: UpdateStatusKind) -> String {
        switch kind {
        case .running: return "arrow.triangle.2.circlepath"
        case .success: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .error: return "xmark.octagon.fill"
        case .recovery: return "arrow.counterclockwise.circle.fill"
        }
    }

    @ViewBuilder
    private func updateStepIcon(_ stepState: UpdateProgressStep.State) -> some View {
        switch stepState {
        case .active:
            ProgressView().controlSize(.small)
        case .complete:
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.green)
        case .failed:
            Image(systemName: "xmark.circle.fill").foregroundStyle(Color.red)
        case .skipped:
            Image(systemName: "minus.circle").foregroundStyle(Color.secondary)
        case .pending:
            Image(systemName: "circle").foregroundStyle(Color.secondary)
        }
    }

    @ViewBuilder
    private func settingsDataStateRow(_ dataState: SettingsDataState, retry: (() -> Void)? = nil) -> some View {
        let presentation = settingsDataPresentation(dataState)
        if dataState.phase == .fresh {
            // Fresh is the normal case and says nothing; only loading or a problem earns a line.
            EmptyView()
        } else if dataState.phase == .loading {
            HStack(spacing: 6) {
                ProgressView().controlSize(.mini)
                Text("Loading…")
            }
            .font(SettingsType.meta)
            .foregroundStyle(.secondary)
            .accessibilityElement(children: .combine)
        } else {
            settingsDataProblemRow(dataState, presentation: presentation, retry: retry)
        }
    }

    private func settingsDataProblemRow(_ dataState: SettingsDataState,
                                        presentation: (title: String, detail: String?, symbol: String, color: Color),
                                        retry: (() -> Void)?) -> some View {
        HStack(alignment: .top, spacing: 8) {
            if dataState.phase == .loading {
                ProgressView().controlSize(.small)
            } else {
                Image(systemName: presentation.symbol)
                    .foregroundStyle(presentation.color)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(presentation.title)
                    .font(SettingsType.note.weight(.semibold))
                    .foregroundStyle(presentation.color)
                if let detail = presentation.detail {
                    Text(detail)
                        .font(SettingsType.meta)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer()
            if let retry, [.stale, .unavailable, .error].contains(dataState.phase) {
                Button("Retry", action: retry)
                    .controlSize(.small)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(presentation.color.opacity(0.08), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private func settingsDataPresentation(_ dataState: SettingsDataState) -> (title: String, detail: String?, symbol: String, color: Color) {
        let timestamp = dataState.lastUpdatedAt.map {
            "Last updated " + $0.formatted(date: .omitted, time: .shortened)
        }
        switch dataState.phase {
        case .loading:
            return ("Loading", timestamp, "arrow.clockwise", .secondary)
        case .fresh:
            return ("Fresh", timestamp, "checkmark.circle.fill", .green)
        case .stale:
            let detail = [dataState.message, timestamp].compactMap { $0 }.joined(separator: " · ")
            return ("Stale", detail.isEmpty ? nil : detail, "clock.badge.exclamationmark", .orange)
        case .unavailable:
            return ("Unavailable", dataState.message, "minus.circle.fill", .orange)
        case .error:
            return ("Could not load", dataState.message, "exclamationmark.triangle.fill", .red)
        }
    }

    @ViewBuilder
    private func inlineError(_ message: String, leadingInset: CGFloat = 0) -> some View {
        HStack(alignment: .top, spacing: 6) {
            if leadingInset > 0 { Color.clear.frame(width: leadingInset) }
            Image(systemName: "exclamationmark.circle.fill")
            Text(message).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .font(SettingsType.meta)
        .foregroundStyle(.red)
    }

    @ViewBuilder
    private func saveFeedbackRow(_ scope: SettingsFeedbackScope) -> some View {
        if let feedback = saveFeedback, feedback.scope == scope {
            Label(
                feedback.message,
                systemImage: feedback.isError ? "exclamationmark.circle.fill" : "checkmark.circle.fill"
            )
            .font(SettingsType.meta.weight(.medium))
            .foregroundStyle(feedback.isError ? Color.red : Color.secondary)
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var publicURLValidationMessage: String? {
        guard settings.publicEndpointMode == "custom" || settings.publicEndpointMode == "cloudflare" else { return nil }
        let value = settings.publicURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return "Public URL is required for this endpoint mode." }
        guard let components = URLComponents(string: value),
              components.scheme?.lowercased() == "https",
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil else {
            return "Enter a valid HTTPS URL without embedded credentials."
        }
        return nil
    }

    private var serverPortValidationMessage: String? {
        (1...65535).contains(settings.serverPort) ? nil : "Port must be between 1 and 65535."
    }

    private var cliPathValidationMessage: String? {
        let value = settings.cliPath.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return nil }
        guard value.hasPrefix("/") else { return "Use an absolute CLI path or leave it blank for auto-detect." }
        guard FileManager.default.isExecutableFile(atPath: value) else { return "No executable was found at this CLI path." }
        return nil
    }

    private func persistEndpointSettings() {
        if let issue = publicURLValidationMessage {
            saveFeedback = SettingsSaveFeedback(scope: .endpoint, message: issue, isError: true)
            return
        }
        persistSettings(scope: .endpoint, success: "Saved · restart Mac MCP to apply connector changes")
    }

    private func persistRuntimeSettings() {
        if let issue = serverPortValidationMessage ?? cliPathValidationMessage {
            saveFeedback = SettingsSaveFeedback(scope: .runtime, message: issue, isError: true)
            return
        }
        persistSettings(scope: .runtime, success: "Saved · restart required")
    }

    private func sectionLead(_ title: String, subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.headline.weight(.semibold))
            Text(subtitle)
                .font(SettingsType.note)
                .foregroundStyle(.secondary)
        }
    }

    private func paneHeader(_ title: String, subtitle: String, refresh: Bool = false) -> some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.title2.weight(.semibold))
                Text(subtitle).font(SettingsType.note).foregroundStyle(.secondary)
            }
            Spacer()
            if refresh {
                Button {
                    Task { await state.refreshProviders() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh provider detection")
                .accessibilityLabel("Refresh provider detection")
            }
        }
    }

    private var defaultAgentCard: some View {
        let status = defaultAgentStatus
        return GroupBox {
            VStack(alignment: .leading, spacing: 11) {
                HStack(alignment: .center, spacing: 10) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 9, style: .continuous)
                            .fill(.quaternary)
                            .frame(width: 36, height: 36)
                        Image(systemName: "person.crop.circle.badge.checkmark")
                            .font(.system(size: 16, weight: .semibold))
                            .foregroundStyle(status.color)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Default Agent")
                            .font(SettingsType.title)
                        Text("Used only when a delegated request omits provider, model, or thinking.")
                            .font(SettingsType.meta)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Label(status.label, systemImage: status.symbol)
                        .font(SettingsType.meta.weight(.semibold))
                        .foregroundStyle(status.color)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(status.color.opacity(0.10), in: Capsule())
                }

                Divider()

                defaultAgentPickerRow(title: "Provider") {
                    Picker("", selection: defaultProviderBinding) {
                        Text("Not set").tag("")
                        ForEach(defaultProviderOptions, id: \.self) { id in
                            Text(providerDisplayName(id)).tag(id)
                        }
                    }
                    .labelsHidden()
                }

                defaultAgentPickerRow(title: "Model") {
                    Picker("", selection: defaultModelBinding) {
                        Text("Provider default").tag("")
                        if !settings.defaultAgentModel.isEmpty,
                           !defaultModelOptions.contains(where: { $0.id == settings.defaultAgentModel }) {
                            Text("\(settings.defaultAgentModel) · Unavailable")
                                .tag(settings.defaultAgentModel)
                        }
                        ForEach(defaultModelOptions) { item in
                            Text(item.displayName).tag(item.id)
                        }
                    }
                    .labelsHidden()
                    .disabled(settings.defaultAgentProvider.isEmpty)
                }

                defaultAgentPickerRow(title: "Thinking") {
                    Picker("", selection: defaultReasoningBinding) {
                        Text("Provider default").tag("")
                        if !settings.defaultAgentReasoning.isEmpty,
                           !defaultReasoningOptions.contains(settings.defaultAgentReasoning) {
                            Text("\(settings.defaultAgentReasoning.capitalized) · Unsupported")
                                .tag(settings.defaultAgentReasoning)
                        }
                        ForEach(defaultReasoningOptions, id: \.self) { value in
                            Text(reasoningDisplayName(value)).tag(value)
                        }
                    }
                    .labelsHidden()
                    .disabled(settings.defaultAgentProvider.isEmpty || defaultReasoningOptions.isEmpty)
                }

                Text(status.detail)
                    .font(SettingsType.meta)
                    .foregroundStyle(status.color == .green ? Color.secondary : status.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.top, 4)
        } label: {
            Label("Delegation Default", systemImage: "slider.horizontal.3")
        }
        .disabled(settings.providerSettingsLocked)
    }

    private func defaultAgentPickerRow<Content: View>(
        title: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(spacing: 12) {
            Text(title)
                .font(SettingsType.note)
                .foregroundStyle(.secondary)
                .frame(width: 72, alignment: .leading)
            Spacer()
            content()
                .fixedSize()  // natural width, right edges aligned (a wider frame only centers the popup)
        }
    }

    private var defaultProviderOptions: [String] {
        AgentChoice.providerOptions(state.providerStatuses, keeping: settings.defaultAgentProvider)
    }

    private var defaultProviderCatalog: ProviderCatalogInfo? {
        guard !settings.defaultAgentProvider.isEmpty else { return nil }
        return state.providerCatalogs[settings.defaultAgentProvider]
    }

    private var defaultModelOptions: [AgentModelInfo] {
        defaultProviderCatalog?.modelItems ?? []
    }

    private var defaultReasoningOptions: [String] {
        AgentChoice.reasoningOptions(settings.defaultAgentModel, in: defaultProviderCatalog)
    }

    private var defaultProviderBinding: Binding<String> {
        Binding(
            get: { settings.defaultAgentProvider },
            set: { value in
                settings.defaultAgentProvider = value
                settings.defaultAgentModel = ""
                settings.defaultAgentReasoning = ""
                persistDefaultAgent()
            }
        )
    }

    private var defaultModelBinding: Binding<String> {
        Binding(
            get: { settings.defaultAgentModel },
            set: { value in
                settings.defaultAgentModel = value
                persistDefaultAgent()
            }
        )
    }

    private var defaultReasoningBinding: Binding<String> {
        Binding(
            get: { settings.defaultAgentReasoning },
            set: { value in
                settings.defaultAgentReasoning = value
                persistDefaultAgent()
            }
        )
    }

    private var defaultAgentStatus: AgentChoice.Status {
        let status = AgentChoice.status(
            provider: settings.defaultAgentProvider,
            model: settings.defaultAgentModel,
            reasoning: settings.defaultAgentReasoning,
            providers: state.providerStatuses,
            catalogs: state.providerCatalogs,
            phase: providersPhase,
            subject: "this default"
        )
        guard status.isReady else { return status }
        return AgentChoice.Status(
            label: status.label,
            detail: status.detail + " Explicit spawn arguments still take precedence.",
            symbol: status.symbol,
            color: status.color
        )
    }

    /// The provider list's load state, for checks that have no provider to look at yet.
    private var providersPhase: AgentChoice.ProvidersPhase {
        switch state.providerSettingsState.phase {
        case .loading: return .loading
        case .error, .unavailable:
            return .failed(state.providerSettingsState.message ?? "Provider status could not be loaded.")
        default: return .loaded
        }
    }

    private func reasoningDisplayName(_ value: String) -> String {
        AgentChoice.reasoningDisplayName(value)
    }

    /// How many agents run, whether a team can talk, and how long finished agents stay: rows in one box, like Usage.
    private var agentLimitsGroup: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Running Agents").font(SettingsType.title)
            VStack(spacing: 0) {
                SettingsRow(
                    title: "Agents at Once",
                    note: "Most delegated agents that run together; more wait in line. Fewer is gentler on memory. New agents also wait while macOS reports memory pressure."
                ) {
                    Stepper(value: maxActiveBinding, in: 1...16) {
                        Text("\(settings.maxActiveAgents)")
                            .font(.system(size: 13, weight: .semibold).monospacedDigit())
                            .frame(minWidth: 22, alignment: .trailing)
                    }
                    .fixedSize()
                    .accessibilityLabel("Agents at once")
                    .accessibilityValue("\(settings.maxActiveAgents)")
                }
                Divider().padding(.leading, 14)
                SettingsRow(
                    title: "Messages Between Agents",
                    note: "Agents in a team, and the app that started the team, can message each other while they work. A message reaches an agent once, after its next tool call, and never interrupts it."
                ) {
                    Toggle("Messages Between Agents", isOn: teamMessagesBinding)
                        .toggleStyle(.switch)
                        .labelsHidden()
                }
                Divider().padding(.leading, 14)
                SettingsRow(
                    title: "Agent History",
                    note: "Prompts, provider output, results and provider state of finished agents are deleted from this Mac after this time. Running agents, agents in a running team and agents with an open worktree are kept. Each provider keeps its own copy under its own policy."
                ) {
                    Picker("Keep for", selection: retentionBinding) {
                        Text("7 days").tag(7)
                        Text("30 days").tag(30)
                        Text("90 days").tag(90)
                        Text("1 year").tag(365)
                        Text("Never delete").tag(0)
                    }
                    .labelsHidden()
                    .frame(width: 140)
                    .accessibilityLabel("Keep finished agents for")
                }
            }
            .usageListBox()
        }
    }

    private var teamMessagesBinding: Binding<Bool> {
        Binding(
            get: { settings.teamMessagesEnabled },
            set: { value in
                settings.teamMessagesEnabled = value
                do {
                    try settings.save()
                    saveFeedback = SettingsSaveFeedback(
                        scope: .agents,
                        message: value ? "Agents can message each other · applies to new agents" : "Messages between agents are off",
                        isError: false
                    )
                } catch {
                    settings.load()
                    saveFeedback = SettingsSaveFeedback(
                        scope: .agents, message: "Could not save: \(error.localizedDescription)", isError: true
                    )
                }
            }
        )
    }

    private var retentionBinding: Binding<Int> {
        Binding(
            get: { settings.agentRetentionDays },
            set: { value in
                settings.agentRetentionDays = value
                do {
                    try settings.save()
                    saveFeedback = SettingsSaveFeedback(
                        scope: .agents,
                        message: value == 0 ? "Finished agents are kept until you delete them"
                            : "Finished agents are deleted after \(value) days",
                        isError: false
                    )
                } catch {
                    settings.load()
                    saveFeedback = SettingsSaveFeedback(
                        scope: .agents, message: "Could not save agent history: \(error.localizedDescription)", isError: true
                    )
                }
            }
        )
    }

    private var maxActiveBinding: Binding<Int> {
        Binding(
            get: { settings.maxActiveAgents },
            set: { value in
                settings.maxActiveAgents = value
                do {
                    try settings.save()
                    saveFeedback = SettingsSaveFeedback(
                        scope: .agents, message: "Up to \(value) agents at once · applies to new agents", isError: false
                    )
                } catch {
                    settings.load()
                    saveFeedback = SettingsSaveFeedback(
                        scope: .agents, message: "Could not save the agent limit: \(error.localizedDescription)", isError: true
                    )
                }
            }
        )
    }

    private func persistDefaultAgent() {
        do {
            try settings.save()
            let message = settings.defaultAgentProvider.isEmpty
                ? "Default Agent cleared · applies live"
                : "Default Agent saved · applies live"
            saveFeedback = SettingsSaveFeedback(scope: .agents, message: message, isError: false)
            notice = message
        } catch {
            settings.load()
            let message = "Could not save Default Agent: \(error.localizedDescription)"
            saveFeedback = SettingsSaveFeedback(scope: .agents, message: message, isError: true)
            notice = message
        }
    }

    private func providerRow(id: String, title: String, subtitle: String) -> some View {
        let provider = state.providerStatuses.first(where: { $0.id == id })
        let detected = provider?.detected ?? false
        let version = provider?.version?.trimmingCharacters(in: .whitespacesAndNewlines)
        let path = provider?.binaryPath?.trimmingCharacters(in: .whitespacesAndNewlines)
        let detectionLabel: String = {
            guard provider != nil else {
                switch state.providerSettingsState.phase {
                case .loading: return "Checking…"
                case .stale: return "Stale"
                case .error, .unavailable: return "Unavailable"
                case .fresh: return "Not found on this Mac"
                }
            }
            return detected ? "Found on this Mac" : "Not found on this Mac"
        }()
        let detectionColor: Color = {
            guard provider != nil else {
                switch state.providerSettingsState.phase {
                case .error: return .red
                case .stale, .unavailable: return .orange
                default: return .secondary
                }
            }
            return detected ? .green : .secondary
        }()

        let versionLine = [version, path].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: "  ")
        return HStack(alignment: .center, spacing: 12) {
            UsageAgentLogo(agent: id, size: 30)
                .opacity(detected ? 1 : 0.5)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(title).font(SettingsType.title)
                    HStack(spacing: 4) {
                        Circle().fill(detectionColor).frame(width: 6, height: 6)
                        Text(detectionLabel).font(SettingsType.meta).foregroundStyle(detected ? .secondary : detectionColor)
                    }
                    .accessibilityElement(children: .combine)
                }
                Text(subtitle).font(SettingsType.note).foregroundStyle(.secondary)
                if detected, !versionLine.isEmpty {
                    Text(versionLine).font(SettingsType.meta.monospaced()).foregroundStyle(.tertiary)
                        .lineLimit(1).truncationMode(.middle)
                        .textSelection(.enabled)
                }
            }
            Spacer()
            Toggle(title, isOn: providerBinding(id))
                .labelsHidden()
                .toggleStyle(.switch)
                .disabled(settings.providerSettingsLocked)
                .help(
                    settings.providerSettingsLocked
                        ? "Repair settings.json before changing delegated providers."
                        : (settings.providerEnabled(id) ? "Disable \(title)" : "Enable \(title)")
                )
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .opacity(provider == nil || detected ? 1 : 0.7)
    }

    private func providerBinding(_ id: String) -> Binding<Bool> {
        Binding(
            get: { settings.providerEnabled(id) },
            set: { enabled in
                settings.setProviderEnabled(id, enabled: enabled)
                do {
                    try settings.save()
                    let message = "\(providerDisplayName(id)) is now \(enabled ? "enabled" : "disabled") · applies live"
                    saveFeedback = SettingsSaveFeedback(scope: .agents, message: message, isError: false)
                    notice = message
                    Task { await state.refreshProviders() }
                } catch {
                    settings.load()
                    let message = "Could not save provider settings."
                    saveFeedback = SettingsSaveFeedback(scope: .agents, message: message, isError: true)
                    notice = message
                }
            }
        )
    }

    private var startAtLoginBinding: Binding<Bool> {
        Binding(
            get: { settings.startAtLogin },
            set: { enabled in state.setStartAtLogin(enabled) }
        )
    }

    private var agentCompletionNotificationsBinding: Binding<Bool> {
        Binding(
            get: { settings.agentCompletionNotificationsEnabled },
            set: { enabled in
                state.setAgentCompletionNotificationsEnabled(enabled)
            }
        )
    }

    private var appsPane: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 13) {
                paneHeader(
                    "Apps",
                    subtitle: "What agents can do in your Mac apps through Mac MCP, and when they ask you first."
                )
                SettingsTabPicker(selection: $tabs.apps, label: "Apps section")
            }
            .padding(.horizontal, 22)
            .padding(.top, 22)
            .padding(.bottom, 12)
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    switch tabs.apps {
                    case .sending:
                        GroupBox("Sending") {
                            VStack(alignment: .leading, spacing: 12) {
                                appSendRow(
                                    bundleId: "com.apple.mail",
                                    title: "Ask before sending email",
                                    detail: "Agents can send email with Mail. When on, each email waits in a confirmation panel showing the sender, recipients, subject and full text, and goes out only when you click Send.",
                                    isOn: confirmSendBinding(\.mailConfirmSend, app: "Mail")
                                )
                                Divider()
                                appSendRow(
                                    bundleId: "com.apple.MobileSMS",
                                    title: "Ask before sending messages",
                                    detail: "Agents can send a message to an existing Messages conversation. When on, each message waits in the same confirmation panel until you click Send.",
                                    isOn: confirmSendBinding(\.messagesConfirmSend, app: "Messages")
                                )
                            }
                            .padding(.top, 5)
                        }

                        GroupBox("When confirmation is off") {
                            Text("Agents send without asking. Cancel, closing the panel or letting it time out never sends, so leave these on unless you trust every agent connected to this Mac.")
                                .font(SettingsType.note)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.top, 5)
                        }

                        MessagesAccessSection(state: state)

                    case .shortcuts:
                        GroupBox("Shortcuts agents may run") {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("A shortcut can do anything its actions do, so agents run shortcuts only when running commands is allowed. The ones you turn on here can run even when it's off; with it on, any shortcut can run.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                if let names = macShortcutNames {
                                    if names.isEmpty {
                                        Text("No shortcuts found on this Mac.").font(SettingsType.note).foregroundStyle(.secondary)
                                    } else {
                                        let allowed = names.filter { settings.allowedShortcuts.contains($0) }.count
                                        let words = shortcutFilter.trimmingCharacters(in: .whitespaces)
                                        let shown = words.isEmpty ? names : names.filter { $0.localizedCaseInsensitiveContains(words) }
                                        HStack(spacing: 10) {
                                            TextField("Filter \(names.count) shortcuts", text: $shortcutFilter)
                                                .textFieldStyle(.roundedBorder)
                                                .accessibilityLabel("Filter shortcuts")
                                            Text("\(allowed) of \(names.count) allowed")
                                                .font(SettingsType.meta).foregroundStyle(.secondary).monospacedDigit()
                                                .fixedSize()
                                        }
                                        .padding(.top, 4)
                                        LazyVStack(spacing: 0) {
                                            ForEach(Array(shown.enumerated()), id: \.element) { index, name in
                                                if index > 0 { Divider() }
                                                HStack {
                                                    Image(systemName: "square.stack.3d.up.fill")
                                                        .font(.system(size: 12))
                                                        .foregroundStyle(settings.allowedShortcuts.contains(name) ? Color.purple : Color.secondary)
                                                        .frame(width: 18)
                                                        .accessibilityHidden(true)
                                                    Text(name).font(SettingsType.body)
                                                    Spacer(minLength: 12)
                                                    Toggle(name, isOn: allowedShortcutBinding(name))
                                                        .toggleStyle(.switch)
                                                        .controlSize(.small)
                                                        .labelsHidden()
                                                }
                                                .padding(.vertical, 5)
                                            }
                                        }
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        if shown.isEmpty {
                                            Text("No shortcut matches.").font(SettingsType.note).foregroundStyle(.secondary)
                                        }
                                    }
                                } else {
                                    ProgressView().controlSize(.small)
                                }
                            }
                            .padding(.top, 5)
                        }
                        .task { macShortcutNames = await MacShortcuts.names() }
                    case .support:
                        GroupBox("What agents can do in each app") {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("Built in means Mac MCP works with the app directly, even while you use it. Other apps are used by looking at their window and clicking, which is slower and depends on what the window shows.")
                                    .font(SettingsType.note)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                if let problem = appSupportProblem {
                                    inlineError(problem)
                                    Button("Try again") { Task { await loadAppSupport() } }.controlSize(.small)
                                } else if let apps = appSupport {
                                    if apps.isEmpty {
                                        Text("Mac MCP found no apps to list.").font(SettingsType.note).foregroundStyle(.secondary)
                                    }
                                    appSupportSummary(apps)
                                    ForEach(Array(apps.enumerated()), id: \.offset) { _, app in
                                        Divider()
                                        appSupportRow(app)
                                    }
                                } else {
                                    ProgressView().controlSize(.small)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 5)
                        }
                        .task { await loadAppSupport() }
                    }
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .id(tabs.apps)  // each tab opens at its top
        }
    }

    private static let appBundleIDs = [
        "Finder": "com.apple.finder", "Notes": "com.apple.Notes", "Mail": "com.apple.mail", "Calendar": "com.apple.iCal",
        "Reminders": "com.apple.reminders", "Contacts": "com.apple.AddressBook", "Shortcuts": "com.apple.shortcuts",
        "Preview": "com.apple.Preview", "System Settings": "com.apple.systempreferences", "Messages": "com.apple.MobileSMS",
        "Photos": "com.apple.Photos", "Numbers": "com.apple.iWork.Numbers", "Keynote": "com.apple.iWork.Keynote",
        "Pages": "com.apple.iWork.Pages", "Music": "com.apple.Music", "Mission Control": "com.apple.exposelauncher", "Xcode": "com.apple.dt.Xcode", "Slack": "com.tinyspeck.slackmacgap", "Visual Studio Code": "com.microsoft.VSCode",
    ]

    /// Loading, a list (possibly empty) and an error are three different answers (#19).
    private func loadAppSupport() async {
        appSupportProblem = nil
        appSupport = nil
        let result = await state.taskAPI("dashboard/api/apps/support")
        if result.ok {
            appSupport = result.object["apps"] as? [[String: Any]] ?? []
        } else {
            appSupportProblem = (result.object["message"] as? String) ?? (result.object["error"] as? String)
                ?? "Couldn’t load the app list."
        }
    }

    private func appSupportRow(_ app: [String: Any]) -> some View {
        let name = app["name"] as? String ?? ""
        let words = { (key: String) in (app[key] as? [String] ?? []).joined(separator: ", ") }
        let lines: [(String, String)] = [
            ("Reads", words("reads")), ("Opens", words("opens")), ("Changes", words("changes")),
            ("Sends, asking you first unless turned off under Sending", words("sends")),
            ("Runs, like a command", words("runs")),
        ].filter { !$0.1.isEmpty }
        let others = (app["other_ways"] as? [[String: Any]] ?? []).map {
            "\($0["task"] as? String ?? ""): \(($0["how"] as? String ?? "").lowercased())"
        }
        return DisclosureGroup {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(lines, id: \.0) { line in
                    Text("\(line.0): \(line.1)").font(SettingsType.meta).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(others, id: \.self) { line in
                    Text(line).font(SettingsType.meta).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.leading, 26)
        } label: {
            HStack(spacing: 8) {
                Image(nsImage: appIcon(Self.appBundleIDs[name] ?? ""))
                    .resizable()
                    .frame(width: 22, height: 22)
                    .accessibilityHidden(true)
                Text(name).font(SettingsType.title)
                Spacer()
                supportBadge(app["support"] as? String ?? "", words: app["support_words"] as? String ?? "")
            }
            .padding(.vertical, 1)
            .accessibilityElement(children: .combine)
        }
    }

    /// How directly Mac MCP works with an app, as a coloured capsule: green built in, blue partly, grey by window.
    private func supportBadge(_ support: String, words: String) -> some View {
        let color: Color = support == "first_party" ? .green : support == "partial_first_party" ? .blue : .secondary
        return Text(words)
            .font(SettingsType.meta.weight(.medium))
            .foregroundStyle(color)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(Capsule().fill(color.opacity(0.14)))
    }

    /// "11 built in · 2 partly · 2 by their window": the whole list at a glance.
    private func appSupportSummary(_ apps: [[String: Any]]) -> some View {
        let count = { (level: String) in apps.filter { $0["support"] as? String == level }.count }
        return UsageFactsRow(facts: [
            UsageFact(value: "\(count("first_party"))", title: "Built in", note: "Works directly, even while you use the app."),
            UsageFact(value: "\(count("partial_first_party"))", title: "Partly built in", note: "Some tasks directly, the rest by the window."),
            UsageFact(value: "\(count("generic_ax"))", title: "By looking at the window", note: "Clicks and reads what the window shows."),
        ], boxed: false)
        .padding(.horizontal, -14)
    }

    private func appSendRow(bundleId: String, title: String, detail: String, isOn: Binding<Bool>) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(nsImage: appIcon(bundleId))
                .resizable()
                .frame(width: 32, height: 32)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(SettingsType.title)
                Text(detail)
                    .font(SettingsType.meta)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            Toggle(title, isOn: isOn)
                .labelsHidden()
                .toggleStyle(.switch)
        }
    }

    private func appIcon(_ bundleId: String) -> NSImage {
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId) {
            return NSWorkspace.shared.icon(forFile: url.path)
        }
        return NSImage(systemSymbolName: "app", accessibilityDescription: nil) ?? NSImage()
    }

    private func allowedShortcutBinding(_ name: String) -> Binding<Bool> {
        Binding(
            get: { settings.allowedShortcuts.contains(name) },
            set: { allowed in
                var names = settings.allowedShortcuts.filter { $0 != name }
                if allowed { names.append(name) }
                settings.allowedShortcuts = names
                persistSettings(success: allowed ? "Agents can run “\(name)”, also with commands off."
                                                 : "“\(name)” runs only when commands are allowed.")
            }
        )
    }

    private func confirmSendBinding(_ keyPath: ReferenceWritableKeyPath<SettingsStore, Bool>, app: String) -> Binding<Bool> {
        Binding(
            get: { settings[keyPath: keyPath] },
            set: { enabled in
                settings[keyPath: keyPath] = enabled
                persistSettings(
                    success: enabled
                        ? "\(app): agents will ask before sending."
                        : "\(app): agents send without asking."
                )
            }
        )
    }

    /// Off, the tool activity bubble, or the notch island (#174); the island is never on by default.
    private var liveActivityStyle: String {
        settings.showLiveIsland ? "island" : (settings.showToolActivity && settings.requireToolDescriptions ? "bubble" : "off")
    }

    private var liveActivityDetail: String {
        switch liveActivityStyle {
        case "island":
            return "Grows out of the notch while Mac MCP works: tool steps, agents, scheduled tasks and anything waiting for you. Hidden when nothing is happening."
                + (settings.requireToolDescriptions ? "" : " Tool steps appear when Require Tool Descriptions is on.")
        case "bubble":
            return "A speech bubble under the menu bar shows each tool step. Drag it anywhere; its position is remembered."
                + " It needs Require Tool Descriptions, which turns on with it: reconnect your AI apps afterwards so they see it."
        default:
            return "Nothing is shown while Mac MCP works. Descriptions can stay enabled."
        }
    }

    private var liveActivityStyleBinding: Binding<String> {
        Binding(
            get: { liveActivityStyle },
            set: { style in
                settings.showLiveIsland = style == "island"
                let turnsOnDescriptions = style == "bubble" && !settings.requireToolDescriptions
                if turnsOnDescriptions {
                    settings.requireToolDescriptions = true  // the bubble shows tool descriptions
                }
                settings.showToolActivity = style == "bubble"
                persistSettings(success: style == "island" ? "Notch island on."
                    : turnsOnDescriptions ? "Bubble on, with Require Tool Descriptions. Reconnect your AI apps so they see it."
                    : style == "bubble" ? "Tool Activity bubble on." : "Live activity off.")
                state.restartToolActivityStream()
                if style != "bubble" { ToolActivityBubbleController.shared.hideImmediately() }
            }
        )
    }

    private var desktopCursorBinding: Binding<Bool> {
        Binding(
            get: { settings.showDesktopCursor },
            set: { enabled in
                settings.showDesktopCursor = enabled
                if !enabled { DesktopCursorOverlayController.shared.hideImmediately() }
                persistSettings(success: enabled ? "Agent cursor on." : "Agent cursor off.")
            }
        )
    }

    private var browserTabPulseBinding: Binding<Bool> {
        Binding(
            get: { settings.showBrowserTabPulse },
            set: { enabled in
                settings.showBrowserTabPulse = enabled
                if !enabled { BrowserTabPulseController.shared.hideImmediately() }
                persistSettings(success: enabled ? "Tab glow on." : "Tab glow off.")
            }
        )
    }

    private var requireToolDescriptionsBinding: Binding<Bool> {
        Binding(
            get: { settings.requireToolDescriptions },
            set: { enabled in
                settings.requireToolDescriptions = enabled
                if !enabled {
                    settings.showToolActivity = false
                    ToolActivityBubbleController.shared.hideImmediately()
                }
                persistSettings(
                    success: enabled
                        ? "Tool descriptions enabled. Reconnect MCP clients to refresh schemas."
                        : "Tool descriptions disabled. Reconnect MCP clients to refresh schemas."
                )
                state.restartToolActivityStream()
            }
        )
    }

    private func persistSettings(
        scope: SettingsFeedbackScope? = nil,
        success: String = "Saved"
    ) {
        do {
            try settings.save()
            notice = success
            if let scope {
                saveFeedback = SettingsSaveFeedback(scope: scope, message: success, isError: false)
            }
        } catch {
            let message = "Could not save settings: \(error.localizedDescription)"
            notice = message
            if let scope {
                saveFeedback = SettingsSaveFeedback(scope: scope, message: message, isError: true)
            }
        }
    }

    /// Saves the typed voice name if edge-tts would take it; otherwise keeps it in the field with a note.
    private func commitVoiceName() {
        guard let draft = voiceNameDraft else { return }
        guard let value = VoiceSettingsFormat.voice(draft) else {
            voiceNameProblem = VoiceSettingsFormat.voiceProblem(draft)
            return
        }
        voiceNameDraft = nil
        voiceNameProblem = nil
        guard value != settings.voiceName else { return }
        settings.voiceName = value
        persistSettings(scope: .voice, success: "Saved · applies live")
    }

    /// Saves the typed rate, written as a signed percentage, if it is one; otherwise keeps it with a note.
    private func commitTTSRate() {
        guard let draft = ttsRateDraft else { return }
        guard let value = VoiceSettingsFormat.rate(draft) else {
            ttsRateProblem = VoiceSettingsFormat.rateProblem(draft)
            return
        }
        ttsRateDraft = nil
        ttsRateProblem = nil
        guard value != settings.ttsRate else { return }
        settings.ttsRate = value
        persistSettings(scope: .voice, success: "Saved · applies live")
    }

    /// How long idle conversations stay listed: saved, then handed to the running server, which
    /// otherwise reads it only when it starts. The menu no longer has its own copy of this (#123).
    private func applySessionRetention(_ minutes: Int) {
        do {
            try settings.save()
        } catch {
            let message = "Could not save settings: \(error.localizedDescription)"
            notice = message
            saveFeedback = SettingsSaveFeedback(scope: .sessions, message: message, isError: true)
            return
        }
        Task {
            let live = await state.updateSteeringRetention(minutes: minutes)
            // A later step is already on its way; let it report.
            guard settings.steeringSessionMinutes == minutes else { return }
            let message = live ? "Saved · applies live" : "Saved · applies when Mac MCP next starts"
            notice = message
            saveFeedback = SettingsSaveFeedback(scope: .sessions, message: message, isError: false)
        }
    }

    private func saveGroqKey() {
        do {
            try settings.saveGroqKey(groqKey)
            groqKey = ""
            notice = "Groq key saved securely in Keychain."
        } catch {
            notice = "Could not save Groq key: \(error.localizedDescription)"
        }
    }

    private func saveDecisionsKey() {
        do {
            try settings.saveDecisionsKey(decisionsKey)
            decisionsKey = ""
            saveFeedback = SettingsSaveFeedback(scope: .decisions, message: "Key saved in Keychain · verifying…", isError: false)
            Task { await state.verifyDecisionsKey() }
        } catch {
            saveFeedback = SettingsSaveFeedback(
                scope: .decisions,
                message: "Could not save key: \(error.localizedDescription)",
                isError: true
            )
        }
    }

    private func removeDecisionsKey() {
        do {
            try settings.removeDecisionsKey()
            saveFeedback = SettingsSaveFeedback(scope: .decisions, message: "Key removed · OpenAI Decisions not used", isError: false)
            Task { await state.refreshDecisionAcceleration(reloadKey: true) }
        } catch {
            saveFeedback = SettingsSaveFeedback(
                scope: .decisions,
                message: "Could not remove key: \(error.localizedDescription)",
                isError: true
            )
        }
    }

    private func removeGroqKey() {
        do {
            try settings.removeGroqKey()
            notice = "Groq key removed."
        } catch {
            notice = "Could not remove Groq key: \(error.localizedDescription)"
        }
    }

    private func profileDisplayName(_ value: String) -> String {
        SafetyDisplay.profileName(value)
    }

    private func approvalSourceDisplayName(_ value: String) -> String {
        switch value.lowercased() {
        case "client": return "Client"
        case "server": return "Mac MCP"
        case "external": return "External guard"
        case "none": return "None"
        default: return value.capitalized
        }
    }

    private func capabilityDisplayName(_ value: String) -> String {
        value.replacingOccurrences(of: "_", with: " ")
    }

    private func destructiveFamiliesLabel(_ values: [String]) -> String {
        if values == ["*"] { return "All" }
        if values.isEmpty { return "None" }
        return values.map(capabilityDisplayName).joined(separator: ", ")
    }

    private func qrImage(for value: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(value.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)) else {
            return nil
        }
        let context = CIContext(options: [.useSoftwareRenderer: false])
        guard let cgImage = context.createCGImage(output, from: output.extent) else { return nil }
        return NSImage(cgImage: cgImage, size: NSSize(width: output.extent.width, height: output.extent.height))
    }

    private func providerDisplayName(_ id: String) -> String {
        AgentProviders.name(id) ?? id
    }
}
