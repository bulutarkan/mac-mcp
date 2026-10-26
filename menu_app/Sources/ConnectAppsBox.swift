import SwiftUI

/// Settings › Connections › AI Clients: the AI apps on this Mac with one button each, the same
/// one-click setup as the tour (`mac-mcp clients` / `mac-mcp connect`). Mac MCP writes the app's
/// setup itself; no address or key goes into the app's settings.
struct ConnectAppsBox: View {
    @ObservedObject var state: AppState

    @MacMCPState private var apps: [[String: Any]] = []
    @MacMCPState private var notes: [String: String] = [:]
    @MacMCPState private var working: String?
    @MacMCPState private var loaded = false

    private static let order = ["claude-code", "codex", "opencode", "claude-desktop", "kimi", "grok",
                                "antigravity", "pi", "cline", "kilo", "goose", "copilot"]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if !loaded {
                ProgressView().controlSize(.small).padding(14)
                Divider()
            }
            ForEach(Array(sortedApps.enumerated()), id: \.offset) { index, app in
                if index > 0 { Divider().padding(.leading, 56) }
                row(app)
            }
            Divider().padding(.leading, 56)
            chatGPTRow
        }
        .usageListBox()
        .task { await load() }
    }

    private var sortedApps: [[String: Any]] {
        apps.sorted { lhs, rhs in
            let left = (lhs["installed"] as? Bool) == true ? 0 : 1
            let right = (rhs["installed"] as? Bool) == true ? 0 : 1
            if left != right { return left < right }
            return (Self.order.firstIndex(of: lhs["id"] as? String ?? "") ?? 9)
                < (Self.order.firstIndex(of: rhs["id"] as? String ?? "") ?? 9)
        }
    }

    private func row(_ app: [String: Any]) -> some View {
        let id = app["id"] as? String ?? ""
        let name = app["name"] as? String ?? id
        let installed = (app["installed"] as? Bool) == true
        let added = (app["added"] as? Bool) == true
        let legacy = added && (app["keyless"] as? Bool) == false
        let detail = notes[id] ?? (legacy ? "Added the old way, with its key in the app’s settings"
            : added ? "Mac MCP is added" : installed ? "On this Mac" : "Not on this Mac")
        return HStack(spacing: 12) {
            logo(id, done: added && !legacy)
            VStack(alignment: .leading, spacing: 2) {
                Text(name).font(SettingsType.title)
                Text(detail).font(SettingsType.note).foregroundStyle(legacy ? UsageColors.warning : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            if installed {
                Button(legacy ? "Update" : added ? "Set up again" : "Connect") { connect(id, name: name) }
                    .controlSize(.small)
                    .disabled(working != nil)
                    .accessibilityLabel("\(legacy ? "Update" : added ? "Set up again" : "Connect") \(name)")
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .opacity(installed ? 1 : 0.55)
    }

    /// The app's logo, with a green check on it once Mac MCP is added.
    private func logo(_ id: String, done: Bool) -> some View {
        UsageAgentLogo(agent: Self.logos[id] ?? id, size: 30)
            .overlay(alignment: .bottomTrailing) {
                if done {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.white, .green)
                        .background(Circle().fill(Color(nsColor: .windowBackgroundColor)).padding(-1.5))
                        .offset(x: 3, y: 3)
                        .accessibilityHidden(true)
                }
            }
    }

    private static let logos = ["claude-code": "claude", "claude-desktop": "claude", "chatgpt": "openai"]

    /// ChatGPT is set up in its own window: Mac MCP copies the connector address (key included).
    private var chatGPTRow: some View {
        HStack(spacing: 12) {
            logo("chatgpt", done: false)
            VStack(alignment: .leading, spacing: 2) {
                Text("ChatGPT").font(SettingsType.title)
                Text(notes["chatgpt"] ?? "Needs a public address (Public Endpoint, above). Copies the connector address to paste in ChatGPT.")
                    .font(SettingsType.note).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            Button("Copy address") { connect("chatgpt", name: "ChatGPT") }
                .controlSize(.small)
                .disabled(working != nil)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }

    private func load() async {
        let result = await state.runCLICommand(["clients", "--json"])
        if let object = OnboardingBridge.jsonResult(result.output), let rows = object["apps"] as? [[String: Any]] {
            apps = rows
        }
        loaded = true
    }

    private func connect(_ id: String, name: String) {
        working = id
        Task {
            let result = await state.runCLICommand(["connect", "--client", id, "--json"])
            let object = OnboardingBridge.jsonResult(result.output)
            if object?["ok"] as? Bool == true {
                notes[id] = object?["next"] as? String ?? "Done."
            } else {
                notes[id] = object?["error"] as? String ?? "Mac MCP could not set up \(name). Use the manual setup below."
            }
            working = nil
            await load()
        }
    }
}
