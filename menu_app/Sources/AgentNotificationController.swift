import Foundation
import UserNotifications

enum AgentNotificationAuthorizationState: String, Equatable {
    case notDetermined
    case authorized
    case denied

    var displayName: String {
        switch self {
        case .notDetermined: return "Not requested"
        case .authorized: return "Allowed"
        case .denied: return "Denied"
        }
    }
}

struct AgentTerminalNotification: Equatable {
    enum TargetKind: String {
        case agent
        case team
    }

    enum Outcome: String {
        case completed
        case needsAttention
    }

    let targetKind: TargetKind
    let targetID: String
    let outcome: Outcome
    let label: String

    var requestIdentifier: String {
        "mac-mcp.(targetKind.rawValue).(targetID).(outcome.rawValue)"
    }
}

struct AgentNotificationAgentState: Equatable {
    let id: String
    let teamID: String?
    let status: String?
    let title: String?
}

struct AgentNotificationTeamState: Equatable {
    let id: String
    let status: String?
    let title: String?
    let success: Bool?
    let partialFailure: Bool?
    let outcome: String?
}

final class AgentNotificationTransitionTracker {
    private var baselineReady = false
    private var previousAgentStatuses: [String: String] = [:]
    private var previousTeamStatuses: [String: String] = [:]

    func reset(
        agents: [AgentNotificationAgentState],
        teams: [AgentNotificationTeamState]
    ) {
        previousAgentStatuses = Self.agentStatusMap(agents)
        previousTeamStatuses = Self.teamStatusMap(teams)
        baselineReady = true
    }

    func events(
        agents: [AgentNotificationAgentState],
        teams: [AgentNotificationTeamState],
        enabled: Bool
    ) -> [AgentTerminalNotification] {
        let currentAgentStatuses = Self.agentStatusMap(agents)
        let currentTeamStatuses = Self.teamStatusMap(teams)
        defer {
            previousAgentStatuses = currentAgentStatuses
            previousTeamStatuses = currentTeamStatuses
            baselineReady = true
        }

        guard baselineReady, enabled else { return [] }
        var output: [AgentTerminalNotification] = []

        for team in teams {
            guard previousTeamStatuses[team.id]?.lowercased() == "running",
                  let outcome = Self.teamOutcome(team) else { continue }
            let fallback = "Team • " + String(team.id.suffix(6))
            output.append(AgentTerminalNotification(
                targetKind: .team,
                targetID: team.id,
                outcome: outcome,
                label: AgentNotificationSanitizer.label(team.title, fallback: fallback)
            ))
        }

        for agent in agents {
            if let teamID = agent.teamID, !teamID.isEmpty { continue }
            let previous = previousAgentStatuses[agent.id]?.lowercased()
            guard previous == "starting" || previous == "running",
                  let outcome = Self.agentOutcome(agent.status) else { continue }
            let fallback = "Agent • " + String(agent.id.suffix(6))
            output.append(AgentTerminalNotification(
                targetKind: .agent,
                targetID: agent.id,
                outcome: outcome,
                label: AgentNotificationSanitizer.label(agent.title, fallback: fallback)
            ))
        }
        return output
    }

    private static func agentStatusMap(
        _ agents: [AgentNotificationAgentState]
    ) -> [String: String] {
        Dictionary<String, String>(
            uniqueKeysWithValues: agents.compactMap { agent -> (String, String)? in
                guard let status = agent.status else { return nil }
                return (agent.id, status)
            }
        )
    }

    private static func teamStatusMap(
        _ teams: [AgentNotificationTeamState]
    ) -> [String: String] {
        Dictionary<String, String>(
            uniqueKeysWithValues: teams.compactMap { team -> (String, String)? in
                guard let status = team.status else { return nil }
                return (team.id, status)
            }
        )
    }

    private static func agentOutcome(
        _ status: String?
    ) -> AgentTerminalNotification.Outcome? {
        switch status?.lowercased() {
        case "completed": return .completed
        case "failed", "timeout", "stalled": return .needsAttention
        default: return nil
        }
    }

    private static func teamOutcome(
        _ team: AgentNotificationTeamState
    ) -> AgentTerminalNotification.Outcome? {
        let status = team.status?.lowercased()
        if status == "cancelled" || status == "running" || status == nil { return nil }
        if status == "completed",
           team.success != false,
           team.partialFailure != true,
           team.outcome?.lowercased() != "partial_failure" {
            return .completed
        }
        if ["completed", "completed_with_failures", "quality_failed", "budget_exhausted"].contains(status ?? "") {
            return .needsAttention
        }
        return nil
    }
}

