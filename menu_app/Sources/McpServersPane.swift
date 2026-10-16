import SwiftUI

/// Settings › MCP Servers: the owner's own MCP servers (Exa, GitHub, a local stdio server…). Mac MCP
/// connects them and offers each tool to agents as <server>__<tool> (external_mcp.py); this view
/// lists, adds, tests and removes them through /dashboard/api/mcp-servers. Secret values (headers,
/// env) are sent once when a server is added and are never read back.

struct McpServer: Identifiable {
    let name: String
    let transport: String
    let status: String
    let enabled: Bool
    let listed: Bool
    let target: String
    let error: String?
    let tools: [(name: String, description: String)]
    var id: String { name }

    init(_ object: [String: Any]) {
        name = object["name"] as? String ?? ""
        transport = object["transport"] as? String ?? ""
        status = object["status"] as? String ?? "unknown"
        enabled = object["enabled"] as? Bool ?? true
        listed = object["listed"] as? Bool ?? true
        if let url = object["url"] as? String, !url.isEmpty {
            target = url
        } else {
            target = ([object["command"] as? String ?? ""] + (object["args"] as? [String] ?? [])).joined(separator: " ")
        }
        error = object["error"] as? String
        tools = (object["tools"] as? [[String: Any]] ?? []).map {
            ($0["name"] as? String ?? "", $0["description"] as? String ?? "")
        }
    }

    var statusLabel: String {
        switch status {
        case "connected": return "Connected"
        case "connecting": return "Connecting…"
        case "disabled": return "Off"
        case "error": return "Can’t connect"
        case "not_started": return "Starts with Mac MCP"
        default: return status.capitalized
        }
    }

    var statusColor: Color {
        switch status {
        case "connected": return .green
        case "error": return .red
        case "connecting": return .orange
        default: return .secondary
        }
    }
}

struct McpServersPane: View {
    @ObservedObject var state: AppState

    @MacMCPState private var servers: [McpServer] = []
    @MacMCPState private var configErrors: [String: String] = [:]
    @MacMCPState private var loaded = false
    @MacMCPState private var message = ""
    @MacMCPState private var expanded: Set<String> = []
    @MacMCPState private var busy: String?
    @MacMCPState private var testResult: [String: String] = [:]
    @MacMCPState private var adding = false
    @MacMCPState private var removing: McpServer?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("MCP Servers").font(.title2.weight(.semibold))
                    Text("Connect your own MCP servers. Agents using Mac MCP get their tools too, with the same permission profile, activity history and limits as Mac MCP’s own tools.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if !loaded, !message.isEmpty {
                    LoadProblem(message: message) { await load() }
                } else if !message.isEmpty {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }

                HStack {
                    Text(servers.isEmpty ? "No servers yet" : "\(servers.count) server\(servers.count == 1 ? "" : "s")")
                        .font(.headline)
                    Spacer()
                    Button {
                        adding = true
                    } label: {
                        Label("Add Server", systemImage: "plus")
                    }
                }

                ForEach(servers) { server in
                    serverCard(server)
                }
                ForEach(configErrors.sorted(by: { $0.key < $1.key }), id: \.key) { name, error in
                    Label("\(name == "*" ? "mcp_servers.json" : name): \(error)", systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.red)
                }

