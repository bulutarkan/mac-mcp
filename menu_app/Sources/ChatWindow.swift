import AppKit
import SwiftUI

// MARK: Mac MCP Chat: the owner's own conversations with a coding agent in a project folder.
// The server runs the agent (chat_sessions.py); this window reads /dashboard/api/chats and polls a
// chat's transcript by cursor while it runs, so answers stream in.

typealias ChatAPI = (_ path: String, _ body: [String: Any]?) async -> (ok: Bool, object: [String: Any])

struct ChatAgentOption: Identifiable, Hashable {
    let id: String
    let label: String
    let ready: Bool
    let reason: String

    init(_ object: [String: Any]) {
        id = object["id"] as? String ?? ""
        label = object["label"] as? String ?? id
        ready = object["ready"] as? Bool ?? false
        reason = object["reason"] as? String ?? ""
    }
}

/// One setting the agent offers (ACP configOptions): its model, thinking level, mode, …
struct ChatConfigChoice: Hashable, Identifiable {
    let value: String
    let name: String
    let description: String
    let group: String
    var id: String { value }
}

struct ChatConfigOption: Hashable, Identifiable {
    let id: String
    let name: String
    let category: String
    let current: String
    let options: [ChatConfigChoice]

    init(_ object: [String: Any]) {
        id = object["id"] as? String ?? ""
        name = object["name"] as? String ?? id
        category = object["category"] as? String ?? ""
        current = object["current"] as? String ?? ""
        options = (object["options"] as? [[String: Any]] ?? []).map {
            ChatConfigChoice(value: $0["value"] as? String ?? "", name: $0["name"] as? String ?? "",
                             description: $0["description"] as? String ?? "", group: $0["group"] as? String ?? "")
        }
    }

    var currentName: String { options.first { $0.value == current }?.name ?? current }
}

struct ChatSummary: Identifiable, Hashable {
    let id: String
    var title: String
    var project: String
    var agent: String
    var model: String
    var status: String
    var running: Bool
    var updatedAt: Double
    var contextUsed: Int
    var contextSize: Int
    var config: [ChatConfigOption]
    var connecting: Bool
    var agentAlive: Bool
    var agentError: String
    var turnStartedAt: Double

    init(_ object: [String: Any]) {
        id = object["id"] as? String ?? ""
        title = object["title"] as? String ?? "Chat"
        project = object["project"] as? String ?? ""
        agent = object["agent"] as? String ?? ""
        model = object["model"] as? String ?? ""
        status = object["status"] as? String ?? "idle"
        running = object["running"] as? Bool ?? false
        updatedAt = (object["updated_at"] as? NSNumber)?.doubleValue ?? 0
        let context = object["context"] as? [String: Any]
        contextUsed = (context?["used"] as? NSNumber)?.intValue ?? 0
        contextSize = (context?["size"] as? NSNumber)?.intValue ?? 0
        config = (object["config"] as? [[String: Any]] ?? []).map(ChatConfigOption.init)
        connecting = object["connecting"] as? Bool ?? false
        agentAlive = object["agent_alive"] as? Bool ?? false
        agentError = object["agent_error"] as? String ?? ""
        turnStartedAt = (object["turn_started_at"] as? NSNumber)?.doubleValue ?? 0
    }

    var projectName: String { (project as NSString).lastPathComponent }
}

struct ChatProjectGroup: Identifiable, Hashable {
    let id: String
    let name: String
    let chats: [ChatSummary]
}

struct ChatPermissionOption: Hashable {
    let id: String
    let label: String
    let kind: String
}

/// A file the owner added to a message.
struct ChatAttachment: Hashable, Identifiable {
    let name: String
    let path: String
    let kind: String
    var id: String { path }
    var isImage: Bool { kind == "image" }
}

struct ChatItem: Identifiable, Hashable {
    let id: String
    let type: String
    var text: String
    var title: String
    var kind: String
    var status: String
    var tone: String
    var files: [String]
    var output: String
    var input: [String: String]
    var options: [ChatPermissionOption]
    var choice: String?
    var entries: [(text: String, status: String)]
    var tool: String
    var server: String
    var attachments: [ChatAttachment]
    var at: Double
    // A turn summary: how long the agent worked, with which model, and the tokens it reported.
    var durationSeconds: Double
    var model: String
    var stop: String
    var tokens: Int
    let rev: Int

