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

struct ChatSummary: Identifiable, Hashable, ChatListed {
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
    var commands: [ChatCommand]
    var queue: [ChatQueued]
    /// Whether the agent takes pictures in a message; nil until it has started and said.
    var takesImages: Bool?
    /// Started in private mode: kept only in memory, gone when Mac MCP stops.
    var isPrivate: Bool
    var pinnedAt: Double
    var archivedAt: Double

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
        commands = (object["commands"] as? [[String: Any]] ?? []).map {
            ChatCommand(name: $0["name"] as? String ?? "", description: $0["description"] as? String ?? "",
                        hint: $0["hint"] as? String ?? "")
        }
        queue = (object["queue"] as? [[String: Any]] ?? []).map {
            ChatQueued(id: $0["id"] as? String ?? "", text: $0["text"] as? String ?? "",
                       files: ($0["attachments"] as? [[String: Any]] ?? []).compactMap { $0["name"] as? String })
        }
        takesImages = object["takes_images"] as? Bool
        isPrivate = object["private"] as? Bool ?? false
        pinnedAt = (object["pinned_at"] as? NSNumber)?.doubleValue ?? 0
        archivedAt = (object["archived_at"] as? NSNumber)?.doubleValue ?? 0
    }

    var projectName: String { (project as NSString).lastPathComponent }
}

/// A message written while the agent worked: it goes out when the answer ends.
struct ChatQueued: Hashable, Identifiable {
    let id: String
    let text: String
    let files: [String]
}

/// A command's output as the agent streamed it, and how it ended.
struct ChatTerminal: Hashable {
    let output: String
    let exitCode: Int?
    let ended: Bool
}

/// A command the agent offers (Claude's /compact, Codex's /review, …), sent as "/name" in a message.
struct ChatCommand: Hashable, Identifiable {
    let name: String
    let description: String
    let hint: String
    var id: String { name }
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

/// An edit as a unified diff: its file, how many lines came and went, and the lines themselves.
struct ChatDiff: Hashable, Identifiable {
    let path: String
    let added: Int
    let removed: Int
    let created: Bool
    let lines: [String]
    let truncated: Bool
    var id: String { path + "#\(lines.count)" }
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
    var diffs: [ChatDiff]
    var terminal: ChatTerminal?
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
        diffs = (object["diffs"] as? [[String: Any]] ?? []).map {
            ChatDiff(path: $0["path"] as? String ?? "", added: ($0["added"] as? NSNumber)?.intValue ?? 0,
                     removed: ($0["removed"] as? NSNumber)?.intValue ?? 0, created: $0["created"] as? Bool ?? false,
                     lines: $0["lines"] as? [String] ?? [], truncated: $0["truncated"] as? Bool ?? false)
        }
        terminal = (object["terminal"] as? [String: Any]).map {
            ChatTerminal(output: $0["output"] as? String ?? "", exitCode: ($0["exit_code"] as? NSNumber)?.intValue,
                         ended: $0["ended"] as? Bool ?? false)
        }
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
    @Published var draft = "" {
        didSet { if draft != oldValue { commandIndex = 0 } }
    }
    /// The highlighted command in the "/" list (↑ and ↓ move it), and a draft whose list Esc closed.
    @Published var commandIndex = 0
    @Published var dismissedCommandDraft: String?
    @Published var search = ""
    /// Files waiting to go with the next message.
    @Published var attachments: [URL] = []
    @Published var error: String?
    @Published var loaded = false
    /// The open chat's live updates: live, reconnecting or offline (see ChatLink).
    @Published private(set) var link = ChatLink()
    /// Messages whose send failed and that could not go back into an empty message box.
    @Published var failedSends: [ChatFailedSend] = []

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
    /// The chats in the sidebar's own order (pinned, then each project's), archived ones left out.
    var listedChats: [ChatSummary] { pinnedChats + shownProjects.flatMap(\.chats) }
    var pinnedChats: [ChatSummary] { ChatLibrary.pinned(allChats, search: search) }
    var archivedChats: [ChatSummary] { ChatLibrary.archived(allChats, search: search) }
    /// The chat just archived, for the sidebar's Undo; it goes after a few seconds.
    @Published var lastArchived: ChatSummary?
    /// The quick switcher (⌘K): what is typed in it, its highlighted row, and the chats it lists.
    @Published var switcherQuery = ""
    @Published var switcherIndex = 0
    var switcherChats: [ChatSummary] { ChatLibrary.recent(allChats, search: switcherQuery, current: selectedID) }

