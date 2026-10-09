import AppKit
import CoreImage
import CoreImage.CIFilterBuiltins
import Foundation
import SwiftUI
import UniformTypeIdentifiers

private enum SettingsSection: String, CaseIterable, Identifiable {
    case general
    case agents
    case usage
    case permissions
    case connections
    case voice
    case advanced
    case help

    var id: String { rawValue }

    var title: String {
        switch self {
        case .general: return "General"
        case .agents: return "Agents"
        case .usage: return "Usage"
        case .permissions: return "Permissions & Safety"
        case .connections: return "Connections"
        case .voice: return "Voice"
        case .advanced: return "Advanced"
        case .help: return "Help & Diagnostics"
        }
    }

    var symbol: String {
        switch self {
        case .general: return "slider.horizontal.3"
        case .agents: return "cpu"
        case .usage: return "chart.bar.xaxis"
        case .permissions: return "checkmark.shield"
        case .connections: return "network"
        case .voice: return "waveform.and.mic"
        case .advanced: return "wrench.and.screwdriver"
        case .help: return "lifepreserver"
        }
    }

    var searchTerms: String {
        SettingsSearchIndex.terms[rawValue] ?? ""
    }
}

private enum ConnectionsTab: String, CaseIterable, Identifiable {
    case browser
    case mobile

    var id: String { rawValue }
    var title: String { self == .browser ? "Browser" : "Mobile" }
    var symbol: String { self == .browser ? "safari" : "iphone" }
}


private struct UsageHeatCell: Identifiable {
    let dateKey: String
    let value: Int
    let calls: Int

    var id: String { dateKey }
}

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


private enum ProviderUsagePeriod: String, CaseIterable, Identifiable {
    case month = "30D"
    case quarter = "90D"
    case year = "1Y"

    var id: String { rawValue }

    var days: Int {
        switch self {
        case .month: return 30
        case .quarter: return 90
        case .year: return 365
        }
    }
}