    init(_ object: [String: Any]) {
        id = object["id"] as? String ?? UUID().uuidString
        type = object["type"] as? String ?? "notice"
        text = object["text"] as? String ?? ""
        title = object["title"] as? String ?? ""
        kind = object["kind"] as? String ?? ""
        status = object["status"] as? String ?? ""
        tone = object["tone"] as? String ?? "neutral"
        files = object["files"] as? [String] ?? []
        output = object["output"] as? String ?? ""
        input = (object["input"] as? [String: Any] ?? [:]).compactMapValues { $0 as? String }
        options = (object["options"] as? [[String: Any]] ?? []).map {
            ChatPermissionOption(id: $0["id"] as? String ?? "", label: $0["label"] as? String ?? "",
                                 kind: $0["kind"] as? String ?? "")
        }
        choice = object["choice"] as? String
        entries = (object["entries"] as? [[String: Any]] ?? []).map {
            (text: $0["text"] as? String ?? "", status: $0["status"] as? String ?? "")
        }
        tool = object["tool"] as? String ?? ""
        server = object["server"] as? String ?? ""
        attachments = (object["attachments"] as? [[String: Any]] ?? []).map {
            ChatAttachment(name: $0["name"] as? String ?? "", path: $0["path"] as? String ?? "", kind: $0["kind"] as? String ?? "file")
        }
        at = (object["ended_at"] as? NSNumber ?? object["at"] as? NSNumber)?.doubleValue ?? 0
        durationSeconds = (object["duration_s"] as? NSNumber)?.doubleValue ?? 0
        model = object["model"] as? String ?? ""
        stop = object["stop"] as? String ?? ""
        tokens = ((object["input_tokens"] as? NSNumber)?.intValue ?? 0) + ((object["output_tokens"] as? NSNumber)?.intValue ?? 0)
        rev = (object["rev"] as? NSNumber)?.intValue ?? 0
    }

    static func == (lhs: ChatItem, rhs: ChatItem) -> Bool { lhs.id == rhs.id && lhs.rev == rhs.rev }
    func hash(into hasher: inout Hasher) { hasher.combine(id); hasher.combine(rev) }
}

@MainActor
final class ChatModel: ObservableObject {
    @Published var projects: [ChatProjectGroup] = []
    @Published var agents: [ChatAgentOption] = []
    @Published var selectedID: String?
    @Published var chat: ChatSummary?
    @Published var items: [ChatItem] = [] {
        // Grouped once per change, not on every redraw: a long chat has hundreds of items.
        didSet { turns = ChatTurn.group(items) }
    }
    @Published private(set) var turns: [ChatTurn] = []
    @Published var draft = ""
    /// Files waiting to go with the next message.
    @Published var attachments: [URL] = []
    @Published var error: String?
    @Published var loaded = false

    private let api: ChatAPI
    /// The request for a chat's event stream (server-sent events); without one the transcript is polled.
    private let eventsRequest: ((String) -> URLRequest?)?
    private var cursor = 0
    private var poller: Task<Void, Never>?

    init(api: @escaping ChatAPI, eventsRequest: ((String) -> URLRequest?)? = nil) {
        self.api = api
        self.eventsRequest = eventsRequest
    }

    var allChats: [ChatSummary] { projects.flatMap(\.chats) }

    func refreshList() async {
        let result = await api("dashboard/api/chats", nil)
        loaded = true
        guard result.ok else {
            error = result.object["message"] as? String ?? "Mac MCP is not answering."
            return
        }
        error = nil
        agents = (result.object["agents"] as? [[String: Any]] ?? []).map(ChatAgentOption.init)
        projects = (result.object["projects"] as? [[String: Any]] ?? []).map { project in
            ChatProjectGroup(id: project["path"] as? String ?? "", name: project["name"] as? String ?? "",
                             chats: (project["chats"] as? [[String: Any]] ?? []).map(ChatSummary.init))
        }
    }

