import AppKit
import SwiftUI

struct MenuBarView: View {
    @ObservedObject var state: AppState
    @Environment(\.colorScheme) private var colorScheme
    // Read once: the header redraws with every pulse while work runs.
    private static let markDark = BrandArt.image("BrandMark")
    private static let markLight = BrandArt.image("BrandMarkLight")
    @ObservedObject var settings: SettingsStore
    /// The question window's queue. Read through onReceive, not observed: its countdown ticks every second.
    let prompts: OwnerPromptModel
    @StateObject private var audio = AudioDeviceStore()
    @MacMCPState private var groqKey = ""
    @MacMCPState private var settingsMessage = ""
    @MacMCPState private var showVoice = false
    @MacMCPState private var showAdvanced = false
    @MacMCPState private var sessionDetailID: String?
    @MacMCPState private var showSessions = false
    @MacMCPState private var showSecurity = false
    @MacMCPState private var showAllAgents = false
    @MacMCPState private var showActivity = false
    /// #20: the "n failed" count opens the list showing only the failed calls.
    @MacMCPState private var showFailedOnly = false
    /// The content's own height (#200): the menu is as tall as what it shows, up to the screen.
    @MacMCPState private var contentHeight: CGFloat = 0
    /// The popover window: MenuBarExtra sizes it once when it opens, so later changes are applied here.
    @MacMCPState private var host = HostWindow()
    /// Questions waiting in Mac MCP's window, for the header line (#123).
    @MacMCPState private var questionsWaiting = 0
    @MacMCPState private var questionAskedBy: String?
    // Follow the user's text size: glyph sizes scale with it.
    @ScaledMetric(relativeTo: .caption2) private var glyphScale: CGFloat = 1

    private static let sessionsAnchor = "sessions"
    private static let agentsAnchor = "agents"
    private static let recentActivityLimit = 8
    /// The agent list shows at most this many agents, inline in the menu's one scroll region (#123).
    private static let agentListLimit = 20
    /// Active teams shown as rows; more are counted in a "+N" row.
    private static let teamRowLimit = 2

    var body: some View {
        // One scroll region: lists below expand inline instead of scrolling inside the popover.
        ScrollViewReader { proxy in
            ScrollView {
                // One surface (#201): header, callouts, chips, then rows in groups split by dividers.
                VStack(alignment: .leading, spacing: 10) {
                    header
                    if let card = menuCard { cardView(card).id(card).transition(AppMotion.arrive) }
                    actionBar
                    if state.serverRunning { chipsRow.transition(.opacity) }
                    Divider()
                    if showsAgentSection {
                        agentCard.id(Self.agentsAnchor)
                        Divider()
                    }
                    summaryCard(proxy)
                    Divider()
                    VStack(alignment: .leading, spacing: 4) {
                        activityCard
                        steeringCard.id(Self.sessionsAnchor)
                    }
                    Divider()
                    footer
                }
                .padding(14)
                // Callouts arrive and leave; the window still follows the measured height without its own motion (#200).
                .animation(AppMotion.content, value: menuCard)
                .animation(AppMotion.content, value: state.serverRunning)
                .background(GeometryReader { box in
                    Color.clear.preference(key: MenuContentHeight.self, value: box.size.height)
                })
            }
        }
        .frame(width: 400, height: menuHeight, alignment: .top)
        // ⌘R refreshes while the menu is open, like Refresh Status in More.
        .background(Button("", action: refreshStatus).keyboardShortcut("r", modifiers: .command).hidden())
        .onReceive(prompts.$queue) { queue in
            if questionsWaiting != queue.count { questionsWaiting = queue.count }
            if questionAskedBy != queue.first?.sender { questionAskedBy = queue.first?.sender }
        }
        // Hover captions are drawn over the whole panel, outside the scroll view and the measured stack.
        .overlayPreferenceValue(MenuHoverTipKey.self) { target in MenuHoverTipLayer(target: target) }
        .background(HostWindowReader { window in
            host.window = window
            if state.menuWindow !== window { state.menuWindow = window }
            fitWindow(animated: false)
        })
        .onChange(of: menuHeight) { _ in fitWindow(animated: false) }
        .onChange(of: showSessions) { expanded in
            if !expanded { sessionDetailID = nil }
        }
        .onChange(of: state.steeringSessions.map(\.sessionID)) { ids in
            if let sessionDetailID, !ids.contains(sessionDetailID) { self.sessionDetailID = nil }
        }
        .onPreferenceChange(MenuContentHeight.self) { measured in
            // No animation of its own: the window follows the content as a section opens or
            // closes, frame by frame with its top edge fixed, so nothing moves twice.
            guard abs(measured - contentHeight) > 0.5 else { return }
            contentHeight = measured
        }
        .background(.regularMaterial)
        .task {
            audio.refresh()
            // Background polls are rarer while idle; opening the menu shows current state.
            Task { await state.refresh() }
        }
    }

    /// As tall as the content, never taller than the screen allows; longer content scrolls.
    private var menuHeight: CGFloat {
        let screen = (NSScreen.main?.visibleFrame.height ?? 900) - 40
        let limit = max(320, min(screen, 820))
        return contentHeight > 0 ? min(contentHeight, limit) : min(660, limit)
    }

    /// Resize the popover window to the menu's height, keeping its top edge under the menu bar.
    private func fitWindow(animated: Bool) {
        guard let window = host.window else { return }
        var frame = window.frame
        let height = menuHeight
        guard abs(frame.height - height) > 0.5 else { return }
        let top = frame.maxY
        frame.size.height = height
        frame.origin.y = top - height
        window.setFrame(frame, display: true, animate: animated)
    }

    private var displayAgents: [AgentInfo] {
        state.agents.filter(\.isActive) + state.agents.filter { !$0.isActive }
    }

    private var activeTeams: [AgentTeamInfo] {
        state.agentTeams.filter(\.isActive)
    }

    private var failedAgentCount: Int {
        state.recentAgentFailureCount
    }

    private var displayActivityEvents: [ToolEvent] {
        let activeIDs = Set(state.activeEvents.map(\.id))
        return state.activeEvents + state.recentEvents.filter { !activeIDs.contains($0.id) }
    }

    /// Header status reflects connection and privacy only; normal session history never overrides it.
    private var headerStatus: StatusDisplay {
        if state.privacyFlags?.paused == true {
            return StatusDisplay(label: "Paused", symbol: "pause.circle.fill",
                                 explanation: "Observation is paused (Settings › Privacy).", tone: .warning)
        }
        if state.privacyFlags?.privateMode == true, state.connectionState == .connected {
            return StatusDisplay(label: "Private", symbol: "eye.slash.fill",
                                 explanation: "Private mode: no activity history, usage, memories, lessons or run timelines are kept. The security log still records tool names and outcomes.", tone: .neutral)
        }
        return connectionDisplay
    }

    /// Right after login the server is still starting; that is not an outage.
    private var connectionDisplay: StatusDisplay {
        state.startingAfterLogin
            ? StatusDisplay(label: "Starting", symbol: "hourglass",
                            explanation: "Mac MCP is starting after login.", tone: .neutral)
            : state.connectionState.display
    }

    private var endpointStatus: StatusDisplay {
        EndpointDisplay.make(mode: settings.publicEndpointMode,
                             cloudflareRunning: state.cloudflareRunning,
                             ngrokRunning: state.ngrokRunning)
    }

    private var safetyStatus: StatusDisplay {
        SafetyDisplay.make(state.securitySemantics, serverRunning: state.serverRunning)
    }

    private func openSettings(_ section: SettingsSection? = nil) {
        SettingsWindowController.shared.show(state: state, settings: settings, section: section)
    }

    /// The one card under the header. Only the most important shows: Mac MCP not answering,
    /// then unfinished setup, then a new version.
    private enum TopCard: Hashable { case connection, setup, update }

    private var menuCard: TopCard? {
        if state.connectionState != .connected { return .connection }
        if state.showsOnboardingReminder { return .setup }
        if state.updateReady { return .update }
        return nil
    }

    @ViewBuilder
    private func cardView(_ card: TopCard) -> some View {
        switch card {
        case .connection: connectionCard
        case .setup: setupCard
        case .update: updateCallout
        }
    }

