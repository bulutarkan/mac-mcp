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
    let defaultModel: String

    init(_ object: [String: Any]) {
        id = object["id"] as? String ?? ""
        label = object["label"] as? String ?? id
        ready = object["ready"] as? Bool ?? false
        reason = object["reason"] as? String ?? ""
        defaultModel = object["default_model"] as? String ?? ""
    }
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
    @Published var items: [ChatItem] = []
    @Published var draft = ""
    @Published var error: String?
    @Published var loaded = false

    private let api: ChatAPI
    private var cursor = 0
    private var poller: Task<Void, Never>?

    init(api: @escaping ChatAPI) {
        self.api = api
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
        selectedID = id
        items = []
        cursor = 0
        chat = allChats.first { $0.id == id }
        poller?.cancel()
        guard id != nil else { return }
        poller = Task { [weak self] in await self?.pollLoop() }
    }

    func start(project: URL, agent: String, model: String) async -> Bool {
        let result = await api("dashboard/api/chats", ["project": project.path, "agent": agent, "model": model])
        guard result.ok, let chat = result.object["chat"] as? [String: Any], let id = chat["id"] as? String else {
            error = result.object["message"] as? String ?? "Could not start the chat."
            return false
        }
        await refreshList()
        select(id)
        return true
    }

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let id = selectedID, !text.isEmpty, chat?.running != true else { return }
        draft = ""
        chat?.running = true
        Task {
            let result = await api("dashboard/api/chats/\(id)/prompt", ["text": text])
            if !result.ok {
                error = result.object["message"] as? String ?? "The message was not sent."
                draft = text
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
        poller?.cancel()
        poller = Task { [weak self] in await self?.pollLoop() }
    }

    private func pollLoop() async {
        while !Task.isCancelled {
            await poll()
            // Quick while the agent works, so the answer streams; slow while idle.
            let busy = chat?.running == true
            try? await Task.sleep(nanoseconds: busy ? 350_000_000 : 3_000_000_000)
        }
    }

    func poll() async {
        guard let id = selectedID else { return }
        let result = await api("dashboard/api/chats/\(id)?after=\(cursor)", nil)
        guard result.ok, id == selectedID else { return }
        let wasRunning = chat?.running == true
        if let meta = result.object["chat"] as? [String: Any] { chat = ChatSummary(meta) }
        for object in result.object["items"] as? [[String: Any]] ?? [] {
            let item = ChatItem(object)
            if let index = items.firstIndex(where: { $0.id == item.id }) {
                items[index] = item
            } else {
                items.append(item)
            }
        }
        cursor = (result.object["cursor"] as? NSNumber)?.intValue ?? cursor
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
        let model = ChatModel { path, body in await state.taskAPI(path, body: body) }
        let window = NSWindow(contentViewController: NSHostingController(rootView: ChatView(model: model)))
        window.title = "Mac MCP Chat"
        window.styleMask = [.titled, .closable, .resizable, .miniaturizable, .fullSizeContentView]
        window.titlebarAppearsTransparent = true
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

struct ChatView: View {
    @ObservedObject var model: ChatModel
    @MacMCPState private var showingNewChat = false

    var body: some View {
        NavigationSplitView {
            ChatSidebar(model: model, newChat: { showingNewChat = true })
                .navigationSplitViewColumnWidth(min: 230, ideal: 260, max: 340)
        } detail: {
            if model.selectedID != nil {
                ChatConversation(model: model)
            } else {
                ChatWelcome(model: model, newChat: { showingNewChat = true })
            }
        }
        .sheet(isPresented: $showingNewChat) { NewChatSheet(model: model) }
        .task {
            await model.refreshList()
            if model.selectedID == nil, let first = model.allChats.first { model.select(first.id) }
        }
    }
}

private struct ChatSidebar: View {
    @ObservedObject var model: ChatModel
    let newChat: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Button(action: newChat) {
                Label("New Chat", systemImage: "square.and.pencil")
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 10).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: VisualTokens.Radius.control).fill(VisualTokens.quietFill))
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 10).padding(.top, 12).padding(.bottom, 6)
            .keyboardShortcut("n", modifiers: .command)

            List(selection: Binding(get: { model.selectedID }, set: { model.select($0) })) {
                ForEach(model.projects) { project in
                    Section {
                        ForEach(project.chats) { chat in
                            ChatRow(chat: chat).tag(Optional(chat.id))
                                .contextMenu {
                                    Button("Delete Chat", role: .destructive) { model.delete(chat.id) }
                                }
                        }
                    } header: {
                        Label(project.name, systemImage: "folder").help(project.id)
                    }
                }
            }
            .listStyle(.sidebar)
        }
    }
}