    func select(_ id: String?) {
        guard id != selectedID || chat == nil else { return }
        // A chat left without a word is not kept: the sidebar shows only chats with something in them.
        if let previous = selectedID, previous != id, items.isEmpty, chat?.running != true {
            Task {
                // Only if the server finds it empty too: its items may not have loaded here yet.
                _ = await api("dashboard/api/chats/\(previous)/delete", ["only_if_empty": true])
                await refreshList()
            }
        }
        selectedID = id
        items = []
        cursor = 0
        chat = allChats.first { $0.id == id }
        poller?.cancel()
        guard let id else { return }
        poller = Task { [weak self] in
            // Opening a chat starts its agent, so the models and modes it offers show before the first message.
            _ = await self?.api("dashboard/api/chats/\(id)/connect", [:])
            await self?.follow(id)
        }
    }

    func reconnect() {
        guard let id = selectedID else { return }
        Task { _ = await api("dashboard/api/chats/\(id)/connect", [:]); await poll() }
    }

    func choose(_ option: ChatConfigOption, value: String) {
        guard let id = selectedID, value != option.current else { return }
        Task {
            let result = await api("dashboard/api/chats/\(id)/config", ["config_id": option.id, "value": value])
            if result.ok, let meta = result.object["chat"] as? [String: Any] {
                chat = ChatSummary(meta)
                error = nil
            } else {
                error = result.object["message"] as? String ?? "The agent did not take that setting."
            }
        }
    }

    func start(project: URL, agent: String) async -> Bool {
        let result = await api("dashboard/api/chats", ["project": project.path, "agent": agent])
        guard result.ok, let chat = result.object["chat"] as? [String: Any], let id = chat["id"] as? String else {
            error = result.object["message"] as? String ?? "Could not start the chat."
            return false
        }
        await refreshList()
        select(id)
        return true
    }

    func retry(_ text: String) {
        guard chat?.running != true else { return }
        draft = text
        send()
    }

    func attach(_ urls: [URL]) {
        let files = urls.filter { url in
            var directory: ObjCBool = false
            return url.isFileURL && FileManager.default.fileExists(atPath: url.path, isDirectory: &directory) && !directory.boolValue
        }
        for url in files where !attachments.contains(url) { attachments.append(url) }
        if attachments.count > 10 {
            attachments = Array(attachments.prefix(10))
            error = "Add at most 10 files to one message."
        }
    }