    /// Connecting, starting, stopped or not fully answering. Only a server a finished check found
    /// stopped gets Start (#123); while the first check runs the card says it is connecting.
    private var connectionCard: some View {
        let stopped = liveStatus.server == .stopped
        let tint = stopped ? VisualTokens.error : connectionDisplay.tone.color
        return MenuCard(tint: tint) {
            HStack(spacing: 10) {
                MenuCardSymbol(symbol: stopped ? "power" : connectionDisplay.symbol, tint: tint)
                VStack(alignment: .leading, spacing: 2) {
                    Text(stopped ? "Your AI apps can't use this Mac" : state.connectionBannerTitle)
                        .font(.body.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                    let detail = stopped ? "Start Mac MCP to let them in again." : state.connectionBannerDetail
                    if !detail.isEmpty {
                        Text(detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
                Spacer(minLength: 6)
                if stopped {
                    Button("Start") { state.startServer() }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                        .accessibilityLabel("Start Server")
                } else if !state.startingAfterLogin && (state.connectionState == .degraded || state.connectionState == .disconnected) {
                    Button("Retry", action: refreshStatus)
                        .controlSize(.small)
                }
            }
        }
    }

    /// Get Started (#59) while setup is unfinished; OnboardingChecklist.shouldShowReminder decides.
    private var setupCard: some View {
        let checklist = state.onboardingChecklist
        return MenuCard(tint: VisualTokens.accent) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    MenuCardSymbol(symbol: "checkmark", tint: VisualTokens.accent)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Get your AI apps ready · \(checklist.completed) of \(checklist.steps.count)")
                            .font(.body.weight(.semibold))
                        if let next = checklist.nextStep {
                            Text("Next: \(next.title)").font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                    Spacer(minLength: 6)
                    Button("Continue") { openSettings(.start) }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                        .accessibilityHint("Opens Get Started in Settings")
                }
                // One segment per step, filled when done.
                HStack(spacing: 4) {
                    ForEach(checklist.steps) { step in
                        Capsule()
                            .fill(step.done ? VisualTokens.accent : VisualTokens.hairline)
                            .frame(height: 4)
                    }
                }
                .accessibilityHidden(true)
            }
        }
    }

    /// A new release is ready (#232): one compact row, the same for every way Mac MCP was installed.
    private var updateCallout: some View {
        HStack(spacing: 10) {
            Image(systemName: "arrow.down")
                .font(.system(size: 12, weight: .bold))
                .foregroundStyle(VisualTokens.accent)
                .frame(width: 24, height: 24)
                .background(VisualTokens.accent.opacity(0.16), in: RoundedRectangle(cornerRadius: VisualTokens.Radius.tile + 1, style: .continuous))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text("A new update is ready")
                    .font(.body.weight(.semibold))
                Text(updateDetail)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 6)
            Button {
                state.performUpdate()
            } label: {
                if state.busyAction == "Updating" {
                    ProgressView().controlSize(.small)
                } else {
                    Text("Update")
                }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
            .disabled(!state.canInstallUpdate)
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(VisualTokens.quietFill, in: RoundedRectangle(cornerRadius: VisualTokens.Radius.card - 2, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: VisualTokens.Radius.card - 2, style: .continuous).strokeBorder(VisualTokens.hairline))
        .accessibilityElement(children: .contain)
    }

    private var updateDetail: String {
        if let version = state.updateCheckInfo?.releaseVersion, !version.isEmpty { return "Mac MCP \(version) is ready to install." }
        return "New improvements for Mac MCP."
    }

    /// The same live state the menu bar icon shows: server, a waiting question, work under way.
    private var liveStatus: MenuLiveStatus {
        state.liveStatus(questionsWaiting: questionsWaiting, askedBy: questionAskedBy)
    }

    /// What Mac MCP is doing, in one line under its name. Idle says only "Ready"; the line is
    /// for news (work, a waiting question, a server not up), not filler.
    private var headerSubtitle: String { liveStatus.subtitle }

    private func refreshStatus() { Task { await state.retryConnection() } }

    /// The agent list shows while agents work, or when asked for from the Agents row.
    private var showsAgentSection: Bool {
        state.activeAgents > 0 || !activeTeams.isEmpty || showAllAgents
    }

    private func revealAgents(_ proxy: ScrollViewProxy) {
        showAllAgents = true
        withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(Self.agentsAnchor, anchor: .top) }
    }

    private func revealSessions(_ proxy: ScrollViewProxy) {
        showSessions = true
        withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(Self.sessionsAnchor, anchor: .top) }
    }

    // MARK: Header and actions

    private var header: some View {
        let status = headerStatus
        return HStack(spacing: 10) {
            // The bare monogram: champagne on the dark popover, bronze on the light one.
            Image(nsImage: colorScheme == .dark ? Self.markDark : Self.markLight)
                .resizable().interpolation(.high).frame(width: 30, height: 30)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text("Mac MCP").font(.headline)
                Text(headerSubtitle)
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            .help(state.version == "—" ? "Version unknown" : "Mac MCP v\(state.version)")
            Spacer()
            HStack(spacing: 5) {
                Image(systemName: status.symbol)
                    .foregroundStyle(status.tone.color)
                Text(status.label)
                    .foregroundStyle(.primary)
            }
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(status.tone.color.opacity(0.14), in: Capsule())
            .help(status.explanation)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Status: \(status.label)")
            .accessibilityHint(status.explanation)
            if state.serverRunning {
                Button { state.restartServer() } label: {
                    if state.busyAction != nil {
                        ProgressView().controlSize(.small).frame(width: 16, height: 16)
                    } else {
                        Image(systemName: "arrow.clockwise").font(.system(size: 13, weight: .semibold))
                            .frame(width: 26, height: 26)
                            .background(.quaternary.opacity(0.6), in: Circle())
                    }
                }
                .buttonStyle(.plain)
                .disabled(state.busyAction != nil)
                .help("Restart Server")
                .accessibilityLabel("Restart Server")
            }
        }
    }

    /// Small facts at a glance that no row repeats: calls and success in the last hour.
    private var chipsRow: some View {
        HStack(spacing: 6) {
            MenuChip(symbol: "bolt.fill", text: "\(state.totalCalls)", tint: nil,
                     caption: MenuHoverCopy.toolCalls(count: state.totalCalls))
            MenuChip(symbol: "checkmark.seal.fill",
                     text: state.totalCalls > 0 ? String(format: "%.0f%%", state.successRate) : "—",
                     tint: successTint,
                     caption: MenuHoverCopy.success(calls: state.totalCalls, rate: state.successRate))
            // Running agents and conversations are counted once, in their own rows below (#123).
            Spacer(minLength: 0)
        }
    }

    private var successTint: Color? {
        guard state.totalCalls > 0 else { return nil }
        if state.successRate < 50 { return .red }
        return state.successRate < 80 ? .orange : nil
    }

    private var sessionsChipText: String {
        guard state.hasReliableSessionSignal else { return "—" }
        return MenuHoverCopy.conversationCount(active: state.sessionActiveCount,
                                                idle: state.sessionIdleCount,
                                                ended: state.sessionEndedCount)
    }

    /// What the owner just started (a restart, an update) and how it went.
    private var actionBar: some View {
        VStack(spacing: 8) {
            if let action = state.busyAction {
                MenuCallout(tint: .accentColor, symbol: nil, title: action, detail: "") {
                    ProgressView().controlSize(.small)
                }
            } else if let notice = state.actionNotice {
                MenuCallout(tint: noticeColor(notice.kind), symbol: notice.symbolName, title: notice.message, detail: "") {
                    EmptyView()
                }
                .transition(AppMotion.arrive)
            }
        }
        .animation(AppMotion.content, value: state.actionNotice?.id)
        .animation(AppMotion.content, value: state.busyAction)
    }

    /// Settings and More close the menu, as in other menu bar apps.
    private var footer: some View {
        VStack(alignment: .leading, spacing: 2) {
            Button { ChatWindowController.shared.show(state: state) } label: {
                MenuRow(symbol: "bubble.left.and.text.bubble.right.fill", tint: MenuPalette.neutral, title: "Chat…") {
                    Text("⇧⌘N").font(.callout).foregroundStyle(.tertiary)
                }
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Open Mac MCP Chat")
            Button { openSettings() } label: {
                MenuRow(symbol: "gearshape.fill", tint: MenuPalette.neutral, title: "Settings…") {
                    Text("⌘,").font(.callout).foregroundStyle(.tertiary)
                }
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Open Settings")
            moreMenu
        }
    }

    private var moreMenu: some View {
        Menu {
            if state.serverRunning {
                Button("Restart Server") { state.restartServer() }
                Button("Stop Server") { state.stopServer() }
            }
            Button("Refresh Status") { Task { await state.retryConnection() } }
                .keyboardShortcut("r", modifiers: .command)
            Divider()
            if let flags = state.privacyFlags {
                Button(flags.paused ? "Resume Observation" : "Pause Observation") { state.setPrivacy(paused: !flags.paused) }
                Button(flags.privateMode ? "Turn Off Private Mode" : "Private Mode") { state.setPrivacy(privateMode: !flags.privateMode) }
                Button("Privacy Settings…") { openSettings(.privacy) }
                Divider()
            }
            Button("Open Dashboard") { state.openDashboard() }
            Button("Review Connections…") { openSettings(.connections) }
            Button("Review Safety…") { openSettings(.permissions) }
            Button("Settings…") { openSettings() }
            Button("Set Up & Tour…") { state.showTour() }
            Divider()
            Button(state.updateCheckLoading ? "Checking for Updates…" : "Check for Updates") { state.checkForUpdates() }
                .disabled(!state.canCheckForUpdates)
            Divider()
            Button("Quit Mac MCP") { state.quitApp() }
        } label: {
            MenuRow(symbol: "ellipsis", tint: MenuPalette.neutral, title: "More") {
                Image(systemName: "chevron.right").font(.system(size: 10 * glyphScale, weight: .semibold))
                    .foregroundStyle(.tertiary)
            }
        }
        // Button style with a plain look keeps the row's tile and chevron; the borderless menu
        // style would draw only its title.
        .menuStyle(.button)
        .buttonStyle(.plain)
        .menuIndicator(.hidden)
        .help("Restart, stop, refresh, privacy, dashboard, tour, updates and quit")
        .accessibilityLabel("More actions")
    }

    // MARK: Summary

    private func summaryCard(_ proxy: ScrollViewProxy) -> some View {
        let endpoint = endpointStatus
        let safety = safetyStatus
        return VStack(alignment: .leading, spacing: 2) {
            summaryRow(symbol: "cpu", color: failedAgentCount > 0 ? VisualTokens.error
                       : state.activeAgents > 0 ? VisualTokens.accent : MenuPalette.neutral, title: "Agents",
                       value: agentsSummaryValue, valueColor: failedAgentCount > 0 ? VisualTokens.error : .secondary,
                       hint: state.agents.isEmpty ? "Opens the dashboard" : "Shows the agent list",
                       tip: state.agents.isEmpty ? "Opens the dashboard" : "Shows the agent list") {
                if state.agents.isEmpty { state.openDashboard() } else { revealAgents(proxy) }
            }
            summaryRow(symbol: endpoint.symbol, color: endpoint.tone == .warning ? VisualTokens.warning : MenuPalette.neutral,
                       title: "Remote access", value: endpoint.label,
                       valueColor: endpoint.tone == .warning ? VisualTokens.warning : .secondary,
                       hint: "Review Connections in Settings", tip: endpoint.explanation) { openSettings(.connections) }
            summaryRow(symbol: safety.symbol, color: safety.tone == .warning ? VisualTokens.warning : MenuPalette.neutral,
                       title: "Safety", value: safety.label,
                       valueColor: safety.tone == .warning ? VisualTokens.warning : .secondary,
                       hint: "Review safety in Settings", tip: safety.explanation) { openSettings(.permissions) }
        }
    }

    private func summaryRow(symbol: String, color: Color, title: String, value: String, valueColor: Color,
                            hint: String, tip: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            MenuRow(symbol: symbol, tint: color, title: title) {
                Text(value).font(.callout).foregroundStyle(valueColor).lineLimit(1)
                Image(systemName: "chevron.right")
                    .font(.system(size: 10 * glyphScale, weight: .semibold))
                    .foregroundStyle(.tertiary)
            }
        }
        .buttonStyle(.plain)
        .menuHoverTip(tip)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title), \(value)")
        .accessibilityHint(hint)
        .accessibilityAddTraits(.isButton)
        // Combining the children dropped the button's press action: give the row its own.
        .accessibilityAction { action() }
    }

    private var agentsSummaryValue: String {
        var parts: [String] = []
        if failedAgentCount > 0 { parts.append("\(failedAgentCount) failed") }
        parts.append(state.activeAgents > 0 ? "\(state.activeAgents) running" : "None running")
        if failedAgentCount == 0, state.activeAgents == 0, let lastRunAt = state.lastAgentRunAt {
            parts.append("Last run \(relativeTime(lastRunAt))")
        }
        return parts.joined(separator: " · ")
    }

    // MARK: Sessions

    private var steeringCard: some View {
        DisclosureGroup(isExpanded: $showSessions) {
            // How long idle conversations stay listed is set in Settings › Advanced (#123).
            VStack(alignment: .leading, spacing: 9) {
                if state.steeringSnapshotStale && state.hasSteeringSnapshot {
                    HStack(spacing: 7) {
                        Image(systemName: "clock.arrow.circlepath")
                            .foregroundStyle(.orange)
                        Text("Showing the last known conversations · not current")
                            .font(.caption2.weight(.medium))
                        Spacer()
                        Button("Retry") { Task { await state.retryConnection() } }
                            .controlSize(.mini)
                    }
                    .padding(.horizontal, 8).padding(.vertical, 6)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                } else if state.steeringSnapshotStale || state.connectionState == .disconnected {
                    HStack(spacing: 7) {
                        Image(systemName: "wifi.slash").foregroundStyle(.red)
                        Text("Conversations unavailable")
                            .font(.caption2.weight(.medium))
                        Spacer()
                        Button("Retry") { Task { await state.retryConnection() } }
                            .controlSize(.mini)
                    }
                    .padding(.horizontal, 8).padding(.vertical, 6)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                }

                if !state.browserTabs.isEmpty {
                    browserTabsCard(state.browserTabs)
                } else if let browserEvent = state.activeBrowserEvents.first,
                   let context = state.resolvedBrowserContext(for: browserEvent) {
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(spacing: 8) {
                            if state.activityStale {
                                Image(systemName: "clock.arrow.circlepath").foregroundStyle(.orange)
                                    .accessibilityHidden(true)
                            } else {
                                ProgressView().controlSize(.small)
                            }
                            VStack(alignment: .leading, spacing: 1) {
                                Text(state.activeBrowserEvents.count == 1 ? "Browser automation active" : "\(state.activeBrowserEvents.count) browser tasks active")
                                    .font(.caption.weight(.semibold))
                                Text(browserActivityLabel(context))
                                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                            }
                            Spacer()
                            if context.canShowTab {
                                Button("Show Tab") { state.showBrowserTab(browserEvent) }
                                    .controlSize(.small)
                                    .help("Bring this real browser tab to the front")
                            }
                        }
                        Text(state.activityStale
                             ? "Last known · not current. Mac MCP couldn't read its activity."
                             : "Visible, non-focus-stealing browser automation · not headless.")
                            .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(8)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }

                let sessionGroups = state.steeringSessionGroups
                if sessionGroups.totalItemCount == 0 {
                    HStack(spacing: 8) {
                        Image(systemName: "rectangle.stack.badge.minus").foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text(state.steeringSnapshotStale
                             ? (state.hasSteeringSnapshot ? "No conversations were open when last checked." : "Conversations unavailable.")
                             : state.steeringEmptyMessage)
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                    }.padding(.vertical, 3)
                } else {
                    if state.steeringSessions.count > 1 {
                        Text("Choose the conversation to send a message to.").font(.caption2).foregroundStyle(.secondary)
                    }
                    // Inline list: the popover's single scroll view handles long lists.
                    VStack(spacing: 6) {
                        // Keep the row identity when Working becomes Idle so an open
                        // inspector follows the session across the two section headers.
                        ForEach(sessionGroups.active + sessionGroups.recent) { session in
                            VStack(spacing: 6) {
                                if session.id == sessionGroups.active.first?.id {
                                    sessionSectionHeader("Active", count: sessionGroups.active.count, symbol: "bolt.fill", color: .accentColor)
                                }
                                if session.id == sessionGroups.recent.first?.id {
                                    sessionSectionHeader("Recent", count: sessionGroups.recent.count, symbol: "clock", color: .secondary)
                                }
                                sessionRow(session)
                            }
                        }
                        if !sessionGroups.ended.isEmpty {
                            sessionSectionHeader("Ended", count: sessionGroups.ended.count, symbol: "clock.arrow.circlepath", color: .secondary)
                            ForEach(sessionGroups.ended, id: \.id) { event in terminalSessionRow(event) }
                        }
                    }
                }

                HStack(spacing: 7) {
                    TextField("Message", text: $state.steeringPrompt)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Message to the selected conversation")
                        .onSubmit { state.sendSteering() }
                    Button { state.sendSteering() } label: {
                        if state.steeringSending { ProgressView().controlSize(.small) }
                        else { Image(systemName: "paperplane.fill") }
                    }
                    .buttonStyle(.borderedProminent)
                    .help("Send to the selected conversation")
                    .accessibilityLabel(state.steeringSending ? "Sending" : "Send to the selected conversation")
                    .disabled(state.steeringSending || state.steeringPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || state.selectedSteeringSessionID == nil || state.steeringSnapshotStale || state.connectionState == .disconnected)
                }
                Text(state.steeringStatus).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
            }
            .padding(.top, 8)
        } label: {
            HStack {
                Label("Conversations", systemImage: "rectangle.stack")
                    .labelStyle(MenuTileLabelStyle(tint: MenuPalette.neutral))
                    .accessibilityLabel("Conversations")
                    .accessibilityHint("AI app conversations using Mac MCP (Mac MCP logical sessions, not the app's own chat IDs). Agents you delegate work to are under Agents.")
                Spacer()
                if !showSessions && state.hasReliableSessionSignal {
                    Text(sessionsChipText)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(state.sessionActiveCount > 0 ? Color.accentColor : .secondary)
                }
            }
            .menuHoverTip(MenuHoverCopy.conversations(
                active: state.sessionActiveCount, idle: state.sessionIdleCount,
                ended: state.sessionEndedCount, reliable: state.hasReliableSessionSignal))
        }
        .disclosureGroupStyle(MenuDisclosureStyle())
    }

    // MARK: Agents

    private var agentCard: some View {
        let activeAgents = state.agents.filter(\.isActive)
        return VStack(alignment: .leading, spacing: 6) {
            // The header opens and closes the full list, like Recent activity's chevron.
            Button { showAllAgents.toggle() } label: {
                HStack(spacing: 6) {
                    Label(state.activeAgents > 0 ? "Active Work" : "Agents", systemImage: "cpu")
                        .labelStyle(MenuTileLabelStyle(tint: VisualTokens.accent))
                    Spacer()
                    if state.activeAgents > 0 {
                        RobotRunner(active: true)
                            .frame(width: 44, height: 46)
                            .scaleEffect(0.5)
                            .frame(width: 22, height: 18)
                    }
                    if !state.agents.isEmpty {
                        Text("\(min(state.agents.count, Self.agentListLimit)) recent")
                            .font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                    }
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(showAllAgents ? 180 : 0))
                        .accessibilityHidden(true)
                }
                .padding(.vertical, 5)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(showAllAgents ? "Expanded" : "Collapsed")
            .accessibilityHint(showAllAgents ? "Collapses the agent list" : "Lists the recent agents with details")
            VStack(spacing: 6) {
                ForEach(activeTeams.prefix(Self.teamRowLimit)) { team in
                    teamRow(team)
                }
                if activeTeams.count > Self.teamRowLimit {
                    moreTeamsRow(activeTeams.count - Self.teamRowLimit)
                }
                if showAllAgents {
                    // At most 20 agents, inline: the menu's one scroll region scrolls them (#123).
                    ForEach(Array(displayAgents.prefix(Self.agentListLimit).enumerated()), id: \.element.id) { index, agent in
                        if index > 0 { Divider() }
                        agentRow(agent)
                    }
                } else {
                    ForEach(Array(activeAgents.prefix(2).enumerated()), id: \.element.id) { index, agent in
                        if index > 0 { Divider() }
                        compactAgentRow(agent)
                    }
                }
            }
        }
    }

    /// Intent and phase only; provider, model and tool counts live in "View all agents".
    private func compactAgentRow(_ agent: AgentInfo) -> some View {
        HStack(spacing: 9) {
            Image(systemName: agentPhaseSymbol(agent))
                .foregroundStyle(agentPhaseColor(agent))
                .frame(width: 18)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(agent.title ?? agent.agentID).font(.caption.weight(.semibold)).lineLimit(1)
                Text("\(agentPhaseLabel(agent)) · \(providerName(agent.provider))")
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 6)
            if let turnElapsed = agent.turnElapsedMS, turnElapsed > 0 {
                Text(compactDuration(turnElapsed)).font(.caption2).foregroundStyle(.tertiary)
            } else if let duration = agent.durationMS {
                Text(compactDuration(duration)).font(.caption2).foregroundStyle(.tertiary)
            }
            agentStopButton(agent)
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .contain)
    }