    func refreshList() async {
        let result = await api("dashboard/api/chats", nil)
        loaded = true
        guard result.ok else {
            error = result.object["message"] as? String ?? "Mac MCP is not answering."
            return
        }
        error = nil
        agents = (result.object["agents"] as? [[String: Any]] ?? []).map(ChatAgentOption.init)
        setProjects(result.object)
    }

    // MARK: The chat list, live

    /// Says a chat finished or is waiting for the owner, unless they are looking at it: (chat id, title, body).
    var notify: ((String, String, String) -> Void)?
    /// Whether the owner has this chat open in front of them.
    var isLooking: (String) -> Bool = { _ in false }
    private var listFollower: Task<Void, Never>?
    private var lastStates: [String: (running: Bool, status: String)] = [:]

    /// Follow the whole chat list (any chat starting, finishing, waiting), so the sidebar is right about chats
    /// working in the background. One stream for the app's life; it reconnects on its own.
    func followList() {
        guard listFollower == nil, eventsRequest != nil else { return }
        listFollower = Task { [weak self] in await self?.listLoop() }
    }

    private func listLoop() async {
        var failures = 0
        while !Task.isCancelled {
            if let request = eventsRequest?("dashboard/api/chats/list-events") {
                do {
                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                    failures = 0
                    for try await line in bytes.lines {
                        guard !Task.isCancelled else { return }
                        guard line.hasPrefix("data: "), let data = line.dropFirst(6).data(using: .utf8),
                              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
                        setProjects(object)
                    }
                } catch {
                    if Task.isCancelled { return }
                }
            }
            failures += 1
            try? await Task.sleep(nanoseconds: UInt64(min(failures, 5)) * 1_000_000_000)
            await refreshList()
        }
    }

    private func setProjects(_ object: [String: Any]) {
        let groups = (object["projects"] as? [[String: Any]] ?? []).map { project in
            ChatProjectGroup(id: project["path"] as? String ?? "", name: project["name"] as? String ?? "",
                             chats: (project["chats"] as? [[String: Any]] ?? []).map(ChatSummary.init))
        }
        let known = !lastStates.isEmpty
        for summary in groups.flatMap(\.chats) {
            if known, let before = lastStates[summary.id], !isLooking(summary.id) {
                let agent = agents.first { $0.id == summary.agent }?.label ?? "The agent"
                if before.status != "waiting" && summary.status == "waiting" {
                    notify?(summary.id, summary.title, "\(agent) asks before it continues.")
                } else if before.running && !summary.running {
                    notify?(summary.id, summary.title, "\(agent) finished.")
                }
            }
            lastStates[summary.id] = (summary.running, summary.status)
        }
        projects = groups
    }

    /// Puts one of the owner's own messages back in the message box, to change and send again.
    @Published var focusComposer = 0

    func edit(_ message: ChatItem) {
        guard chat?.running != true else { return }
        draft = message.text
        attachments = message.attachments.map { URL(fileURLWithPath: $0.path) }
            .filter { FileManager.default.fileExists(atPath: $0.path) }
        focusComposer += 1
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
        link = ChatLink()
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

    /// Ask Again: sends the same words straight away, leaving what is in the message box alone.
    func retry(_ text: String) {
        guard let id = selectedID, chat?.running != true else { return }
        submit(text, [], to: id)
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

    /// Whether the message box has the keyboard (⌘V adds images and files only there).
    var composerFocused = false

    /// Adds what is on the clipboard if it is files or an image: files as they are, an image saved as a PNG next to
    /// the chats. False when it holds neither, so the text field pastes text as usual.
    func pasteFiles() -> Bool {
        let board = NSPasteboard.general
        if let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL],
           !urls.isEmpty {
            attach(urls)
            return true
        }
        guard board.canReadObject(forClasses: [NSImage.self], options: nil),
              let image = (board.readObjects(forClasses: [NSImage.self], options: nil) as? [NSImage])?.first,
              let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
              let png = bitmap.representation(using: .png, properties: [:]) else { return false }
        let environment = ProcessInfo.processInfo.environment
        let state = environment["MAC_MCP_STATE_DIR"].map { URL(fileURLWithPath: NSString(string: $0).expandingTildeInPath) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".mac-mcp")
        let folder = state.appendingPathComponent("chats/pasted", isDirectory: true)
        let formatter = DateFormatter()  // local time, named like a macOS screenshot
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd 'at' HH.mm.ss"
        let stamp = formatter.string(from: Date())
        let file: URL
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            // Two pastes in the same second get two files ("… 2.png"), never one written over the other.
            file = try ChatPaste.save(png, in: folder, stamp: stamp)
        } catch {
            self.error = "Could not keep the pasted image: \(error.localizedDescription)"
            return true
        }
        attach([file])
        return true
    }