    func detach(_ url: URL) {
        attachments.removeAll { $0 == url }
    }

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let files = attachments
        guard let id = selectedID, !text.isEmpty || !files.isEmpty, chat?.running != true else { return }
        draft = ""
        attachments = []
        chat?.running = true
        Task {
            let result = await api("dashboard/api/chats/\(id)/prompt", ["text": text, "attachments": files.map(\.path)])
            if !result.ok {
                error = result.object["message"] as? String ?? "The message was not sent."
                draft = text
                attachments = files
                chat?.running = false
            }
            await poll()
            restartPolling()
        }
    }

    func stop() {
        guard let id = selectedID else { return }
        Task { _ = await api("dashboard/api/chats/\(id)/cancel", [:]); await poll() }
    }

    func answer(_ item: ChatItem, option: ChatPermissionOption) {
        guard let id = selectedID else { return }
        Task {
            let result = await api("dashboard/api/chats/\(id)/permission", ["item_id": item.id, "option_id": option.id])
            if !result.ok { error = result.object["message"] as? String }
            await poll()
        }
    }

    func delete(_ chatID: String) {
        Task {
            _ = await api("dashboard/api/chats/\(chatID)/delete", [:])
            if selectedID == chatID { select(nil); chat = nil }
            await refreshList()
        }
    }

    private func restartPolling() {
        guard eventsRequest == nil, let id = selectedID else { return }  // the event stream needs no restart
        poller?.cancel()
        poller = Task { [weak self] in await self?.follow(id) }
    }

    /// Keep the open chat current: its event stream when there is one, else polling.
    private func follow(_ id: String) async {
        guard eventsRequest != nil else { return await pollLoop() }
        await poll()  // everything up to now, then only changes
        var failures = 0
        while !Task.isCancelled && id == selectedID {
            if let request = eventsRequest?("dashboard/api/chats/\(id)/events?after=\(cursor)") {
                do {
                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                    failures = 0
                    for try await line in bytes.lines {
                        guard !Task.isCancelled, id == selectedID else { return }
                        guard line.hasPrefix("data: "), let data = line.dropFirst(6).data(using: .utf8),
                              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
                        await apply(object, for: id)
                    }
                } catch {
                    if Task.isCancelled { return }
                }
            }
            // The stream ended (server restart, sleep): catch up once, then reconnect, slower each time.
            failures += 1
            try? await Task.sleep(nanoseconds: UInt64(min(failures, 5)) * 1_000_000_000)
            await poll()
        }
    }

    private func pollLoop() async {
        while !Task.isCancelled {
            await poll()
            // Quick while the agent works or connects, so the answer streams; slow while idle.
            let busy = chat?.running == true || chat?.connecting == true
            try? await Task.sleep(nanoseconds: busy ? 350_000_000 : 3_000_000_000)
        }
    }

    func poll() async {
        guard let id = selectedID else { return }
        let result = await api("dashboard/api/chats/\(id)?after=\(cursor)", nil)
        guard result.ok else { return }
        await apply(result.object, for: id)
    }

    /// One update from the server (a poll answer or a stream event): the chat's state and its changed items.
    private func apply(_ object: [String: Any], for id: String) async {
        guard id == selectedID else { return }
        let wasRunning = chat?.running == true
        if let meta = object["chat"] as? [String: Any] { chat = ChatSummary(meta) }
        let fresh = (object["items"] as? [[String: Any]] ?? []).map(ChatItem.init)
        if !fresh.isEmpty {
            // One assignment for the whole batch, so the transcript regroups and redraws once.
            var merged = items
            var position = Dictionary(uniqueKeysWithValues: merged.enumerated().map { ($1.id, $0) })
            for item in fresh {
                if let index = position[item.id] {
                    merged[index] = item
                } else {
                    position[item.id] = merged.count
                    merged.append(item)
                }
            }
            items = merged
        }
        cursor = (object["cursor"] as? NSNumber)?.intValue ?? cursor
        if wasRunning != (chat?.running == true) { await refreshList() }
    }
}

// MARK: Window

@MainActor
final class ChatWindowController: NSObject, NSWindowDelegate {
    static let shared = ChatWindowController()
    private var windowController: NSWindowController?

    func show(state: AppState) {
        if let window = windowController?.window {
            NSApplication.shared.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            return
        }
        let model = ChatModel(api: { path, body in await state.taskAPI(path, body: body) }, eventsRequest: { path in
            guard let base = URL(string: "http://127.0.0.1:\(state.settings.serverPort)/") else { return nil }
            var components = URLComponents(url: base.appendingPathComponent(path.components(separatedBy: "?")[0]),
                                           resolvingAgainstBaseURL: false)
            components?.percentEncodedQuery = path.components(separatedBy: "?").dropFirst().first
            guard let url = components?.url else { return nil }
            var request = URLRequest(url: url)
            request.timeoutInterval = 35  // the server sends a keep-alive every 15 s; silence past this reconnects
            state.authorizeDashboardRequest(&request)
            return request
        })
        let window = NSWindow(contentViewController: NSHostingController(rootView: ChatView(model: model)))
        window.title = "Mac MCP Chat"
        window.titleVisibility = .hidden  // the chat draws its own header in the title bar's place
        window.styleMask = [.titled, .closable, .resizable, .miniaturizable, .fullSizeContentView]
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.setContentSize(NSSize(width: 1080, height: 720))
        window.contentMinSize = NSSize(width: 760, height: 500)
        window.setFrameAutosaveName("MacMCPChatWindow")
        window.center()
        window.isReleasedWhenClosed = false
        window.delegate = self
        let controller = NSWindowController(window: window)
        windowController = controller
        NSApplication.shared.activate(ignoringOtherApps: true)
        controller.showWindow(nil)
        window.makeKeyAndOrderFront(nil)
    }
}

// MARK: Conversation

struct PermissionCard: View {
    let item: ChatItem
    let agent: String
    let answer: (ChatPermissionOption) -> Void

