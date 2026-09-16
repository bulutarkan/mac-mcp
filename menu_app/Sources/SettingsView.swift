import AppKit
import SwiftUI

private enum SettingsSection: String, CaseIterable, Identifiable {
    case subagents
    case browser
    case permissions
    case voice
    case advanced

    var id: String { rawValue }

    var title: String {
        switch self {
        case .subagents: return "Subagents"
        case .browser: return "Browser Activity"
        case .permissions: return "Permissions & Approvals"
        case .voice: return "Voice"
        case .advanced: return "Advanced"
        }
    }

    var symbol: String {
        switch self {
        case .subagents: return "cpu"
        case .browser: return "sparkles.rectangle.stack"
        case .permissions: return "checkmark.shield"
        case .voice: return "waveform.and.mic"
        case .advanced: return "gearshape.2"
        }
    }
}

struct SettingsView: View {
    @ObservedObject var state: AppState
    @ObservedObject var settings: SettingsStore
    @StateObject private var audio = AudioDeviceStore()
    @State private var selection: SettingsSection = .subagents
    @State private var notice = ""
    @State private var groqKey = ""
    @State private var cloudflareToken = ""

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            detail
        }
        .frame(width: 720, height: 520)
        .background(.regularMaterial)
        .task {
            audio.refresh()
            state.refreshCloudflareCredentialState()
            await state.refreshProviders()
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Settings")
                .font(.title3.weight(.semibold))
                .padding(.horizontal, 14)
                .padding(.top, 16)
                .padding(.bottom, 8)

            ForEach(SettingsSection.allCases) { item in
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
        .frame(width: 160)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.38))
    }

    @ViewBuilder
    private var detail: some View {
        switch selection {
        case .subagents: subagentsPane
        case .browser: browserPane
        case .permissions: permissionsPane
        case .voice: voicePane
        case .advanced: advancedPane
        }
    }

    private var subagentsPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                paneHeader(
                    "Subagents",
                    subtitle: "Choose which delegated-agent providers Mac MCP may expose and spawn.",
                    refresh: true
                )

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

    private var browserPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader("Browser Activity", subtitle: "Visual Companion setup and background browser integration status.")

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
                paneHeader("Permissions & Approvals", subtitle: "Review capability profiles and approval behavior for Mac MCP actions.")

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

    private var voicePane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader("Voice", subtitle: "Configure the experimental Ask User Voice tool and audio devices.")

                Toggle(isOn: $settings.voiceEnabled) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Ask User Voice").font(.subheadline.weight(.semibold))
                        Text("Falls back to ask_user when disabled.")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                .onChange(of: settings.voiceEnabled) { _ in persistSettings() }

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
                .onChange(of: settings.inputDevice) { _ in persistSettings() }
                .onChange(of: settings.outputDevice) { _ in persistSettings() }
                .onChange(of: settings.language) { _ in persistSettings() }
                .onChange(of: settings.timeoutSeconds) { _ in persistSettings() }
                .onChange(of: settings.voiceName) { _ in persistSettings() }
                .onChange(of: settings.ttsRate) { _ in persistSettings() }

                Spacer(minLength: 0)
            }
            .padding(20)
        }
    }

    private var advancedPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                paneHeader("Advanced", subtitle: "Server startup, CLI location, and update controls.")

                GroupBox("Server") {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text("Public endpoint")
                                .foregroundStyle(.secondary)
                                .frame(width: 105, alignment: .leading)
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
                                persistSettings()
                            }
                        }
                        if settings.publicEndpointMode == "custom" || settings.publicEndpointMode == "cloudflare" {
                            HStack {
                                Text("Public URL")
                                    .foregroundStyle(.secondary)
                                    .frame(width: 105, alignment: .leading)
                                TextField("https://example.com/mcp", text: $settings.publicURL)
                                    .textFieldStyle(.roundedBorder)
                                    .onSubmit { persistSettings() }
                            }
                        }
                        if settings.publicEndpointMode == "cloudflare" {
                            HStack {
                                Text("Tunnel token")
                                    .foregroundStyle(.secondary)
                                    .frame(width: 105, alignment: .leading)
                                SecureField("Paste once; it is never written to settings.json", text: $cloudflareToken)
                                    .textFieldStyle(.roundedBorder)
                                Button(state.cloudflareCredentialConfigured ? "Replace credential" : "Save credential") {
                                    let token = cloudflareToken
                                    cloudflareToken = ""
                                    state.saveCloudflareCredential(token)
                                }
                                .disabled(cloudflareToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || state.busyAction != nil)
                            }
                            HStack(spacing: 8) {
                                Text("")
                                    .frame(width: 105)
                                Label(
                                    state.cloudflareCredentialConfigured ? "Credential configured" : "Credential not configured",
                                    systemImage: state.cloudflareCredentialConfigured ? "checkmark.shield.fill" : "exclamationmark.shield"
                                )
                                .foregroundStyle(state.cloudflareCredentialConfigured ? Color.secondary : Color.orange)
                                Spacer()
                            }
                            .font(.caption)
                            HStack {
                                Text("Named tunnel")
                                    .foregroundStyle(.secondary)
                                    .frame(width: 105, alignment: .leading)
                                TextField("Optional name or UUID (token-file is preferred)", text: $settings.cloudflareTunnel)
                                    .textFieldStyle(.roundedBorder)
                                    .onSubmit { persistSettings() }
                            }
                            Text("cloudflared runs directly on this Mac and forwards to localhost. Install it with `brew install cloudflared` if it is not already available. The tunnel token is stored only in an owner-only 0600 credential file.")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        HStack {
                            Text("Port")
                                .foregroundStyle(.secondary)
                                .frame(width: 55, alignment: .leading)
                            TextField("8000", value: $settings.serverPort, format: .number)
                                .frame(width: 90)
                                .onSubmit {
                                    persistSettings()
                                    Task { await state.refresh() }
                                }
                            Spacer()
                        }
                        HStack {
                            Text("CLI")
                                .foregroundStyle(.secondary)
                                .frame(width: 55, alignment: .leading)
                            TextField("Auto-detect", text: $settings.cliPath)
                                .font(.system(.caption, design: .monospaced))
                                .onSubmit { persistSettings() }
                        }
                    }
                    .padding(.top, 5)
                }

                GroupBox("Updates") {
                    HStack {
                        Button("Check Update") { state.checkForUpdates() }
                        Button("Update Now") { state.installUpdate() }
                        Spacer()
                    }
                    .padding(.top, 5)
                }

                if let action = state.actionNotice {
                    Label(action.message, systemImage: action.symbolName)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Text("Some server settings take effect the next time Mac MCP is restarted.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)

                Spacer(minLength: 0)
            }
            .padding(20)
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

    private func providerRow(id: String, title: String, subtitle: String) -> some View {
        let provider = state.providerStatuses.first(where: { $0.id == id })
        let detected = provider?.detected ?? false
        let version = provider?.version?.trimmingCharacters(in: .whitespacesAndNewlines)
        let path = provider?.binaryPath?.trimmingCharacters(in: .whitespacesAndNewlines)

        return VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 10) {
                Image(systemName: detected ? "checkmark.circle.fill" : "minus.circle")
                    .foregroundStyle(detected ? Color.green : Color.secondary)
                    .font(.system(size: 16, weight: .medium))
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 7) {
                        Text(title).font(.subheadline.weight(.semibold))
                        Text(detected ? "Detected" : "Not detected")
                            .font(.system(size: 9, weight: .semibold))
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(.quaternary, in: Capsule())
                    }
                    Text(subtitle).font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("", isOn: providerBinding(id))
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .help(settings.providerEnabled(id) ? "Disable \(title)" : "Enable \(title)")
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
                    notice = "\(providerDisplayName(id)) is now \(enabled ? "enabled" : "disabled")."
                    Task { await state.refreshProviders() }
                } catch {
                    settings.load()
                    notice = "Could not save provider settings."
                }
            }
        )
    }

    private func persistSettings() {
        do {
            try settings.save()
            notice = "Saved."
        } catch {
            notice = "Could not save settings: \(error.localizedDescription)"
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

    private func providerDisplayName(_ id: String) -> String {
        switch id {
        case "opencode": return "OpenCode"
        case "codex": return "Codex"
        case "chatgpt": return "ChatGPT Web CLI"
        default: return id
        }
    }
}
