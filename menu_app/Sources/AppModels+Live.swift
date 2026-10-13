import Foundation

/// What the live-activity island (#174) shows, built from data AppState already has.
/// The island view and window live in LiveIsland.swift; this file holds only the plain model
/// so AppState (and its test harnesses) can feed it without the AppKit window code.

enum LiveTone: Equatable {
    case working, done, attention, problem
}

enum LiveTarget: Equatable {
    case dashboard
    case agent(String)
    case settings(String)
}

struct LiveItem: Identifiable, Equatable {
    let id: String
    let symbol: String
    let title: String
    let detail: String
    let tone: LiveTone
    let startedAt: Date?
    let target: LiveTarget?
    var actionTitle: String? = nil
}

struct LivePeek: Identifiable, Equatable {
    let id: String
    let symbol: String
    let title: String
    let detail: String
    let tone: LiveTone
    var actionTitle: String? = nil
    var target: LiveTarget? = nil
}

/// A tool call reported by the activity stream (only calls that carry an intent sentence).
struct LiveToolCall: Equatable {
    let eventID: String
    let tool: String
    let description: String
    let sessionID: String?
    let agentID: String?
    /// The app that made the call (ChatGPT, Claude Code, ...), when the server knows it.
    let client: String?
    /// The browser or app the call works in ("safari", "chrome", "Reminders"), for its icon.
    let target: String?
    let startedAt: Date
}

/// Something that happened, for the island's Recent list.
struct LiveHistoryEntry: Identifiable, Equatable {
    let id: String
    let tool: String
    let title: String
    let client: String?
    let target: String?
    let at: Date
    let ok: Bool
}
