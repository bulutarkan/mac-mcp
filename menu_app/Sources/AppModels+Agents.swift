import AppKit
import Combine
import CryptoKit
import Darwin
import Foundation
import SafariServices

// Delegated agent, provider and mobile companion API models (split out of AppState.swift).

struct AgentResourceActivity: Decodable, Equatable {
    let kind: String?
    let mode: String?
    let label: String?
}

struct AgentInfo: Decodable, Identifiable, Equatable {
    /// Failed runs remain noteworthy for a short handoff window, not indefinitely.
    static let recentFailureWindow: TimeInterval = 4 * 60 * 60

    let agentID: String
    let teamID: String?
    let teamTaskID: String?
    let status: String?
    let phase: String?
    let title: String?
    let provider: String?
    let model: String?
    let reasoning: String?
    let accessMode: String?
    let capabilityProfile: String?
    let lastTool: String?
    let lastToolDurationMS: Int?
    let toolCallCount: Int?
    let resourceActivity: [AgentResourceActivity]?
    let retryCount: Int?
    let durationMS: Int?
    let turnCount: Int?
    let turnElapsedMS: Int?
    let turnBudgetS: Int?
    let checkpointCount: Int?
    let checkpointPending: Bool?
    let throttleCount: Int?
    let lastThrottledAt: Double?
    let lastThrottleReason: String?
    let cooldownUntil: Double?
    let endedAt: Double?
    var id: String { agentID }
    var isActive: Bool { status == "starting" || status == "running" }

    /// Matches the menu's terminal status precedence without treating historical
    /// failures as a permanent alert.
    var isFailure: Bool {
        let terminalStatus = status?.lowercased()
        let phaseKey: String
        if let terminalStatus,
           ["completed", "failed", "cancelled", "timeout", "stalled"].contains(terminalStatus) {
            phaseKey = terminalStatus
        } else {
            phaseKey = (phase ?? status ?? "").lowercased()
        }
        return ["failed", "timeout", "stalled"].contains(phaseKey)
    }

    func isRecentFailure(at now: Double = Date().timeIntervalSince1970) -> Bool {
        guard isFailure, let endedAt else { return false }
        let age = now - endedAt
        return age >= 0 && age < Self.recentFailureWindow
    }
    enum CodingKeys: String, CodingKey {
        case agentID = "agent_id"
        case teamID = "team_id"
        case teamTaskID = "team_task_id"
        case status, phase, title, provider, model, reasoning
        case accessMode = "access_mode"
        case capabilityProfile = "capability_profile"
        case lastTool = "last_tool"
        case lastToolDurationMS = "last_tool_duration_ms"
        case toolCallCount = "tool_call_count"
        case resourceActivity = "resource_activity"
        case retryCount = "retry_count"
        case durationMS = "duration_ms"
        case turnCount = "turn_count"
        case turnElapsedMS = "turn_elapsed_ms"
        case turnBudgetS = "turn_budget_s"
        case checkpointCount = "checkpoint_count"
        case checkpointPending = "checkpoint_pending"
        case throttleCount = "throttle_count"
        case lastThrottledAt = "last_throttled_at"
        case lastThrottleReason = "last_throttle_reason"
        case cooldownUntil = "cooldown_until"
        case endedAt = "ended_at"
    }
}

struct AgentTeamInfo: Decodable, Identifiable, Equatable {
    let teamID: String
    let status: String?
    let success: Bool?
    let outcome: String?
    let partialFailure: Bool?
    let successfulCount: Int?
    let failureCount: Int?
    let pendingCount: Int?
    let workCount: Int?
    let title: String?
    /// The saved team (Settings › Agents) it runs as; nil for a team made up on the spot.
    let teamPreset: Preset?
    let provider: String?
    let model: String?
    let count: Int?
    let terminalCount: Int?
    let unresolvedConflictCount: Int?
    let tasks: [TeamTaskInfo]?
    let budget: Budget?
    /// Plan quota of each provider the team's running agents spend; empty once it finishes.
    let quota: [ProviderQuota]?

    struct Preset: Decodable, Equatable {
        let id: String?
        let name: String?
    }

    /// Tokens the team's agents used; incomplete when a provider reported none (a lower bound then).
    struct Budget: Decodable, Equatable {
        let totalTokensUsed: Int?
        let totalTokensComplete: Bool?

        enum CodingKeys: String, CodingKey {
            case totalTokensUsed = "total_tokens_used"
            case totalTokensComplete = "total_tokens_complete"
        }