private struct ChatRow: View {
    let chat: ChatSummary

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(chat.title).lineLimit(1)
                Text(Date(timeIntervalSince1970: chat.updatedAt), style: .relative)
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 4)
            if chat.status == "waiting" {
                Image(systemName: "hand.raised.fill").foregroundStyle(VisualTokens.warning)
                    .help("Waiting for your answer")
            } else if chat.running {
                ProgressView().controlSize(.small)
            }
        }
        .padding(.vertical, 2)
    }
}

private struct ChatWelcome: View {
    @ObservedObject var model: ChatModel
    let newChat: () -> Void

    var body: some View {
        VStack(spacing: VisualTokens.Space.m) {
            Image(systemName: "bubble.left.and.text.bubble.right")
                .font(.system(size: 44, weight: .light)).foregroundStyle(VisualTokens.accent)
            Text("Work on a project with an agent").font(.title2.weight(.semibold))
            Text("Pick a folder and talk. The agent reads and changes files there and uses your Mac through Mac MCP. It asks you before it changes anything.")
                .multilineTextAlignment(.center).foregroundStyle(.secondary).frame(maxWidth: 420)
            Button("Choose a Project Folder…", action: newChat)
                .buttonStyle(.borderedProminent).controlSize(.large)
            if let error = model.error {
                Text(error).font(.callout).foregroundStyle(VisualTokens.error)
            }
        }
        .padding(VisualTokens.Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: Conversation

private struct ChatConversation: View {
    @ObservedObject var model: ChatModel

    var body: some View {
        VStack(spacing: 0) {
            ChatHeader(chat: model.chat)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: VisualTokens.Space.s) {
                        ForEach(model.items) { item in
                            ChatItemView(item: item, agent: agentLabel, answer: { model.answer(item, option: $0) })
                                .id(item.id)
                        }
                        if model.chat?.running == true, model.items.last?.type == "user" {
                            ThinkingDots().id("thinking")
                        }
                        Color.clear.frame(height: 4).id("end")
                    }
                    .padding(.horizontal, VisualTokens.Space.l)
                    .padding(.vertical, VisualTokens.Space.m)
                    .frame(maxWidth: 820)
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: model.items.last?.rev) { _ in
                    withAnimation(AppMotion.content) { proxy.scrollTo("end", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("end", anchor: .bottom) }
            }
            ChatComposer(model: model)
        }
    }

    private var agentLabel: String {
        model.agents.first { $0.id == model.chat?.agent }?.label ?? "The agent"
    }
}

private struct ChatHeader: View {
    let chat: ChatSummary?

    var body: some View {
        HStack(spacing: VisualTokens.Space.s) {
            VStack(alignment: .leading, spacing: 2) {
                Text(chat?.title ?? "").font(.headline).lineLimit(1)
                HStack(spacing: 6) {
                    Label(chat?.projectName ?? "", systemImage: "folder")
                    if let model = chat?.model, !model.isEmpty {
                        Text("·")
                        Text(model).lineLimit(1)
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if let chat, chat.contextSize > 0 {
                ContextGauge(used: chat.contextUsed, size: chat.contextSize)
            }
        }
        .padding(.horizontal, VisualTokens.Space.l)
        .padding(.top, 14).padding(.bottom, 10)
    }
}

private struct ContextGauge: View {
    let used: Int
    let size: Int

    var body: some View {
        let fraction = min(1, Double(used) / Double(max(size, 1)))
        HStack(spacing: 6) {
            ZStack {
                Circle().stroke(VisualTokens.hairline, lineWidth: 3)
                Circle().trim(from: 0, to: fraction)
                    .stroke(fraction > 0.8 ? VisualTokens.warning : VisualTokens.accent,
                            style: StrokeStyle(lineWidth: 3, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
            .frame(width: 16, height: 16)
            Text("\(Int(fraction * 100))% of memory").font(.caption).foregroundStyle(.secondary)
        }
        .help("How much of the agent's working memory this chat uses")
    }
}

private struct ChatItemView: View {
    let item: ChatItem
    let agent: String
    let answer: (ChatPermissionOption) -> Void

    var body: some View {
        switch item.type {
        case "user": UserBubble(text: item.text)
        case "assistant": AssistantText(text: item.text)
        case "thought": ThoughtView(text: item.text)
        case "tool": ToolCard(item: item)
        case "permission": PermissionCard(item: item, agent: agent, answer: answer)
        case "plan": PlanCard(item: item)
        default: NoticeLine(item: item)
        }
    }
}

private struct UserBubble: View {
    let text: String

    var body: some View {
        HStack {
            Spacer(minLength: 80)
            Text(text)
                .textSelection(.enabled)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous)
                    .fill(VisualTokens.accent.opacity(0.14)))
        }
        .padding(.top, VisualTokens.Space.xs)
    }
}

private struct AssistantText: View {
    let text: String

    var body: some View {
        Text(markdown)
            .textSelection(.enabled)
            .lineSpacing(3)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var markdown: AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}

private struct ThoughtView: View {
    let text: String
    @MacMCPState private var open = false

    var body: some View {
        DisclosureGroup(isExpanded: $open) {
            Text(text).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading).padding(.top, 4)
        } label: {
            Label("Thinking", systemImage: "sparkles").font(.callout).foregroundStyle(.secondary)
        }
    }
}

private struct ToolCard: View {
    let item: ChatItem
    @MacMCPState private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { withAnimation(AppMotion.content) { open.toggle() } } label: {
                HStack(spacing: 10) {
                    Image(systemName: symbol).frame(width: 18).foregroundStyle(VisualTokens.accent)
                    Text(item.title).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 8)
                    statusMark
                    if hasDetail {
                        Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(open ? 90 : 0))
                    }
                }
                .contentShape(Rectangle())
                .padding(.horizontal, 12).padding(.vertical, 9)
            }
            .buttonStyle(.plain)
            .disabled(!hasDetail)
            if open && hasDetail {
                Divider()
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(item.input.sorted(by: { $0.key < $1.key }), id: \.key) { key, value in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(key).font(.caption).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
                            Text(value).font(.caption.monospaced()).textSelection(.enabled).lineLimit(4)
                        }
                    }
                    if !item.output.isEmpty {
                        ScrollView {
                            Text(item.output).font(.caption.monospaced()).textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .frame(maxHeight: 180)
                        .padding(8)
                        .background(RoundedRectangle(cornerRadius: VisualTokens.Radius.tile).fill(VisualTokens.quietFill))
                    }
                }
                .padding(12)
            }
        }
        .background(RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous)
            .fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous)
            .strokeBorder(VisualTokens.hairline))
    }

    private var hasDetail: Bool { !item.output.isEmpty || !item.input.isEmpty }

    private var symbol: String {
        switch item.kind {
        case "read": return "doc.text"
        case "edit": return "pencil"
        case "delete": return "trash"
        case "move": return "arrow.right.doc.on.clipboard"
        case "search": return "magnifyingglass"
        case "execute": return "terminal"
        case "fetch": return "globe"
        case "think": return "sparkles"
        default: return "wrench.and.screwdriver"
        }
    }

    @ViewBuilder private var statusMark: some View {
        switch item.status {
        case "running": ProgressView().controlSize(.small)
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(VisualTokens.success)
        case "failed": Image(systemName: "xmark.circle.fill").foregroundStyle(VisualTokens.error)
        default: Image(systemName: "stop.circle").foregroundStyle(.secondary)
        }
    }
}