enum AgentNotificationSanitizer {
    static func label(_ raw: String?, fallback: String) -> String {
        guard var value = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
            return fallback
        }
        value = value.replacingOccurrences(of: "[\r\n\t]+", with: " ", options: .regularExpression)
        value = value.replacingOccurrences(
            of: "(?i)https?://\\S+|file://\\S+",
            with: "[link]",
            options: .regularExpression
        )
        value = value.replacingOccurrences(
            of: "(?i)(?:^|\\s)(?:~?/|/(?:Users|home|private|tmp|Volumes)/)\\S+",
            with: " [path]",
            options: .regularExpression
        )
        value = value.replacingOccurrences(
            of: "(?i)\\bBearer\\s+[A-Za-z0-9._~+/=-]{8,}",
            with: "Bearer [redacted]",
            options: .regularExpression
        )
        value = value.replacingOccurrences(
            of: "(?i)\\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\\s*[=:]\\s*\\S+",
            with: "[redacted]",
            options: .regularExpression
        )
        value = value.replacingOccurrences(
            of: "\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b",
            with: "[email]",
            options: [.regularExpression, .caseInsensitive]
        )
        value = value.replacingOccurrences(of: "\\s{2,}", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return fallback }
        if value.count > 72 {
            let end = value.index(value.startIndex, offsetBy: 69)
            value = String(value[..<end]).trimmingCharacters(in: .whitespacesAndNewlines) + "…"
        }
        return value
    }
}

final class AgentNotificationController: NSObject, UNUserNotificationCenterDelegate {
    static let shared = AgentNotificationController()

    private let center = UNUserNotificationCenter.current()
    private var openHandler: ((AgentTerminalNotification.TargetKind, String) -> Void)?

    private override init() {
        super.init()
    }

    func configure(openHandler: @escaping (AgentTerminalNotification.TargetKind, String) -> Void) {
        self.openHandler = openHandler
        center.delegate = self
    }

    func authorizationState() async -> AgentNotificationAuthorizationState {
        await withCheckedContinuation { continuation in
            center.getNotificationSettings { settings in
                switch settings.authorizationStatus {
                case .authorized, .provisional:
                    continuation.resume(returning: .authorized)
                case .notDetermined:
                    continuation.resume(returning: .notDetermined)
                default:
                    continuation.resume(returning: .denied)
                }
            }
        }
    }

    func requestAuthorization() async -> Bool {
        let current = await authorizationState()
        if current == .authorized { return true }
        if current == .denied { return false }
        return await withCheckedContinuation { continuation in
            center.requestAuthorization(options: [.alert, .badge, .sound]) { granted, _ in
                continuation.resume(returning: granted)
            }
        }
    }

    func schedule(_ event: AgentTerminalNotification) async {
        guard await authorizationState() == .authorized else { return }

        let content = UNMutableNotificationContent()
        switch (event.targetKind, event.outcome) {
        case (.agent, .completed):
            content.title = "Agent completed"
        case (.agent, .needsAttention):
            content.title = "Agent needs attention"
            content.sound = .default
        case (.team, .completed):
            content.title = "Agent team completed"
        case (.team, .needsAttention):
            content.title = "Agent team needs attention"
            content.sound = .default
        }
        content.body = event.label
        content.userInfo = [
            "target_kind": event.targetKind.rawValue,
            "target_id": event.targetID,
        ]
        let request = UNNotificationRequest(
            identifier: event.requestIdentifier,
            content: content,
            trigger: nil
        )
        try? await center.add(request)
    }

    func removeDeliveredNotifications() {
        center.removeAllDeliveredNotifications()
        center.removeAllPendingNotificationRequests()
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        defer { completionHandler() }
        guard
            let rawKind = response.notification.request.content.userInfo["target_kind"] as? String,
            let kind = AgentTerminalNotification.TargetKind(rawValue: rawKind),
            let targetID = response.notification.request.content.userInfo["target_id"] as? String,
            !targetID.isEmpty
        else { return }
        DispatchQueue.main.async { [weak self] in
            self?.openHandler?(kind, targetID)
        }
    }
}