        init(from decoder: Decoder) throws {
            // A field the app cannot read drops out rather than failing the whole agents poll.
            let values = try decoder.container(keyedBy: CodingKeys.self)
            totalTokensUsed = (try? values.decodeIfPresent(Int.self, forKey: .totalTokensUsed)) ?? nil
            totalTokensComplete = (try? values.decodeIfPresent(Bool.self, forKey: .totalTokensComplete)) ?? nil
        }
    }

    struct TeamTaskInfo: Decodable, Equatable {
        let id: String?
        let state: String?
        let queuedReason: String?
        /// The member's name for a preset team, else the task's title.
        let title: String?
        let role: String?
        let dependsOn: [String]?
        let gateResult: String?
        /// The agent running it, else the one that last ran it.
        let agentID: String?
        let provider: String?
        let model: String?
        let reasoning: String?

        enum CodingKeys: String, CodingKey {
            case id, state, title, role, provider, model, reasoning
            case queuedReason = "queued_reason"
            case dependsOn = "depends_on"
            case gateResult = "gate_result"
            case agentID = "agent_id"
        }
    }

    var id: String { teamID }
    var isActive: Bool { (pendingCount ?? 0) > 0 }
    /// "2/4 done · 1 waiting · 1 conflict" for the menu.
    var progressText: String {
        let all = tasks ?? []
        let done = all.filter { ["completed", "skipped"].contains($0.state ?? "") }.count
        let waiting = all.filter { $0.state == "queued" || $0.queuedReason != nil }.count
        var parts = ["\(done)/\(all.isEmpty ? (workCount ?? 0) : all.count) done"]
        if waiting > 0 { parts.append("\(waiting) waiting") }
        if let conflicts = unresolvedConflictCount, conflicts > 0 {
            parts.append(conflicts == 1 ? "1 conflict" : "\(conflicts) conflicts")
        }
        return parts.joined(separator: " · ")
    }

    enum CodingKeys: String, CodingKey {
        case teamID = "team_id"
        case status, success, outcome, title, provider, model, count, tasks, quota, budget
        case teamPreset = "team_preset"
        case unresolvedConflictCount = "unresolved_conflict_count"
        case partialFailure = "partial_failure"
        case successfulCount = "successful_count"
        case failureCount = "failure_count"
        case pendingCount = "pending_count"
        case workCount = "work_count"
        case terminalCount = "terminal_count"
    }
}

// MARK: Teams in the menu: one card while it runs, one row once it finished.

