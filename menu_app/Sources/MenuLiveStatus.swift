import Foundation

/// What Mac MCP is doing right now, worked out once for the menu bar icon and the popup (#123),
/// so the icon, the header line and the server card never disagree. Plain values in, so a test
/// can check it without the app.
struct MenuLiveStatus: Equatable {
    /// The server as far as the app has checked.
    enum Server: Equatable {
        /// No answer yet (the app just opened, or a check is under way): neither running nor stopped.
        case checking
        /// Right after login the supervisor is still starting it.
        case starting
        /// It answered the last check, fully or in part.
        case running
        /// A finished check found nothing answering. Only this state offers Start.
        case stopped
    }

    /// The menu bar icon, most important first.
    enum IconLook: Equatable {
        case stopped, needsYou, paused, working, idle
    }

    let server: Server
    let questionsWaiting: Int
    /// Who asked the oldest waiting question ("Claude Code", "An agent").
    let askedBy: String?
    let agentsWorking: Int
    let conversationsActive: Int
    let paused: Bool

    init(serverAnswered: Bool, checking: Bool, startingAfterLogin: Bool,
         questionsWaiting: Int = 0, askedBy: String? = nil,
         agentsWorking: Int = 0, conversationsActive: Int = 0, paused: Bool = false) {
        server = Self.server(answered: serverAnswered, checking: checking, startingAfterLogin: startingAfterLogin)
        self.questionsWaiting = max(0, questionsWaiting)
        let name = askedBy?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        self.askedBy = name.isEmpty ? nil : name
        self.agentsWorking = max(0, agentsWorking)
        self.conversationsActive = max(0, conversationsActive)
        self.paused = paused
    }

    static func server(answered: Bool, checking: Bool, startingAfterLogin: Bool) -> Server {
        if answered { return .running }
        if startingAfterLogin { return .starting }
        return checking ? .checking : .stopped
    }

    /// Delegated agents or an AI app conversation at work.
    var isWorking: Bool { agentsWorking > 0 || conversationsActive > 0 }

    var iconLook: IconLook {
        guard server == .running else { return .stopped }
        if questionsWaiting > 0 { return .needsYou }
        if paused { return .paused }
        if isWorking { return .working }
        return .idle
    }

    /// The line under "Mac MCP" in the popup. It carries news only: the server not up yet or
    /// not running, a question waiting, work under way. Idle says just "Ready".
    var subtitle: String {
        switch server {
        case .checking: return "Connecting…"
        case .starting: return "Starting…"
        case .stopped: return "Not running"
        case .running: break
        }
        if questionsWaiting > 1 { return "\(questionsWaiting) questions are waiting for you" }
        if questionsWaiting == 1 { return "\(askedBy ?? "An agent") is asking you something" }
        var parts: [String] = []
        if agentsWorking > 0 {
            parts.append(agentsWorking == 1 ? "1 agent working" : "\(agentsWorking) agents working")
        }
        if conversationsActive > 0 {
            parts.append(conversationsActive == 1 ? "1 conversation active" : "\(conversationsActive) conversations active")
        }
        return parts.isEmpty ? "Ready" : parts.joined(separator: " · ")
    }
}
