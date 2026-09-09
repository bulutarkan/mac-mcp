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

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                header
                serverCard
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
                    HStack(spacing: 8) { ProgressView().controlSize(.small); Text(action).font(.caption).foregroundStyle(.secondary); Spacer() }
                } else if !state.actionMessage.isEmpty {
                    Text(state.actionMessage).font(.caption2.monospaced()).foregroundStyle(state.actionIsError ? .red : .secondary)
                        .lineLimit(3).frame(maxWidth: .infinity, alignment: .leading)
                }
            }.padding(2)
        } label: { Label("Server", systemImage: "server.rack") }
    }

    private var agentCard: some View {
        GroupBox {
            VStack(spacing: 8) {
                if state.activeAgents > 0 {
                    HStack(spacing: 10) {
                        RobotRunner(active: true).frame(width: 44, height: 38)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(state.activeAgents) delegated agent\(state.activeAgents == 1 ? "" : "s") working")
                                .font(.caption.weight(.semibold))
                            Text("Live from /dashboard/api/agents").font(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                }
                ScrollView(.vertical) {
                    LazyVStack(spacing: 0) {
                        ForEach(Array(displayAgents.enumerated()), id: \.element.id) { index, agent in
                            agentRow(agent)
                            if index < displayAgents.count - 1 { Divider() }
                        }
                    }
                }
                .frame(height: CGFloat(min(max(state.agents.count, 1), 3)) * 57)
            }
        } label: {
            Label(state.activeAgents > 0 ? "Delegated Agents — Active & Recent" : "Delegated Agents — Recent", systemImage: "cpu")
        }
    }

    private func agentRow(_ agent: AgentInfo) -> some View {
        HStack(spacing: 9) {
            Image(systemName: agent.isActive ? "bolt.circle.fill" : "clock.arrow.circlepath")
                .foregroundStyle(agent.isActive ? Color.green : Color.secondary).frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(agent.title ?? agent.agentID).font(.caption.weight(.semibold)).lineLimit(1)
                    Spacer()
                    Text(agent.phase ?? agent.status ?? "unknown").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
                Text([agent.provider, agent.model].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                Text(agent.lastTool.map { "Last: \($0) · \(agent.toolCallCount ?? 0) calls" } ?? "\(agent.toolCallCount ?? 0) tool calls")
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
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
                                Image(systemName: event.status == "success" ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                                    .foregroundStyle(event.status == "success" ? Color.green : Color.orange).font(.caption)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(event.tool).font(.caption.weight(.medium)).lineLimit(1)
                                    Text("\(event.source.uppercased()) · \(event.durationMS ?? 0) ms · \(relativeTime(event.timestamp))")
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                                Spacer()
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