    var body: some View {
        if item.status == "pending" { card } else { answered }
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Image(systemName: "hand.raised.fill").foregroundStyle(VisualTokens.warning)
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(agent) asks before it continues").font(.callout.weight(.semibold))
                    Text(subject).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
            if let command = item.input["command"] {
                Text(command).font(.caption.monospaced()).textSelection(.enabled).lineLimit(3)
                    .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: VisualTokens.Radius.tile).fill(VisualTokens.quietFill))
            }
            HStack(spacing: 8) {
                ForEach(item.options, id: \.id) { option in
                    if option.kind == "allow_once" {
                        Button(option.label) { answer(option) }.buttonStyle(.borderedProminent)
                            .keyboardShortcut(.defaultAction)
                    } else {
                        Button(option.label) { answer(option) }.buttonStyle(.bordered)
                    }
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous)
            .fill(VisualTokens.warning.opacity(0.12)))
        .overlay(RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous)
            .strokeBorder(VisualTokens.warning.opacity(0.45)))
    }

    /// Once answered, the question shrinks to one quiet line under the step it was about.
    private var answered: some View {
        HStack(spacing: 6) {
            Image(systemName: allowed ? "checkmark.shield" : "hand.raised")
            Text("\(resultText): \(subject)").lineLimit(1).truncationMode(.middle)
        }
        .font(.caption)
        .foregroundStyle(allowed ? Color.secondary : VisualTokens.warning)
        .padding(.leading, 12)
    }

    /// "Edit file · notes.md": what the agent asked, and the file it is about.
    private var subject: String {
        let names = item.files.map { ($0 as NSString).lastPathComponent }.filter { !$0.isEmpty }
        let path = item.input["path"].map { ($0 as NSString).lastPathComponent }
        let shown = (names.isEmpty ? (path.map { [$0] } ?? []) : names).filter { !item.title.contains($0) }
        return shown.isEmpty ? item.title : "\(item.title) · \(shown.joined(separator: ", "))"
    }

    private var allowed: Bool {
        item.options.first(where: { $0.id == item.choice })?.kind.hasPrefix("allow") == true
    }

    private var resultText: String {
        guard item.status == "answered", item.choice != nil else {
            return item.status == "expired" ? "Not answered in time" : "Not answered"
        }
        return allowed ? "You allowed" : "You said no"
    }
}

struct PlanCard: View {
    let item: ChatItem

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Plan", systemImage: "list.bullet").font(.callout.weight(.semibold))
            ForEach(Array(item.entries.enumerated()), id: \.offset) { _, entry in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: entry.status == "completed" ? "checkmark.circle.fill"
                          : entry.status == "in_progress" ? "circle.dotted" : "circle")
                        .foregroundStyle(entry.status == "completed" ? VisualTokens.success : .secondary)
                    Text(entry.text).font(.callout).strikethrough(entry.status == "completed", color: .secondary)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: VisualTokens.Radius.card).fill(VisualTokens.quietFill))
    }
}

// MARK: The agent's own settings: model, thinking, mode (what it offers, from the owner's own account)

struct ChatSettingsBar: View {
    @ObservedObject var model: ChatModel

