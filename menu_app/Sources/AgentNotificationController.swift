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

    /// How it ended, said in the title (#124): "Agent timed out", not just "needs attention".
    enum Category: String {
        case done
        case failed
        case timedOut
        case stuck
        case doneWithFailures
        case checkFailed
        case outOfBudget
    }

    let targetKind: TargetKind
    let targetID: String
    let outcome: Outcome
    let label: String
    var category: Category = .done

    var title: String {
        let who = targetKind == .team ? "Agent team" : "Agent"
        switch category {
        case .done: return "\(who) finished"
        case .failed: return "\(who) failed"
        case .timedOut: return "\(who) timed out"
        case .stuck: return "\(who) is stuck"
        case .doneWithFailures: return "\(who) finished with failures"
        case .checkFailed: return "\(who) finished, but its check failed"
        case .outOfBudget: return "\(who) ran out of budget"
        }
    }

    var requestIdentifier: String {
        "mac-mcp.\(targetKind.rawValue).\(targetID).\(outcome.rawValue)"
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
                label: AgentNotificationSanitizer.label(team.title, fallback: fallback),
                category: Self.teamCategory(team, outcome: outcome)
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
                label: AgentNotificationSanitizer.label(agent.title, fallback: fallback),
                category: Self.agentCategory(agent.status)
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

    static func agentCategory(_ status: String?) -> AgentTerminalNotification.Category {
        switch status?.lowercased() {
        case "timeout": return .timedOut
        case "stalled": return .stuck
        case "failed": return .failed
        default: return .done
        }
    }

    static func teamCategory(
        _ team: AgentNotificationTeamState,
        outcome: AgentTerminalNotification.Outcome
    ) -> AgentTerminalNotification.Category {
        guard outcome == .needsAttention else { return .done }
        switch team.status?.lowercased() {
        case "quality_failed": return .checkFailed
        case "budget_exhausted": return .outOfBudget
        default: return .doneWithFailures
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
    private var updateHandler: (() -> Void)?
    private var fleetHandler: (() -> Void)?
    /// True when the owner is already looking at what the notification is about (the menu, or the
    /// dashboard in front): then it goes quietly to Notification Center, without a banner (#124).
    var isTargetVisible: (@MainActor (AgentTerminalNotification) -> Bool)?
    static let updateReadyIdentifier = "mac-mcp.update-ready"
    static let fleetRequestPrefix = "mac-mcp.fleet-request."

    private override init() {
        super.init()
    }

    func configure(
        openHandler: @escaping (AgentTerminalNotification.TargetKind, String) -> Void,
        updateHandler: @escaping () -> Void,
        fleetHandler: @escaping () -> Void = {}
    ) {
        self.openHandler = openHandler
        self.updateHandler = updateHandler
        self.fleetHandler = fleetHandler
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
        content.title = event.title
        let check = isTargetVisible
        let visible = await MainActor.run { check?(event) ?? false }
        if visible {
            // Already in view: listed in Notification Center, no banner and no sound.
            content.interruptionLevel = .passive
        } else if event.outcome == .needsAttention {
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

    /// Posts a one-off message (for example a recipe result); false when not allowed.
    func scheduleMessage(title: String, body: String) async -> Bool {
        guard await authorizationState() == .authorized else { return false }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let request = UNNotificationRequest(
            identifier: "mac-mcp.message.\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        do {
            try await center.add(request)
            return true
        } catch {
            return false
        }
    }

    /// One calm alert, the same for every way Mac MCP was installed; a newer one replaces it.
    func scheduleUpdateReady() async -> Bool {
        guard await authorizationState() == .authorized else { return false }
        let content = UNMutableNotificationContent()
        content.title = "A new update is ready"
        content.body = "Mac MCP has new improvements waiting. Open it to update."
        content.userInfo = ["kind": "update"]
        let request = UNNotificationRequest(identifier: Self.updateReadyIdentifier, content: content, trigger: nil)
        do {
            try await center.add(request)
            return true
        } catch {
            return false
        }
    }

    /// How an update ended (#78), said once by the relaunched app.
    func scheduleUpdateResult(id: String, title: String, body: String) async {
        guard await authorizationState() == .authorized else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.userInfo = ["kind": "update"]
        removeUpdateReady()
        try? await center.add(UNNotificationRequest(identifier: "mac-mcp.update.result." + id, content: content, trigger: nil))
    }

    /// Another Mac asks to connect (#147): who, what it asks for and the check both Macs show.
    func scheduleFleetRequest(id: String, name: String, permission: String, checkCode: String) async {
        guard await authorizationState() == .authorized else { return }
        let content = UNMutableNotificationContent()
        content.title = "\(name) wants to connect"
        let asks: String
        switch permission {
        case "full": asks = "It asks for full access to this Mac."
        case "read_only": asks = "It asks for read-only access to this Mac."
        default: asks = "It asks to see this Mac’s status."
        }
        content.body = checkCode.isEmpty ? asks + " Click to allow or refuse."
            : asks + " Check that it shows \(checkCode), then allow or refuse."
        content.sound = .default
        content.threadIdentifier = "mac-mcp.fleet"
        content.userInfo = ["kind": "fleet_request"]
        let request = UNNotificationRequest(identifier: Self.fleetRequestPrefix + id, content: content, trigger: nil)
        try? await center.add(request)
    }

    /// Takes back the notifications of requests that were answered or expired.
    func removeFleetRequests(keeping pending: Set<String>) {
        let prefix = Self.fleetRequestPrefix
        center.getDeliveredNotifications { [center] delivered in
            let stale = delivered.map(\.request.identifier).filter {
                $0.hasPrefix(prefix) && !pending.contains(String($0.dropFirst(prefix.count)))
            }
            if !stale.isEmpty { center.removeDeliveredNotifications(withIdentifiers: stale) }
        }
    }

    /// Opens a chat when its notification is clicked (set by the chat window).
    var chatHandler: ((String) -> Void)?

    /// A chat finished, or its agent asks before it continues, while the owner was elsewhere.
    func scheduleChat(id: String, title: String, body: String) async {
        guard await authorizationState() == .authorized else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.threadIdentifier = "mac-mcp.chat." + id
        content.userInfo = ["kind": "chat", "chat_id": id]
        try? await center.add(UNNotificationRequest(identifier: "mac-mcp.chat.\(id).\(Date().timeIntervalSince1970)",
                                                    content: content, trigger: nil))
    }

    func removeUpdateReady() {
        center.removeDeliveredNotifications(withIdentifiers: [Self.updateReadyIdentifier])
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
        let quiet = notification.request.content.interruptionLevel == .passive
        completionHandler(quiet ? [.list] : [.banner])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        defer { completionHandler() }
        if response.notification.request.content.userInfo["kind"] as? String == "update" {
            DispatchQueue.main.async { [weak self] in self?.updateHandler?() }
            return
        }
        if response.notification.request.content.userInfo["kind"] as? String == "chat",
           let id = response.notification.request.content.userInfo["chat_id"] as? String {
            DispatchQueue.main.async { [weak self] in self?.chatHandler?(id) }
            return
        }
        if response.notification.request.content.userInfo["kind"] as? String == "fleet_request" {
            DispatchQueue.main.async { [weak self] in self?.fleetHandler?() }
            return
        }
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