extension AgentTeamInfo {
    private static func text(_ value: String?) -> String? {
        let trimmed = (value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// The saved team's name when it runs as one, else its task title.
    var displayName: String { Self.text(teamPreset?.name) ?? Self.text(title) ?? "Team" }

    /// The task title beside a saved team's name; nil when the name already is the title.
    var taskTitle: String? {
        guard Self.text(teamPreset?.name) != nil, let title = Self.text(title), title != displayName else { return nil }
        return title
    }

    var doneCount: Int {
        let all = tasks ?? []
        return all.isEmpty ? (successfulCount ?? 0) : all.filter { ["completed", "skipped"].contains($0.state ?? "") }.count
    }

    var memberCount: Int {
        let all = tasks ?? []
        return all.isEmpty ? (workCount ?? count ?? 0) : all.count
    }

    /// "4/4 · review passed" or "3/4 · 1 failed" once the team finished.
    var resultText: String {
        var parts = ["\(doneCount)/\(memberCount)"]
        let failed = failureCount ?? 0
        let gates = (tasks ?? []).compactMap { $0.gateResult?.lowercased() }
        if failed > 0 {
            parts.append("\(failed) failed")
        } else if !gates.isEmpty, gates.allSatisfy({ $0 == "pass" }) {
            parts.append(gates.count == 1 ? "review passed" : "reviews passed")
        }
        return parts.joined(separator: " · ")
    }

    /// The tokens the team used, only when every member's provider reported its own.
    var totalTokens: Int? {
        guard let used = budget?.totalTokensUsed, used > 0, budget?.totalTokensComplete != false else { return nil }
        return used
    }

    /// First start to last end of the members the menu knows, while none is running.
    func durationMS(agents: [AgentInfo]) -> Int? {
        let own = agents.filter { $0.teamID == teamID }
        guard !own.isEmpty, !own.contains(where: \.isActive) else { return nil }
        let spans = own.compactMap { agent -> (Double, Double)? in
            guard let end = agent.endedAt, let duration = agent.durationMS else { return nil }
            return (end - Double(duration) / 1000, end)
        }
        guard let start = spans.map(\.0).min(), let end = spans.map(\.1).max() else { return nil }
        return Int(((end - start) * 1000).rounded())
    }

    /// Each member in the team's plan order, joined to its agent once one runs it. A team without a
    /// task graph lists its agents.
    func members(agents: [AgentInfo]) -> [AgentTeamMember] {
        let own = agents.filter { $0.teamID == teamID }
        let all = tasks ?? []
        guard !all.isEmpty else {
            return own.map { agent in
                AgentTeamMember(id: agent.agentID, name: Self.text(agent.title) ?? "Agent", role: nil, agent: agent,
                                provider: agent.provider, model: agent.model, reasoning: agent.reasoning,
                                stage: agent.isActive ? .running : agent.isFailure || agent.status == "cancelled" ? .failed : .done,
                                waitingFor: nil)
            }
        }
        let names = Dictionary(all.compactMap { task in task.id.map { ($0, Self.text(task.title) ?? $0) } },
                               uniquingKeysWith: { first, _ in first })
        let states = Dictionary(all.compactMap { task in task.id.map { ($0, task.state ?? "") } },
                                uniquingKeysWith: { first, _ in first })
        return all.enumerated().map { index, task in
            let agent = own.first { $0.agentID == task.agentID && task.agentID != nil }
                ?? own.first { $0.teamTaskID != nil && $0.teamTaskID == task.id }
            let state = (task.state ?? "").lowercased()
            let stage: AgentTeamMember.Stage
            switch state {
            case "completed", "skipped": stage = .done
            case "failed", "quality_failed", "cancelled": stage = .failed
            case "running", "spawning", "stopping": stage = .running
            default: stage = agent?.isActive == true ? .running : .waiting
            }
            var waitingFor: String?
            if stage == .waiting {
                let open = (task.dependsOn ?? []).filter { !["completed", "skipped"].contains(states[$0] ?? "") }
                if let first = open.first {
                    let name = names[first] ?? first
                    waitingFor = open.count == 1 ? "Waiting for \(name)" : "Waiting for \(name) +\(open.count - 1)"
                } else {
                    waitingFor = state == "queued" || task.queuedReason != nil ? "Queued" : "Waiting"
                }
            }
            return AgentTeamMember(id: task.id ?? "task-\(index)", name: Self.text(task.title) ?? Self.text(agent?.title) ?? task.id ?? "Member",
                                   role: Self.text(task.role), agent: agent,
                                   provider: agent?.provider ?? task.provider, model: agent?.model ?? task.model,
                                   reasoning: agent?.reasoning ?? task.reasoning, stage: stage, waitingFor: waitingFor)
        }
    }
}

/// One member of a team as the menu shows it: its agent once one runs, else the task it waits to run.
struct AgentTeamMember: Identifiable, Equatable {
    enum Stage: Equatable { case done, failed, running, waiting }
    let id: String
    let name: String
    let role: String?
    let agent: AgentInfo?
    let provider: String?
    let model: String?
    let reasoning: String?
    let stage: Stage
    /// "Waiting for Atlas", "Queued": only for a member that has not started.
    let waitingFor: String?
}

/// One line of the menu's agent list: a team shows once, where its newest member would, never as
/// separate agent rows.
enum MenuAgentEntry: Identifiable, Equatable {
    case team(AgentTeamInfo)
    case agent(AgentInfo)

    var id: String {
        switch self {
        case .team(let team): return "team-\(team.teamID)"
        case .agent(let agent): return agent.agentID
        }
    }

    /// Running agents first, then the rest in the server's (newest first) order; a team's agents fold
    /// into the team. Agents of a team the menu has no summary for stay separate rows.
    static func list(agents: [AgentInfo], teams: [AgentTeamInfo]) -> [MenuAgentEntry] {
        let byID = Dictionary(teams.map { ($0.teamID, $0) }, uniquingKeysWith: { first, _ in first })
        var shown = Set<String>()
        var entries: [MenuAgentEntry] = []
        for agent in agents.filter(\.isActive) + agents.filter({ !$0.isActive }) {
            guard let teamID = agent.teamID, let team = byID[teamID] else {
                entries.append(.agent(agent))
                continue
            }
            if shown.insert(teamID).inserted { entries.append(.team(team)) }
        }
        return entries
    }
}

/// One subscription's 5-hour and weekly limits, as its CLI reports them (provider_quota.py).
struct ProviderQuota: Decodable, Identifiable, Equatable {
    struct Window: Decodable, Equatable {
        let kind: String
        let usedPercent: Double
        let resetsAt: Double?

        var longLabel: String { kind == "1w" ? "Weekly" : "5-hour" }

        enum CodingKeys: String, CodingKey {
            case kind
            case usedPercent = "used_percent"
            case resetsAt = "resets_at"
        }
    }

    let source: String
    let label: String?
    let plan: String?
    let windows: [Window]
    let error: String?
    var id: String { source }

    enum CodingKeys: String, CodingKey { case source, label, plan, windows, error }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        source = try values.decode(String.self, forKey: .source)
        label = try values.decodeIfPresent(String.self, forKey: .label)
        plan = try values.decodeIfPresent(String.self, forKey: .plan)
        // A window the app cannot read drops out rather than failing the whole agents poll.
        windows = ((try? values.decodeIfPresent([Window].self, forKey: .windows)) ?? nil) ?? []
        error = try values.decodeIfPresent(String.self, forKey: .error)
    }

    /// The agent provider whose symbol and tint the badge borrows (opencode-go → opencode).
    var providerID: String { source.hasPrefix("opencode") ? "opencode" : source }
    var highestUse: Double { windows.map(\.usedPercent).max() ?? 0 }
}

struct AgentsEnvelope: Decodable {
    let agents: [AgentInfo]
    let teams: [AgentTeamInfo]?
}

struct ProviderInfo: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let enabled: Bool
    let detected: Bool
    let binaryPath: String?
    let version: String?