    func detach(_ url: URL) {
        attachments.removeAll { $0 == url }
    }

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let files = attachments
        guard let id = selectedID, !text.isEmpty || !files.isEmpty else { return }
        draft = ""
        attachments = []
        submit(text, files, to: id)
    }

    /// Sends a message to a chat. If that fails, the words go back into its message box only while the owner is
    /// still in that chat and the box is empty; otherwise they wait above it as "Not sent", to send again or copy.
    private func submit(_ text: String, _ files: [URL], to id: String) {
        // While the agent works, the message waits on the server and goes out when the answer ends.
        let wasRunning = chat?.running == true
        chat?.running = true
        Task {
            let result = await api("dashboard/api/chats/\(id)/prompt", ["text": text, "attachments": files.map(\.path)])
            if !result.ok {
                let message = result.object["message"] as? String ?? "The message was not sent."
                if id == selectedID {
                    error = message
                    if !wasRunning { chat?.running = false }
                }
                if ChatDraftRestore.fits(failedChat: id, openChat: selectedID, draft: draft, attachments: attachments.count) {
                    draft = text
                    attachments = files
                } else {
                    failedSends.append(ChatFailedSend(chatID: id, text: text, files: files, reason: message))
                }
            }
            await poll()
            restartPolling()
        }
    }

    /// A message that was not sent, tried again in its own chat.
    func resend(_ failed: ChatFailedSend) {
        guard failed.chatID == selectedID else { return }
        failedSends.removeAll { $0.id == failed.id }
        submit(failed.text, failed.files.filter { FileManager.default.fileExists(atPath: $0.path) }, to: failed.chatID)
    }

    func discard(_ failed: ChatFailedSend) {
        failedSends.removeAll { $0.id == failed.id }
    }

