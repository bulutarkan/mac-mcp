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
    var id: String { agentID }
    var isActive: Bool { status == "starting" || status == "running" }
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
        case status, success, outcome, title, provider, model, count, tasks
        case unresolvedConflictCount = "unresolved_conflict_count"
        case partialFailure = "partial_failure"
        case successfulCount = "successful_count"
        case failureCount = "failure_count"
        case pendingCount = "pending_count"
        case workCount = "work_count"
        case terminalCount = "terminal_count"
    }
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