    enum CodingKeys: String, CodingKey {
        case id, name, enabled, detected, version
        case binaryPath = "binary_path"
    }
}

struct ProvidersEnvelope: Decodable { let providers: [ProviderInfo] }

struct AgentModelInfo: Decodable, Identifiable, Equatable {
    let id: String
    let displayName: String
    let reasoningValues: [String]
    let defaultReasoning: String?

    enum CodingKeys: String, CodingKey {
        case id
        case displayName = "display_name"
        case reasoningValues = "reasoning_values"
        case defaultReasoning = "default_reasoning"
    }
}

struct ProviderCatalogInfo: Decodable, Equatable {
    let available: Bool
    let modelItems: [AgentModelInfo]
    let defaultModel: String?
    let defaultReasoning: String?
    let catalogSource: String?
    let catalogFreshness: String?
    let catalogError: String?

    enum CodingKeys: String, CodingKey {
        case available
        case modelItems = "model_items"
        case defaultModel = "default_model"
        case defaultReasoning = "default_reasoning"
        case catalogSource = "catalog_source"
        case catalogFreshness = "catalog_freshness"
        case catalogError = "catalog_error"
    }
}

struct AgentCatalogEnvelope: Decodable {
    let providers: [String: ProviderCatalogInfo]
}

struct MobileDeviceInfo: Decodable, Identifiable, Equatable {
    let deviceID: String
    let deviceName: String
    let createdAt: Double
    let expiresAt: Double
    let lastSeenAt: Double
    var id: String { deviceID }

    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case deviceName = "device_name"
        case createdAt = "created_at"
        case expiresAt = "expires_at"
        case lastSeenAt = "last_seen_at"
    }
}

struct MobileDevicesEnvelope: Decodable {
    let ok: Bool
    let devices: [MobileDeviceInfo]
}

struct MobilePairingEnvelope: Decodable {
    let ok: Bool
    let pairURL: String
    let manualCode: String
    let mobileURL: String
    let expiresAt: Double

    enum CodingKeys: String, CodingKey {
        case ok
        case pairURL = "pair_url"
        case manualCode = "manual_code"
        case mobileURL = "mobile_url"
        case expiresAt = "expires_at"
    }
}

struct MobileRevokeEnvelope: Decodable {
    let ok: Bool
    let revoked: Bool
}

// MARK: Team History (#263): past agent teams as counts only, from dashboard/api/agents/history.

/// Tokens by kind; nil means the provider did not report that kind (shown as unknown, never 0).
struct TeamHistoryTokens: Decodable, Equatable {
    let input: Int?
    let output: Int?
    let cacheRead: Int?
    let cacheWrite: Int?
    let reasoning: Int?
    let total: Int?