    var body: some View {
        if let chat = model.chat {
            if !chat.agentError.isEmpty {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(VisualTokens.warning)
                    Text(chat.agentError).lineLimit(2).textSelection(.enabled)
                    Button("Try Again", action: model.reconnect).buttonStyle(.link)
                }
                .font(.caption)
            } else if chat.config.isEmpty {
                if chat.connecting || !chat.agentAlive {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.mini)
                        Text("Connecting to \(agentLabel)…")
                    }
                    .font(.caption).foregroundStyle(.secondary)
                }
            } else {
                HStack(spacing: 4) {
                    ForEach(shown) { option in
                        if option.category == "model" && option.options.count > 12 {
                            SearchablePicker(option: option) { model.choose(option, value: $0) }
                        } else {
                            ConfigMenu(option: option) { model.choose(option, value: $0) }
                        }
                    }
                    ForEach(toggles) { option in
                        ToggleChip(option: option) { model.choose(option, value: $0) }
                    }
                    if !more.isEmpty {
                        // The agent's other settings, each under its own name.
                        Menu {
                            ForEach(more) { option in
                                Section(option.name) {
                                    ForEach(option.options) { choice in
                                        Button { model.choose(option, value: choice.value) } label: {
                                            if choice.value == option.current {
                                                Label(choice.name, systemImage: "checkmark")
                                            } else {
                                                Text(choice.name)
                                            }
                                        }
                                    }
                                }
                            }
                        } label: {
                            ChipLabel(symbol: "slider.horizontal.3", text: "More")
                        }
                        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                        .modifier(ChipBackground())
                        .help(more.map(\.name).joined(separator: ", "))
                    }
                }
            }
        }
    }

    private var agentLabel: String { model.agents.first { $0.id == model.chat?.agent }?.label ?? "the agent" }

    private static let order = ["model", "thought_level", "mode"]

    private var shown: [ChatConfigOption] {
        (model.chat?.config ?? []).filter { Self.order.contains($0.category) }
            .sorted { (Self.order.firstIndex(of: $0.category) ?? 9) < (Self.order.firstIndex(of: $1.category) ?? 9) }
    }

    /// Settings that are only on or off (Claude's and Codex's Fast mode, Copilot's allow-all) are switches.
    private var toggles: [ChatConfigOption] {
        (model.chat?.config ?? []).filter { !Self.order.contains($0.category) && ToggleChip.isSwitch($0) }
    }

    private var more: [ChatConfigOption] {
        (model.chat?.config ?? []).filter {
            !Self.order.contains($0.category) && $0.options.count > 1 && !ToggleChip.isSwitch($0)
        }
    }
}

/// An on/off setting as a chip named after it, lit while on. Allowing everything is lit in the warning colour.
private struct ToggleChip: View {
    let option: ChatConfigOption
    let choose: (String) -> Void

    static func isSwitch(_ option: ChatConfigOption) -> Bool {
        Set(option.options.map { $0.value.lowercased() }) == ["on", "off"]
    }

    var body: some View {
        let on = option.current.lowercased() == "on"
        let risky = option.category == "permissions"
        Button {
            let target = option.options.first { $0.value.lowercased() == (on ? "off" : "on") }?.value ?? (on ? "off" : "on")
            choose(target)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: risky ? "exclamationmark.shield" : "bolt.fill").font(.caption)
                Text(option.name).font(.caption).lineLimit(1)
            }
            .foregroundStyle(on ? (risky ? VisualTokens.warning : ChatStyle.accent) : Color.secondary)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(Capsule().fill(on ? (risky ? VisualTokens.warning : ChatStyle.accent).opacity(0.16) : VisualTokens.quietFill))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help("\(option.name): \(on ? "on" : "off")")
        .accessibilityValue(on ? "On" : "Off")
    }
}

/// A chip that names the setting's current value; the menu lists what the agent offers.
private struct ConfigMenu: View {
    let option: ChatConfigOption
    let choose: (String) -> Void

    var body: some View {
        Menu {
            ForEach(option.options) { choice in
                Button { choose(choice.value) } label: {
                    if choice.value == option.current {
                        Label(choice.name, systemImage: "checkmark")
                    } else {
                        Text(choice.name)
                    }
                }
                .help(choice.description)
            }
        } label: {
            ChipLabel(symbol: ConfigChip.symbol(option.category), text: ConfigChip.text(option))
        }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        .modifier(ChipBackground())
        .help(option.name)
    }
}

/// Long model lists (OpenCode has hundreds): a popover with a search field, grouped by provider.
private struct SearchablePicker: View {
    let option: ChatConfigOption
    let choose: (String) -> Void
    @MacMCPState private var open = false
    @MacMCPState private var query = ""

