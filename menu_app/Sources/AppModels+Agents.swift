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
    let provider: String?
    let model: String?
    let count: Int?
    let terminalCount: Int?
    let unresolvedConflictCount: Int?
    let tasks: [TeamTaskInfo]?
    /// Plan quota of each provider the team's running agents spend; empty once it finishes.
    let quota: [ProviderQuota]?

    struct TeamTaskInfo: Decodable, Equatable {
        let id: String?
        let state: String?
        let queuedReason: String?

        enum CodingKeys: String, CodingKey {
            case id, state
            case queuedReason = "queued_reason"
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
        case status, success, outcome, title, provider, model, count, tasks, quota
        case unresolvedConflictCount = "unresolved_conflict_count"
        case partialFailure = "partial_failure"
        case successfulCount = "successful_count"
        case failureCount = "failure_count"
        case pendingCount = "pending_count"
        case workCount = "work_count"
        case terminalCount = "terminal_count"
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

    var id: String { agentID }

    enum CodingKeys: String, CodingKey {
        case label, provider, model, status, outcome, tokens
        case agentID = "agent_id"
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