    enum CodingKeys: String, CodingKey {
        case input, output, reasoning, total
        case cacheRead = "cache_read"
        case cacheWrite = "cache_write"
    }
}

struct TeamHistoryAgent: Decodable, Identifiable, Equatable {
    let agentID: String
    let label: String
    let provider: String?
    let model: String?
    let status: String?
    let outcome: String?
    let toolCalls: Int
    let durationMS: Int?
    let tokens: TeamHistoryTokens
    /// The task it ran: team messages name their sender "task <id>".
    let taskID: String?

    var id: String { agentID }

    enum CodingKeys: String, CodingKey {
        case label, provider, model, status, outcome, tokens
        case agentID = "agent_id"
        case taskID = "task_id"
        case toolCalls = "tool_calls"
        case durationMS = "duration_ms"
    }
}

struct TeamHistoryTeam: Decodable, Identifiable, Equatable {
    let teamID: String
    let title: String
    let provider: String?
    let model: String?
    let status: String?
    let outcome: String?
    let successfulCount: Int?
    let failureCount: Int?
    let createdAt: Double?
    let endedAt: Double?
    let durationMS: Int?
    let agentCount: Int
    let toolCalls: Int
    let tokens: TeamHistoryTokens
    let tokensUnknownAgents: Int
    /// Messages the agents sent each other; nil when the server could not count them (or predates the count).
    let messageCount: Int?
    let agents: [TeamHistoryAgent]

    var id: String { teamID }

    enum CodingKeys: String, CodingKey {
        case title, provider, model, status, outcome, tokens, agents
        case teamID = "team_id"
        case successfulCount = "successful_count"
        case failureCount = "failure_count"
        case createdAt = "created_at"
        case endedAt = "ended_at"
        case durationMS = "duration_ms"
        case agentCount = "agent_count"
        case toolCalls = "tool_calls"
        case tokensUnknownAgents = "tokens_unknown_agents"
        case messageCount = "message_count"
    }
}

/// One team's conversation for Team History (#104): who wrote to whom, and who was shown or opened it.
struct TeamMessagesEnvelope: Decodable, Equatable {
    let messages: [TeamMessageItem]
    let hasMore: Bool
    let nextAfter: Int?
    let messageCounts: TeamMessageCounts?
    let dependencyReads: [TeamDependencyRead]?

    enum CodingKeys: String, CodingKey {
        case messages
        case hasMore = "has_more"
        case nextAfter = "next_after"
        case messageCounts = "message_counts"
        case dependencyReads = "dependency_reads"
    }
}

struct TeamMessageItem: Decodable, Identifiable, Equatable {
    let messageID: String
    let from: String
    let toLabel: String?
    let kind: String
    let body: String
    let sentAt: String
    let fromLead: Bool?
    let forwardedFrom: String?
    let deliveryNote: String?
    let deliveries: [TeamMessageDelivery]

    var id: String { messageID }

    enum CodingKeys: String, CodingKey {
        case from, kind, body, deliveries
        case messageID = "message_id"
        case toLabel = "to_label"
        case sentAt = "sent_at"
        case fromLead = "from_lead"
        case forwardedFrom = "forwarded_from"
        case deliveryNote = "delivery_note"
    }
}

struct TeamMessageDelivery: Decodable, Equatable {
    let label: String
    let channel: String
    let readAt: String?

    enum CodingKeys: String, CodingKey {
        case label, channel
        case readAt = "read_at"
    }
}

struct TeamMessageCounts: Decodable, Equatable {
    let sent: Int
    let toFinishedRecipients: Int
    let forwarded: Int

    enum CodingKeys: String, CodingKey {
        case sent, forwarded
        case toFinishedRecipients = "to_finished_recipients"
    }
}

struct TeamDependencyRead: Decodable, Equatable {
    let readerTask: String
    let targetTask: String
    let count: Int

    enum CodingKeys: String, CodingKey {
        case count
        case readerTask = "reader_task"
        case targetTask = "target_task"
    }
}

struct TeamHistoryEnvelope: Decodable, Equatable {
    let ok: Bool
    let teams: [TeamHistoryTeam]
    let total: Int
    let limit: Int
    let offset: Int
    let retentionDays: Int?

    enum CodingKeys: String, CodingKey {
        case ok, teams, total, limit, offset
        case retentionDays = "retention_days"
    }
}
