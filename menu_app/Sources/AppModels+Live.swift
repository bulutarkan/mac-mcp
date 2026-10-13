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
    let startedAt: Date
}
