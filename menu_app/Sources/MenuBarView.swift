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
    @State private var lastActiveAgentID: String? = nil

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                header
                serverCard
                steeringCard
                if state.activeAgents > 0 || !state.agents.isEmpty { agentCard }
                activityCard
                voiceCard
                advancedCard
                footer
            }
            .padding(16)
        }
        .frame(width: 400, height: 660)
        .background(.regularMaterial)
        .task { await state.refresh(); audio.refresh() }
    }

    private var displayAgents: [AgentInfo] {
        state.agents.filter(\.isActive) + state.agents.filter { !$0.isActive }
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
                    Circle().fill(state.serverRunning ? Color.green : Color.secondary).frame(width: 7, height: 7)
                    Text(state.serverRunning ? "Server running · v\(state.version)" : "Server stopped")
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
                    Button { Task { await state.refresh() } } label: { Image(systemName: "arrow.triangle.2.circlepath") }.help("Refresh")
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
            VStack(alignment: .leading, spacing: 9) {
                if state.steeringTargets.isEmpty {
                    HStack(spacing: 8) {
                        Image(systemName: "bubble.left.and.bubble.right").foregroundStyle(.secondary)
                        Text("No active ChatGPT tool call right now.").font(.caption).foregroundStyle(.secondary)
                        Spacer()
                    }.padding(.vertical, 2)
                } else {
                    if state.steeringTargets.count > 1 {
                        Text("Choose the active flow you want to steer.").font(.caption2).foregroundStyle(.secondary)
                    }
                    VStack(spacing: 6) {
                        ForEach(state.steeringTargets.prefix(5)) { target in
                            Button { state.selectedSteeringEventID = target.eventID } label: {
                                HStack(spacing: 8) {
                                    Image(systemName: state.selectedSteeringEventID == target.eventID ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(state.selectedSteeringEventID == target.eventID ? Color.accentColor : Color.secondary)
                                    Text("Flow \(target.flowNumber)")
                                        .font(.system(size: 9, weight: .semibold))
                                        .padding(.horizontal, 5).padding(.vertical, 2)
                                        .background(.quaternary, in: Capsule())
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(target.label).font(.caption.weight(.semibold)).lineLimit(1)
                                        Text("\(target.detail) · \(compactDuration(target.durationMS))")
                                            .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                    Spacer()
                                    if target.queued > 0 {
                                        Text("Queued").font(.caption2.weight(.medium)).foregroundStyle(.orange)
                                    }
                                }.padding(.vertical, 3).contentShape(Rectangle())
                            }.buttonStyle(.plain)
                        }
                    }
                }

                HStack(spacing: 7) {
                    TextField("Steer this ChatGPT flow…", text: $state.steeringPrompt)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { state.sendSteering() }
                    Button { state.sendSteering() } label: {
                        if state.steeringSending { ProgressView().controlSize(.small) }
                        else { Image(systemName: "paperplane.fill") }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(state.steeringSending || state.steeringPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || state.selectedSteeringEventID == nil)
                }
                Text(state.steeringStatus).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
            }.padding(2)
        } label: { Label("Steer ChatGPT", systemImage: "arrow.triangle.branch") }
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
                            Text("Working in background").font(.caption2).foregroundStyle(.secondary)
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
            if state.serverRunning && state.recentEvents.isEmpty {
                Text("No tool calls recorded in the last hour.").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
            } else if !state.serverRunning {
                Text("Start the server to see live tool activity.").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
            } else {
                ScrollView(.vertical) {
                    LazyVStack(spacing: 0) {
                        ForEach(Array(state.recentEvents.enumerated()), id: \.element.id) { index, event in
                            HStack(spacing: 8) {
                                Image(systemName: toolSymbol(event.tool))
                                    .foregroundStyle(.secondary).font(.caption).frame(width: 16)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(cleanToolName(event.tool)).font(.caption.weight(.medium)).lineLimit(1)
                                    Text("\(event.source.uppercased()) · \(event.durationMS ?? 0) ms · \(relativeTime(event.timestamp))")
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Image(systemName: eventStatusSymbol(event.status))
                                    .foregroundStyle(eventStatusColor(event.status)).font(.caption)
                                    .help(event.status.capitalized)
                            }.padding(.vertical, 5)
                            if index < state.recentEvents.count - 1 { Divider() }
                        }
                    }
                }
                .frame(height: CGFloat(min(max(state.recentEvents.count, 1), 5)) * 39)
            }
        } label: { Label("Latest Tool Usage", systemImage: "waveform.path.ecg") }
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

    private func eventStatusSymbol(_ status: String) -> String {
        switch status.lowercased() {
        case "success": return "checkmark.circle.fill"
        case "error": return "xmark.circle.fill"
        default: return "ellipsis.circle.fill"
        }
    }

    private func eventStatusColor(_ status: String) -> Color {
        switch status.lowercased() {
        case "success": return .green
        case "error": return .red
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