                GroupBox("How agents use them") {
                    Text("Each tool is named server__tool, for example exa__web_search_exa. Tools with “Show in tool list” on appear directly in the AI app’s tool list; the others are found with tool_discover and run with tool_invoke. Tools a server marks read-only also work in the Read Only profile; tools it marks destructive need the Trusted profile. API keys stay in ~/.mac-mcp/mcp_servers.json, readable only by you.")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, 5)
                }
                Spacer(minLength: 0)
            }
            .padding(22)
        }
        .task { await load() }
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                await load()
            }
        }
        .sheet(isPresented: $adding) {
            McpServerAddSheet(state: state) { added in
                adding = false
                if added { Task { await load() } }
            }
        }
        .alert(item: Binding(get: { removing }, set: { removing = $0 })) { server in
            Alert(
                title: Text("Remove \(server.name)?"),
                message: Text("Agents lose its \(server.tools.count) tools. Its saved keys are deleted too."),
                primaryButton: .destructive(Text("Remove")) { Task { await act("remove", server.name) } },
                secondaryButton: .cancel()
            )
        }
    }

    private func serverCard(_ server: McpServer) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .top, spacing: 10) {
                    Circle().fill(server.statusColor).frame(width: 9, height: 9).padding(.top, 5)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(server.name).font(.subheadline.weight(.semibold))
                            Text(server.transport == "stdio" ? "Local" : "Remote")
                                .font(.caption2).padding(.horizontal, 5).padding(.vertical, 1)
                                .background(Capsule().fill(Color.primary.opacity(0.08)))
                        }
                        Text(server.target).font(.caption2.monospaced()).foregroundStyle(.secondary)
                            .lineLimit(1).truncationMode(.middle)
                        Text(server.status == "connected" ? "\(server.statusLabel) · \(server.tools.count) tools" : server.statusLabel)
                            .font(.caption).foregroundStyle(server.status == "error" ? .red : .secondary)
                    }
                    Spacer()
                    Toggle("Use \(server.name)", isOn: Binding(
                        get: { server.enabled },
                        set: { on in Task { await act(on ? "enable" : "disable", server.name) } }
                    ))
                    .labelsHidden().toggleStyle(.switch)
                }
                if let error = server.error, server.status == "error" {
                    Text(error).font(.caption2.monospaced()).foregroundStyle(.secondary)
                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
                if let result = testResult[server.name] {
                    Text(result).font(.caption).foregroundStyle(.secondary)
                }
                HStack(spacing: 10) {
                    Toggle("Show in tool list", isOn: Binding(
                        get: { server.listed },
                        set: { on in Task { await act(on ? "list" : "unlist", server.name) } }
                    ))
                    .toggleStyle(.checkbox).font(.caption)
                    .help("Off: agents find these tools with tool_discover instead, keeping the AI app’s tool list short.")
                    Spacer()
                    if busy == server.name { ProgressView().controlSize(.small) }
                    Button("Test") { Task { await test(server.name) } }.disabled(busy != nil)
                    Button("Remove", role: .destructive) { removing = server }.disabled(busy != nil)
                }
                if !server.tools.isEmpty {
                    DisclosureGroup(isExpanded: Binding(
                        get: { expanded.contains(server.name) },
                        set: { open in if open { expanded.insert(server.name) } else { expanded.remove(server.name) } }
                    )) {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(server.tools, id: \.name) { tool in
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(tool.name).font(.caption.monospaced())
                                    if !tool.description.isEmpty {
                                        Text(tool.description.components(separatedBy: "\n").first ?? "")
                                            .font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                                    }
                                }
                            }
                        }
                        .padding(.top, 4)
                    } label: {
                        Text("Tools").font(.caption)
                    }
                }
            }
            .padding(.top, 4)
        }
    }

    private func load() async {
        let result = await state.taskAPI("dashboard/api/mcp-servers")
        guard result.ok else {
            message = result.object["message"] as? String ?? "Start Mac MCP to see your MCP servers."
            return
        }
        servers = (result.object["servers"] as? [[String: Any]] ?? []).map(McpServer.init)
        configErrors = result.object["config_errors"] as? [String: String] ?? [:]
        loaded = true
        message = ""
    }

    private func act(_ action: String, _ name: String) async {
        busy = name
        let result = await state.taskAPI("dashboard/api/mcp-servers/action", body: ["action": action, "name": name])
        busy = nil
        if !result.ok { message = result.object["message"] as? String ?? "Couldn’t do that." }
        testResult[name] = nil
        await load()
    }

    private func test(_ name: String) async {
        busy = name
        testResult[name] = "Testing…"
        let result = await state.taskAPI("dashboard/api/mcp-servers/action", body: ["action": "test", "name": name])
        busy = nil
        testResult[name] = McpServerAddSheet.describe(result)
    }
}

extension McpServer: Equatable {
    static func == (lhs: McpServer, rhs: McpServer) -> Bool { lhs.name == rhs.name }
}

// MARK: - Add

struct McpServerAddSheet: View {
    @ObservedObject var state: AppState
    let done: (Bool) -> Void

    private enum Kind: String, CaseIterable, Identifiable {
        case remote = "Remote URL"
        case local = "Local command"
        case json = "Paste JSON"
        var id: String { rawValue }
    }