private enum SettingsFeedbackScope: String, Equatable {
    case agents
    case decisions
    case endpoint
    case runtime
    case voice
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
    @MacMCPState private var selection: SettingsSection = .general
    @MacMCPState private var notice = ""
    @MacMCPState private var confirmClearUsage = false
    @MacMCPState private var memoryClearPreview: MemoryClearResult?
    @MacMCPState private var groqKey = ""
    @MacMCPState private var decisionsKey = ""
    @MacMCPState private var cloudflareToken = ""
    @MacMCPState private var settingsSearch = ""
    @MacMCPState private var connectionsTab: ConnectionsTab = .browser
    @MacMCPState private var usageAggregation: UsageAggregation = .daily
    @MacMCPState private var usageActor: UsageActorFilter = .all
    @MacMCPState private var providerUsagePeriod: ProviderUsagePeriod = .year
    @MacMCPState private var saveFeedback: SettingsSaveFeedback?

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            detail
        }
        .frame(minWidth: 820, idealWidth: 920, minHeight: 560, idealHeight: 640)
        .background(.regularMaterial)
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

    private var generalPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader(
                    "General",
                    subtitle: "A quick view of this Mac MCP instance, software updates, and everyday status."
                )

                GroupBox("Overview") {
                    HStack(spacing: 12) {
                        overviewMetric(
                            title: "Server",
                            value: state.serverRunning ? "Running" : "Disconnected",
                            symbol: state.serverRunning ? "checkmark.circle.fill" : "exclamationmark.circle",
                            accent: state.serverRunning ? .green : .orange
                        )
                        overviewMetric(
                            title: "Version",
                            value: state.version == "—" ? "Unknown" : "v\(state.version)",
                            symbol: "shippingbox",
                            accent: .secondary
                        )
                        overviewMetric(
                            title: "Active Agents",
                            value: String(state.activeAgents),
                            symbol: "cpu",
                            accent: state.activeAgents > 0 ? .accentColor : .secondary
                        )
                    }
                    .padding(.top, 5)
                }

                GroupBox("Connectivity") {
                    HStack(spacing: 12) {
                        Image(systemName: settings.publicEndpointMode == "none" ? "lock.laptopcomputer" : "network")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(settings.publicEndpointMode == "none" ? Color.secondary : Color.accentColor)
                            .frame(width: 34)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(publicEndpointDisplayName)
                                .font(.subheadline.weight(.semibold))
                            Text(
                                settings.publicEndpointMode == "none"
                                    ? "Local only. Configure remote access in Connections when you need mobile or external access."
                                    : "Remote access is configured under Connections. Runtime/server internals stay in Advanced."
                            )
                            .font(.caption2)
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

                GroupBox("Tool Activity") {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(alignment: .center, spacing: 12) {
                            Image(systemName: "bubble.left.and.text.bubble.right.fill")
                                .font(.system(size: 18, weight: .semibold))
                                .foregroundStyle(Color.accentColor)
                                .frame(width: 34)
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Explain active tool work")
                                    .font(.subheadline.weight(.semibold))
                                Text("Show a short, human-readable intent while an MCP tool is running.")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            if settings.showToolActivity && settings.requireToolDescriptions {
                                Button("Preview") {
                                    ToolActivityBubbleController.shared.preview(
                                        tool: "run_command",
                                        description: "Checking RAM & Disk Health"
                                    )
                                }
                                .controlSize(.small)
                            }
                        }

                        Divider()

                        HStack(alignment: .top, spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Show Tool Activity")
                                    .font(.subheadline.weight(.medium))
                                Text("Displays the native menu-bar speech bubble. Drag it anywhere; its position is remembered. Descriptions can stay enabled when this is off.")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            Toggle("", isOn: showToolActivityBinding)
                                .labelsHidden()
                                .toggleStyle(.switch)
                                .disabled(!settings.requireToolDescriptions)
                        }

                        HStack(alignment: .top, spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Require Tool Descriptions")
                                    .font(.subheadline.weight(.medium))
                                Text("Adds a short required description field to MCP tool schemas. Turn this off to remove the field entirely.")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            Toggle("", isOn: requireToolDescriptionsBinding)
                                .labelsHidden()
                                .toggleStyle(.switch)
                        }

                        if settings.requireToolDescriptions {
                            Label(
                                "Reconnect MCP clients after changing this setting so they refresh their tool schemas.",
                                systemImage: "arrow.triangle.2.circlepath"
                            )
                            .font(.caption2)
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
                                    .font(.subheadline.weight(.semibold))
                                Text("Notify when standalone delegated agents or whole agent teams finish while you work elsewhere.")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            if state.agentNotificationPermissionChanging {
                                ProgressView()
                                    .controlSize(.small)
                            } else {
                                Toggle("", isOn: agentCompletionNotificationsBinding)
                                    .labelsHidden()
                                    .toggleStyle(.switch)
                            }
                        }

                        Divider()

                        HStack {
                            Text("macOS permission")
                                .font(.caption.weight(.medium))
                            Spacer()
                            Text(state.agentNotificationAuthorizationState.displayName)
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(
                                    state.agentNotificationAuthorizationState == .denied
                                        ? Color.orange
                                        : Color.secondary
                                )
                        }

                        Text("Teams are coalesced into one terminal notification. Alerts contain only a sanitized agent/team label — never prompts, results, file paths, URLs, or secrets.")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)

                        if state.agentNotificationAuthorizationState == .denied {
                            Label(
                                "Notification permission is denied in macOS. Re-enable it in System Settings before turning this on.",
                                systemImage: "bell.slash"
                            )
                            .font(.caption2)
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.top, 5)
                }

                updateCard

                if let action = state.actionNotice {
                    Label(action.message, systemImage: action.symbolName)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Spacer(minLength: 0)
            }
            .padding(22)
        }
    }

    private func overviewMetric(title: String, value: String, symbol: String, accent: Color) -> some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(accent)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text(value)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
        }
        .padding(11)
        .frame(maxWidth: .infinity)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.55), in: RoundedRectangle(cornerRadius: 9))
    }

    private var publicEndpointDisplayName: String {
        switch settings.publicEndpointMode {
        case "ngrok": return "ngrok"
        case "cloudflare": return "Cloudflare Tunnel"
        case "custom": return "Custom HTTPS"
        default: return "Local only"
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Settings")
                .font(.title3.weight(.semibold))
                .padding(.horizontal, 14)
                .padding(.top, 16)
                .padding(.bottom, 8)

            TextField("Search Settings", text: $settingsSearch)
                .textFieldStyle(.roundedBorder)
                .font(.caption)
                .padding(.horizontal, 10)
                .padding(.bottom, 5)
                .onChange(of: settingsSearch) { _ in
                    // Never leave a pane on screen that the search no longer lists.
                    let matches = filteredSections
                    if !matches.contains(selection), let first = matches.first {
                        selection = first
                    }
                }

            if filteredSections.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("No settings found for “\(trimmedSettingsSearch)”")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Button("Clear Search") { settingsSearch = "" }
                        .controlSize(.small)
                }
                .padding(.horizontal, 14)
                .padding(.top, 4)
            }

            ForEach(filteredSections) { item in
                Button {
                    selection = item
                    notice = ""
                } label: {
                    HStack(spacing: 9) {
                        Image(systemName: item.symbol)
                            .frame(width: 17)
                        Text(item.title)
                        Spacer()
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .background(
                    selection == item ? Color.accentColor.opacity(0.16) : Color.clear,
                    in: RoundedRectangle(cornerRadius: 8, style: .continuous)
                )
                .padding(.horizontal, 7)
            }

            Spacer()

            if !notice.isEmpty {
                Text(notice)
                    .font(.caption2)
                    .foregroundStyle(notice.hasPrefix("Could not") ? Color.red : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 14)
                    .padding(.bottom, 14)
            }
        }
        .frame(width: 200)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.38))
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
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Clear Search") { settingsSearch = "" }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            selectedPane
        }
    }

    @ViewBuilder
    private var selectedPane: some View {
        switch selection {
        case .general: generalPane
        case .agents: subagentsPane
        case .usage: usagePane
        case .permissions: permissionsPane
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
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                paneHeader(
                    "Agents",
                    subtitle: "Choose delegated providers, models, and the default agent Mac MCP uses for new work.",
                    refresh: true
                )

                if !settings.settingsLoadIssue.isEmpty {
                    Label(settings.settingsLoadIssue, systemImage: "exclamationmark.shield")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(settings.providerSettingsLocked ? Color.orange : Color.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                settingsDataStateRow(state.providerSettingsState) {
                    Task { await state.refreshProviders() }
                }
                saveFeedbackRow(.agents)

                defaultAgentCard

                concurrencyCard

                Text("PROVIDERS")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.tertiary)
                    .tracking(0.5)

                providerRow(id: "opencode", title: "OpenCode", subtitle: "External OpenCode CLI provider")
                providerRow(id: "codex", title: "Codex", subtitle: "External Codex CLI provider")
                providerRow(
                    id: "chatgpt",
                    title: "ChatGPT Web CLI",
                    subtitle: "Experimental authenticated chatgpt.com browser provider. Use it at your own risk."
                )

                Text("Disabled providers are hidden from the agent catalog and cannot be spawned. Changes apply live to new agent requests.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
            }
            .padding(20)
        }
    }

    private var usageRetentionDays: Int {
        state.usagePrivacy?.retentionDays ?? state.usageSummary?.retentionDays ?? 365
    }

    private var usageMeteringEnabled: Bool {
        state.usagePrivacy?.enabled ?? state.usageSummary?.meteringEnabled ?? true
    }

    private var usageDataControls: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 9) {
                HStack(spacing: 16) {
                    Toggle(
                        "Record usage",
                        isOn: Binding(
                            get: { usageMeteringEnabled },
                            set: { value in Task { await state.updateUsagePrivacy(enabled: value) } }
                        )
                    )
                    Picker(
                        "Keep for",
                        selection: Binding(
                            get: { usageRetentionDays },
                            set: { value in Task { await state.updateUsagePrivacy(retentionDays: value) } }
                        )
                    ) {
                        Text("30 days").tag(30)
                        Text("90 days").tag(90)
                        Text("365 days").tag(365)
                    }
                    .frame(width: 190)
                    Spacer(minLength: 8)
                    Button("Clear Usage Data…", role: .destructive) { confirmClearUsage = true }
                }
                Text("Only daily per-tool counts, sizes and latency buckets are kept, never prompts, arguments or results. Older days are removed automatically.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let notice = state.usageDataNotice {
                    Text(notice)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        } label: {
            Label("Data & Retention", systemImage: "hand.raised")
        }
        .confirmationDialog("Delete all stored usage data?", isPresented: $confirmClearUsage) {
            Button("Delete Usage Data", role: .destructive) {
                Task { await state.clearUsage() }
            }
        } message: {
            Text("This removes tool and provider usage history from this Mac. It cannot be undone.")
        }
    }

    private var memoryControls: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 9) {
                if let memory = state.memoryOverview {
                    Text(memory.count == 0
                         ? "No memories are stored."
                         : "\(memory.count) memories (\(memory.importantCount) high or critical), \(memory.oldest ?? "?") to \(memory.newest ?? "?"), \(ByteCountFormatter.string(fromByteCount: Int64(memory.bytesOnDisk), countStyle: .file)) on disk.")
                        .font(.caption)
                }
                HStack(spacing: 16) {
                    Picker(
                        "Keep for",
                        selection: Binding(
                            get: { state.memoryOverview?.retentionDays ?? 0 },
                            set: { value in Task { await state.updateMemoryRetention(days: value) } }
                        )
                    ) {
                        Text("Until deleted").tag(0)
                        Text("90 days").tag(90)
                        Text("180 days").tag(180)
                        Text("1 year").tag(365)
                        Text("2 years").tag(730)
                    }
                    .frame(width: 210)
                    Toggle(
                        "Keep high and critical",
                        isOn: Binding(
                            get: { state.memoryOverview?.keepImportant ?? true },
                            set: { value in Task { await state.updateMemoryRetention(keepImportant: value) } }
                        )
                    )
                    .disabled((state.memoryOverview?.retentionDays ?? 0) == 0)
                    Spacer(minLength: 8)
                }
                HStack(spacing: 8) {
                    Button("Export Memories…") { exportMemories() }
                    Button("Delete All Memories…", role: .destructive) {
                        Task { memoryClearPreview = await state.clearMemory(confirm: false) }
                    }
                    .disabled((state.memoryOverview?.count ?? 0) == 0)
                    Spacer()
                }
                Text("Agents save memories as Markdown day files plus a local search index; both are owner-only. Deleting removes the text from both, which is not a forensic disk wipe.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let notice = state.memoryNotice {
                    Text(notice).font(.caption).foregroundStyle(.secondary)
                }
            }
        } label: {
            Label("Memory", systemImage: "brain")
        }
        .task { await state.refreshMemory() }
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

    private func exportMemories() {
        let panel = NSSavePanel()
        let stamp = ISO8601DateFormatter().string(from: Date()).prefix(10)
        panel.nameFieldStringValue = "mac-mcp-memories-\(stamp).json"
        panel.allowedContentTypes = [.json]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await state.exportMemory(to: url) }
    }

    private var usagePane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .top) {
                    paneHeader(
                        "Usage",
                        subtitle: "Up to \(usageRetentionDays) days of Mac-MCP-attributable tool payload activity. This is not provider billing or model context usage."
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
                    }
                }

                HStack(spacing: 10) {
                    Picker("View", selection: $usageAggregation) {
                        ForEach(UsageAggregation.allCases) { item in
                            Text(item.rawValue).tag(item)
                        }
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 310)

                    Picker("Source", selection: $usageActor) {
                        ForEach(UsageActorFilter.allCases) { item in
                            Text(item.rawValue).tag(item)
                        }
                    }
                    .frame(width: 150)
                    .onChange(of: usageActor) { value in
                        Task { await state.refreshUsage(actorClass: value.apiValue) }
                    }
                }

                if let issue = state.usageIssue {
                    Label(issue, systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }

                usageDataControls
                memoryControls

                if let usage = state.usageSummary {
                    GroupBox {
                        VStack(alignment: .leading, spacing: 11) {
                            HStack(spacing: 9) {
                                usageMetricCard(
                                    title: "Input",
                                    value: compactNumber(usage.totals.inputTokens),
                                    detail: "payload tokens",
                                    symbol: "arrow.down.left"
                                )
                                usageMetricCard(
                                    title: "Output",
                                    value: compactNumber(usage.totals.outputTokens),
                                    detail: "payload tokens",
                                    symbol: "arrow.up.right"
                                )
                                usageMetricCard(
                                    title: "Tool Calls",
                                    value: compactNumber(usage.totals.calls),
                                    detail: "\(usage.totals.errorCount) errors",
                                    symbol: "hammer"
                                )
                                usageMetricCard(
                                    title: "Latency",
                                    value: latencyPairText(
                                        p50: usage.totals.p50LatencyMs,
                                        p50Relation: usage.totals.p50LatencyRelation,
                                        p95: usage.totals.p95LatencyMs,
                                        p95Relation: usage.totals.p95LatencyRelation
                                    ),
                                    detail: "p50 / p95",
                                    symbol: "timer"
                                )
                            }

                            Divider()

                            HStack(spacing: 8) {
                                Label(
                                    "\(byteText(usage.totals.inputBytes + usage.totals.outputBytes)) payload",
                                    systemImage: "doc.text"
                                )
                                if usage.totals.imageCount > 0 {
                                    Label("\(usage.totals.imageCount) images", systemImage: "photo")
                                }
                                if usage.totals.binaryBytes > 0 {
                                    Label("\(byteText(usage.totals.binaryBytes)) binary", systemImage: "shippingbox")
                                }
                                Spacer()
                                if let since = usage.availableSince {
                                    Text("Available since \(since)")
                                } else {
                                    Text("Collection starts with the first measured call")
                                }
                            }
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        }
                        .padding(2)
                    } label: {
                        Label("MCP Payload Tokens", systemImage: "waveform.path.ecg")
                    }

                    GroupBox {
                        VStack(alignment: .leading, spacing: 10) {
                            usageHeatmap(usage)

                            HStack(spacing: 8) {
                                Text("Less")
                                ForEach(0..<5, id: \.self) { level in
                                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                                        .fill(usageHeatColor(level: level))
                                        .frame(width: 11, height: 11)
                                }
                                Text("More")
                                Spacer()
                                Text(usageAggregation.rawValue)
                            }
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 2)
                    } label: {
                        Label("Activity", systemImage: "square.grid.3x3.fill")
                    }

                    if !usage.topTools.isEmpty {
                        GroupBox {
                            VStack(spacing: 0) {
                                let maxTokens = max(1, usage.topTools.map(\.totalTokens).max() ?? 1)
                                ForEach(Array(usage.topTools.enumerated()), id: \.element.id) { index, tool in
                                    VStack(spacing: 7) {
                                        HStack {
                                            VStack(alignment: .leading, spacing: 2) {
                                                Text(tool.tool)
                                                    .font(.system(.caption, design: .monospaced).weight(.medium))
                                                    .lineLimit(1)
                                                Text("\(compactNumber(tool.calls)) calls · \(compactNumber(tool.totalTokens)) tokens")
                                                    .font(.caption2)
                                                    .foregroundStyle(.secondary)
                                            }
                                            Spacer()
                                            if tool.errorCount > 0 {
                                                Label("\(tool.errorCount)", systemImage: "exclamationmark.circle")
                                                    .font(.caption2)
                                                    .foregroundStyle(.orange)
                                            }
                                        }
                                        ProgressView(value: Double(tool.totalTokens), total: Double(maxTokens))
                                            .controlSize(.mini)
                                    }
                                    .padding(.vertical, 8)
                                    if index < usage.topTools.count - 1 { Divider() }
                                }
                            }
                        } label: {
                            Label("Top Tools", systemImage: "list.number")
                        }
                    }

                    GroupBox {
                        VStack(alignment: .leading, spacing: 12) {
                            HStack(alignment: .center) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("Provider Tokens")
                                        .font(.subheadline.weight(.semibold))
                                    Text("Native delegated-agent usage. Never added to MCP Payload Tokens.")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Picker("Period", selection: $providerUsagePeriod) {
                                    ForEach(ProviderUsagePeriod.allCases) { period in
                                        Text(period.rawValue).tag(period)
                                    }
                                }
                                .labelsHidden()
                                .pickerStyle(.segmented)
                                .frame(width: 150)
                                .onChange(of: providerUsagePeriod) { period in
                                    Task {
                                        await state.refreshProviderUsage(days: period.days)
                                    }
                                }
                            }

                            if state.providerUsageLoading && state.providerUsageSummary == nil {
                                HStack {
                                    ProgressView().controlSize(.small)
                                    Text("Loading provider usage…")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                    Spacer()
                                }
                            } else {
                                HStack(alignment: .top, spacing: 10) {
                                    providerUsageCard(
                                        title: "Codex",
                                        symbol: "terminal",
                                        usage: state.providerUsageSummary?.providers["codex"]
                                    )
                                    providerUsageCard(
                                        title: "OpenCode",
                                        symbol: "chevron.left.forwardslash.chevron.right",
                                        usage: state.providerUsageSummary?.providers["opencode"]
                                    )
                                }
                            }

                            if let issue = state.providerUsageIssue {
                                Label(issue, systemImage: "exclamationmark.triangle")
                                    .font(.caption2)
                                    .foregroundStyle(.orange)
                            } else if let summary = state.providerUsageSummary {
                                HStack(alignment: .top) {
                                    Text(summary.metricScope)
                                        .fixedSize(horizontal: false, vertical: true)
                                    Spacer(minLength: 12)
                                    if let since = summary.availableSince {
                                        Text("Since \(since)")
                                    }
                                }
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                            }
                        }
                        .padding(.vertical, 2)
                    } label: {
                        Label("Delegated Agents", systemImage: "cpu")
                    }

                    GroupBox {
                        VStack(alignment: .leading, spacing: 7) {
                            Text(usage.metricScope)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            HStack(spacing: 14) {
                                Label("Input = tool arguments", systemImage: "arrow.down.left")
                                Label("Output = tool result", systemImage: "arrow.up.right")
                                Spacer()
                            }
                            .font(.caption2)
                            .foregroundStyle(.secondary)

                            HStack {
                                Text("Metric")
                                Spacer()
                                Text(usage.tokenizerId)
                                    .font(.system(.caption2, design: .monospaced))
                            }
                            .font(.caption2)
                            .foregroundStyle(.tertiary)

                            if let diagnostics = usage.diagnostics,
                               (diagnostics.queueDropped ?? 0) > 0 || (diagnostics.workerErrors ?? 0) > 0 {
                                Label(
                                    "Metering diagnostics: \(diagnostics.queueDropped ?? 0) dropped · \(diagnostics.workerErrors ?? 0) worker errors",
                                    systemImage: "exclamationmark.triangle"
                                )
                                .font(.caption2)
                                .foregroundStyle(.orange)
                            }
                        }
                    } label: {
                        Label("Measurement", systemImage: "ruler")
                    }
                } else if state.usageLoading {
                    GroupBox {
                        HStack {
                            ProgressView().controlSize(.small)
                            Text("Loading Usage…")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Spacer()
                        }
                        .padding(.vertical, 8)
                    }
                } else {
                    GroupBox {
                        Text("Usage becomes available after the first measured MCP tool call.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .padding(.vertical, 8)
                    }
                }

                Spacer(minLength: 0)
            }
            .padding(22)
        }
    }

    private func providerUsageCard(
        title: String,
        symbol: String,
        usage: ProviderUsageProvider?
    ) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Label(title, systemImage: symbol)
                    .font(.subheadline.weight(.semibold))
                Spacer()
                if let usage, usage.available != false {
                    Text(providerSourceLabel(usage.source))
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 3)
                        .background(.quaternary, in: Capsule())
                }
            }

            if let usage, usage.available != false, usage.turns > 0 {
                HStack(alignment: .firstTextBaseline) {
                    Text(providerTokenText(usage.totalTokens))
                        .font(.title3.weight(.semibold))
                        .monospacedDigit()
                        .help(providerExactTokenText(usage.totalTokens))
                    Text("total tokens")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Spacer()
                }

                HStack(spacing: 8) {
                    providerUsageMetric("Input", usage.inputTokens)
                    providerUsageMetric("Cache R", usage.cacheReadTokens)
                    providerUsageMetric("Cache W", usage.cacheWriteTokens)
                    providerUsageMetric("Output", usage.outputTokens)
                    providerUsageMetric("Reason", usage.reasoningTokens)
                }

                HStack {
                    Text("\(usage.turns) turns · \(usage.agents) agents")
                    Spacer()
                    if usage.models.isEmpty {
                        Text("Model unattributed")
                    }
                }
                .font(.caption2)
                .foregroundStyle(.tertiary)

                if !usage.models.isEmpty {
                    DisclosureGroup("By model") {
                        VStack(spacing: 5) {
                            ForEach(usage.models) { model in
                                HStack {
                                    Text(model.model)
                                        .font(.system(.caption2, design: .monospaced))
                                        .lineLimit(1)
                                    Spacer()
                                    Text(providerTokenText(model.totalTokens))
                                        .font(.caption2.monospacedDigit())
                                        .help(providerExactTokenText(model.totalTokens))
                                }
                            }
                        }
                        .padding(.top, 5)
                    }
                    .font(.caption2)
                }
            } else {
                Text("No provider-reported usage in this period.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(minHeight: 62, alignment: .leading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(11)
        .background(
            .quaternary.opacity(0.38),
            in: RoundedRectangle(cornerRadius: 10, style: .continuous)
        )
    }

    private func providerUsageMetric(_ title: String, _ value: Int?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 9))
                .foregroundStyle(.tertiary)
            Text(providerTokenText(value))
                .font(.caption.weight(.medium).monospacedDigit())
                .help(providerExactTokenText(value))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func providerSourceLabel(_ source: String?) -> String {
        switch source {
        case "report": return "Exact · Provider"
        case "native_tokenizer": return "Native tokenizer"
        case "estimate": return "Estimate"
        case "mixed": return "Mixed sources"
        default: return "Provider"
        }
    }

    private func providerTokenText(_ value: Int?) -> String {
        guard let value else { return "—" }
        return compactNumber(value)
    }

    private func providerExactTokenText(_ value: Int?) -> String {
        guard let value else { return "Unavailable for part of this period" }
        return NumberFormatter.localizedString(
            from: NSNumber(value: value),
            number: .decimal
        ) + " tokens"
    }

    @ViewBuilder
    private func usageHeatmap(_ usage: UsageSummaryEnvelope) -> some View {
        let cells = usageHeatCells(usage)
        let paddedCount = ((cells.count + 6) / 7) * 7
        let padded: [UsageHeatCell?] = cells.map(Optional.some)
            + Array(repeating: nil, count: max(0, paddedCount - cells.count))
        let columns = stride(from: 0, to: padded.count, by: 7).map {
            Array(padded[$0..<min($0 + 7, padded.count)])
        }
        let maxValue = max(1, cells.map(\.value).max() ?? 1)

        GeometryReader { proxy in
            let spacing: CGFloat = 3
            let weekCount = max(1, columns.count)
            let totalSpacing = CGFloat(max(0, weekCount - 1)) * spacing
            let fittedSize = (proxy.size.width - totalSpacing) / CGFloat(weekCount)
            let cellSize = min(10, max(7, fittedSize))

            HStack(alignment: .top, spacing: spacing) {
                ForEach(Array(columns.enumerated()), id: \.offset) { _, week in
                    VStack(spacing: spacing) {
                        ForEach(Array(week.enumerated()), id: \.offset) { _, cell in
                            if let cell {
                                let ratio = Double(cell.value) / Double(maxValue)
                                RoundedRectangle(cornerRadius: 2, style: .continuous)
                                    .fill(usageHeatColor(ratio: ratio, active: cell.value > 0))
                                    .frame(width: cellSize, height: cellSize)
                                    .help("\(cell.dateKey) · \(compactNumber(cell.value)) tokens · \(cell.calls) calls")
                            } else {
                                Color.clear.frame(width: cellSize, height: cellSize)
                            }
                        }
                    }
                }
            }
        }
        .frame(height: 88)
        .accessibilityLabel("MCP Payload Tokens activity heatmap")
    }

    private func usageHeatCells(_ usage: UsageSummaryEnvelope) -> [UsageHeatCell] {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        let calendar = Calendar(identifier: .gregorian)
        let today = calendar.startOfDay(for: Date())
        let start = calendar.date(byAdding: .day, value: -364, to: today) ?? today
        let dailyMap = Dictionary(uniqueKeysWithValues: usage.daily.map { ($0.date, $0) })

        var base: [(String, Int, Int, Date)] = []
        for offset in 0..<365 {
            guard let date = calendar.date(byAdding: .day, value: offset, to: start) else { continue }
            let key = formatter.string(from: date)
            let day = dailyMap[key]
            base.append((key, day?.totalTokens ?? 0, day?.calls ?? 0, date))
        }

        switch usageAggregation {
        case .daily:
            return base.map { UsageHeatCell(dateKey: $0.0, value: $0.1, calls: $0.2) }
        case .weekly:
            var weekTotals: [String: (tokens: Int, calls: Int)] = [:]
            for row in base {
                let comps = calendar.dateComponents([.yearForWeekOfYear, .weekOfYear], from: row.3)
                let weekKey = "\(comps.yearForWeekOfYear ?? 0)-\(comps.weekOfYear ?? 0)"
                let current = weekTotals[weekKey] ?? (0, 0)
                weekTotals[weekKey] = (current.tokens + row.1, current.calls + row.2)
            }
            return base.map { row in
                let comps = calendar.dateComponents([.yearForWeekOfYear, .weekOfYear], from: row.3)
                let key = "\(comps.yearForWeekOfYear ?? 0)-\(comps.weekOfYear ?? 0)"
                let total = weekTotals[key] ?? (0, 0)
                return UsageHeatCell(dateKey: row.0, value: total.tokens, calls: total.calls)
            }
        case .cumulative:
            var tokens = 0
            var calls = 0
            return base.map { row in
                tokens += row.1
                calls += row.2
                return UsageHeatCell(dateKey: row.0, value: tokens, calls: calls)
            }
        }
    }

    private func usageHeatColor(ratio: Double, active: Bool) -> Color {
        guard active else { return Color.secondary.opacity(0.10) }
        let clamped = max(0.0, min(1.0, ratio))
        return Color.accentColor.opacity(0.24 + 0.70 * sqrt(clamped))
    }

    private func usageHeatColor(level: Int) -> Color {
        guard level > 0 else { return Color.secondary.opacity(0.10) }
        return Color.accentColor.opacity(0.20 + Double(level) * 0.17)
    }

    private func usageMetricCard(title: String, value: String, detail: String, symbol: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Label(title, systemImage: symbol)
                .font(.caption2.weight(.medium))
                .foregroundStyle(.secondary)
            Text(value)
                .font(.title3.weight(.semibold))
                .monospacedDigit()
            Text(detail)
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
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
                sectionLead("Browser Companions", subtitle: "Visual Companion status and background-safe browser integration.")

                settingsDataStateRow(state.browserSettingsState) {
                    state.refreshSafariExtensionState()
                }

                GroupBox("Safari") {
                    HStack(spacing: 12) {
                        ZStack {
                            RoundedRectangle(cornerRadius: 9, style: .continuous)
                                .fill(.quaternary)
                                .frame(width: 38, height: 38)
                            Image(systemName: state.safariExtensionEnabled ? "safari.fill" : "safari")
                                .foregroundStyle(state.safariExtensionEnabled ? Color.accentColor : Color.secondary)
                        }
                        VStack(alignment: .leading, spacing: 3) {
                            Text(state.safariExtensionEnabled ? "Visual Companion · On" : "Safari Visual Companion")
                                .font(.subheadline.weight(.semibold))
                            Text(state.safariExtensionEnabled
                                 ? "Shows when Mac MCP is actively using a Safari page."
                                 : (state.safariExtensionRegistered
                                    ? "Enable the bundled extension in Safari, then allow website access."
                                    : "Unsigned build: Safari developer setup is required."))
                                .font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                        if state.safariExtensionEnabled {
                            Button { state.refreshSafariExtensionState() } label: { Image(systemName: "arrow.clockwise") }
                                .help("Refresh extension status")
                        } else if state.safariExtensionRegistered {
                            Button("Enable in Safari…") { state.openSafariExtensionPreferences() }
                        } else {
                            Button("Developer Setup…") { state.openSafariExtensionPreferences() }
                        }
                    }
                    .padding(.top, 5)
                }

                GroupBox("Google Chrome") {
                    HStack(spacing: 12) {
                        ZStack {
                            RoundedRectangle(cornerRadius: 9, style: .continuous)
                                .fill(.quaternary)
                                .frame(width: 38, height: 38)
                            Image(systemName: "globe")
                                .foregroundStyle(Color.secondary)
                        }
                        VStack(alignment: .leading, spacing: 3) {
                            Text("Chrome Visual Companion").font(.subheadline.weight(.semibold))
                            Text("Enables focus-safe background tabs, DOM/page actions, and background-safe visual capture.")
                                .font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                        Button("Chrome Setup…") { state.openChromeExtensionSetup() }
                    }
                    .padding(.top, 5)
                }

                HStack {
                    Text("Safari status").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Text(state.safariExtensionStatus).font(.caption.weight(.semibold))
                }

                Spacer(minLength: 0)
            }
            .padding(20)
        }
    }

    private var permissionsPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader("Permissions & Safety", subtitle: "Review capability boundaries, approval behavior, and the active security profile.")

                settingsDataStateRow(state.permissionsSettingsState) {
                    Task { await state.refresh() }
                }
                if let issue = state.permissionActionIssue {
                    inlineError(issue)
                }

                if let semantics = state.securitySemantics {
                    if let profile = semantics.profiles.first(where: { $0.name == semantics.activeProfile }) {
                        GroupBox("Active Profile") {
                            VStack(alignment: .leading, spacing: 10) {
                                HStack {
                                    Text("Profile").foregroundStyle(.secondary)
                                    Spacer()
                                    Text(profileDisplayName(profile.name)).fontWeight(.semibold)
                                }
                                Divider()
                                Label("Allowed Capabilities", systemImage: "lock.shield")
                                    .font(.caption.weight(.semibold))
                                Text(profile.allowedCapabilities.map(capabilityDisplayName).joined(separator: " · "))
                                    .font(.caption).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                HStack {
                                    Text("Destructive families").font(.caption).foregroundStyle(.secondary)
                                    Spacer()
                                    Text(destructiveFamiliesLabel(profile.destructiveFamilies)).font(.caption.weight(.medium))
                                }
                                HStack {
                                    Text("Agent access ceiling").font(.caption).foregroundStyle(.secondary)
                                    Spacer()
                                    Text(capabilityDisplayName(profile.accessModeCeiling)).font(.caption.weight(.medium))
                                }
                            }
                            .padding(.top, 5)
                        }

                        GroupBox("Approval Behavior") {
                            VStack(alignment: .leading, spacing: 9) {
                                HStack {
                                    Text("Source").font(.caption).foregroundStyle(.secondary)
                                    Spacer()
                                    Text(approvalSourceDisplayName(profile.approvalBehavior.source))
                                        .font(.caption.weight(.semibold))
                                }
                                Text(profile.approvalBehavior.summary)
                                    .font(.caption).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                Text("Capability allowed means the server permits it. Approval is separate; a confirmation prompt is not guaranteed.")
                                    .font(.caption.weight(.medium)).foregroundStyle(.orange)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .padding(.top, 5)
                        }

                        if let serverApproval = semantics.serverApproval {
                            GroupBox("Server Approval") {
                                VStack(alignment: .leading, spacing: 10) {
                                    HStack {
                                        Text("Risk profile").font(.caption).foregroundStyle(.secondary)
                                        Spacer()
                                        Text(profileDisplayName(serverApproval.activeProfile))
                                            .font(.caption.weight(.semibold))
                                    }
                                    HStack {
                                        Text("Approval source").font(.caption).foregroundStyle(.secondary)
                                        Spacer()
                                        Text(approvalSourceDisplayName(serverApproval.source))
                                            .font(.caption.weight(.medium))
                                    }
                                    HStack {
                                        Text("Headless behavior").font(.caption).foregroundStyle(.secondary)
                                        Spacer()
                                        Text(serverApproval.headlessBehavior.capitalized)
                                            .font(.caption.weight(.medium))
                                    }
                                    HStack {
                                        Text("Timeout").font(.caption).foregroundStyle(.secondary)
                                        Spacer()
                                        Text("\(serverApproval.approvalTimeoutS)s · \(serverApproval.timeoutBehavior.capitalized)")
                                            .font(.caption.weight(.medium))
                                    }
                                    HStack {
                                        Text("Remote sessions").font(.caption).foregroundStyle(.secondary)
                                        Spacer()
                                        Text("Approve on this Mac")
                                            .font(.caption.weight(.medium))
                                    }
                                    Text(serverApproval.summary ?? "Risk-based server approval status unavailable.")
                                        .font(.caption).foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)

                                    if !serverApproval.configValid {
                                        Label(
                                            "Invalid Server Approval configuration. High-risk calls fail closed until repaired.",
                                            systemImage: "exclamationmark.shield"
                                        )
                                        .font(.caption.weight(.medium))
                                        .foregroundStyle(.orange)
                                        .fixedSize(horizontal: false, vertical: true)
                                    }

                                    Divider()

                                    ForEach(serverApproval.availableProfiles) { item in
                                        Button { state.setServerApprovalProfile(item.name) } label: {
                                            HStack(alignment: .top, spacing: 8) {
                                                Image(systemName: item.name == serverApproval.activeProfile ? "checkmark.circle.fill" : "circle")
                                                    .foregroundStyle(item.name == serverApproval.activeProfile ? Color.accentColor : Color.secondary)
                                                VStack(alignment: .leading, spacing: 2) {
                                                    Text(profileDisplayName(item.name))
                                                        .font(.caption.weight(.semibold))
                                                    Text(item.summary)
                                                        .font(.caption2)
                                                        .foregroundStyle(.secondary)
                                                        .fixedSize(horizontal: false, vertical: true)
                                                }
                                                Spacer(minLength: 8)
                                            }
                                            .padding(.vertical, 3)
                                            .contentShape(Rectangle())
                                        }
                                        .buttonStyle(.plain)
                                        .disabled(state.serverApprovalProfileChanging)
                                    }

                                    Text(serverApproval.doublePromptGuidance)
                                        .font(.caption2.weight(.medium))
                                        .foregroundStyle(.orange)
                                        .fixedSize(horizontal: false, vertical: true)
                                    Text("Remote callers cannot approve locally on their behalf. Allow Once is exact-action and single-use.")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                .padding(.top, 5)
                            }
                        }

                        if semantics.profileWasNormalized == true,
                           let configured = semantics.normalizedFromProfile ?? semantics.configuredProfile {
                            Label(
                                "Configured \(profileDisplayName(configured)) is not a global server preset. "
                                + "Mac MCP is safely using \(profileDisplayName(semantics.activeProfile)).",
                                systemImage: "exclamationmark.shield"
                            )
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                        }

                        GroupBox("Profiles") {
                            VStack(spacing: 4) {
                                ForEach(semantics.profiles) { item in
                                    Button { state.setPermissionProfile(item.name) } label: {
                                        HStack(spacing: 8) {
                                            Image(systemName: item.active ? "checkmark.circle.fill" : "circle")
                                                .foregroundStyle(item.active ? Color.accentColor : Color.secondary)
                                            Text(profileDisplayName(item.name)).font(.caption.weight(.semibold))
                                            Spacer()
                                            Text("\(item.allowedCapabilities.count) caps · Approval \(approvalSourceDisplayName(item.approvalBehavior.source))")
                                                .font(.caption2).foregroundStyle(.secondary)
                                        }
                                        .padding(.vertical, 4)
                                        .contentShape(Rectangle())
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                            .padding(.top, 5)
                        }
                    } else {
                        Text("Unknown permission profile: \(semantics.activeProfile). Calls fail closed until a known profile is configured.")
                            .font(.caption).foregroundStyle(.orange)
                    }
                } else {
                    Text(state.serverRunning ? "Permission semantics unavailable." : "Start the server to inspect permission semantics.")
                        .font(.caption).foregroundStyle(.secondary)
                }

                Spacer(minLength: 0)
            }
            .padding(20)
        }
    }

    private var connectionsPane: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 13) {
                paneHeader(
                    "Connections",
                    subtitle: "Browser companions, mobile access, and the public endpoint used to reach this Mac."
                )

                Picker("", selection: $connectionsTab) {
                    ForEach(ConnectionsTab.allCases) { item in
                        Label(item.title, systemImage: item.symbol).tag(item)
                    }
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .frame(maxWidth: 300)

                publicEndpointCard
            }
            .padding(.horizontal, 22)
            .padding(.top, 22)
            .padding(.bottom, 12)

            Divider()

            if connectionsTab == .browser {
                browserPane
            } else {
                mobilePane
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
                    .font(.caption)

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
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.top, 6)
                    }
                    .font(.caption)
                }

                saveFeedbackRow(.endpoint)
            }
            .padding(.top, 5)
        }
    }

    private var voicePane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader("Voice", subtitle: "Configure the experimental Ask User Voice tool and audio devices.")
                saveFeedbackRow(.voice)

                Toggle(isOn: $settings.voiceEnabled) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Ask User Voice").font(.subheadline.weight(.semibold))
                        Text("Off by default. Agents fall back to a text question (ask_user) when this is off or you decline a recording.")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                .onChange(of: settings.voiceEnabled) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }

                Label {
                    Text("Each voice question sends its text to Microsoft's online text-to-speech, and your recorded answer to Groq for transcription; Groq's own retention applies. The recording is deleted from this Mac afterwards and the transcript is not kept in Mac MCP's activity history.")
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "network")
                }
                .font(.caption)
                .foregroundStyle(.secondary)

                Toggle(isOn: $settings.voiceAskEveryTime) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Ask before every recording").font(.subheadline.weight(.medium))
                        Text(settings.voiceAskEveryTime
                             ? "A dialog asks you to Record, Always Allow or decline before anything is recorded or sent."
                             : "Recordings are sent without asking because you chose Always Allow. Turn this on to be asked again.")
                            .font(.caption2).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .disabled(!settings.voiceEnabled)
                .onChange(of: settings.voiceAskEveryTime) { _ in
                    // Values re-read from disk are not edits and are not written back.
                    if settings.voiceConsentEdited { persistSettings(scope: .voice, success: "Saved · applies live") }
                }

                Divider()

                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Label(
                            settings.hasGroqKey ? "Groq key configured" : "Groq key required",
                            systemImage: settings.hasGroqKey ? "checkmark.shield.fill" : "key"
                        )
                        .font(.caption)
                        .foregroundStyle(settings.hasGroqKey ? Color.secondary : Color.orange)
                        Spacer()
                        Button { audio.refresh() } label: { Image(systemName: "arrow.clockwise") }
                            .help("Refresh audio devices")
                    }
                    HStack {
                        SecureField(settings.hasGroqKey ? "Replace Groq API key" : "Groq API key", text: $groqKey)
                        Button("Save") { saveGroqKey() }
                            .disabled(groqKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        if settings.hasGroqKey {
                            Button { removeGroqKey() } label: { Image(systemName: "trash") }
                                .help("Remove key")
                        }
                    }
                }

                Divider()

                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 12) {
                    GridRow {
                        Text("Input").foregroundStyle(.secondary)
                        Picker("", selection: $settings.inputDevice) {
                            Text("Auto").tag("auto")
                            Text("Built-in Mic").tag("built-in")
                            ForEach(audio.inputs) { device in Text(device.name).tag(device.settingValue) }
                        }
                        .labelsHidden()
                    }
                    GridRow {
                        Text("Output").foregroundStyle(.secondary)
                        Picker("", selection: $settings.outputDevice) {
                            Text("System").tag("system")
                            Text("Built-in Speakers").tag("built-in")
                            ForEach(audio.outputs) { device in Text(device.name).tag(device.settingValue) }
                        }
                        .labelsHidden()
                    }
                    GridRow {
                        Text("Language").foregroundStyle(.secondary)
                        HStack {
                            Picker("", selection: $settings.language) {
                                Text("Auto").tag("auto")
                                Text("Turkish").tag("tr")
                                Text("English").tag("en")
                            }
                            .labelsHidden()
                            Stepper("Timeout: \(settings.timeoutSeconds)s", value: $settings.timeoutSeconds, in: 5...180, step: 5)
                                .frame(maxWidth: 170)
                        }
                    }
                    GridRow {
                        Text("Voice").foregroundStyle(.secondary)
                        HStack {
                            TextField("tr-TR-AhmetNeural", text: $settings.voiceName)
                            Text("Rate").foregroundStyle(.secondary)
                            TextField("-5%", text: $settings.ttsRate).frame(width: 58)
                        }
                    }
                }
                .font(.caption)
                .onChange(of: settings.inputDevice) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }
                .onChange(of: settings.outputDevice) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }
                .onChange(of: settings.language) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }
                .onChange(of: settings.timeoutSeconds) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }
                .onChange(of: settings.voiceName) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }
                .onChange(of: settings.ttsRate) { _ in persistSettings(scope: .voice, success: "Saved · applies live") }

                Spacer(minLength: 0)
            }
            .padding(20)
        }
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
                            .font(.caption)
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
                                            .font(.subheadline.weight(.semibold))
                                        Text("Or pair from the Home Screen app with this one-time code:")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)

                                        if let code = state.mobilePairingCode {
                                            Text(code)
                                                .font(.system(size: 22, weight: .semibold, design: .monospaced))
                                                .tracking(2)
                                                .textSelection(.enabled)
                                                .padding(.vertical, 5)
                                        }

                                        Text("The QR secret stays in the URL fragment; the manual code is short-lived and single-use.")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                            .fixedSize(horizontal: false, vertical: true)
                                        if let expiresAt = state.mobilePairingExpiresAt {
                                            Text("Expires " + Date(timeIntervalSince1970: expiresAt).formatted(date: .omitted, time: .shortened))
                                                .font(.caption2)
                                                .foregroundStyle(.secondary)
                                        }
                                        Text("After pairing, the device receives its own read-only session. Your MCP API key and local dashboard token are never sent to the phone.")
                                            .font(.caption2)
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
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .padding(.vertical, 6)
                            case .error, .unavailable:
                                Text("Connected devices could not be loaded. Use Retry above.")
                                    .font(.caption)
                                    .foregroundStyle(.orange)
                                    .padding(.vertical, 6)
                            default:
                                Text("No paired mobile devices.")
                                    .font(.caption)
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
                                            .font(.subheadline.weight(.medium))
                                        Text("Last seen " + Date(timeIntervalSince1970: device.lastSeenAt).formatted(date: .abbreviated, time: .shortened))
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Text("Read-only")
                                        .font(.system(size: 9, weight: .semibold))
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
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                Spacer(minLength: 0)
            }
            .padding(20)
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
                                .font(.caption2.weight(.medium))
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
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("Steering session visibility")
                                .font(.subheadline.weight(.semibold))
                            Text("How long inactive delegated sessions stay visible in the control surface.")
                                .font(.caption2)
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
                        .onChange(of: settings.steeringSessionMinutes) { _ in persistSettings(scope: .runtime, success: "Saved · applies live") }
                    }
                    .padding(.top, 5)
                }

                decisionAccelerationBox

                Label(
                    "Changes here affect runtime behavior; public endpoint and mobile/browser connectivity now live under Connections.",
                    systemImage: "wrench.and.screwdriver"
                )
                .font(.caption)
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
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("OpenAI Decisions for ambiguous targets")
                            .font(.subheadline.weight(.semibold))
                        Text("Off by default. When off, or without a verified key, browser and Mac actions run exactly as before.")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Toggle("", isOn: $settings.decisionAccelerationEnabled)
                        .labelsHidden()
                        .toggleStyle(.switch)
                        .onChange(of: settings.decisionAccelerationEnabled) { _ in
                            persistSettings(scope: .decisions, success: "Saved · applies live")
                            Task { await state.refreshDecisionAcceleration(reloadKey: true) }
                        }
                }

                HStack {
                    Label(decisionKeyStatusText, systemImage: decisionKeyStatusSymbol)
                        .font(.caption)
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
                            .help("Remove key")
                    }
                }

                Text("Only used when the deterministic resolver finds several equally likely targets. The key is stored in Keychain, never in settings.json.")
                    .font(.caption2)
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

    private var decisionKeyStatusText: String {
        switch decisionKeyStatus {
        case "missing":
            return "No key · Decision layer inactive"
        case "valid":
            return settings.decisionAccelerationEnabled ? "Key verified · Decision layer active" : "Key verified · turn on to use"
        case "invalid":
            return "Key rejected by OpenAI · Decision layer inactive"
        default:
            if let status = state.decisionAcceleration?.status, status != "valid" {
                return "Key check: \(status.replacingOccurrences(of: "_", with: " "))"
            }
            return "Key saved · press Test to verify"
        }
    }

    private var decisionKeyStatusSymbol: String {
        switch decisionKeyStatus {
        case "valid": return "checkmark.shield.fill"
        case "invalid": return "xmark.shield"
        case "missing": return "key"
        default: return "questionmark.circle"
        }
    }

    private var decisionKeyStatusColor: Color {
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

    private var updateCard: some View {
        GroupBox("Updates") {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top, spacing: 18) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Current")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Text(currentUpdateVersion)
                            .font(.subheadline.weight(.semibold))
                        Text(currentUpdateCommit)
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    Divider().frame(height: 48)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Available")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Text(availableUpdateText)
                            .font(.subheadline.weight(.medium))
                            .lineLimit(2)
                        if state.updateCheckLoading {
                            Text("Checking verified release channel…")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        } else if state.updateCheckInfo?.releaseVerified == true {
                            Text("Verified release")
                                .font(.caption2)
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
                                .font(.subheadline.weight(.semibold))
                            Spacer()
                            if state.updateTransactionActive {
                                Text("Running")
                                    .font(.caption2.weight(.semibold))
                                    .foregroundStyle(.secondary)
                                    .padding(.horizontal, 7)
                                    .padding(.vertical, 3)
                                    .background(.quaternary, in: Capsule())
                            }
                        }
                        Text(progress.statusDetail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)

                        if !progress.steps.isEmpty {
                            VStack(spacing: 5) {
                                ForEach(progress.steps) { step in
                                    HStack(spacing: 8) {
                                        updateStepIcon(step.state)
                                            .frame(width: 15)
                                        Text(step.title)
                                            .font(.caption)
                                        Spacer()
                                        Text(step.detail)
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                            }
                            .padding(9)
                            .background(Color(nsColor: .controlBackgroundColor).opacity(0.55), in: RoundedRectangle(cornerRadius: 8))
                        }
                    }
                }

                Divider()
                HStack {
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

                    Button(state.updateActionTitle) {
                        state.installUpdate()
                    }
                    .disabled(!state.canInstallUpdate)

                    Spacer()

                    if state.updateTransactionActive {
                        Text("Updater transaction is active")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.top, 5)
        }
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
        HStack(alignment: .top, spacing: 8) {
            if dataState.phase == .loading {
                ProgressView().controlSize(.small)
            } else {
                Image(systemName: presentation.symbol)
                    .foregroundStyle(presentation.color)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(presentation.title)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(presentation.color)
                if let detail = presentation.detail {
                    Text(detail)
                        .font(.caption2)
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
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(presentation.color.opacity(0.07), in: RoundedRectangle(cornerRadius: 8))
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
        .font(.caption2)
        .foregroundStyle(.red)
    }

    @ViewBuilder
    private func saveFeedbackRow(_ scope: SettingsFeedbackScope) -> some View {
        if let feedback = saveFeedback, feedback.scope == scope {
            Label(
                feedback.message,
                systemImage: feedback.isError ? "exclamationmark.circle.fill" : "checkmark.circle.fill"
            )
            .font(.caption2.weight(.medium))
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
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func paneHeader(_ title: String, subtitle: String, refresh: Bool = false) -> some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.title2.weight(.semibold))
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if refresh {
                Button {
                    Task { await state.refreshProviders() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh provider detection")
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
                            .font(.subheadline.weight(.semibold))
                        Text("Used only when a delegated request omits provider, model, or thinking.")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Label(status.label, systemImage: status.symbol)
                        .font(.system(size: 9, weight: .semibold))
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
                    .frame(maxWidth: 250)
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
                    .frame(maxWidth: 250)
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
                    .frame(maxWidth: 250)
                    .disabled(settings.defaultAgentProvider.isEmpty || defaultReasoningOptions.isEmpty)
                }

                Text(status.detail)
                    .font(.caption2)
                    .foregroundStyle(status.color == .green ? Color.secondary : status.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.top, 4)
        } label: {
            Label("Delegation Default", systemImage: "slider.horizontal.3")
                .font(.caption.weight(.semibold))
        }
        .disabled(settings.providerSettingsLocked)
    }

    private func defaultAgentPickerRow<Content: View>(
        title: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(spacing: 12) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 72, alignment: .leading)
            Spacer()
            content()
        }
    }

    private var defaultProviderOptions: [String] {
        var values = state.providerStatuses
            .filter { $0.enabled && $0.detected }
            .map(\.id)
        let current = settings.defaultAgentProvider
        if !current.isEmpty && !values.contains(current) {
            values.append(current)
        }
        let order = ["opencode", "codex", "chatgpt"]
        return values.sorted {
            (order.firstIndex(of: $0) ?? 99) < (order.firstIndex(of: $1) ?? 99)
        }
    }

    private var defaultProviderCatalog: ProviderCatalogInfo? {
        guard !settings.defaultAgentProvider.isEmpty else { return nil }
        return state.providerCatalogs[settings.defaultAgentProvider]
    }

    private var defaultModelOptions: [AgentModelInfo] {
        defaultProviderCatalog?.modelItems ?? []
    }

    private var defaultSelectedModelInfo: AgentModelInfo? {
        guard let catalog = defaultProviderCatalog else { return nil }
        let modelID = settings.defaultAgentModel.isEmpty
            ? (catalog.defaultModel ?? "")
            : settings.defaultAgentModel
        guard !modelID.isEmpty else { return nil }
        return catalog.modelItems.first(where: { $0.id == modelID })
    }

    private var defaultReasoningOptions: [String] {
        defaultSelectedModelInfo?.reasoningValues ?? []
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

    private var defaultAgentStatus: (label: String, detail: String, symbol: String, color: Color) {
        let providerID = settings.defaultAgentProvider
        guard !providerID.isEmpty else {
            return (
                "Not configured",
                "Delegated requests must keep passing a provider explicitly until a default is selected.",
                "minus.circle",
                .secondary
            )
        }
        if state.providerStatuses.isEmpty {
            switch state.providerSettingsState.phase {
            case .loading:
                return ("Loading", "Checking the delegated provider before validating this default.", "arrow.clockwise", .secondary)
            case .error, .unavailable:
                return ("Unavailable", state.providerSettingsState.message ?? "Provider status could not be loaded.", "exclamationmark.triangle.fill", .orange)
            default:
                break
            }
        }
        guard let provider = state.providerStatuses.first(where: { $0.id == providerID }) else {
            return ("Unavailable", "The saved provider is not present in the latest provider response.", "exclamationmark.triangle.fill", .orange)
        }
        guard provider.enabled else {
            return ("Disabled", "Enable \(providerDisplayName(providerID)) before using it as the default.", "pause.circle.fill", .orange)
        }
        guard provider.detected else {
            return ("Unavailable", "\(providerDisplayName(providerID)) CLI is not currently detected.", "exclamationmark.triangle.fill", .orange)
        }
        guard let catalog = defaultProviderCatalog, catalog.available else {
            return ("Unavailable", "The live model catalog for \(providerDisplayName(providerID)) is unavailable.", "exclamationmark.triangle.fill", .orange)
        }
        if catalog.catalogFreshness == "stale" {
            return ("Stale", "The provider model catalog is stale. Refresh the provider before spawning this default.", "clock.badge.exclamationmark", .orange)
        }
        if !settings.defaultAgentModel.isEmpty && defaultSelectedModelInfo == nil {
            return ("Stale model", "\(settings.defaultAgentModel) is no longer in the live provider catalog.", "exclamationmark.triangle.fill", .orange)
        }
        if !settings.defaultAgentReasoning.isEmpty {
            guard let model = defaultSelectedModelInfo else {
                return ("Check thinking", "Choose a catalog-backed model before pinning a thinking level.", "exclamationmark.triangle.fill", .orange)
            }
            guard model.reasoningValues.contains(settings.defaultAgentReasoning) else {
                return (
                    "Unsupported",
                    "\(reasoningDisplayName(settings.defaultAgentReasoning)) is not supported by \(model.displayName).",
                    "exclamationmark.triangle.fill",
                    .orange
                )
            }
        }
        let modelName = defaultSelectedModelInfo?.displayName ?? "Provider default model"
        let thinking = settings.defaultAgentReasoning.isEmpty
            ? "provider default thinking"
            : reasoningDisplayName(settings.defaultAgentReasoning)
        return (
            "Ready",
            "\(providerDisplayName(providerID)) · \(modelName) · \(thinking). Explicit spawn arguments still take precedence.",
            "checkmark.circle.fill",
            .green
        )
    }

    private func reasoningDisplayName(_ value: String) -> String {
        switch value.lowercased() {
        case "xhigh": return "Extra High"
        case "extra-high": return "Extra High"
        case "max": return "Max"
        case "ultra": return "Ultra"
        default: return value.capitalized
        }
    }

    private var concurrencyCard: some View {
        GroupBox {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Agents at Once")
                        .font(.subheadline.weight(.semibold))
                    Text("Most delegated agents that run together; more wait in line. Fewer is gentler on memory. New agents also wait while macOS reports memory pressure.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 12)
                Stepper(value: maxActiveBinding, in: 1...16) {
                    Text("\(settings.maxActiveAgents)")
                        .font(.body.monospacedDigit().weight(.semibold))
                        .frame(minWidth: 22, alignment: .trailing)
                }
                .fixedSize()
                .accessibilityLabel("Agents at once")
                .accessibilityValue("\(settings.maxActiveAgents)")
            }
            .padding(4)
        }
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
                case .fresh: return "Not detected"
                }
            }
            return detected ? "Detected" : "Not detected"
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

        return VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 10) {
                Image(systemName: detected ? "checkmark.circle.fill" : "minus.circle")
                    .foregroundStyle(detectionColor)
                    .font(.system(size: 16, weight: .medium))
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 7) {
                        Text(title).font(.subheadline.weight(.semibold))
                        Text(detectionLabel)
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(detectionColor)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(.quaternary, in: Capsule())
                    }
                    Text(subtitle).font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("", isOn: providerBinding(id))
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .disabled(settings.providerSettingsLocked)
                    .help(
                        settings.providerSettingsLocked
                            ? "Repair settings.json before changing delegated providers."
                            : (settings.providerEnabled(id) ? "Disable \(title)" : "Enable \(title)")
                    )
            }

            if detected {
                HStack(spacing: 6) {
                    if let version, !version.isEmpty {
                        Text(version).font(.caption2).foregroundStyle(.secondary)
                    }
                    if let path, !path.isEmpty {
                        if version != nil { Text("·").font(.caption2).foregroundStyle(.tertiary) }
                        Text(path).font(.caption2.monospaced()).foregroundStyle(.tertiary).lineLimit(1)
                    }
                }
                .padding(.leading, 26)
            }
        }
        .padding(12)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
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

    private var agentCompletionNotificationsBinding: Binding<Bool> {
        Binding(
            get: { settings.agentCompletionNotificationsEnabled },
            set: { enabled in
                state.setAgentCompletionNotificationsEnabled(enabled)
            }
        )
    }

    private var showToolActivityBinding: Binding<Bool> {
        Binding(
            get: { settings.showToolActivity && settings.requireToolDescriptions },
            set: { enabled in
                settings.showToolActivity = enabled && settings.requireToolDescriptions
                persistSettings(success: enabled ? "Tool Activity bubble enabled." : "Tool Activity bubble hidden.")
                state.restartToolActivityStream()
                if !enabled {
                    ToolActivityBubbleController.shared.hideImmediately()
                }
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
            saveFeedback = SettingsSaveFeedback(scope: .decisions, message: "Key removed · Decision layer inactive", isError: false)
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
        switch value.lowercased() {
        case "read_only": return "Read Only"
        case "standard": return "Standard"
        case "trusted": return "Trusted"
        default: return value.replacingOccurrences(of: "_", with: " ").capitalized
        }
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
        switch id {
        case "opencode": return "OpenCode"
        case "codex": return "Codex"
        case "chatgpt": return "ChatGPT Web CLI"
        default: return id
        }
    }
}