    /// A waiting message: "remove" takes it back, "send_now" stops the answer in progress and sends it.
    func queued(_ entry: ChatQueued, _ action: String) {
        guard let id = selectedID else { return }
        chat?.queue.removeAll { $0.id == entry.id }
        Task {
            let result = await api("dashboard/api/chats/\(id)/queue", ["id": entry.id, "action": action])
            if !result.ok { error = result.object["message"] as? String }
            await poll()
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

    /// The agent's commands matching what follows a leading "/" (until the first space), short ones first.
    var commandSuggestions: [ChatCommand] {
        guard draft.hasPrefix("/"), !draft.contains(" "), !draft.contains("\n"), draft != dismissedCommandDraft else { return [] }
        let typed = draft.dropFirst().lowercased()
        let commands = chat?.commands ?? []
        if commands.contains(where: { $0.name.lowercased() == typed }) { return [] }  // complete: Return sends
        let matches = commands.filter { typed.isEmpty || $0.name.lowercased().hasPrefix(typed) }
            .sorted { ($0.name.count, $0.name) < ($1.name.count, $1.name) }
        return Array(matches.prefix(8))
    }

    func moveCommandSelection(_ step: Int) {
        let count = commandSuggestions.count
        guard count > 0 else { return }
        commandIndex = (commandIndex + step + count) % count
    }

    /// Puts the highlighted (or given) command in the message box, ready for its arguments.
    func acceptCommand(_ command: ChatCommand? = nil) {
        let list = commandSuggestions
        guard let picked = command ?? (list.indices.contains(commandIndex) ? list[commandIndex] : list.first) else { return }
        draft = "/\(picked.name) "
    }

    func rename(_ chatID: String, to title: String) {
        let title = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return }
        Task {
            let result = await api("dashboard/api/chats/\(chatID)/rename", ["title": title])
            if result.ok, chatID == selectedID, let meta = result.object["chat"] as? [String: Any] {
                chat = ChatSummary(meta)
            }
            if !result.ok { error = result.object["message"] as? String }
            await refreshList()
        }
    }

    /// The sidebar's projects with only the chats whose title or project matches the search; pinned chats sit
    /// above them and archived ones below, so neither is here.
    var shownProjects: [ChatProjectGroup] {
        projects.compactMap { project in
            let chats = ChatLibrary.listed(project.chats, search: search)
            return chats.isEmpty ? nil : ChatProjectGroup(id: project.id, name: project.name, chats: chats)
        }
    }

    /// Pin a chat on top of the list, or let it go back to its project.
    func pin(_ chatID: String, _ pinned: Bool) {
        organize(chatID, ["pinned": pinned])
    }

    /// Archive a chat out of the list (Undo brings it back for a few seconds; the Archived section always can).
    /// The open chat makes way for the next one in the list.
    func archive(_ chat: ChatSummary) {
        if selectedID == chat.id { select(ChatLibrary.next(after: chat.id, in: listedChats.map(\.id))) }
        lastArchived = chat
        organize(chat.id, ["archived": true])
        Task {
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            if lastArchived?.id == chat.id { lastArchived = nil }
        }
    }

    func unarchive(_ chatID: String) {
        if lastArchived?.id == chatID { lastArchived = nil }
        organize(chatID, ["archived": false])
    }

    private func organize(_ chatID: String, _ change: [String: Any]) {
        Task {
            let result = await api("dashboard/api/chats/\(chatID)/organize", change)
            if result.ok, chatID == selectedID, let meta = result.object["chat"] as? [String: Any] {
                chat = ChatSummary(meta)
            }
            if !result.ok { error = result.object["message"] as? String ?? "Could not change the chat." }
            await refreshList()
        }
    }

    func delete(_ chatID: String) {
        Task {
            if await api("dashboard/api/chats/\(chatID)/delete", [:]).ok { failedSends.removeAll { $0.chatID == chatID } }
            if selectedID == chatID { select(nil); chat = nil }
            await refreshList()
        }
    }

    private func restartPolling() {
        guard eventsRequest == nil, let id = selectedID else { return }  // the event stream needs no restart
        poller?.cancel()
        poller = Task { [weak self] in await self?.follow(id) }
    }

    /// Moves `link`, publishing only a real change: polls come every 350 ms without a stream.
    private func moveLink(_ change: (inout ChatLink) -> Void) {
        var next = link
        change(&next)
        if next != link { link = next }
    }

    /// The owner pressed Retry on "Offline": follow the open chat again now, instead of after the wait.
    func retryConnection() {
        guard let id = selectedID else { return }
        moveLink { $0.retrying() }
        poller?.cancel()
        poller = Task { [weak self] in await self?.follow(id) }
    }

    /// Keep the open chat current: its event stream when there is one, else polling. Each real outcome (the
    /// stream opened or dropped, a poll answered or not) moves `link`, which the composer shows.
    private func follow(_ id: String) async {
        guard eventsRequest != nil else { return await pollLoop() }
        await poll()  // everything up to now, then only changes
        while !Task.isCancelled && id == selectedID {
            if let request = eventsRequest?("dashboard/api/chats/\(id)/events?after=\(cursor)") {
                do {
                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                    if !Task.isCancelled && id == selectedID { moveLink { $0.opened() } }
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
            guard !Task.isCancelled, id == selectedID else { return }
            moveLink { $0.dropped() }
            try? await Task.sleep(nanoseconds: UInt64(min(link.failures, 5)) * 1_000_000_000)
            guard !Task.isCancelled else { return }
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
        // An error the server sent (it names one) still means Mac MCP answered; only no answer at all counts.
        if id == selectedID {
            let answered = result.ok || result.object["error"] != nil
            moveLink { $0.polled(ok: answered, streaming: eventsRequest != nil) }
        }
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
    private var model: ChatModel?
    private weak var state: AppState?

    /// Opens the chat window on one chat (a notification was clicked).
    func open(chat id: String) {
        guard let state else { return }
        show(state: state)
        model?.select(id)
    }

    func show(state: AppState) {
        self.state = state
        // In the Dock and in ⌘Tab while the chat is open, like any app window; back to the menu bar when it closes.
        NSApplication.shared.setActivationPolicy(.regular)
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
        self.model = model
        let window = NSWindow(contentViewController: NSHostingController(rootView: ChatView(model: model)))
        model.isLooking = { [weak window, weak model] id in
            guard let window, let model else { return false }
            return NSApp.isActive && window.isKeyWindow && window.isVisible && model.selectedID == id
        }
        if Bundle.main.bundleIdentifier != nil {  // macOS notifications need an app bundle
            model.notify = { id, title, body in
                Task { await AgentNotificationController.shared.scheduleChat(id: id, title: title, body: body) }
            }
            AgentNotificationController.shared.chatHandler = { [weak self] id in self?.open(chat: id) }
        }
        model.followList()
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

    func windowWillClose(_ notification: Notification) {
        // Only the menu bar again (LSUIElement); the chat list keeps following, so finished chats still notify.
        DispatchQueue.main.async { NSApplication.shared.setActivationPolicy(.accessory) }
    }
}

// MARK: Conversation

struct PermissionCard: View {
    let item: ChatItem
    let agent: String
    /// ⌘1, ⌘2 … pick its choices in order (only on the question answered first, so two never share a key).
    /// No choice is the default button: a stray Return, with the message box closed, answers nothing.
    var shortcuts = false
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
                ForEach(Array(item.options.enumerated()), id: \.element.id) { index, option in
                    choice(option, number: index + 1)
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

    /// One choice, with its ⌘ number shown when it has one.
    @ViewBuilder private func choice(_ option: ChatPermissionOption, number: Int) -> some View {
        let key: KeyboardShortcut? = shortcuts && number <= 9
            ? KeyboardShortcut(KeyEquivalent(Character("\(number)")), modifiers: .command) : nil
        let label = HStack(spacing: 6) {
            Text(option.label)
            if key != nil { Text("⌘\(number)").foregroundStyle(.secondary) }
        }
        Group {
            if option.kind == "allow_once" {
                Button { answer(option) } label: { label }.buttonStyle(.borderedProminent)
            } else {
                Button { answer(option) } label: { label }.buttonStyle(.bordered)
            }
        }
        .keyboardShortcut(key)
        .help(key == nil ? option.label : "\(option.label) (⌘\(number))")
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
                    // Under the provider or group the agent names, so two models of the same name tell apart.
                    ForEach(sections, id: \.name) { section in
                        Section {
                            ForEach(section.choices) { row($0) }
                        } header: {
                            if !section.name.isEmpty { Text(section.name) }
                        }
                    }
                }
                .listStyle(.plain)
            }
            .frame(width: 380, height: 420)
        }
    }

    private func row(_ choice: ChatConfigChoice) -> some View {
        Button {
            choose(choice.value)
            open = false
        } label: {
            HStack {
                VStack(alignment: .leading, spacing: 1) {
                    Text(choice.name).lineLimit(1)
                    let provider = ChatLibrary.provider(group: choice.group, value: choice.value)
                    let detail = [provider, choice.name != choice.value ? choice.value : ""].filter { !$0.isEmpty }
                    if !detail.isEmpty {
                        Text(detail.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary).lineLimit(1)
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
        .help(choice.description)
    }

    /// The matching models by provider, in the order the agent lists them.
    private var sections: [(name: String, choices: [ChatConfigChoice])] {
        var out: [(name: String, choices: [ChatConfigChoice])] = []
        for choice in matches {
            let name = ChatLibrary.provider(group: choice.group, value: choice.value)
            if let index = out.firstIndex(where: { $0.name == name }) {
                out[index].choices.append(choice)
            } else {
                out.append((name, [choice]))
            }
        }
        return out
    }

    private var matches: [ChatConfigChoice] {
        let words = query.lowercased().split(separator: " ").map(String.init)
        guard !words.isEmpty else { return option.options }
        return option.options.filter { choice in
            let provider = ChatLibrary.provider(group: choice.group, value: choice.value)
            let text = "\(choice.name) \(choice.value) \(provider)".lowercased()
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
                            Text(option.ready ? option.label : "\(option.label) (not ready)").tag(option.id)
                        }
                    }
                    .labelsHidden()
                    if let option = selectedAgent, !option.ready {
                        Label(option.reason.isEmpty ? "\(option.label) is not ready." : option.reason,
                              systemImage: "exclamationmark.triangle.fill")
                            .font(.caption).foregroundStyle(VisualTokens.warning)
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
                // An agent that is not ready cannot start one; the reason shows above.
                .disabled(folder == nil || selectedAgent?.ready != true || starting)
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
        .task {
            // Whether each agent is ready now (the owner may have just installed one).
            await model.refreshList()
            if selectedAgent?.ready != true, let ready = model.agents.first(where: \.ready) { agent = ready.id }
        }
    }

    private var selectedAgent: ChatAgentOption? { model.agents.first { $0.id == agent } }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        DispatchQueue.main.async {
            panel.begin { response in
                if response == .OK { folder = panel.url }
            }
        }
    }
}
