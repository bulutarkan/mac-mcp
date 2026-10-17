import Foundation

/// Notices a scheduled run that never started (#225), from the app: a process apart from the server's
/// scheduler loop, so a loop that stopped waking, or a server that stopped answering, is still seen.
/// It only says so, once per run; it never starts the run itself.
struct ScheduledTaskWatchdog {
    struct Due: Equatable {
        let id: String
        let name: String
        let at: Date
        let notify: Bool
    }

    struct Alert: Equatable {
        enum Kind: Equatable { case notStarted, notAnswering }
        let kind: Kind
        let task: Due
    }

    /// Runs already announced, as "task id|expected time".
    private(set) var announced: [String] = []
    /// What was due in the next hours at the last answer, for when Mac MCP stops answering.
    private(set) var dueSoon: [Due] = []
    private(set) var lastAnswerAt: Date?
    private(set) var answeringSince: Date?
    var lastWakeAt: Date?
    static let checkEvery: TimeInterval = 60
    static let awayAfter: TimeInterval = 150

    private func key(_ due: Due) -> String { "\(due.id)|\(Int(due.at.timeIntervalSince1970))" }

    private mutating func remember(_ due: Due) -> Bool {
        let key = key(due)
        guard !announced.contains(key) else { return false }
        announced.append(key)
        if announced.count > 200 { announced.removeFirst(announced.count - 200) }
        return true
    }

    /// Mac MCP answered with the runs that are past their time with no start and no skip recorded. Right
    /// after waking or Mac MCP answering again the scheduler gets the tolerance to record what it missed
    /// (as skipped); a run still listed after that never started.
    mutating func answered(now: Date, overdue: [Due], dueSoon: [Due], tolerance: TimeInterval) -> [Alert] {
        // The app asks once a minute: a longer silence means Mac MCP was away and has just come back.
        if let last = lastAnswerAt, now.timeIntervalSince(last) > Self.awayAfter { answeringSince = now }
        lastAnswerAt = now
        self.dueSoon = dueSoon
        guard now.timeIntervalSince(quietSince) >= tolerance else { return [] }
        var alerts: [Alert] = []
        for due in overdue where remember(due) {
            alerts.append(Alert(kind: .notStarted, task: due))
        }
        return alerts
    }

    /// Mac MCP did not answer: a run that was due since the last answer is said to be at risk, unless
    /// the owner stopped Mac MCP on purpose.
    mutating func unanswered(now: Date, intendedRunning: Bool, tolerance: TimeInterval) -> [Alert] {
        guard intendedRunning, let lastAnswerAt else { return [] }
        var alerts: [Alert] = []
        for due in dueSoon where due.at > lastAnswerAt && due.at > (lastWakeAt ?? .distantPast)
            && now.timeIntervalSince(due.at) >= tolerance && remember(due) {
            alerts.append(Alert(kind: .notAnswering, task: due))
        }
        return alerts
    }

    /// Nothing is judged until this long after waking or Mac MCP answering again.
    private var quietSince: Date {
        [lastWakeAt, answeringSince].compactMap { $0 }.max() ?? .distantPast
    }
}