    @MacMCPState private var kind: Kind = .remote
    @MacMCPState private var name = ""
    @MacMCPState private var url = ""
    @MacMCPState private var command = ""
    @MacMCPState private var secrets = ""
    @MacMCPState private var json = ""
    @MacMCPState private var busy = false
    @MacMCPState private var result = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Add MCP Server").font(.title3.weight(.semibold))
            Picker("Kind", selection: $kind) {
                ForEach(Kind.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented).labelsHidden()

            if kind == .json {
                Text("Paste the \"mcpServers\" block from Claude Desktop, Cursor or Claude Code.")
                    .font(.caption).foregroundStyle(.secondary)
                TextEditor(text: $json)
                    .font(.caption.monospaced())
                    .frame(minHeight: 160)
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.primary.opacity(0.15)))
                    .accessibilityLabel("Server JSON")
            } else {
                TextField("Name (e.g. exa)", text: $name)
                if kind == .remote {
                    TextField("URL (e.g. https://mcp.exa.ai/mcp)", text: $url)
                    Text("Headers, one per line (e.g. Authorization: Bearer …). Optional.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    TextField("Command (e.g. npx -y @modelcontextprotocol/server-github)", text: $command)
                    Text("Environment, one per line (e.g. GITHUB_TOKEN=…). Optional.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                TextEditor(text: $secrets)
                    .font(.caption.monospaced())
                    .frame(height: 60)
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.primary.opacity(0.15)))
                    .accessibilityLabel(kind == .remote ? "Headers" : "Environment")
            }
            if !result.isEmpty {
                Text(result).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if busy { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { done(false) }.keyboardShortcut(.cancelAction)
                if kind != .json {
                    Button("Test") { Task { await run("test") } }.disabled(busy || !ready)
                }
                Button("Add") { Task { await run(kind == .json ? "import" : "add") } }
                    .keyboardShortcut(.defaultAction).disabled(busy || !ready)
            }
        }
        .padding(20)
        .frame(width: 520)
    }

    private var ready: Bool {
        switch kind {
        case .json: return !json.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        case .remote: return !name.isEmpty && !url.isEmpty
        case .local: return !name.isEmpty && !command.isEmpty
        }
    }

    private func pairs(separator: Character) -> [String: String] {
        var out: [String: String] = [:]
        for line in secrets.split(separator: "\n") {
            guard let index = line.firstIndex(of: separator) else { continue }
            let key = line[..<index].trimmingCharacters(in: .whitespaces)
            let value = line[line.index(after: index)...].trimmingCharacters(in: .whitespaces)
            if !key.isEmpty { out[key] = value }
        }
        return out
    }

    private func run(_ action: String) async {
        var body: [String: Any] = ["action": action]
        switch kind {
        case .json:
            body["json"] = json
        case .remote:
            body["name"] = name.trimmingCharacters(in: .whitespaces).lowercased()
            body["url"] = url.trimmingCharacters(in: .whitespaces)
            body["headers"] = pairs(separator: ":")
        case .local:
            body["name"] = name.trimmingCharacters(in: .whitespaces).lowercased()
            body["command"] = command
            body["env"] = pairs(separator: "=")
        }
        busy = true
        result = action == "test" ? "Connecting…" : ""
        let response = await state.taskAPI("dashboard/api/mcp-servers/action", body: body)
        busy = false
        if action == "test" {
            result = Self.describe(response)
        } else if response.ok {
            done(true)
        } else {
            result = response.object["message"] as? String ?? "Couldn’t add the server."
        }
    }

    static func describe(_ response: (ok: Bool, object: [String: Any])) -> String {
        let detail = response.object["result"] as? [String: Any] ?? [:]
        if response.ok, detail["ok"] as? Bool == true {
            let tools = (detail["tools"] as? [[String: Any]] ?? []).compactMap { $0["name"] as? String }
            let ms = detail["elapsed_ms"] as? Int ?? 0
            return "Connected in \(ms) ms · \(tools.count) tools: " + tools.prefix(8).joined(separator: ", ")
                + (tools.count > 8 ? "…" : "")
        }
        return "Couldn’t connect: " + (response.object["message"] as? String ?? detail["error"] as? String ?? "unknown error")
    }
}