    /// One active team in a line: progress through its task graph and the plan limits its agents
    /// spend; opens the dashboard on that team. Badges sit beside the title, or under it when the
    /// line is too narrow for both.
    private func teamRow(_ team: AgentTeamInfo) -> some View {
        let quota = team.quota ?? []
        return Button { state.openDashboard(focusTeamID: team.teamID) } label: {
            Group {
                if quota.isEmpty {
                    teamLine(team, quota: [])
                } else {
                    ViewThatFits(in: .horizontal) {
                        teamLine(team, quota: quota)
                        VStack(alignment: .leading, spacing: 4) {
                            teamLine(team, quota: [])
                            QuotaBadges(quota: quota).padding(.leading, 25)
                        }
                    }
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Show this team's tasks in the dashboard")
        .accessibilityLabel("Team \(team.title ?? ""), \(team.progressText)")
    }

    /// Active teams beyond the rows shown: counted, and the dashboard lists them all.
    private func moreTeamsRow(_ extra: Int) -> some View {
        let text = extra == 1 ? "1 more active team" : "\(extra) more active teams"
        return Button { state.openDashboard() } label: {
            HStack(spacing: 7) {
                Text("+\(extra)").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                    .frame(width: 18)
                Text(text).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 6)
                Image(systemName: "chevron.right").font(.system(size: 8 * glyphScale, weight: .semibold)).foregroundStyle(.tertiary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Show all active teams in the dashboard")
        .accessibilityLabel(text)
        .accessibilityHint("Opens the dashboard")
    }

    private func teamLine(_ team: AgentTeamInfo, quota: [ProviderQuota]) -> some View {
        HStack(spacing: 7) {
            Image(systemName: "point.3.connected.trianglepath.dotted")
                .font(.caption2).foregroundStyle(.secondary).frame(width: 18)
            Text(team.title ?? "Team").font(.caption2.weight(.semibold)).lineLimit(1)
            if !quota.isEmpty { QuotaBadges(quota: quota) }
            Spacer(minLength: 6)
            Text(team.progressText).font(.caption2).foregroundStyle(
                (team.unresolvedConflictCount ?? 0) > 0 ? Color.orange : Color.secondary
            ).lineLimit(1)
            Image(systemName: "chevron.right").font(.system(size: 8 * glyphScale, weight: .semibold)).foregroundStyle(.tertiary)
        }
    }

    private func agentRow(_ agent: AgentInfo) -> some View {
        HStack(spacing: 9) {
            Image(systemName: agentPhaseSymbol(agent))
                .foregroundStyle(agentPhaseColor(agent))
                .frame(width: 18)
                .help(agentPhaseLabel(agent))
                .accessibilityLabel(agentPhaseLabel(agent))

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(agent.title ?? agent.agentID).font(.caption.weight(.semibold)).lineLimit(1)
                    Spacer()
                    if agent.isActive, let turnElapsed = agent.turnElapsedMS, turnElapsed > 0 {
                        Text("turn \(compactDuration(turnElapsed))").font(.caption2).foregroundStyle(.tertiary)
                    } else if let duration = agent.durationMS {
                        Text(compactDuration(duration)).font(.caption2).foregroundStyle(.tertiary)
                    }
                    if agent.isActive {
                        agentStopButton(agent)
                    }
                }

                if agent.isFailure {
                    Text(agentFailureDisplay(agent))
                        .font(.caption2)
                        .foregroundStyle(agent.isRecentFailure(at: state.agentFailureReferenceTime) ? Color.red : Color.secondary)
                }

                HStack(spacing: 6) {
                    Text(providerName(agent.provider)).font(.caption2).foregroundStyle(.secondary)
                    Text("·").font(.caption2).foregroundStyle(.tertiary)
                    Text(modelName(agent.model)).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    if let reasoning = agent.reasoning, !reasoning.isEmpty {
                        HStack(spacing: 3) {
                            Image(systemName: "brain").font(.system(size: 8 * glyphScale, weight: .semibold))
                            Text(reasoningBadge(reasoning)).font(.caption2.weight(.semibold))
                        }
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(.quaternary, in: Capsule())
                        .help("Reasoning: \(reasoningBadge(reasoning))")
                    }
                    if let capabilityProfile = agent.capabilityProfile, capabilityProfile != "legacy", !capabilityProfile.isEmpty {
                        Text(profileDisplayName(capabilityProfile))
                            .font(.caption2.weight(.semibold))
                            .padding(.horizontal, 5).padding(.vertical, 2)
                            .background(.quaternary, in: Capsule())
                            .help("Agent capability profile: \(profileDisplayName(capabilityProfile))")
                    }
                }

                HStack(spacing: 5) {
                    if let resource = primaryInteractiveResource(agent),
                       let label = resource.label, !label.isEmpty {
                        Image(systemName: resourceSymbol(resource.kind))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Text(label)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    } else if let tool = agent.lastTool, !tool.isEmpty {
                        Image(systemName: toolSymbol(tool)).font(.caption2).foregroundStyle(.secondary)
                        Text(cleanToolName(tool)).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    } else {
                        Image(systemName: "ellipsis.circle").font(.caption2).foregroundStyle(.tertiary)
                        Text(agent.isActive ? "Waiting for first tool" : "No tool calls").font(.caption2).foregroundStyle(.tertiary)
                    }
                    Spacer()
                    Image(systemName: "wrench.and.screwdriver.fill").font(.system(size: 8 * glyphScale)).foregroundStyle(.tertiary)
                    Text("\(agent.toolCallCount ?? 0)").font(.caption2).foregroundStyle(.secondary)
                    if let checkpoints = agent.checkpointCount, checkpoints > 0 {
                        Image(systemName: "arrow.trianglehead.branch").font(.system(size: 8 * glyphScale)).foregroundStyle(.secondary)
                        Text("\(checkpoints)").font(.caption2).foregroundStyle(.secondary)
                    }
                    if let throttles = agent.throttleCount, throttles > 0 {
                        Image(systemName: "gauge.with.dots.needle.33percent").font(.system(size: 8 * glyphScale)).foregroundStyle(.orange)
                        Text("\(throttles)").font(.caption2).foregroundStyle(.orange)
                    }
                    if let retries = agent.retryCount, retries > 0 {
                        Image(systemName: "arrow.clockwise").font(.system(size: 8 * glyphScale)).foregroundStyle(.orange)
                        Text("\(retries)").font(.caption2).foregroundStyle(.orange)
                    }
                }
            }
        }.padding(.vertical, 5)
    }

    @ViewBuilder
    private func agentStopButton(_ agent: AgentInfo) -> some View {
        if state.cancellingAgentIDs.contains(agent.agentID) {
            ProgressView().controlSize(.mini).frame(width: 16 * glyphScale, height: 16 * glyphScale)
                .accessibilityLabel("Stopping \(agent.title ?? "agent")")
        } else {
            Button { Task { await state.cancelAgent(agent) } } label: {
                Image(systemName: "stop.fill")
                    .font(.system(size: 7 * glyphScale, weight: .bold))
                    .foregroundStyle(.secondary)
                    .frame(width: 16 * glyphScale, height: 16 * glyphScale)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 4, style: .continuous))
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Stop this agent")
            .accessibilityLabel("Stop \(agent.title ?? "agent")")
        }
    }

    // MARK: Recent activity

    private var activityCard: some View {
        let events = displayActivityEvents
        let failed = events.filter { $0.status.lowercased() == "error" }
        let errorCount = failed.count
        let liveCount = events.filter { $0.status.lowercased() == "running" }.count
        let shown = showFailedOnly ? failed : events
        // The failed count filters the list. It sits beside the section's toggle, not inside it (#123).
        let failedFilter = Group {
            if errorCount > 0 {
                Button {
                    showFailedOnly.toggle()
                    if showFailedOnly { showActivity = true }
                } label: {
                    Label("\(errorCount) failed", systemImage: "xmark.circle.fill")
                        .font(.caption2.weight(.semibold)).foregroundStyle(.red)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(showFailedOnly ? "Show all recent activity" : "Show the \(errorCount) failed calls")
                .help(showFailedOnly ? "Show all recent activity" : "Show only the failed calls")
            }
        }
        return DisclosureGroup(isExpanded: $showActivity) {
            VStack(alignment: .leading, spacing: 5) {
                // A failed read keeps the last good list (#123); say it is not current.
                if state.activityStale && !events.isEmpty {
                    HStack(spacing: 7) {
                        Image(systemName: "clock.arrow.circlepath").foregroundStyle(.orange)
                            .accessibilityHidden(true)
                        Text("Showing the last known activity · not current")
                            .font(.caption2.weight(.medium))
                        Spacer()
                        Button("Retry", action: refreshStatus)
                            .controlSize(.mini)
                    }
                    .padding(.horizontal, 8).padding(.vertical, 6)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                }
                if showFailedOnly {
                    HStack(spacing: 6) {
                        Text(errorCount == 0 ? "No failed calls now." : "Showing \(errorCount) failed")
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Button("Show all") { showFailedOnly = false }
                            .font(.caption.weight(.medium)).buttonStyle(.plain).foregroundStyle(Color.accentColor)
                    }
                }
                if showFailedOnly {
                    ForEach(Array(shown.prefix(Self.recentActivityLimit).enumerated()), id: \.element.id) { index, event in
                        if index > 0 { Divider() }
                        activityRow(event)
                    }
                } else if events.isEmpty && !state.activityLoaded && !state.activityStale {
                    // The first read has not come back yet: not "no tool calls".
                    HStack(spacing: 7) {
                        ProgressView().controlSize(.small)
                        Text("Loading activity…").font(.caption).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
                } else if state.connectionState == .disconnected && events.isEmpty {
                    activityEmptyText("Disconnected · tool activity unavailable.")
                } else if state.connectionState == .degraded && events.isEmpty {
                    activityEmptyText("Tool activity is temporarily unavailable.")
                } else if state.serverRunning && events.isEmpty {
                    activityEmptyText("No tool calls recorded in the last hour.")
                } else if !state.serverRunning {
                    activityEmptyText("Start the server or retry the connection to see live tool activity.")
                } else {
                    ForEach(Array(events.prefix(Self.recentActivityLimit).enumerated()), id: \.element.id) { index, event in
                        if index > 0 { Divider() }
                        activityRow(event)
                    }
                    if !state.browserActionStatus.isEmpty {
                        Text(state.browserActionStatus).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
                Button { state.openDashboard() } label: {
                    HStack(spacing: 4) {
                        Text(events.count > Self.recentActivityLimit
                             ? "Full activity in Dashboard (\(events.count))"
                             : "Full activity in Dashboard")
                            .font(.caption.weight(.medium))
                        Image(systemName: "arrow.up.forward").font(.system(size: 9 * glyphScale, weight: .semibold))
                            .accessibilityHidden(true)
                    }
                    .foregroundStyle(Color.accentColor)
                }
                .buttonStyle(.plain)
                .padding(.top, 4)
                .accessibilityHint("Opens the dashboard in your browser")
            }
            .padding(.top, 8)
        } label: {
            HStack(spacing: 6) {
                Label("Recent activity", systemImage: "waveform.path.ecg")
                    .labelStyle(MenuTileLabelStyle(tint: MenuPalette.neutral))
                Spacer()
                if state.activityStale && !events.isEmpty {
                    Text("Not current").font(.caption2.weight(.semibold)).foregroundStyle(.orange)
                } else if errorCount == 0 && liveCount > 0 {
                    Text("\(liveCount) live").font(.caption2.weight(.semibold)).foregroundStyle(Color.accentColor)
                }
            }
        }
        .disclosureGroupStyle(MenuDisclosureStyle(accessory: failedFilter))
    }

    private func activityEmptyText(_ text: String) -> some View {
        Text(text).font(.caption).foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
    }

    private func activityRow(_ event: ToolEvent) -> some View {
        HStack(spacing: 8) {
            Image(systemName: toolSymbol(event.tool))
                .foregroundStyle(event.status.lowercased() == "running" ? Color.accentColor : Color.secondary)
                .font(.caption).frame(width: 16)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 5) {
                    Text(cleanToolName(event.tool)).font(.caption.weight(.medium)).lineLimit(1)
                    if event.status.lowercased() == "running" {
                        Text("LIVE").font(.caption2.weight(.bold))
                            .padding(.horizontal, 4).padding(.vertical, 1)
                            .background(.quaternary, in: Capsule())
                    }
                }
                // Every row says when it ran; how long it took is written to be read ("4.5s", not "4.498 ms").
                Text(verbatim: activityDetail(event))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            if let context = state.resolvedBrowserContext(for: event), context.canShowTab, event.tool != "browser_close_tab" {
                Button { state.showBrowserTab(event) } label: {
                    Image(systemName: "arrow.up.forward.app").font(.caption2)
                }
                .buttonStyle(.borderless)
                .help("Show the real browser tab")
                .accessibilityLabel("Show browser tab")
            }
            Image(systemName: eventStatusSymbol(event.status))
                .foregroundStyle(eventStatusColor(event.status)).font(.caption)
                .help(event.status.capitalized)
                .accessibilityLabel(event.status.capitalized)
        }
        .padding(.vertical, 4)
    }

    private var safariVisualCard: some View {
        GroupBox {
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(.quaternary)
                        .frame(width: 34, height: 34)
                    Image(systemName: state.safariExtensionEnabled ? "safari.fill" : "safari")
                        .foregroundStyle(state.safariExtensionEnabled ? Color.accentColor : Color.secondary)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(state.safariExtensionEnabled ? "Visual Companion · On" : "Safari Visual Companion")
                        .font(.caption.weight(.semibold))
                    Text(state.safariExtensionEnabled
                         ? "Shows when Mac MCP is actively using a Safari page."
                         : (state.safariExtensionRegistered
                            ? "One-time setup: enable the bundled extension in Safari."
                            : "Unsigned build: Safari developer setup is required."))
                        .font(.caption2).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                if state.safariExtensionEnabled {
                    Button { state.refreshSafariExtensionState() } label: { Image(systemName: "arrow.clockwise") }
                        .buttonStyle(.borderless).help("Refresh extension status")
                } else if state.safariExtensionRegistered {
                    Button("Enable in Safari…") { state.openSafariExtensionPreferences() }
                        .controlSize(.small)
                } else {
                    Button("Developer Setup…") { state.openSafariExtensionPreferences() }
                        .controlSize(.small)
                }
            }
            .padding(.vertical, 2)
            Divider()
            // Chrome in the same layout as Safari (#124): connected, needs a reload, or not set up.
            let chrome = state.chromeCompanion
            let chromeReady = chrome.connected && !chrome.needsReload
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(.quaternary)
                        .frame(width: 34, height: 34)
                    Image(systemName: "globe")
                        .foregroundStyle(chromeReady ? Color.accentColor : (chrome.connected ? Color.orange : Color.secondary))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(chromeReady ? "Chrome Visual Companion · Connected"
                         : (chrome.connected ? "Chrome Visual Companion · Needs a reload" : "Chrome Visual Companion"))
                        .font(.caption.weight(.semibold))
                    Text(chromeReady
                         ? "Chrome tabs work in the background: page actions and background-safe visual capture."
                         : (chrome.connected
                            ? "Older than this Mac MCP: reload it once at chrome://extensions."
                            : "Not connected. Chrome Setup loads the companion once."))
                        .font(.caption2).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                if chromeReady {
                    Button { Task { await state.refreshChromeCompanion() } } label: { Image(systemName: "arrow.clockwise") }
                        .buttonStyle(.borderless).help("Refresh companion status")
                } else if chrome.connected {
                    Button("Reload in Chrome…") { state.openChromeExtensionSetup() }
                        .controlSize(.small)
                } else {
                    Button("Chrome Setup…") { state.openChromeExtensionSetup() }
                        .controlSize(.small)
                }
            }
            .padding(.vertical, 2)
        } label: {
            HStack {
                Label("Browser Activity", systemImage: "sparkles.rectangle.stack")
                Spacer()
                Text(browserCompanionHeader).font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    /// "Safari On · Chrome Connected": both companions at a glance.
    private var browserCompanionHeader: String {
        let chrome = state.chromeCompanion
        let chromeText = chrome.connected ? (chrome.needsReload ? "Needs a reload" : "Connected") : "Not connected"
        return "Safari \(state.safariExtensionStatus) · Chrome \(chromeText)"
    }

    private var securityCard: some View {
        GroupBox {
            DisclosureGroup(isExpanded: $showSecurity) {
                if let semantics = state.securitySemantics {
                    VStack(alignment: .leading, spacing: 9) {
                        if let profile = semantics.profiles.first(where: { $0.name == semantics.activeProfile }) {
                            HStack {
                                Text("Active profile").font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Text(profileDisplayName(profile.name)).font(.caption.weight(.semibold))
                            }

                            Divider()
                            Label("Allowed Capabilities", systemImage: "lock.shield")
                                .font(.caption.weight(.semibold))
                            Text(profile.allowedCapabilities.map(capabilityDisplayName).joined(separator: " · "))
                                .font(.caption2).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            HStack {
                                Text("Destructive families").font(.caption2).foregroundStyle(.secondary)
                                Spacer()
                                Text(destructiveFamiliesLabel(profile.destructiveFamilies)).font(.caption2.weight(.medium))
                            }
                            HStack {
                                Text("Agent access ceiling").font(.caption2).foregroundStyle(.secondary)
                                Spacer()
                                Text(capabilityDisplayName(profile.accessModeCeiling)).font(.caption2.weight(.medium))
                            }

                            Divider()
                            Label("Approval Behavior", systemImage: "person.badge.shield.checkmark")
                                .font(.caption.weight(.semibold))
                            HStack {
                                Text("Source").font(.caption2).foregroundStyle(.secondary)
                                Spacer()
                                Text(approvalSourceDisplayName(profile.approvalBehavior.source))
                                    .font(.caption2.weight(.semibold))
                            }
                            Text(profile.approvalBehavior.summary)
                                .font(.caption2).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            Text("Capability allowed = server permits it. Approval is separate; a confirmation prompt is not guaranteed.")
                                .font(.caption2.weight(.medium)).foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)

                            if semantics.profileWasNormalized == true,
                               let configured = semantics.normalizedFromProfile ?? semantics.configuredProfile {
                                Divider()
                                Label(
                                    "Configured \(profileDisplayName(configured)) is delegated-only or invalid. "
                                    + "Using \(profileDisplayName(semantics.activeProfile)).",
                                    systemImage: "exclamationmark.shield"
                                )
                                .font(.caption2.weight(.medium))
                                .foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                            }

                            Divider()
                            Text("Preset overview").font(.caption.weight(.semibold))
                            ForEach(semantics.profiles) { item in
                                Button { state.setPermissionProfile(item.name) } label: {
                                    HStack(spacing: 6) {
                                        Image(systemName: item.active ? "checkmark.circle.fill" : "circle")
                                            .foregroundStyle(item.active ? Color.accentColor : Color.secondary)
                                            .font(.caption2)
                                        Text(profileDisplayName(item.name)).font(.caption2.weight(.medium))
                                        Spacer()
                                        Text("\(item.allowedCapabilities.count) caps · Approval \(approvalSourceDisplayName(item.approvalBehavior.source))")
                                            .font(.caption2).foregroundStyle(.secondary)
                                    }
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                            }
                        } else {
                            Text("Unknown permission profile: \(semantics.activeProfile). Calls fail closed until a known profile is configured.")
                                .font(.caption2).foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.top, 8)
                } else {
                    Text(state.serverRunning ? "Permission semantics unavailable." : "Start the server to inspect permission semantics.")
                        .font(.caption).foregroundStyle(.secondary)
                        .padding(.vertical, 4)
                }
            } label: {
                HStack {
                    Label("Permissions & Approval", systemImage: "checkmark.shield")
                    Spacer()
                    if !showSecurity, let semantics = state.securitySemantics {
                        Text(profileDisplayName(semantics.activeProfile)).font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private var voiceCard: some View {
        GroupBox {
            DisclosureGroup(isExpanded: $showVoice) {
                VStack(spacing: 10) {
                    Toggle(isOn: $settings.voiceEnabled) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Ask User Voice")
                            Text("Experimental · falls back to ask_user when disabled").font(.caption2).foregroundStyle(.secondary)
                        }
                    }.onChange(of: settings.voiceEnabled) { _ in persistSettings() }

                    Divider()
                    HStack {
                        Label(settings.hasGroqKey ? "Groq key configured" : "Groq key required", systemImage: settings.hasGroqKey ? "checkmark.shield.fill" : "key")
                            .font(.caption).foregroundStyle(settings.hasGroqKey ? Color.secondary : Color.orange)
                        Spacer()
                        Button { audio.refresh() } label: { Image(systemName: "arrow.clockwise") }.help("Refresh audio devices")
                    }
                    HStack {
                        SecureField(settings.hasGroqKey ? "Replace Groq API key" : "Groq API key", text: $groqKey)
                        Button("Save") { saveGroqKey() }.disabled(groqKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        if settings.hasGroqKey { Button { removeGroqKey() } label: { Image(systemName: "trash") }.help("Remove key") }
                    }
                    HStack {
                        Text("Input").frame(width: 58, alignment: .leading)
                        Picker("", selection: $settings.inputDevice) {
                            Text("Auto").tag("auto"); Text("Built-in Mic").tag("built-in")
                            ForEach(audio.inputs) { device in Text(device.name).tag(device.settingValue) }
                        }.labelsHidden()
                    }
                    HStack {
                        Text("Output").frame(width: 58, alignment: .leading)
                        Picker("", selection: $settings.outputDevice) {
                            Text("System").tag("system"); Text("Built-in Speakers").tag("built-in")
                            ForEach(audio.outputs) { device in Text(device.name).tag(device.settingValue) }
                        }.labelsHidden()
                    }
                    HStack {
                        Text("Language").frame(width: 58, alignment: .leading)
                        Picker("", selection: $settings.language) { Text("Auto").tag("auto"); Text("Turkish").tag("tr"); Text("English").tag("en") }.labelsHidden()
                        Spacer()
                        Stepper("\(settings.timeoutSeconds)s", value: $settings.timeoutSeconds, in: 5...180, step: 5).frame(maxWidth: 120)
                    }
                    HStack {
                        Text("Voice").frame(width: 58, alignment: .leading)
                        TextField("tr-TR-AhmetNeural", text: $settings.voiceName)
                        Text("Rate")
                        TextField("-5%", text: $settings.ttsRate).frame(width: 54)
                    }
                    .onChange(of: settings.inputDevice) { _ in persistSettings() }
                    .onChange(of: settings.outputDevice) { _ in persistSettings() }
                    .onChange(of: settings.language) { _ in persistSettings() }
                    .onChange(of: settings.timeoutSeconds) { _ in persistSettings() }
                    .onChange(of: settings.voiceName) { _ in persistSettings() }
                    .onChange(of: settings.ttsRate) { _ in persistSettings() }

                    if !settingsMessage.isEmpty {
                        Text(settingsMessage).font(.caption2).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.font(.caption).padding(.top, 9)
            } label: {
                HStack {
                    Label("Voice", systemImage: "waveform.and.mic")
                    Spacer()
                    if !showVoice { Text(settings.voiceEnabled ? "On" : "Off").font(.caption2).foregroundStyle(.secondary) }
                }
            }
        }
    }

    private var advancedCard: some View {
        GroupBox {
            DisclosureGroup("Advanced", isExpanded: $showAdvanced) {
                VStack(spacing: 10) {
                    HStack {
                        Text("Public endpoint")
                        Spacer()
                        Text(settings.publicEndpointMode == "none" ? "This Mac only" : settings.publicEndpointMode.capitalized)
                            .foregroundStyle(.secondary)
                        Button("Configure…") { SettingsWindowController.shared.show(state: state, settings: settings) }
                    }
                    .font(.caption)
                    HStack {
                        Text("Port")
                        TextField("8000", value: $settings.serverPort, format: .number).frame(width: 70).onSubmit { persistSettings(); Task { await state.refresh() } }
                        Spacer()
                        Button(state.updateCheckLoading ? "Checking…" : "Check Update") { state.checkForUpdates() }
                            .disabled(!state.canCheckForUpdates)
                        Button(state.updateActionTitle) { state.performUpdate() }
                            .disabled(!state.canInstallUpdate)
                    }
                    if let progress = state.updateProgress,
                       progress.isInProgress || ["failed", "recovered", "recovery_failed", "rolling_back"].contains(progress.normalizedStatus) {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                if progress.isInProgress && state.updateTransactionActive {
                                    ProgressView().controlSize(.mini)
                                } else {
                                    Image(systemName: progress.statusKind == .error ? "exclamationmark.triangle.fill" : "arrow.triangle.2.circlepath")
                                        .foregroundStyle(progress.statusKind == .error ? Color.red : Color.secondary)
                                }
                                Text(progress.statusTitle)
                                    .lineLimit(1)
                                Spacer()
                                if let position = progress.stepPosition {
                                    Text("Step \(position.current) of \(position.total)").foregroundStyle(.secondary)
                                }
                            }
                            if progress.isInProgress {
                                Text("Mac MCP closes and opens again by itself during the update.")
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .font(.caption2)
                        .accessibilityElement(children: .combine)
                    }
                    HStack { Text("CLI"); TextField("Auto-detect", text: $settings.cliPath).onSubmit { persistSettings() } }.font(.caption)
                }.padding(.top, 8)
            }.font(.caption)
        }
    }

    private func agentPhaseKey(_ agent: AgentInfo) -> String {
        let terminalStatus = agent.status?.lowercased()
        if let terminalStatus, ["completed", "failed", "cancelled", "timeout", "stalled"].contains(terminalStatus) {
            return terminalStatus
        }
        let raw = (agent.phase ?? agent.status ?? "unknown").lowercased()
        switch raw {
        case "worker_starting", "provider_starting", "starting": return "starting"
        case "reasoning", "working": return "reasoning"
        case "tool": return "tool"
        case "finalizing": return "finalizing"
        case "retrying": return "retrying"
        case "checkpointing": return "checkpointing"
        case "throttled": return "throttled"
        case "completed": return "completed"
        case "failed": return "failed"
        case "cancelled": return "cancelled"
        case "timeout": return "timeout"
        case "stalled": return "stalled"
        default: return agent.status?.lowercased() ?? raw
        }
    }

    private func agentPhaseSymbol(_ agent: AgentInfo) -> String {
        switch agentPhaseKey(agent) {
        case "starting": return "hourglass"
        case "reasoning": return "brain"
        case "tool": return "hammer.fill"
        case "finalizing": return "text.bubble.fill"
        case "retrying": return "arrow.clockwise.circle.fill"
        case "checkpointing": return "arrow.trianglehead.branch"
        case "throttled": return "gauge.with.dots.needle.33percent"
        case "completed": return "checkmark.circle.fill"
        case "failed": return "xmark.octagon.fill"
        case "cancelled": return "stop.circle.fill"
        case "timeout": return "clock.badge.exclamationmark"
        case "stalled": return "pause.circle.fill"
        default: return "questionmark.circle"
        }
    }

    private func agentPhaseLabel(_ agent: AgentInfo) -> String {
        switch agentPhaseKey(agent) {
        case "starting": return "Starting"
        case "reasoning": return "Reasoning"
        case "tool": return "Using a tool"
        case "finalizing": return "Finalizing"
        case "retrying": return "Retrying"
        case "checkpointing": return "Checkpointing"
        case "throttled": return "Provider cooldown"
        case "completed": return "Completed"
        case "failed": return "Failed"
        case "cancelled": return "Cancelled"
        case "timeout": return "Timed out"
        case "stalled": return "Stalled"
        default: return "Unknown status"
        }
    }

    private func agentPhaseColor(_ agent: AgentInfo) -> Color {
        if agent.isFailure && !agent.isRecentFailure(at: state.agentFailureReferenceTime) {
            return .secondary
        }
        switch agentPhaseKey(agent) {
        case "completed": return .green
        case "failed": return .red
        case "timeout", "stalled", "retrying": return .orange
        case "cancelled": return .secondary
        case "starting", "reasoning", "tool", "finalizing": return .accentColor
        default: return .secondary
        }
    }

    private func agentFailureDisplay(_ agent: AgentInfo) -> String {
        guard let endedAt = agent.endedAt else { return agentPhaseLabel(agent) }
        return "\(agentPhaseLabel(agent)) · \(relativeTime(endedAt))"
    }

    private func providerName(_ provider: String?) -> String {
        guard let provider else { return "AI" }
        return AgentProviders.name(provider) ?? provider.capitalized
    }

    private func modelName(_ model: String?) -> String {
        guard var value = model, !value.isEmpty else { return "Default model" }
        if let slash = value.lastIndex(of: "/") { value = String(value[value.index(after: slash)...]) }
        value = value.replacingOccurrences(of: "-contributor-free", with: "")
        let lower = value.lowercased()
        if lower.hasPrefix("muse-spark-") {
            return "Muse Spark " + value.dropFirst("muse-spark-".count).replacingOccurrences(of: "-", with: ".")
        }
        if lower.hasPrefix("gpt-") {
            let parts = value.split(separator: "-")
            if parts.count >= 3 {
                let version = parts[1]
                let suffix = parts.dropFirst(2).map { String($0).capitalized }.joined(separator: " ")
                return "GPT-\(version) \(suffix)"
            }
            return value.uppercased()
        }
        return value.replacingOccurrences(of: "-", with: " ").capitalized
    }

    private func reasoningBadge(_ value: String) -> String {
        switch value.lowercased() {
        case "xhigh": return "XHigh"
        case "none": return "None"
        default: return value.capitalized
        }
    }

    private func primaryInteractiveResource(_ agent: AgentInfo) -> AgentResourceActivity? {
        agent.resourceActivity?.first {
            guard let kind = $0.kind?.lowercased() else { return false }
            return ["browser_tab", "native_window", "native_app"].contains(kind)
        }
    }

    private func resourceSymbol(_ kind: String?) -> String {
        switch kind?.lowercased() {
        case "browser_tab": return "safari.fill"
        case "native_window": return "macwindow"
        case "native_app": return "app.fill"
        default: return "square.stack.3d.up.fill"
        }
    }

    private func cleanToolName(_ tool: String) -> String {
        let value = AgentProviders.toolName(tool)
        if value == "command_execution" { return "Terminal command" }
        return value
    }

    private func toolSymbol(_ tool: String) -> String {
        let value = cleanToolName(tool).lowercased()
        if value.contains("browser") || value.contains("safari") || value.contains("chrome") { return "safari.fill" }
        if value.contains("execute_js") || value.contains("javascript") { return "chevron.left.forwardslash.chevron.right" }
        if value.contains("command") || value.contains("terminal") || value == "bash" || value.contains("applescript") { return "terminal.fill" }
        if value.contains("read") || value.contains("write") || value.contains("edit") || value.contains("file") { return "doc.text.fill" }
        if value.contains("search") || value.contains("find") { return "magnifyingglass" }
        if value.contains("agent") { return "cpu" }
        if value.contains("memory") { return "brain" }
        if value.contains("http") || value.contains("web") || value.contains("network") { return "network" }
        if value.contains("observe") || value.contains("ui") || value.contains("screen") { return "rectangle.and.hand.point.up.left.fill" }
        return "wrench.and.screwdriver.fill"
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

    // MARK: Browser tabs in use (#124)

    /// One row per tab an agent or chat is using: who and what, then Take over, Show Tab and Stop.
    private func browserTabsCard(_ tabs: [BrowserTabActivity]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                if state.activityStale {
                    Image(systemName: "clock.arrow.circlepath").foregroundStyle(.orange)
                        .accessibilityHidden(true)
                } else if tabs.contains(where: \.running) {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "globe").foregroundStyle(.secondary).accessibilityHidden(true)
                }
                Text(browserTabsHeader(tabs)).font(.caption.weight(.semibold))
                Spacer()
            }
            ForEach(tabs) { tab in browserTabRow(tab) }
            Text(state.activityStale
                 ? "Last known · not current. Mac MCP couldn't read its activity."
                 : "Visible, non-focus-stealing browser automation · not headless.")
                .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(8)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func browserTabsHeader(_ tabs: [BrowserTabActivity]) -> String {
        let inUse = tabs.filter { !$0.takenOver }.count
        let yours = tabs.count - inUse
        if inUse == 0 { return yours == 1 ? "You took over a browser tab" : "You took over \(yours) browser tabs" }
        return inUse == 1 ? "Browser automation active" : "\(inUse) browser tabs in use"
    }

    private func browserTabRow(_ tab: BrowserTabActivity) -> some View {
        HStack(spacing: 6) {
            VStack(alignment: .leading, spacing: 1) {
                Text(state.browserTabTitle(tab)).font(.caption.weight(.medium)).lineLimit(1)
                Text(browserTabDetail(tab)).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            if tab.takenOver {
                Button("Hand back") { state.handBackBrowserTab(tab) }
                    .controlSize(.mini)
                    .help("Let agents use this tab again")
            } else {
                Button("Take over") { state.takeOverBrowserTab(tab) }
                    .controlSize(.mini)
                    .help("Take this tab: the agent stops acting in it, and the tab comes to the front")
            }
            if tab.browser != nil {
                Button { state.showBrowserTab(tab) } label: {
                    Image(systemName: "arrow.up.forward.app").font(.caption2)
                }
                .buttonStyle(.borderless)
                .help("Show Tab: bring this real browser tab to the front")
                .accessibilityLabel("Show tab")
            }
            if tab.canStop, let agentID = tab.agentID {
                if state.cancellingAgentIDs.contains(agentID) {
                    ProgressView().controlSize(.mini).frame(width: 16 * glyphScale, height: 16 * glyphScale)
                        .accessibilityLabel("Stopping the agent")
                } else {
                    Button { state.stopBrowserTabAgent(tab) } label: {
                        Image(systemName: "stop.fill")
                            .font(.system(size: 7 * glyphScale, weight: .bold))
                            .foregroundStyle(.secondary)
                            .frame(width: 16 * glyphScale, height: 16 * glyphScale)
                            .background(.quaternary, in: RoundedRectangle(cornerRadius: 4, style: .continuous))
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Stop Agent: end this agent's whole task")
                    .accessibilityLabel("Stop agent")
                }
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .contain)
    }

    /// "Safari · github.com · Clicking", or that the owner has the tab.
    private func browserTabDetail(_ tab: BrowserTabActivity) -> String {
        let status = tab.takenOver ? "You have this tab" : (tab.action ?? (tab.running ? "Working" : "Waiting"))
        return [tab.browserName, tab.site ?? "tab", status].joined(separator: " · ")
    }

    private func browserActivityLabel(_ context: BrowserContext) -> String {
        let rawBrowser = (context.browser ?? "Browser").lowercased()
        let browser: String
        if rawBrowser == "google chrome" || rawBrowser == "chrome" || rawBrowser == "chromium" {
            browser = "Chrome"
        } else if rawBrowser == "safari" {
            browser = "Safari"
        } else {
            browser = context.browser ?? "Browser"
        }
        let site = context.site ?? "tab"
        let action = context.action ?? "Active"
        return "\(browser) · \(site) · \(action)"
    }

    private func eventStatusSymbol(_ status: String) -> String {
        switch status.lowercased() {
        case "success": return "checkmark.circle.fill"
        case "error": return "xmark.circle.fill"
        case "running": return "dot.radiowaves.left.and.right"
        default: return "ellipsis.circle.fill"
        }
    }

    private func eventStatusColor(_ status: String) -> Color {
        switch status.lowercased() {
        case "success": return .green
        case "error": return .red
        case "running": return .accentColor
        default: return .orange
        }
    }

    private func noticeColor(_ kind: ActionNotice.Kind) -> Color {
        switch kind {
        case .success: return VisualTokens.success
        case .error: return VisualTokens.error
        case .update: return VisualTokens.accent
        case .info: return .secondary
        }
    }

    private func sessionSectionHeader(_ title: String, count: Int, symbol: String, color: Color) -> some View {
        HStack(spacing: 5) {
            Image(systemName: symbol).font(.system(size: 9 * glyphScale, weight: .semibold)).foregroundStyle(color)
            Text(title).font(.caption2.weight(.semibold)).foregroundStyle(color)
            Text("\(count)").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.top, 2)
    }

    private func sessionRow(_ session: SteeringSession) -> some View {
        Button {
            state.selectedSteeringSessionID = session.sessionID
            sessionDetailID = session.sessionID
        } label: {
            HStack(spacing: 8) {
                Image(systemName: state.selectedSteeringSessionID == session.sessionID ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(state.selectedSteeringSessionID == session.sessionID ? Color.accentColor : Color.secondary)
                Text("Chat \(session.flowNumber)")
                    .font(.caption2.weight(.semibold))
                    .padding(.horizontal, 5).padding(.vertical, 2)
                    .background(.quaternary, in: Capsule())
                VStack(alignment: .leading, spacing: 1) {
                    Text(session.label).font(.caption.weight(.semibold)).lineLimit(1)
                    TimelineView(.periodic(from: .now, by: 60)) { context in
                        Text(session.lastError?.hasPrefix("security:") == true
                             ? sessionDetailLine(session)
                             : SteeringSessionPresentation(session: session, now: context.date.timeIntervalSince1970,
                                                           isStale: !state.hasReliableSessionSignal).summary)
                            .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Spacer()
                if let lifecycleSymbol = sessionLifecycleSymbol(session.effectiveLifecycleState) {
                    Image(systemName: lifecycleSymbol)
                        .foregroundStyle(sessionLifecycleColor(session.effectiveLifecycleState))
                        .font(.caption)
                        .help(sessionLifecycleLabel(session.effectiveLifecycleState))
                }
                Image(systemName: session.isWorking ? "bolt.circle.fill" : "pause.circle.fill")
                    .foregroundStyle(session.isWorking ? Color.accentColor : Color.secondary)
                    .font(.caption)
                    .help(session.isWorking ? "Working" : "Idle")
                    .accessibilityLabel(session.isWorking ? "Working" : "Idle")
            }.padding(.vertical, 3).contentShape(Rectangle())
        }.buttonStyle(.plain)
        .accessibilityHint("Selects this conversation for messages and opens its details")
        .popover(isPresented: Binding(
            get: { sessionDetailID == session.sessionID },
            set: { shown in
                if shown { sessionDetailID = session.sessionID }
                else if sessionDetailID == session.sessionID { sessionDetailID = nil }
            }
        ), arrowEdge: .trailing) {
            SessionDetailView(session: session, isStale: !state.hasReliableSessionSignal)
        }
    }

    private func terminalSessionRow(_ event: SteeringRecent) -> some View {
        HStack(spacing: 8) {
            Image(systemName: sessionLifecycleSymbol(event.effectiveLifecycleState) ?? "clock.arrow.circlepath")
                .foregroundStyle(sessionLifecycleColor(event.effectiveLifecycleState))
                .font(.caption)
            VStack(alignment: .leading, spacing: 1) {
                Text("Ended conversation").font(.caption.weight(.semibold))
                HStack(spacing: 4) {
                    Text(sessionLifecycleLabel(event.effectiveLifecycleState)).font(.caption2).foregroundStyle(.secondary)
                    if let transitionedAt = event.transitionedAt {
                        Text("· \(relativeTime(transitionedAt))").font(.caption2).foregroundStyle(.tertiary)
                    }
                }
            }
            Spacer()
        }
        .padding(.vertical, 3)
        .help("This conversation has ended; messages can no longer be sent to it")
    }

    private func sessionLifecycleLabel(_ state: SteeringLifecycleState) -> String {
        state.label
    }

    private func sessionLifecycleSymbol(_ state: SteeringLifecycleState) -> String? {
        switch state {
        case .queued: return "clock.fill"
        case .delivered: return "paperplane.circle.fill"
        case .failed: return "arrow.uturn.backward.circle"
        case .disconnected: return "wifi.slash"
        case .expired: return "clock"
        case .unknown: return "questionmark.circle"
        case .ready, .acknowledged: return nil
        }
    }

    private func sessionLifecycleColor(_ state: SteeringLifecycleState) -> Color {
        switch state {
        case .queued, .delivered: return .accentColor
        default: return .secondary
        }
    }

    private func sessionDetailLine(_ session: SteeringSession) -> String {
        let base = "\(session.detail) · \(compactDuration(session.activityMS))"
        if let error = session.lastError, error.hasPrefix("security:") {
            let reason = String(error.dropFirst("security:".count)).replacingOccurrences(of: "_", with: " ")
            return base + " · Security blocked: " + reason
        }
        let lifecycle = session.effectiveLifecycleState
        return lifecycle.isSteeringNotice ? base + " · " + lifecycle.label : base
    }

    private func compactDuration(_ milliseconds: Int) -> String {
        let seconds = max(0, milliseconds / 1000)
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m" }
        return "\(minutes / 60)h \(minutes % 60)m"
    }

    private func persistSettings() { do { try settings.save(); settingsMessage = "Saved" } catch { settingsMessage = error.localizedDescription } }
    private func saveGroqKey() { do { try settings.saveGroqKey(groqKey); groqKey = ""; settingsMessage = "Groq key saved securely in Keychain" } catch { settingsMessage = error.localizedDescription } }
    private func removeGroqKey() { do { try settings.removeGroqKey(); settingsMessage = "Groq key removed" } catch { settingsMessage = error.localizedDescription } }
    private func activityDetail(_ event: ToolEvent) -> String {
        let origin = state.resolvedBrowserContext(for: event).map(browserActivityLabel) ?? event.source.uppercased()
        return [origin, activityDuration(event.durationMS ?? 0), relativeTime(event.timestamp)].joined(separator: " · ")
    }

    /// Milliseconds under a second, then seconds with one decimal, then minutes.
    private func activityDuration(_ milliseconds: Int) -> String {
        if milliseconds < 1000 { return "\(milliseconds) ms" }
        if milliseconds < 60_000 { return String(format: "%.1fs", Double(milliseconds) / 1000) }
        return compactDuration(milliseconds)
    }

    private func relativeTime(_ timestamp: Double) -> String {
        let seconds = max(0, Int(Date().timeIntervalSince1970 - timestamp))
        if seconds < 5 { return "just now" }
        if seconds < 60 { return "\(seconds)s ago" }; if seconds < 3600 { return "\(seconds / 60)m ago" }; return "\(seconds / 3600)h ago"
    }
}

struct RobotRunner: View {
    let active: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            if active && !reduceMotion {
                TimelineView(.animation(minimumInterval: 0.12)) { context in
                    Canvas { canvas, size in
                        Self.draw(canvas, size, t: context.date.timeIntervalSinceReferenceDate, active: true, moving: true)
                    }
                }
            } else {
                // Reduce Motion (or idle): one still pose; the green antenna and the
                // label below still say the agent is working.
                Canvas { canvas, size in
                    Self.draw(canvas, size, t: 0, active: active, moving: false)
                }
            }
        }.accessibilityLabel(active ? "Agent working" : "Agent idle")
    }

    private static func draw(_ canvas: GraphicsContext, _ size: CGSize, t: TimeInterval, active: Bool, moving: Bool) {
        let phase = moving ? sin(t * 9) : 0
        let bob = moving ? abs(phase) * 1.4 : 0
        let centerX = size.width / 2
        let bodyY = size.height / 2 - bob
        let stroke = Color.primary.opacity(0.85)
        var antenna = Path(); antenna.move(to: CGPoint(x: centerX, y: bodyY - 13)); antenna.addLine(to: CGPoint(x: centerX, y: bodyY - 18)); canvas.stroke(antenna, with: .color(stroke), lineWidth: 1.6)
        canvas.fill(Path(ellipseIn: CGRect(x: centerX - 2, y: bodyY - 21, width: 4, height: 4)), with: .color(active ? .green : .secondary))
        let head = RoundedRectangle(cornerRadius: 5).path(in: CGRect(x: centerX - 12, y: bodyY - 12, width: 24, height: 18)); canvas.fill(head, with: .color(Color.primary.opacity(0.10))); canvas.stroke(head, with: .color(stroke), lineWidth: 1.6)
        canvas.fill(Path(ellipseIn: CGRect(x: centerX - 6, y: bodyY - 6, width: 3.5, height: 3.5)), with: .color(stroke)); canvas.fill(Path(ellipseIn: CGRect(x: centerX + 3, y: bodyY - 6, width: 3.5, height: 3.5)), with: .color(stroke))
        var torso = Path(); torso.move(to: CGPoint(x: centerX, y: bodyY + 6)); torso.addLine(to: CGPoint(x: centerX, y: bodyY + 14)); canvas.stroke(torso, with: .color(stroke), lineWidth: 2)
        let swing = moving ? CGFloat(phase) * 6 : 0
        var limbs = Path(); limbs.move(to: CGPoint(x: centerX, y: bodyY + 14)); limbs.addLine(to: CGPoint(x: centerX - 7 + swing, y: bodyY + 21)); limbs.move(to: CGPoint(x: centerX, y: bodyY + 14)); limbs.addLine(to: CGPoint(x: centerX + 7 - swing, y: bodyY + 21)); limbs.move(to: CGPoint(x: centerX, y: bodyY + 8)); limbs.addLine(to: CGPoint(x: centerX - 10 - swing * 0.7, y: bodyY + 12)); limbs.move(to: CGPoint(x: centerX, y: bodyY + 8)); limbs.addLine(to: CGPoint(x: centerX + 10 + swing * 0.7, y: bodyY + 12)); canvas.stroke(limbs, with: .color(stroke), style: StrokeStyle(lineWidth: 2, lineCap: .round))
    }
}

/// The height of the menu's content, measured where it is laid out (#200).
private struct MenuContentHeight: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

/// Holds the popover's window without keeping it alive.
private final class HostWindow {
    weak var window: NSWindow?
}

/// Reports the window the menu is shown in, once it is in one.
private struct HostWindowReader: NSViewRepresentable {
    let found: (NSWindow) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async { if let window = view.window { found(window) } }
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {
        DispatchQueue.main.async { if let window = view.window { found(window) } }
    }
}

// MARK: - Menu building blocks (#201)

/// Row tiles are calm: a row that only names a place has no colour of its own. Colour means
/// state (VisualTokens): work in progress, something to look at, something that failed.
private enum MenuPalette {
    static let neutral = Color.secondary
}

/// A small quiet square with its symbol; tinted only when the row reports a state.
private struct MenuIconTile: View {
    let symbol: String
    let tint: Color

    var body: some View {
        let quiet = tint == MenuPalette.neutral
        RoundedRectangle(cornerRadius: VisualTokens.Radius.tile, style: .continuous)
            .fill(quiet ? VisualTokens.quietFill : tint.opacity(0.16))
            .overlay(RoundedRectangle(cornerRadius: VisualTokens.Radius.tile, style: .continuous)
                .strokeBorder(quiet ? VisualTokens.hairline : tint.opacity(0.28), lineWidth: 0.5))
            .frame(width: 20, height: 20)
            .overlay(Image(systemName: symbol).font(.system(size: 11, weight: .semibold)).foregroundStyle(tint))
            .accessibilityHidden(true)
    }
}

/// One menu row: tile, title, then whatever trails it (a value, a shortcut, a chevron).
private struct MenuRow<Trailing: View>: View {
    let symbol: String
    let tint: Color
    let title: String
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(spacing: 10) {
            MenuIconTile(symbol: symbol, tint: tint)
            Text(title).font(.callout)
            Spacer(minLength: 8)
            trailing
        }
        .padding(.vertical, 5)
        .contentShape(Rectangle())
    }
}

/// A Label drawn as a menu row title: its icon in a colour tile.
private struct MenuTileLabelStyle: LabelStyle {
    let tint: Color

    func makeBody(configuration: Configuration) -> some View {
        let quiet = tint == MenuPalette.neutral
        HStack(spacing: 10) {
            RoundedRectangle(cornerRadius: VisualTokens.Radius.tile, style: .continuous)
                .fill(quiet ? VisualTokens.quietFill : tint.opacity(0.16))
                .overlay(RoundedRectangle(cornerRadius: VisualTokens.Radius.tile, style: .continuous)
                    .strokeBorder(quiet ? VisualTokens.hairline : tint.opacity(0.28), lineWidth: 0.5))
                .frame(width: 20, height: 20)
                .overlay(configuration.icon.font(.system(size: 11, weight: .semibold)).foregroundStyle(tint))
                .accessibilityHidden(true)
            configuration.title.font(.callout)
        }
    }
}

/// A disclosure row with its chevron on the right. The content fades in and the chevron turns;
/// the menu window itself follows the content without a motion of its own (#200). An accessory
/// (a filter) sits between the title and the chevron as its own control, never inside the
/// toggle (#123); VoiceOver reads the toggle as expanded or collapsed.
private struct MenuDisclosureStyle<Accessory: View>: DisclosureGroupStyle {
    let accessory: Accessory

    func makeBody(configuration: Configuration) -> some View {
        let toggle = { withAnimation(AppMotion.content) { configuration.isExpanded.toggle() } }
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Button(action: toggle) {
                    configuration.label
                        .padding(.vertical, 5)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityValue(configuration.isExpanded ? "Expanded" : "Collapsed")
                .accessibilityHint(configuration.isExpanded ? "Collapses this section" : "Expands this section")
                accessory
                Image(systemName: "chevron.down")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.tertiary)
                    .rotationEffect(.degrees(configuration.isExpanded ? 180 : 0))
                    .frame(width: 14, height: 24)
                    .contentShape(Rectangle())
                    .onTapGesture(perform: toggle)
                    .accessibilityHidden(true)
            }
            if configuration.isExpanded {
                configuration.content.transition(.opacity)
            }
        }
    }
}

extension MenuDisclosureStyle where Accessory == EmptyView {
    init() { accessory = EmptyView() }
}

/// A compact fact in a capsule: symbol and value; tinted when it needs a glance.
private struct MenuChip: View {
    let symbol: String
    let text: String
    let tint: Color?
    let caption: String

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: symbol).foregroundStyle(tint ?? .secondary)
            Text(text).foregroundStyle(tint ?? .primary)
                .monospacedDigit()
                .contentTransition(.numericText())
        }
        .animation(AppMotion.control, value: text)
        .font(.caption2.weight(.semibold))
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background((tint ?? Color.primary).opacity(tint == nil ? 0.07 : 0.14), in: Capsule())
        .menuHoverTip(caption)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(caption)
    }
}

/// A team's plan limits, one badge per provider: its symbol, then 5-hour and weekly use.
private struct QuotaBadges: View {
    let quota: [ProviderQuota]

    var body: some View {
        HStack(spacing: 4) {
            ForEach(quota) { QuotaBadge(quota: $0) }
        }
        .fixedSize()
    }
}

private struct QuotaBadge: View {
    let quota: ProviderQuota

    var body: some View {
        let style = ProviderUsageStyle.of(quota.providerID)
        HStack(spacing: 3) {
            Image(systemName: style.symbol).foregroundStyle(style.tint)
            if quota.windows.isEmpty {
                Text(quota.error ?? "…").foregroundStyle(.secondary)
            } else {
                ForEach(Array(quota.windows.enumerated()), id: \.offset) { index, window in
                    if index > 0 { Text("·").foregroundStyle(.tertiary) }
                    Text(window.kind).foregroundStyle(.secondary)
                    Text("\(Int(window.usedPercent.rounded()))%").foregroundStyle(Self.tint(window.usedPercent))
                }
                if quota.error != nil {
                    // The last reading, kept while the provider cannot be read.
                    Image(systemName: "clock.arrow.circlepath").foregroundStyle(.tertiary)
                }
            }
        }
        .font(.caption2.weight(.semibold))
        .monospacedDigit()
        .lineLimit(1)
        .padding(.horizontal, 6).padding(.vertical, 2)
        .background(style.tint.opacity(0.13), in: Capsule())
        .help(helpText)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(helpText)
    }

    static func tint(_ used: Double) -> Color {
        used >= 90 ? .red : used >= 75 ? .orange : .primary
    }

    private var title: String {
        let name = quota.label ?? AgentProviders.name(quota.providerID) ?? quota.source.capitalized
        guard let plan = quota.plan, !plan.isEmpty else { return name }
        return "\(name) · \(plan.capitalized) plan"
    }

    private var helpText: String {
        var lines = [title]
        for window in quota.windows {
            var line = "\(window.longLabel): \(Int(window.usedPercent.rounded()))% used"
            if let resetsAt = window.resetsAt, let left = Self.countdown(until: resetsAt) {
                line += ", resets in \(left)"
            }
            lines.append(line)
        }
        if let error = quota.error {
            lines.append(quota.windows.isEmpty ? Self.explain(error) : "Last reading; now \(Self.explain(error).lowercased())")
        }
        return lines.joined(separator: "\n")
    }

    static func countdown(until timestamp: Double) -> String? {
        let seconds = Int(timestamp - Date().timeIntervalSince1970)
        guard seconds > 0 else { return nil }
        let minutes = seconds / 60, hours = minutes / 60, days = hours / 24
        if days > 0 { return "\(days)d \(hours % 24)h" }
        if hours > 0 { return "\(hours)h \(minutes % 60)m" }
        return "\(max(1, minutes))m"
    }

    static func explain(_ error: String) -> String {
        switch error {
        case "login expired": return "Login expired. Run the provider's CLI to sign in again."
        case "not signed in": return "Not signed in on this Mac."
        case "key expired": return "API key expired. Replace it in OpenCode."
        case "rate limited": return "The provider is limiting usage reads. Mac MCP tries again later."
        case "offline": return "Could not reach the provider."
        default: return "Usage unavailable (\(error))."
        }
    }
}

/// A tinted callout for problems and progress: symbol, title, detail and an optional control.
private struct MenuCallout<Control: View>: View {
    let tint: Color
    let symbol: String?
    let title: String
    let detail: String
    @ViewBuilder var control: Control

    var body: some View {
        HStack(spacing: 9) {
            if let symbol {
                Image(systemName: symbol).foregroundStyle(tint).accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.caption.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                if !detail.isEmpty {
                    Text(detail).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            Spacer(minLength: 6)
            control
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(tint.opacity(0.35)))
    }
}

/// The card under the header: a tinted panel with a hairline in the same tint.
private struct MenuCard<Content: View>: View {
    let tint: Color
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(12)
            .background(tint.opacity(0.10), in: RoundedRectangle(cornerRadius: VisualTokens.Radius.card - 2, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: VisualTokens.Radius.card - 2, style: .continuous).strokeBorder(tint.opacity(0.28)))
            .accessibilityElement(children: .contain)
    }
}

/// The round symbol at the start of a card.
private struct MenuCardSymbol: View {
    let symbol: String
    let tint: Color

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 12, weight: .bold))
            .foregroundStyle(tint)
            .frame(width: 28, height: 28)
            .background(tint.opacity(0.18), in: Circle())
            .accessibilityHidden(true)
    }
}
