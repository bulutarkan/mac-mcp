import AppKit
import SwiftUI

struct MenuBarView: View {
    @ObservedObject var state: AppState
    @ObservedObject var settings: SettingsStore
    @StateObject private var audio = AudioDeviceStore()
    @State private var groqKey = ""
    @State private var settingsMessage = ""
    @State private var showVoice = false
    @State private var showAdvanced = false
    @State private var showSessions = false
    @State private var showSecurity = false
    @State private var sessionMinutesText = ""
    @State private var lastActiveAgentID: String? = nil

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                header
                serverCard
                if state.activeAgents > 0 || !state.agents.isEmpty { agentCard }
                activityCard
                securityCard
                steeringCard
                voiceCard
                advancedCard
                footer
            }
            .padding(16)
        }
        .frame(width: 400, height: 660)
        .background(.regularMaterial)
        .task {
            audio.refresh()
            if settings.steeringSessionMinutes != 10 {
                sessionMinutesText = String(settings.steeringSessionMinutes)
            }
        }
    }

    private var displayAgents: [AgentInfo] {
        state.agents.filter(\.isActive) + state.agents.filter { !$0.isActive }
    }

    private var displayActivityEvents: [ToolEvent] {
        let activeIDs = Set(state.activeEvents.map(\.id))
        return state.activeEvents + state.recentEvents.filter { !activeIDs.contains($0.id) }
    }

    private var firstActiveAgentID: String? {
        state.agents.first(where: \.isActive)?.id
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(nsImage: NSApplication.shared.applicationIconImage)
                .resizable().frame(width: 42, height: 42)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                .shadow(radius: 3, y: 1)
            VStack(alignment: .leading, spacing: 2) {
                Text("Mac MCP").font(.system(size: 17, weight: .semibold))
                HStack(spacing: 6) {
                    Circle().fill(connectionColor).frame(width: 7, height: 7)
                    Text(state.connectionStatusText)
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if state.ngrokRunning {
                Label("Tunnel", systemImage: "network")
                    .font(.caption2.weight(.medium)).padding(.horizontal, 8).padding(.vertical, 5)
                    .background(.thinMaterial, in: Capsule())
            }
        }
    }

    private var serverCard: some View {
        GroupBox {
            VStack(spacing: 10) {
                HStack {
                    metric(title: "Calls / 1h", value: "\(state.totalCalls)")
                    Divider().frame(height: 28)
                    metric(title: "Success", value: String(format: "%.0f%%", state.successRate))
                    Divider().frame(height: 28)
                    metric(title: "Agents", value: "\(state.activeAgents) active")
                }
                if state.connectionState != .connected {
                    HStack(spacing: 8) {
                        Image(systemName: connectionBannerSymbol)
                            .foregroundStyle(connectionColor)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(state.connectionBannerTitle).font(.caption.weight(.semibold))
                            if !state.connectionBannerDetail.isEmpty {
                                Text(state.connectionBannerDetail)
                                    .font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                            }
                        }
                        Spacer()
                        if state.connectionState == .degraded || state.connectionState == .disconnected {
                            Button("Retry") { Task { await state.retryConnection() } }
                                .controlSize(.small)
                        }
                    }
                    .padding(.horizontal, 9).padding(.vertical, 7)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
                HStack(spacing: 8) {
                    if state.serverRunning {
                        Button { state.restartServer() } label: { Label("Restart", systemImage: "arrow.clockwise") }.accessibilityLabel("Restart Server")
                        Button { state.stopServer() } label: { Label("Stop", systemImage: "stop.fill") }.accessibilityLabel("Stop Server")
                    } else {
                        Button { state.startServer() } label: { Label("Start Server", systemImage: "play.fill") }
                            .buttonStyle(.borderedProminent).accessibilityLabel("Start Server")
                    }
                    Spacer()
                    Button { state.openDashboard() } label: { Image(systemName: "chart.xyaxis.line") }.help("Open Dashboard")
                    Button { Task { await state.retryConnection() } } label: { Image(systemName: "arrow.triangle.2.circlepath") }.help("Refresh now")
                }
                if let action = state.busyAction {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text(action).font(.caption).foregroundStyle(.secondary)
                        Spacer()
                    }
                } else if let notice = state.actionNotice {
                    HStack(spacing: 8) {
                        Image(systemName: notice.symbolName)
                            .foregroundStyle(noticeColor(notice.kind))
                        Text(notice.message).font(.caption.weight(.medium))
                        Spacer()
                    }
                    .padding(.horizontal, 9).padding(.vertical, 7)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .animation(.easeInOut(duration: 0.2), value: state.actionNotice?.id)
                }
            }.padding(2)
        } label: { Label("Server", systemImage: "server.rack") }
    }

    private var steeringCard: some View {
        GroupBox {
            DisclosureGroup(isExpanded: $showSessions) {
                VStack(alignment: .leading, spacing: 9) {
                    HStack(spacing: 6) {
                        Image(systemName: "timer").font(.caption2).foregroundStyle(.secondary)
                        Text("Keep idle").font(.caption2).foregroundStyle(.secondary)
                        Spacer()
                        TextField("10", text: $sessionMinutesText)
                            .textFieldStyle(.roundedBorder)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 48)
                            .onChange(of: sessionMinutesText) { value in
                                let digits = value.filter(\.isNumber)
                                if digits != value { sessionMinutesText = digits; return }
                                if !digits.isEmpty, Int(digits) == 0 { sessionMinutesText = "" }
                            }
                            .onSubmit { applySessionRetention() }
                        Text("min").font(.caption2).foregroundStyle(.secondary)
                        Button { applySessionRetention() } label: {
                            Image(systemName: "checkmark").font(.caption2.weight(.semibold))
                        }
                        .buttonStyle(.borderless)
                        .help("Apply session retention")
                    }

                    if state.steeringSnapshotStale && state.hasSteeringSnapshot {
                        HStack(spacing: 7) {
                            Image(systemName: "clock.arrow.circlepath")
                                .foregroundStyle(.orange)
                            Text("Last successful session snapshot · stale")
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
                            Text("Session data unavailable")
                                .font(.caption2.weight(.medium))
                            Spacer()
                            Button("Retry") { Task { await state.retryConnection() } }
                                .controlSize(.mini)
                        }
                        .padding(.horizontal, 8).padding(.vertical, 6)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    }

                    if let browserEvent = state.activeBrowserEvents.first,
                       let context = state.resolvedBrowserContext(for: browserEvent) {
                        VStack(alignment: .leading, spacing: 5) {
                            HStack(spacing: 8) {
                                ProgressView().controlSize(.small)
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
                            Text("Visible real browser tab · background means non-focus-stealing, not headless.")
                                .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(8)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }

                    if state.steeringSessions.isEmpty {
                        HStack(spacing: 8) {
                            Image(systemName: "rectangle.stack.badge.minus").foregroundStyle(.secondary)
                            Text(state.steeringSnapshotStale
                                 ? (state.hasSteeringSnapshot ? "Last session snapshot contains no sessions." : "Session data unavailable.")
                                 : state.steeringEmptyMessage)
                                .font(.caption).foregroundStyle(.secondary)
                            Spacer()
                        }.padding(.vertical, 3)
                    } else {
                        if state.steeringSessions.count > 1 {
                            Text("Choose the session you want to steer.").font(.caption2).foregroundStyle(.secondary)
                        }
                        ScrollView(.vertical) {
                            LazyVStack(spacing: 4) {
                                ForEach(state.steeringSessions) { session in
                                    Button { state.selectedSteeringSessionID = session.sessionID } label: {
                                        HStack(spacing: 8) {
                                            Image(systemName: state.selectedSteeringSessionID == session.sessionID ? "checkmark.circle.fill" : "circle")
                                                .foregroundStyle(state.selectedSteeringSessionID == session.sessionID ? Color.accentColor : Color.secondary)
                                            Text("Agent \(session.flowNumber)")
                                                .font(.system(size: 9, weight: .semibold))
                                                .padding(.horizontal, 5).padding(.vertical, 2)
                                                .background(.quaternary, in: Capsule())
                                            VStack(alignment: .leading, spacing: 1) {
                                                Text(session.label).font(.caption.weight(.semibold)).lineLimit(1)
                                                Text(sessionDetailLine(session))
                                                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
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
                                }
                            }
                        }
                        .frame(height: CGFloat(min(max(state.steeringSessions.count, 1), 5)) * 42)
                    }

                    HStack(spacing: 7) {
                        TextField("Prompt", text: $state.steeringPrompt)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit { state.sendSteering() }
                        Button { state.sendSteering() } label: {
                            if state.steeringSending { ProgressView().controlSize(.small) }
                            else { Image(systemName: "paperplane.fill") }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(state.steeringSending || state.steeringPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || state.selectedSteeringSessionID == nil || state.steeringSnapshotStale || state.connectionState == .disconnected)
                    }
                    Text(state.steeringStatus).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                }
                .padding(.top, 8)
            } label: {
                Label("Sessions", systemImage: "rectangle.stack")
            }
        }
    }

    private var agentCard: some View {
        GroupBox {
            VStack(spacing: 8) {
                if state.activeAgents > 0 {
                    HStack(spacing: 10) {
                        RobotRunner(active: true).frame(width: 44, height: 46)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(state.activeAgents) Agent\(state.activeAgents == 1 ? "" : "s") Active")
                                .font(.caption.weight(.semibold))
                            Text("Working without taking focus").font(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                }
                ScrollViewReader { proxy in
                    ScrollView(.vertical) {
                        LazyVStack(spacing: 0) {
                            ForEach(Array(displayAgents.enumerated()), id: \.element.id) { index, agent in
                                agentRow(agent).id(agent.id)
                                if index < displayAgents.count - 1 { Divider() }
                            }
                        }
                    }
                    .frame(height: CGFloat(min(max(state.agents.count, 1), 3)) * 61)
                    .onAppear {
                        lastActiveAgentID = firstActiveAgentID
                        if let id = firstActiveAgentID { proxy.scrollTo(id, anchor: .top) }
                    }
                    .onChange(of: firstActiveAgentID) { id in
                        guard id != lastActiveAgentID else { return }
                        lastActiveAgentID = id
                        if let id {
                            withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(id, anchor: .top) }
                        }
                    }
                }
            }
        } label: {
            Label("Delegated Agents", systemImage: "cpu")
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
                    if let duration = agent.durationMS { Text(compactDuration(duration)).font(.caption2).foregroundStyle(.tertiary) }
                }

                HStack(spacing: 6) {
                    Text(providerName(agent.provider)).font(.caption2).foregroundStyle(.secondary)
                    Text("·").font(.caption2).foregroundStyle(.tertiary)
                    Text(modelName(agent.model)).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    if let reasoning = agent.reasoning, !reasoning.isEmpty {
                        HStack(spacing: 3) {
                            Image(systemName: "brain").font(.system(size: 8, weight: .semibold))
                            Text(reasoningBadge(reasoning)).font(.system(size: 9, weight: .semibold))
                        }
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(.quaternary, in: Capsule())
                        .help("Reasoning: \(reasoningBadge(reasoning))")
                    }
                }

                HStack(spacing: 5) {
                    if let tool = agent.lastTool, !tool.isEmpty {
                        Image(systemName: toolSymbol(tool)).font(.caption2).foregroundStyle(.secondary)
                        Text(cleanToolName(tool)).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    } else {
                        Image(systemName: "ellipsis.circle").font(.caption2).foregroundStyle(.tertiary)
                        Text(agent.isActive ? "Waiting for first tool" : "No tool calls").font(.caption2).foregroundStyle(.tertiary)
                    }
                    Spacer()
                    Image(systemName: "wrench.and.screwdriver.fill").font(.system(size: 8)).foregroundStyle(.tertiary)
                    Text("\(agent.toolCallCount ?? 0)").font(.caption2).foregroundStyle(.secondary)
                    if let retries = agent.retryCount, retries > 0 {
                        Image(systemName: "arrow.clockwise").font(.system(size: 8)).foregroundStyle(.orange)
                        Text("\(retries)").font(.caption2).foregroundStyle(.orange)
                    }
                }
            }
        }.padding(.vertical, 5)
    }

    private var activityCard: some View {
        GroupBox {
            if state.connectionState == .disconnected && displayActivityEvents.isEmpty {
                Text("Disconnected · tool activity unavailable.").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
            } else if state.connectionState == .degraded && displayActivityEvents.isEmpty {
                Text("Tool activity is temporarily unavailable.").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
            } else if state.serverRunning && displayActivityEvents.isEmpty {
                Text("No tool calls recorded in the last hour.").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
            } else if !state.serverRunning {
                Text("Start the server or retry the connection to see live tool activity.").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
            } else {
                VStack(alignment: .leading, spacing: 5) {
                    ScrollView(.vertical) {
                        LazyVStack(spacing: 0) {
                            ForEach(Array(displayActivityEvents.enumerated()), id: \.element.id) { index, event in
                                HStack(spacing: 8) {
                                    Image(systemName: toolSymbol(event.tool))
                                        .foregroundStyle(event.status.lowercased() == "running" ? Color.accentColor : Color.secondary)
                                        .font(.caption).frame(width: 16)
                                    VStack(alignment: .leading, spacing: 1) {
                                        HStack(spacing: 5) {
                                            Text(cleanToolName(event.tool)).font(.caption.weight(.medium)).lineLimit(1)
                                            if event.status.lowercased() == "running" {
                                                Text("LIVE").font(.system(size: 8, weight: .bold))
                                                    .padding(.horizontal, 4).padding(.vertical, 1)
                                                    .background(.quaternary, in: Capsule())
                                            }
                                        }
                                        if let context = state.resolvedBrowserContext(for: event) {
                                            Text("\(browserActivityLabel(context)) · \(event.durationMS ?? 0) ms")
                                                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                                        } else {
                                            Text("\(event.source.uppercased()) · \(event.durationMS ?? 0) ms · \(relativeTime(event.timestamp))")
                                                .font(.caption2).foregroundStyle(.secondary)
                                        }
                                    }
                                    Spacer()
                                    if let context = state.resolvedBrowserContext(for: event), context.canShowTab, event.tool != "browser_close_tab" {
                                        Button { state.showBrowserTab(event) } label: {
                                            Image(systemName: "arrow.up.forward.app").font(.caption2)
                                        }
                                        .buttonStyle(.borderless)
                                        .help("Show the real browser tab")
                                    }
                                    Image(systemName: eventStatusSymbol(event.status))
                                        .foregroundStyle(eventStatusColor(event.status)).font(.caption)
                                        .help(event.status.capitalized)
                                }.padding(.vertical, 5)
                                if index < displayActivityEvents.count - 1 { Divider() }
                            }
                        }
                    }
                    .frame(height: CGFloat(min(max(displayActivityEvents.count, 1), 5)) * 39)
                    if !state.browserActionStatus.isEmpty {
                        Text(state.browserActionStatus).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
        } label: { Label("Latest Tool Usage", systemImage: "waveform.path.ecg") }
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
                            Text("Allowed means the server permits the capability. It does not mean a confirmation prompt will appear.")
                                .font(.caption2.weight(.medium)).foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)

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
                    Toggle("Start ngrok with server", isOn: $settings.ngrokOnStart).onChange(of: settings.ngrokOnStart) { _ in persistSettings() }
                    HStack {
                        Text("Port")
                        TextField("8000", value: $settings.serverPort, format: .number).frame(width: 70).onSubmit { persistSettings(); Task { await state.refresh() } }
                        Spacer()
                        Button("Check Update") { state.checkForUpdates() }
                        Button("Update Now") { state.installUpdate() }
                    }
                    HStack { Text("CLI"); TextField("Auto-detect", text: $settings.cliPath).onSubmit { persistSettings() } }.font(.caption)
                }.padding(.top, 8)
            }.font(.caption)
        }
    }

    private var footer: some View {
        HStack {
            Text("Settings apply live; server controls stay available when Voice is collapsed.").font(.caption2).foregroundStyle(.secondary).lineLimit(2)
            Spacer(); Button("Quit") { state.quitApp() }
        }
    }

    private var connectionColor: Color {
        switch state.connectionState {
        case .connected: return .green
        case .connecting, .degraded: return .orange
        case .disconnected: return .red
        }
    }

    private var connectionBannerSymbol: String {
        switch state.connectionState {
        case .connecting: return "arrow.triangle.2.circlepath"
        case .connected: return "checkmark.circle.fill"
        case .degraded: return "exclamationmark.triangle.fill"
        case .disconnected: return "wifi.slash"
        }
    }

    private func metric(title: String, value: String) -> some View {
        VStack(spacing: 2) { Text(value).font(.subheadline.weight(.semibold)); Text(title).font(.caption2).foregroundStyle(.secondary) }.frame(maxWidth: .infinity)
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
        case "completed": return "Completed"
        case "failed": return "Failed"
        case "cancelled": return "Cancelled"
        case "timeout": return "Timed out"
        case "stalled": return "Stalled"
        default: return "Unknown status"
        }
    }

    private func agentPhaseColor(_ agent: AgentInfo) -> Color {
        switch agentPhaseKey(agent) {
        case "completed": return .green
        case "failed": return .red
        case "timeout", "stalled", "retrying": return .orange
        case "cancelled": return .secondary
        case "starting", "reasoning", "tool", "finalizing": return .accentColor
        default: return .secondary
        }
    }

    private func providerName(_ provider: String?) -> String {
        switch provider?.lowercased() {
        case "opencode": return "OpenCode"
        case "codex": return "Codex"
        case .some(let value): return value.capitalized
        case .none: return "AI"
        }
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

    private func cleanToolName(_ tool: String) -> String {
        var value = tool
        for prefix in ["mac-mcp_", "mac_mcp_", "mcp_"] where value.hasPrefix(prefix) { value.removeFirst(prefix.count) }
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
        case .success: return .green
        case .error: return .red
        case .update: return .accentColor
        case .info: return .secondary
        }
    }

    private func sessionLifecycleLabel(_ state: SteeringLifecycleState) -> String {
        switch state {
        case .ready: return "Ready"
        case .queued: return "Steering queued"
        case .delivered: return "Steering delivered"
        case .acknowledged: return "Steering acknowledged"
        case .failed: return "Delivery failed; retry pending"
        case .disconnected: return "Disconnected"
        case .expired: return "Expired"
        case .unknown: return "Unknown lifecycle"
        }
    }

    private func sessionLifecycleSymbol(_ state: SteeringLifecycleState) -> String? {
        switch state {
        case .queued: return "clock.badge.exclamationmark.fill"
        case .delivered: return "paperplane.circle.fill"
        case .failed: return "exclamationmark.triangle.fill"
        case .disconnected: return "wifi.slash"
        case .expired: return "clock.badge.xmark"
        case .ready, .acknowledged, .unknown: return nil
        }
    }

    private func sessionLifecycleColor(_ state: SteeringLifecycleState) -> Color {
        switch state {
        case .queued: return .orange
        case .delivered: return .accentColor
        case .failed, .disconnected, .expired: return .red
        default: return .secondary
        }
    }

    private func sessionDetailLine(_ session: SteeringSession) -> String {
        let base = "\(session.detail) · \(compactDuration(session.activityMS))"
        switch session.effectiveLifecycleState {
        case .queued, .delivered, .acknowledged, .failed:
            return base + " · " + sessionLifecycleLabel(session.effectiveLifecycleState)
        default:
            return base
        }
    }

    private func compactDuration(_ milliseconds: Int) -> String {
        let seconds = max(0, milliseconds / 1000)
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m" }
        return "\(minutes / 60)h \(minutes % 60)m"
    }

    private func applySessionRetention() {
        let trimmed = sessionMinutesText.trimmingCharacters(in: .whitespacesAndNewlines)
        let minutes = trimmed.isEmpty ? 10 : (Int(trimmed) ?? 10)
        guard minutes > 0 else { sessionMinutesText = ""; return }
        if minutes == 10 && trimmed.isEmpty {
            sessionMinutesText = ""
        } else {
            sessionMinutesText = String(minutes)
        }
        state.updateSteeringRetention(minutes: minutes)
    }

    private func persistSettings() { do { try settings.save(); settingsMessage = "Saved" } catch { settingsMessage = error.localizedDescription } }
    private func saveGroqKey() { do { try settings.saveGroqKey(groqKey); groqKey = ""; settingsMessage = "Groq key saved securely in Keychain" } catch { settingsMessage = error.localizedDescription } }
    private func removeGroqKey() { do { try settings.removeGroqKey(); settingsMessage = "Groq key removed" } catch { settingsMessage = error.localizedDescription } }
    private func relativeTime(_ timestamp: Double) -> String {
        let seconds = max(0, Int(Date().timeIntervalSince1970 - timestamp))
        if seconds < 60 { return "\(seconds)s ago" }; if seconds < 3600 { return "\(seconds / 60)m ago" }; return "\(seconds / 3600)h ago"
    }
}

struct RobotRunner: View {
    let active: Bool
    var body: some View {
        TimelineView(.animation(minimumInterval: active ? 0.12 : 1.0)) { context in
            Canvas { canvas, size in
                let t = context.date.timeIntervalSinceReferenceDate
                let phase = active ? sin(t * 9) : 0
                let bob = active ? abs(phase) * 1.4 : 0
                let centerX = size.width / 2
                let bodyY = size.height / 2 - bob
                let stroke = Color.primary.opacity(0.85)
                var antenna = Path(); antenna.move(to: CGPoint(x: centerX, y: bodyY - 13)); antenna.addLine(to: CGPoint(x: centerX, y: bodyY - 18)); canvas.stroke(antenna, with: .color(stroke), lineWidth: 1.6)
                canvas.fill(Path(ellipseIn: CGRect(x: centerX - 2, y: bodyY - 21, width: 4, height: 4)), with: .color(active ? .green : .secondary))
                let head = RoundedRectangle(cornerRadius: 5).path(in: CGRect(x: centerX - 12, y: bodyY - 12, width: 24, height: 18)); canvas.fill(head, with: .color(Color.primary.opacity(0.10))); canvas.stroke(head, with: .color(stroke), lineWidth: 1.6)
                canvas.fill(Path(ellipseIn: CGRect(x: centerX - 6, y: bodyY - 6, width: 3.5, height: 3.5)), with: .color(stroke)); canvas.fill(Path(ellipseIn: CGRect(x: centerX + 3, y: bodyY - 6, width: 3.5, height: 3.5)), with: .color(stroke))
                var torso = Path(); torso.move(to: CGPoint(x: centerX, y: bodyY + 6)); torso.addLine(to: CGPoint(x: centerX, y: bodyY + 14)); canvas.stroke(torso, with: .color(stroke), lineWidth: 2)
                let swing = active ? CGFloat(phase) * 6 : 0
                var limbs = Path(); limbs.move(to: CGPoint(x: centerX, y: bodyY + 14)); limbs.addLine(to: CGPoint(x: centerX - 7 + swing, y: bodyY + 21)); limbs.move(to: CGPoint(x: centerX, y: bodyY + 14)); limbs.addLine(to: CGPoint(x: centerX + 7 - swing, y: bodyY + 21)); limbs.move(to: CGPoint(x: centerX, y: bodyY + 8)); limbs.addLine(to: CGPoint(x: centerX - 10 - swing * 0.7, y: bodyY + 12)); limbs.move(to: CGPoint(x: centerX, y: bodyY + 8)); limbs.addLine(to: CGPoint(x: centerX + 10 + swing * 0.7, y: bodyY + 12)); canvas.stroke(limbs, with: .color(stroke), style: StrokeStyle(lineWidth: 2, lineCap: .round))
            }
        }.accessibilityLabel(active ? "Agent working" : "Agent idle")
    }
}