    var body: some View {
        Button { open.toggle() } label: {
            ChipLabel(symbol: ConfigChip.symbol(option.category), text: ConfigChip.text(option))
        }
        .buttonStyle(.plain)
        .modifier(ChipBackground())
        .help(option.name)
        .popover(isPresented: $open, arrowEdge: .top) {
            VStack(spacing: 0) {
                TextField("Search \(option.options.count) models", text: $query)
                    .textFieldStyle(.roundedBorder).padding(10)
                Divider()
                List {
                    ForEach(matches) { choice in
                        Button {
                            choose(choice.value)
                            open = false
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(choice.name).lineLimit(1)
                                    if !choice.group.isEmpty || choice.name != choice.value {
                                        Text(choice.value).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                }
                                Spacer()
                                if choice.value == option.current {
                                    Image(systemName: "checkmark").foregroundStyle(VisualTokens.accent)
                                }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .listStyle(.plain)
            }
            .frame(width: 380, height: 420)
        }
    }

    private var matches: [ChatConfigChoice] {
        let words = query.lowercased().split(separator: " ").map(String.init)
        guard !words.isEmpty else { return option.options }
        return option.options.filter { choice in
            let text = "\(choice.name) \(choice.value) \(choice.group)".lowercased()
            return words.allSatisfy { text.contains($0) }
        }
    }
}

private enum ConfigChip {
    static func symbol(_ category: String) -> String {
        switch category {
        case "model": return "cpu"
        case "thought_level": return "brain"
        case "mode": return "shield.lefthalf.filled"
        default: return "slider.horizontal.3"
        }
    }

    static func text(_ option: ChatConfigOption) -> String {
        option.category == "thought_level" ? "Thinking: \(option.currentName)" : option.currentName
    }
}

private struct ChipLabel: View {
    let symbol: String
    let text: String

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: symbol).font(.caption)
            Text(text).font(.caption).lineLimit(1)
            Image(systemName: "chevron.down").font(.system(size: 8, weight: .semibold))
        }
        .foregroundStyle(.secondary)
    }
}

/// The chip's capsule, on the menu or button itself: a borderless menu would drop it from its label.
private struct ChipBackground: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(Capsule().fill(VisualTokens.quietFill))
            .contentShape(Capsule())
    }
}

// MARK: New chat

struct NewChatSheet: View {
    @ObservedObject var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @MacMCPState private var folder: URL?
    @MacMCPState private var agent = ""
    @MacMCPState private var starting = false

    init(model: ChatModel) {
        self.model = model
    }

    var body: some View {
        VStack(alignment: .leading, spacing: VisualTokens.Space.m) {
            Text("New Chat").font(.title3.weight(.semibold))
            VStack(alignment: .leading, spacing: 6) {
                Text("Project folder").font(.callout.weight(.medium))
                HStack {
                    Label(folder?.path ?? "No folder chosen", systemImage: "folder")
                        .lineLimit(1).truncationMode(.head)
                        .foregroundStyle(folder == nil ? .secondary : .primary)
                    Spacer()
                    Button("Choose…", action: chooseFolder)
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: VisualTokens.Radius.control).fill(VisualTokens.quietFill))
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("Agent").font(.callout.weight(.medium))
                if model.agents.isEmpty {
                    Text("Turn on an agent in Settings › Agents first.").font(.callout).foregroundStyle(.secondary)
                } else {
                    Picker("Agent", selection: $agent) {
                        ForEach(model.agents) { option in
                            Text(option.label).tag(option.id)
                        }
                    }
                    .labelsHidden()
                    if let option = selectedAgent, !option.ready {
                        Text(option.reason).font(.caption).foregroundStyle(VisualTokens.warning)
                    }
                }
            }
            Text("You pick the model, thinking and mode in the chat; they come from your own \(selectedAgent?.label ?? "agent") account.")
                .font(.caption).foregroundStyle(.secondary)
            if let error = model.error {
                Text(error).font(.caption).foregroundStyle(VisualTokens.error)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Start Chat") {
                    guard let folder else { return }
                    starting = true
                    Task {
                        if await model.start(project: folder, agent: agent) { dismiss() }
                        starting = false
                    }
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(folder == nil || agent.isEmpty || starting)
            }
        }
        .padding(VisualTokens.Space.l)
        .frame(width: 460)
        .onAppear {
            model.error = nil
            if let first = model.agents.first(where: \.ready) ?? model.agents.first {
                agent = first.id
            }
        }
    }

    private var selectedAgent: ChatAgentOption? { model.agents.first { $0.id == agent } }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        if panel.runModal() == .OK { folder = panel.url }
    }
}
