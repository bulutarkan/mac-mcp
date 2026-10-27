import Foundation

// MARK: Keeping Mac MCP Chat's conversations in order: pinned on top, archived out of the way, a quick switch
// between recent ones. Plain logic, so a test can run it without the window.

/// What the sidebar and the quick switcher know of a chat.
protocol ChatListed {
    var id: String { get }
    var title: String { get }
    var projectName: String { get }
    var updatedAt: Double { get }
    /// When it was pinned (0: not pinned) and archived (0: in the list).
    var pinnedAt: Double { get }
    var archivedAt: Double { get }
}

extension ChatListed {
    var pinned: Bool { pinnedAt > 0 }
    var archived: Bool { archivedAt > 0 }
}

enum ChatLibrary {
    /// The search's words, lowercased.
    static func words(_ search: String) -> [String] {
        search.lowercased().split(separator: " ").map(String.init)
    }

    /// Whether a chat's title or project has every word of the search.
    static func matches(_ chat: some ChatListed, _ words: [String]) -> Bool {
        let text = (chat.title + " " + chat.projectName).lowercased()
        return words.allSatisfy { text.contains($0) }
    }

    /// Pinned chats still in the list, in the order they were pinned.
    static func pinned<T: ChatListed>(_ chats: [T], search: String = "") -> [T] {
        let words = words(search)
        return chats.filter { $0.pinned && !$0.archived && matches($0, words) }.sorted { $0.pinnedAt < $1.pinnedAt }
    }

    /// Archived chats, the most recently archived first.
    static func archived<T: ChatListed>(_ chats: [T], search: String = "") -> [T] {
        let words = words(search)
        return chats.filter { $0.archived && matches($0, words) }.sorted { $0.archivedAt > $1.archivedAt }
    }

    /// The chats a project shows: neither pinned (they sit on top) nor archived.
    static func listed<T: ChatListed>(_ chats: [T], search: String = "") -> [T] {
        let words = words(search)
        return chats.filter { !$0.pinned && !$0.archived && matches($0, words) }
    }

    /// The quick switcher: the most recently used chats, the open one left out. Archived chats come only when the
    /// search names them, after the others.
    static func recent<T: ChatListed>(_ chats: [T], search: String, current: String?, limit: Int = 12) -> [T] {
        let words = words(search)
        let found = chats.filter { $0.id != current && matches($0, words) && (!$0.archived || !words.isEmpty) }
        let sorted = found.sorted { lhs, rhs in
            lhs.archived != rhs.archived ? !lhs.archived : lhs.updatedAt > rhs.updatedAt
        }
        return Array(sorted.prefix(limit))
    }

    /// The highlighted row after ↑ (-1) or ↓ (+1), going round at the ends.
    static func move(_ index: Int, by step: Int, count: Int) -> Int {
        count > 0 ? (index + step % count + count) % count : 0
    }

    /// The chat to open after the open one is archived: the next one in the list, else the one before, else none.
    static func next(after id: String, in order: [String]) -> String? {
        guard let index = order.firstIndex(of: id) else { return order.first }
        if index + 1 < order.count { return order[index + 1] }
        return index > 0 ? order[index - 1] : nil
    }

    /// A diff's file on disk: an absolute path as it is, a relative one inside the chat's project.
    static func filePath(_ path: String, project: String) -> String {
        if path.hasPrefix("/") || project.isEmpty { return path }
        if path.hasPrefix("~/") { return (path as NSString).expandingTildeInPath }
        return (project as NSString).appendingPathComponent(path)
    }

    /// Where a model comes from, for the picker: the group the agent put it in, else the provider its id names
    /// ("anthropic/claude-…"), else nothing.
    static func provider(group: String, value: String) -> String {
        if !group.isEmpty { return group }
        guard let slash = value.firstIndex(of: "/"), slash != value.startIndex else { return "" }
        return String(value[..<slash])
    }

    /// What Return and Option-Return do in the message box.
    static func sendHint(running: Bool) -> String {
        running ? "↩ send after this answer · ⌥↩ new line" : "↩ send · ⌥↩ new line"
    }
}