private struct PermissionCard: View {
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
        let shown = names.isEmpty ? (path.map { [$0] } ?? []) : names
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

private struct PlanCard: View {
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

private struct NoticeLine: View {
    let item: ChatItem

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: item.tone == "error" ? "exclamationmark.triangle.fill"
                  : item.tone == "warning" ? "exclamationmark.circle" : "stop.circle")
            Text(item.text).textSelection(.enabled)
        }
        .font(.caption)
        .foregroundStyle(item.tone == "error" ? VisualTokens.error : item.tone == "warning" ? VisualTokens.warning : .secondary)
        .frame(maxWidth: .infinity)
    }
}

private struct ThinkingDots: View {
    @MacMCPState private var phase = 0.0

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3) { index in
                Circle().fill(Color.secondary).frame(width: 6, height: 6)
                    .opacity(0.3 + 0.7 * abs(sin(phase + Double(index) * 0.6)))
            }
        }
        .padding(.vertical, 6)
        .onAppear { withAnimation(.linear(duration: 1.2).repeatForever(autoreverses: false)) { phase = .pi } }
        .accessibilityLabel("The agent is working")
    }
}

private struct ChatComposer: View {
    @ObservedObject var model: ChatModel
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 6) {
            if let error = model.error {
                Text(error).font(.caption).foregroundStyle(VisualTokens.error).frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField(placeholder, text: $model.draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...8)
                    .focused($focused)
                    .onSubmit { model.send() }
                    .disabled(waiting)
                if running {
                    Button(action: model.stop) {
                        Image(systemName: "stop.fill").font(.system(size: 12, weight: .bold))
                            .frame(width: 28, height: 28)
                            .background(Circle().fill(Color.primary.opacity(0.85)))
                            .foregroundStyle(Color(nsColor: .windowBackgroundColor))
                    }
                    .buttonStyle(.plain).help("Stop").keyboardShortcut(".", modifiers: .command)
                } else {
                    Button(action: model.send) {
                        Image(systemName: "arrow.up").font(.system(size: 13, weight: .bold))
                            .frame(width: 28, height: 28)
                            .background(Circle().fill(canSend ? VisualTokens.accent : VisualTokens.hairline))
                            .foregroundStyle(.white)
                    }
                    .buttonStyle(.plain).disabled(!canSend).help("Send")
                }
            }
            .padding(.leading, 14).padding(.trailing, 8).padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Color(nsColor: .textBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(VisualTokens.hairline))
            .shadow(color: .black.opacity(0.06), radius: 8, y: 2)
        }
        .frame(maxWidth: 820)
        .padding(.horizontal, VisualTokens.Space.l)
        .padding(.bottom, VisualTokens.Space.m).padding(.top, 6)
        .onAppear { focused = true }
    }

    private var running: Bool { model.chat?.running == true }
    private var waiting: Bool { model.chat?.status == "waiting" }
    private var canSend: Bool { !running && !model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    private var placeholder: String { waiting ? "Answer the question above first" : "Ask for anything in this project" }
}

// MARK: New chat

private struct NewChatSheet: View {
    @ObservedObject var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @MacMCPState private var folder: URL?
    @MacMCPState private var agent = ""
    @MacMCPState private var modelName = ""
    @MacMCPState private var starting = false

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
                    .onChange(of: agent) { value in
                        modelName = model.agents.first { $0.id == value }?.defaultModel ?? ""
                    }
                    if let option = selectedAgent, !option.ready {
                        Text(option.reason).font(.caption).foregroundStyle(VisualTokens.warning)
                    }
                }
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("Model").font(.callout.weight(.medium))
                TextField("The agent's default", text: $modelName).textFieldStyle(.roundedBorder)
            }
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
                        if await model.start(project: folder, agent: agent, model: modelName) { dismiss() }
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
                modelName = first.defaultModel
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
