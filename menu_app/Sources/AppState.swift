import AppKit
import Combine
import CryptoKit
import Darwin
import Foundation
import SafariServices

struct UsageTotals: Decodable, Equatable {
    let calls: Int
    let successCount: Int
    let errorCount: Int
    let inputTokens: Int
    let outputTokens: Int
    let inputBytes: Int
    let outputBytes: Int
    let imageCount: Int
    let binaryBytes: Int
    let durationTotalMs: Int
    let p50LatencyMs: Int?
    let p95LatencyMs: Int?
    let p50LatencyRelation: String?
    let p95LatencyRelation: String?

    enum CodingKeys: String, CodingKey {
        case calls
        case successCount = "success_count"
        case errorCount = "error_count"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case inputBytes = "input_bytes"
        case outputBytes = "output_bytes"
        case imageCount = "image_count"
        case binaryBytes = "binary_bytes"
        case durationTotalMs = "duration_total_ms"
        case p50LatencyMs = "p50_latency_ms"
        case p95LatencyMs = "p95_latency_ms"
        case p50LatencyRelation = "p50_latency_relation"
        case p95LatencyRelation = "p95_latency_relation"
    }

    static let empty = UsageTotals(
        calls: 0, successCount: 0, errorCount: 0,
        inputTokens: 0, outputTokens: 0, inputBytes: 0, outputBytes: 0,
        imageCount: 0, binaryBytes: 0, durationTotalMs: 0,
        p50LatencyMs: nil, p95LatencyMs: nil,
        p50LatencyRelation: nil, p95LatencyRelation: nil
    )
}

struct UsageDay: Decodable, Identifiable, Equatable {
    let date: String
    let calls: Int
    let successCount: Int
    let errorCount: Int
    let inputTokens: Int
    let outputTokens: Int
    let inputBytes: Int
    let outputBytes: Int
    let imageCount: Int
    let binaryBytes: Int
    let durationTotalMs: Int
    let p50LatencyMs: Int?
    let p95LatencyMs: Int?

    var id: String { date }
    var totalTokens: Int { inputTokens + outputTokens }

    enum CodingKeys: String, CodingKey {
        case date, calls
        case successCount = "success_count"
        case errorCount = "error_count"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case inputBytes = "input_bytes"
        case outputBytes = "output_bytes"
        case imageCount = "image_count"
        case binaryBytes = "binary_bytes"
        case durationTotalMs = "duration_total_ms"
        case p50LatencyMs = "p50_latency_ms"
        case p95LatencyMs = "p95_latency_ms"
    }
}

struct UsageTool: Decodable, Identifiable, Equatable {
    let tool: String
    let calls: Int
    let successCount: Int
    let errorCount: Int
    let inputTokens: Int
    let outputTokens: Int
    let inputBytes: Int
    let outputBytes: Int
    let imageCount: Int
    let binaryBytes: Int
    let durationTotalMs: Int
    let p50LatencyMs: Int?
    let p95LatencyMs: Int?

    var id: String { tool }
    var totalTokens: Int { inputTokens + outputTokens }

    enum CodingKeys: String, CodingKey {
        case tool, calls
        case successCount = "success_count"
        case errorCount = "error_count"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case inputBytes = "input_bytes"
        case outputBytes = "output_bytes"
        case imageCount = "image_count"
        case binaryBytes = "binary_bytes"
        case durationTotalMs = "duration_total_ms"
        case p50LatencyMs = "p50_latency_ms"
        case p95LatencyMs = "p95_latency_ms"
    }
}

struct UsageDiagnostics: Decodable, Equatable {
    let enabled: Bool?
    let queueDepth: Int?
    let queueCapacity: Int?
    let queueDropped: Int?
    let workerErrors: Int?
    let processed: Int?
    let workerAlive: Bool?
    let tokenizerId: String?
    let measurementClass: String?

    enum CodingKeys: String, CodingKey {
        case enabled
        case queueDepth = "queue_depth"
        case queueCapacity = "queue_capacity"
        case queueDropped = "queue_dropped"
        case workerErrors = "worker_errors"
        case processed
        case workerAlive = "worker_alive"
        case tokenizerId = "tokenizer_id"
        case measurementClass = "measurement_class"
    }
}

struct UsageSummaryEnvelope: Decodable, Equatable {
    let ok: Bool
    let days: Int
    let actorClass: String
    let availableSince: String?
    let historyCompleteSince: String?
    let tokenizerId: String
    let measurementClass: String
    let metricName: String
    let metricScope: String
    let inputDefinition: String
    let outputDefinition: String
    let daily: [UsageDay]
    let totals: UsageTotals
    let topTools: [UsageTool]
    let diagnostics: UsageDiagnostics?
    let meteringEnabled: Bool?
    let retentionDays: Int?

    enum CodingKeys: String, CodingKey {
        case ok, days, daily, totals, diagnostics
        case meteringEnabled = "metering_enabled"
        case retentionDays = "retention_days"
        case actorClass = "actor_class"
        case availableSince = "available_since"
        case historyCompleteSince = "history_complete_since"
        case tokenizerId = "tokenizer_id"
        case measurementClass = "measurement_class"
        case metricName = "metric_name"
        case metricScope = "metric_scope"
        case inputDefinition = "input_definition"
        case outputDefinition = "output_definition"
        case topTools = "top_tools"
    }
}

struct UsagePrivacyEnvelope: Decodable, Equatable {
    let ok: Bool
    let enabled: Bool
    let retentionDays: Int

    enum CodingKeys: String, CodingKey {
        case ok, enabled
        case retentionDays = "retention_days"
    }
}

struct MemoryOverview: Decodable, Equatable {
    let count: Int
    let importantCount: Int
    let oldest: String?
    let newest: String?
    let bytesOnDisk: Int
    let retentionDays: Int
    let keepImportant: Bool

    enum CodingKeys: String, CodingKey {
        case count, oldest, newest
        case importantCount = "important_count"
        case bytesOnDisk = "bytes_on_disk"
        case retentionDays = "retention_days"
        case keepImportant = "keep_important"
    }
}

struct MemoryClearResult: Decodable, Equatable {
    let count: Int?
    let deleted: Int?
    let oldest: String?
    let newest: String?
}

struct UsageClearEnvelope: Decodable, Equatable {
    let ok: Bool
    let toolUsageRows: Int
    let providerUsageRows: Int

    enum CodingKeys: String, CodingKey {
        case ok
        case toolUsageRows = "tool_usage_rows"
        case providerUsageRows = "provider_usage_rows"
    }
}

struct ProviderUsageModel: Decodable, Identifiable, Equatable {
    let model: String
    let turns: Int
    let totalTokens: Int?

    var id: String { model }

    enum CodingKeys: String, CodingKey {
        case model, turns
        case totalTokens = "total_tokens"
    }
}

struct ProviderUsageProvider: Decodable, Equatable {
    let provider: String
    let available: Bool?
    let reason: String?
    let turns: Int
    let agents: Int
    let source: String?
    let sources: [String]?
    let schemaVersion: Int
    let inputTokens: Int?
    let outputTokens: Int?
    let reasoningTokens: Int?
    let cacheReadTokens: Int?
    let cacheWriteTokens: Int?
    let totalTokens: Int?
    let models: [ProviderUsageModel]

    enum CodingKeys: String, CodingKey {
        case provider, available, reason, turns, agents, source, sources, models
        case schemaVersion = "schema_version"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case reasoningTokens = "reasoning_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case totalTokens = "total_tokens"
    }
}

struct ProviderUsageSummaryEnvelope: Decodable, Equatable {
    let ok: Bool
    let days: Int
    let availableSince: String?
    let schemaVersion: Int
    let metricName: String
    let metricScope: String
    let fallbackPolicy: String?
    let providers: [String: ProviderUsageProvider]
    let diagnostics: [String: Int]

    enum CodingKeys: String, CodingKey {
        case ok, days, providers, diagnostics
        case availableSince = "available_since"
        case schemaVersion = "schema_version"
        case metricName = "metric_name"
        case metricScope = "metric_scope"
        case fallbackPolicy = "fallback_policy"
    }
}

struct DashboardSummary: Decodable {
    let version: String?
    let totalCalls: Int?
    let successRate: Double?
    let activeAgents: Int?
    enum CodingKeys: String, CodingKey {
        case version
        case totalCalls = "total_calls"
        case successRate = "success_rate"
        case activeAgents = "active_agents"
    }
}

struct BrowserContext: Decodable, Equatable {
    let browser: String?
    let tabHandle: String?
    let site: String?
    let action: String?

    enum CodingKeys: String, CodingKey {
        case browser, site, action
        case tabHandle = "tab_handle"
    }

    var canShowTab: Bool {
        !(browser ?? "").isEmpty && !(tabHandle ?? "").isEmpty
    }
}

struct ToolEvent: Decodable, Identifiable, Equatable {
    let eventID: String
    let timestamp: Double
    let source: String
    let tool: String
    let status: String
    let durationMS: Int?
    let browserContext: BrowserContext?
    var id: String { eventID }
    enum CodingKeys: String, CodingKey {
        case eventID = "event_id"
        case timestamp, source, tool, status
        case durationMS = "duration_ms"
        case browserContext = "browser_context"
    }
}

struct EventsEnvelope: Decodable {
    let events: [ToolEvent]
    let active: [ToolEvent]
}

struct BrowserShowTabEnvelope: Decodable {
    let ok: Bool
}

struct DecisionAccelerationEnvelope: Decodable, Equatable {
    let enabled: Bool?
    let scope: String?
    let active: Bool?
    let keyStatus: String?
    let status: String?
    let latencyMs: Int?

    enum CodingKeys: String, CodingKey {
        case enabled, scope, active, status
        case keyStatus = "key_status"
        case latencyMs = "latency_ms"
    }
}

struct ApprovalBehaviorInfo: Decodable, Equatable {
    let source: String
    let automaticConfirmation: Bool
    let summary: String

    enum CodingKeys: String, CodingKey {
        case source, summary
        case automaticConfirmation = "automatic_confirmation"
    }
}

struct ServerApprovalProfileInfo: Decodable, Identifiable, Equatable {
    let name: String
    let summary: String
    var id: String { name }
}

struct ServerApprovalInfo: Decodable, Equatable {
    let configuredProfile: String
    let activeProfile: String
    let configValid: Bool
    let enabled: Bool
    let source: String
    let availableProfiles: [ServerApprovalProfileInfo]
    let headlessBehavior: String
    let timeoutBehavior: String
    let approvalTimeoutS: Int
    let remoteSessionBehavior: String
    let grantScope: String
    let clientAttestationAccepted: Bool
    let clientPromptDeduplication: String
    let concurrentBehavior: String
    let sameCallDeduplication: String
    let doublePromptGuidance: String
    let precedence: [String]
    let summary: String?

    enum CodingKeys: String, CodingKey {
        case enabled, source, precedence, summary
        case configuredProfile = "configured_profile"
        case activeProfile = "active_profile"
        case configValid = "config_valid"
        case availableProfiles = "available_profiles"
        case headlessBehavior = "headless_behavior"
        case timeoutBehavior = "timeout_behavior"
        case approvalTimeoutS = "approval_timeout_s"
        case remoteSessionBehavior = "remote_session_behavior"
        case grantScope = "grant_scope"
        case clientAttestationAccepted = "client_attestation_accepted"
        case clientPromptDeduplication = "client_prompt_deduplication"
        case concurrentBehavior = "concurrent_behavior"
        case sameCallDeduplication = "same_call_deduplication"
        case doublePromptGuidance = "double_prompt_guidance"
    }
}

struct PermissionProfileInfo: Decodable, Identifiable, Equatable {
    let name: String
    let active: Bool
    let capabilityEnforcement: String
    let allowedCapabilities: [String]
    let deniedCapabilities: [String]
    let destructiveFamilies: [String]
    let accessModeCeiling: String
    let approvalBehavior: ApprovalBehaviorInfo
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, active
        case capabilityEnforcement = "capability_enforcement"
        case allowedCapabilities = "allowed_capabilities"
        case deniedCapabilities = "denied_capabilities"
        case destructiveFamilies = "destructive_families"
        case accessModeCeiling = "access_mode_ceiling"
        case approvalBehavior = "approval_behavior"
    }
}

struct SecuritySemanticsEnvelope: Decodable, Equatable {
    let activeProfile: String
    let configuredProfile: String?
    let configuredProfileScope: String?
    let profileWasNormalized: Bool?
    let normalizedFromProfile: String?
    let globalProfileNames: [String]?
    let delegatedProfileNames: [String]?
    let knownProfile: Bool
    let capabilityEnforcement: String
    let approvalContract: String
    let askConfirmationIsAutomaticGate: Bool
    let supportedApprovalSources: [String]
    let serverApproval: ServerApprovalInfo?
    let profiles: [PermissionProfileInfo]

    enum CodingKeys: String, CodingKey {
        case profiles
        case activeProfile = "active_profile"
        case configuredProfile = "configured_profile"
        case configuredProfileScope = "configured_profile_scope"
        case profileWasNormalized = "profile_was_normalized"
        case normalizedFromProfile = "normalized_from_profile"
        case globalProfileNames = "global_profile_names"
        case delegatedProfileNames = "delegated_profile_names"
        case knownProfile = "known_profile"
        case capabilityEnforcement = "capability_enforcement"
        case approvalContract = "approval_contract"
        case askConfirmationIsAutomaticGate = "ask_confirmation_is_automatic_gate"
        case supportedApprovalSources = "supported_approval_sources"
        case serverApproval = "server_approval"
    }
}

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

    var id: String { teamID }

    enum CodingKeys: String, CodingKey {
        case teamID = "team_id"
        case status, success, outcome, title, provider, model, count
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

enum SteeringActivityState: String, Decodable, Equatable {
    case working
    case idle
    case unknown

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        self = SteeringActivityState(rawValue: value) ?? .unknown
    }
}

enum SteeringLifecycleState: String, Decodable, Equatable {
    case ready
    case queued
    case delivered
    case acknowledged
    case failed
    case disconnected
    case expired
    case unknown

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        self = SteeringLifecycleState(rawValue: value) ?? .unknown
    }
}

struct SteeringSession: Decodable, Identifiable, Equatable {
    let schemaVersion: Int?
    let sessionID: String
    let flowNumber: Int
    let label: String
    let detail: String
    let tool: String
    let state: String
    let activityState: SteeringActivityState?
    let lifecycleState: SteeringLifecycleState?
    let lastTransitionAt: Double?
    let lastError: String?
    let pendingInstructionCount: Int?
    let awaitingAcknowledgementCount: Int?
    let createdAt: Double
    let lastActivityAt: Double
    let activityMS: Int
    let queued: Int
    let activeCalls: Int
    var id: String { sessionID }
    var effectiveActivityState: SteeringActivityState {
        activityState ?? (state == "working" ? .working : .idle)
    }
    var effectiveLifecycleState: SteeringLifecycleState {
        lifecycleState ?? (queued > 0 ? .queued : .ready)
    }
    var pendingCount: Int { pendingInstructionCount ?? queued }
    var isWorking: Bool { effectiveActivityState == .working }
    var activityDisplayBucket: Int {
        let seconds = max(0, activityMS / 1000)
        return seconds < 60 ? seconds : 60 + (seconds / 60)
    }
    func isPresentationEquivalent(to other: SteeringSession) -> Bool {
        schemaVersion == other.schemaVersion
            && sessionID == other.sessionID
            && flowNumber == other.flowNumber
            && label == other.label
            && detail == other.detail
            && tool == other.tool
            && state == other.state
            && activityState == other.activityState
            && lifecycleState == other.lifecycleState
            && lastTransitionAt == other.lastTransitionAt
            && lastError == other.lastError
            && pendingInstructionCount == other.pendingInstructionCount
            && awaitingAcknowledgementCount == other.awaitingAcknowledgementCount
            && createdAt == other.createdAt
            && lastActivityAt == other.lastActivityAt
            && activityDisplayBucket == other.activityDisplayBucket
            && queued == other.queued
            && activeCalls == other.activeCalls
    }
    enum CodingKeys: String, CodingKey {
        case label, detail, tool, state, queued
        case schemaVersion = "schema_version"
        case sessionID = "session_id"
        case flowNumber = "flow_number"
        case activityState = "activity_state"
        case lifecycleState = "lifecycle_state"
        case lastTransitionAt = "last_transition_at"
        case lastError = "last_error"
        case pendingInstructionCount = "pending_instruction_count"
        case awaitingAcknowledgementCount = "awaiting_acknowledgement_count"
        case createdAt = "created_at"
        case lastActivityAt = "last_activity_at"
        case activityMS = "activity_ms"
        case activeCalls = "active_calls"
    }
}

struct SteeringRecent: Decodable, Equatable {
    let schemaVersion: Int?
    let kind: String?
    let id: String
    let sessionID: String
    let status: String
    let lifecycleState: SteeringLifecycleState?
    let transitionedAt: Double?
    let deliveredAt: Double?
    let deliveryMode: String?
    let lastError: String?
    let clientInstructionID: String?
    var effectiveLifecycleState: SteeringLifecycleState {
        if let lifecycleState { return lifecycleState }
        switch status {
        case "queued": return .queued
        case "delivered", "preempted": return .delivered
        case "acknowledged": return .acknowledged
        case "delivery_failed": return .failed
        case "session_ended": return .disconnected
        case "session_expired": return .expired
        default: return .unknown
        }
    }
    enum CodingKeys: String, CodingKey {
        case id, kind, status
        case schemaVersion = "schema_version"
        case sessionID = "session_id"
        case lifecycleState = "lifecycle_state"
        case transitionedAt = "transitioned_at"
        case deliveredAt = "delivered_at"
        case deliveryMode = "delivery_mode"
        case lastError = "last_error"
        case clientInstructionID = "client_instruction_id"
    }
}

struct SteeringEnvelope: Decodable {
    let schemaVersion: Int?
    let generationID: String?
    let sessions: [SteeringSession]
    let recent: [SteeringRecent]

    init(schemaVersion: Int?, generationID: String? = nil, sessions: [SteeringSession], recent: [SteeringRecent]) {
        self.schemaVersion = schemaVersion
        self.generationID = generationID
        self.sessions = sessions
        self.recent = recent
    }

    enum CodingKeys: String, CodingKey {
        case sessions, recent
        case schemaVersion = "schema_version"
        case generationID = "generation_id"
    }
}

enum SteeringSessionSection: String {
    case needsAttention
    case active
    case recent
}

struct SteeringSessionDiff: Equatable {
    let addedIDs: [String]
    let updatedIDs: [String]
    let removedIDs: [String]

    var hasChanges: Bool {
        !addedIDs.isEmpty || !updatedIDs.isEmpty || !removedIDs.isEmpty
    }
}

struct SteeringSessionGroups {
    let needsAttention: [SteeringSession]
    let active: [SteeringSession]
    let recent: [SteeringSession]
    let terminalAttention: [SteeringRecent]

    var totalItemCount: Int {
        needsAttention.count + active.count + recent.count + terminalAttention.count
    }

    var sectionCount: Int {
        var count = 0
        if !needsAttention.isEmpty || !terminalAttention.isEmpty { count += 1 }
        if !active.isEmpty { count += 1 }
        if !recent.isEmpty { count += 1 }
        return count
    }
}

struct SteeringSessionGrouping {
    static func section(for session: SteeringSession) -> SteeringSessionSection {
        let lifecycle = session.effectiveLifecycleState
        let hasError = !(session.lastError ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if hasError || [.failed, .disconnected, .expired, .unknown].contains(lifecycle) {
            return .needsAttention
        }
        if session.effectiveActivityState == .working
            || [.queued, .delivered].contains(lifecycle)
            || session.pendingCount > 0
            || (session.awaitingAcknowledgementCount ?? 0) > 0 {
            return .active
        }
        return .recent
    }

    static func diff(current: [SteeringSession], incoming: [SteeringSession]) -> SteeringSessionDiff {
        let currentByID = current.reduce(into: [String: SteeringSession]()) { $0[$1.sessionID] = $1 }
        let incomingByID = incoming.reduce(into: [String: SteeringSession]()) { $0[$1.sessionID] = $1 }
        let added = incoming.compactMap { currentByID[$0.sessionID] == nil ? $0.sessionID : nil }
        let updated = incoming.compactMap { session -> String? in
            guard let existing = currentByID[session.sessionID], !existing.isPresentationEquivalent(to: session) else { return nil }
            return session.sessionID
        }
        let removed = current.compactMap { incomingByID[$0.sessionID] == nil ? $0.sessionID : nil }
        return SteeringSessionDiff(addedIDs: added, updatedIDs: updated, removedIDs: removed)
    }

    static func groups(
        sessions: [SteeringSession],
        recentEvents: [SteeringRecent],
        retentionMinutes: Int,
        now: Double = Date().timeIntervalSince1970
    ) -> SteeringSessionGroups {
        let retentionSeconds = Double(max(1, retentionMinutes) * 60)
        let cutoff = now - retentionSeconds
        let liveSessionIDs = Set(sessions.map(\.sessionID))

        let needsAttention = sessions
            .filter { section(for: $0) == .needsAttention }
            .sorted { ($0.lastTransitionAt ?? $0.lastActivityAt) > ($1.lastTransitionAt ?? $1.lastActivityAt) }
        let active = sessions
            .filter { section(for: $0) == .active }
            .sorted {
                if $0.isWorking != $1.isWorking { return $0.isWorking && !$1.isWorking }
                return $0.lastActivityAt > $1.lastActivityAt
            }
        let recent = sessions
            .filter { section(for: $0) == .recent && $0.lastActivityAt >= cutoff }
            .sorted { $0.lastActivityAt > $1.lastActivityAt }

        var seenTerminalSessionIDs = Set<String>()
        let terminalAttention = recentEvents.filter { event in
            guard event.kind == "session", !liveSessionIDs.contains(event.sessionID) else { return false }
            guard [.failed, .disconnected, .expired, .unknown].contains(event.effectiveLifecycleState) else { return false }
            guard let transitionedAt = event.transitionedAt, transitionedAt >= cutoff else { return false }
            return seenTerminalSessionIDs.insert(event.sessionID).inserted
        }

        return SteeringSessionGroups(
            needsAttention: needsAttention,
            active: active,
            recent: recent,
            terminalAttention: terminalAttention
        )
    }
}

struct SteeringSettingsEnvelope: Decodable {
    let ok: Bool
    let sessionTTLMinutes: Int
    enum CodingKeys: String, CodingKey {
        case ok
        case sessionTTLMinutes = "session_ttl_minutes"
    }
}

struct SteeringSendEnvelope: Decodable {
    struct Message: Decodable {
        let id: String
        let clientInstructionID: String?
        let idempotentReplay: Bool?
        let sessionState: String?
        let activityState: SteeringActivityState?
        let lifecycleState: SteeringLifecycleState?
        enum CodingKeys: String, CodingKey {
            case id
            case clientInstructionID = "client_instruction_id"
            case idempotentReplay = "idempotent_replay"
            case sessionState = "session_state"
            case activityState = "activity_state"
            case lifecycleState = "lifecycle_state"
        }
    }
    let ok: Bool
    let status: String?
    let message: Message?
}

struct SteeringErrorEnvelope: Decodable {
    let error: String?
    let reason: String?
    let canonicalMessageID: String?
    enum CodingKeys: String, CodingKey {
        case error, reason
        case canonicalMessageID = "canonical_message_id"
    }
}

enum SteeringPostError: Error {
    case server(status: Int, code: String, reason: String?)
}

struct PersistedPendingSteeringSubmission: Codable, Equatable {
    let schemaVersion: Int
    let clientInstructionID: String
    let sessionID: String
    let textHash: String
    let generationID: String?
    let createdAt: Double

    init(
        schemaVersion: Int,
        clientInstructionID: String,
        sessionID: String,
        textHash: String,
        generationID: String? = nil,
        createdAt: Double
    ) {
        self.schemaVersion = schemaVersion
        self.clientInstructionID = clientInstructionID
        self.sessionID = sessionID
        self.textHash = textHash
        self.generationID = generationID
        self.createdAt = createdAt
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case clientInstructionID = "client_instruction_id"
        case sessionID = "session_id"
        case textHash = "text_hash"
        case generationID = "generation_id"
        case createdAt = "created_at"
    }
}

enum PendingSteeringSubmissionStore {
    static let schemaVersion = 1
    static let defaultMaxAgeSeconds: Double = 3600

    static func stateURL() -> URL {
        let env = ProcessInfo.processInfo.environment
        if let configured = env["MAC_MCP_PENDING_STEERING_STATE_FILE"], !configured.isEmpty {
            return URL(fileURLWithPath: NSString(string: configured).expandingTildeInPath)
        }
        let stateDirectory: URL
        if let configured = env["MAC_MCP_STATE_DIR"], !configured.isEmpty {
            stateDirectory = URL(fileURLWithPath: NSString(string: configured).expandingTildeInPath)
        } else {
            stateDirectory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".mac-mcp")
        }
        return stateDirectory.appendingPathComponent("pending-steering.json")
    }

    static func textHash(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    static func load(now: Double = Date().timeIntervalSince1970, maxAgeSeconds: Double = defaultMaxAgeSeconds) -> PersistedPendingSteeringSubmission? {
        let url = stateURL()
        guard let data = try? Data(contentsOf: url),
              let state = try? JSONDecoder().decode(PersistedPendingSteeringSubmission.self, from: data),
              state.schemaVersion == schemaVersion,
              !state.clientInstructionID.isEmpty,
              !state.sessionID.isEmpty,
              !state.textHash.isEmpty else {
            return nil
        }
        guard now - state.createdAt <= max(60, maxAgeSeconds), now + 60 >= state.createdAt else {
            clear()
            return nil
        }
        return state
    }

    static func save(_ state: PersistedPendingSteeringSubmission) throws {
        let url = stateURL()
        let parent = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: parent,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let data = try JSONEncoder().encode(state)
        try data.write(to: url, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    static func clear() {
        try? FileManager.default.removeItem(at: stateURL())
    }
}

struct UpdateCheckInfo: Decodable, Equatable {
    let deployedCommit: String
    let targetCommit: String
    let updateAvailable: Bool
    let dirty: Bool
    let behindBy: Int
    let releaseVerified: Bool
    let releaseID: String?
    let releaseVersion: String?
    let unverifiedAhead: Int?

    enum CodingKeys: String, CodingKey {
        case dirty
        case deployedCommit = "deployed_commit"
        case targetCommit = "target_commit"
        case updateAvailable = "update_available"
        case behindBy = "behind_by"
        case releaseVerified = "release_verified"
        case releaseID = "release_id"
        case releaseVersion = "release_version"
        case unverifiedAhead = "unverified_ahead"
    }

    var deployedShort: String { String(deployedCommit.prefix(8)) }
    var targetShort: String { String(targetCommit.prefix(8)) }
}

struct UpdateOutcomeInfo: Decodable, Equatable {
    let status: String?
    let reason: String?
}

struct UpdateRecoveryInfo: Decodable, Equatable {
    let status: String?
    let dependency: UpdateOutcomeInfo?
    let repo: UpdateOutcomeInfo?
    let runtime: UpdateOutcomeInfo?
    let health: UpdateOutcomeInfo?
}

enum UpdateStatusKind: Equatable {
    case running
    case success
    case warning
    case error
    case recovery
}

struct UpdateProgressStep: Identifiable, Equatable {
    enum State: Equatable {
        case pending
        case active
        case complete
        case failed
        case skipped
    }

    let id: String
    let title: String
    let detail: String
    let state: State
}

struct UpdateStateSnapshot: Decodable, Equatable {
    let status: String
    let transactionID: String?
    let updaterPID: Int32?
    let updatedAt: Double?
    let fromCommit: String?
    let toCommit: String?
    let fromShort: String?
    let toShort: String?
    let releaseID: String?
    let releaseVersion: String?
    let backup: String?
    let syncedFiles: Int?
    let healthURL: String?
    let healthSkipped: Bool?
    let error: String?
    let dependencyInstallAttempted: Bool?
    let dependenciesUpdated: Bool?
    let recoveredAfterCrash: Bool?
    let runtimeRollback: UpdateOutcomeInfo?
    let repoRollback: UpdateOutcomeInfo?
    let rollbackHealth: UpdateOutcomeInfo?
    let dependencyRollback: UpdateOutcomeInfo?
    let recovery: UpdateRecoveryInfo?

    enum CodingKeys: String, CodingKey {
        case status, backup, error, recovery
        case transactionID = "transaction_id"
        case updaterPID = "updater_pid"
        case updatedAt = "updated_at"
        case fromCommit = "from_commit"
        case toCommit = "to_commit"
        case fromShort = "from_short"
        case toShort = "to_short"
        case releaseID = "release_id"
        case releaseVersion = "release_version"
        case syncedFiles = "synced_files"
        case healthURL = "health_url"
        case healthSkipped = "health_skipped"
        case dependencyInstallAttempted = "dependency_install_attempted"
        case dependenciesUpdated = "dependencies_updated"
        case recoveredAfterCrash = "recovered_after_crash"
        case runtimeRollback = "runtime_rollback"
        case repoRollback = "repo_rollback"
        case rollbackHealth = "rollback_health"
        case dependencyRollback = "dependency_rollback"
    }

    var normalizedStatus: String {
        status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    var transactionPID: pid_t? {
        guard let transactionID else { return nil }
        let parts = transactionID.split(separator: "-", omittingEmptySubsequences: true)
        guard parts.count >= 3, parts[0] == "upd", let raw = Int32(parts[1]), raw > 0 else { return nil }
        return pid_t(raw)
    }

    var activeProcessPID: pid_t? {
        if let transactionPID { return transactionPID }
        if let updaterPID, updaterPID > 0 { return pid_t(updaterPID) }
        return nil
    }

    var isInProgress: Bool {
        [
            "starting", "preparing", "prepared", "repo_updating", "repo_updated",
            "runtime_syncing", "runtime_synced", "dependency_activating",
            "dependencies_activated", "restarting", "health_verified",
            "marker_committed", "dependency_commit_started", "dependency_committed",
            "rolling_back",
        ].contains(normalizedStatus)
    }

    var statusKind: UpdateStatusKind {
        switch normalizedStatus {
        case "completed":
            return healthSkipped == true ? .warning : .success
        case "recovered":
            return .recovery
        case "rolling_back":
            return .recovery
        case "blocked":
            return .warning
        case "failed", "recovery_failed":
            return .error
        default:
            return .running
        }
    }

    var statusTitle: String {
        switch normalizedStatus {
        case "starting": return "Starting updater"
        case "preparing": return "Preparing update"
        case "prepared": return "Backup ready"
        case "repo_updating": return "Updating source"
        case "repo_updated": return "Source updated"
        case "runtime_syncing": return "Syncing runtime"
        case "runtime_synced": return "Runtime synced"
        case "dependency_activating": return "Updating dependencies"
        case "dependencies_activated": return "Dependencies ready"
        case "restarting": return "Restarting Mac MCP"
        case "health_verified": return "Health verified"
        case "marker_committed", "dependency_commit_started", "dependency_committed": return "Finalizing update"
        case "completed":
            return healthSkipped == true ? "Update completed without health verification" : "Update completed"
        case "rolling_back": return "Restoring previous version"
        case "recovered": return "Interrupted update recovered"
        case "recovery_failed": return "Update recovery needs attention"
        case "blocked": return "Update blocked"
        case "failed":
            if runtimeRollback?.status == "restored", rollbackHealth?.status == "passed" {
                return "Update rolled back safely"
            }
            return "Update failed"
        default: return "Update in progress"
        }
    }

    var statusDetail: String {
        switch normalizedStatus {
        case "starting":
            return "The detached updater is starting and will publish durable progress shortly."
        case "preparing":
            return "Verifying the release and preparing a safe runtime merge."
        case "prepared":
            return "The previous runtime is backed up and ready for rollback if needed."
        case "repo_updating", "repo_updated":
            return "Applying the verified release to the source checkout."
        case "runtime_syncing", "runtime_synced":
            return syncedFiles.map { "Syncing managed runtime files · \($0) file(s) recorded." }
                ?? "Syncing managed runtime files while preserving local configuration."
        case "dependency_activating", "dependencies_activated":
            return "Preparing the dependency environment transactionally."
        case "restarting":
            return "Restarting the service before the final health gate."
        case "health_verified", "marker_committed", "dependency_commit_started", "dependency_committed":
            return "The updated runtime passed its health gate; finalizing durable state."
        case "completed":
            return healthSkipped == true
                ? "Files were updated, but runtime health was not verified."
                : "The installed runtime is updated and health verified."
        case "rolling_back":
            return "The update did not complete, so Mac MCP is restoring the previous working checkpoint."
        case "recovered":
            return "A previous interrupted update was recovered to a known checkpoint. Check for updates again before retrying."
        case "recovery_failed":
            return "Automatic recovery could not establish a verified working state. Run mac-mcp doctor before retrying."
        case "blocked":
            return "The updater refused to continue because a required safety condition was not met."
        case "failed":
            if runtimeRollback?.status == "restored", rollbackHealth?.status == "passed" {
                return "The attempted update failed, but the previous runtime was restored and verified healthy."
            }
            if runtimeRollback?.status == "restore_unverified" {
                return "Previous runtime files were restored, but service health could not be verified. Run mac-mcp doctor."
            }
            if runtimeRollback?.status == "failed" {
                return "Automatic rollback did not complete. Run mac-mcp doctor before retrying."
            }
            return "The update did not complete. Review Mac MCP diagnostics before retrying."
        default:
            return "Mac MCP is following the updater's durable transaction state."
        }
    }

    var steps: [UpdateProgressStep] {
        let titles = [
            ("prepare", "Prepare"),
            ("backup", "Backup"),
            ("update", "Update"),
            ("sync", "Sync"),
            ("dependencies", "Dependencies"),
            ("restart", "Restart"),
            ("health", "Health"),
        ]
        let status = normalizedStatus
        if ["blocked", "failed", "rolling_back", "recovered", "recovery_failed"].contains(status) {
            return []
        }

        let activeIndex: Int?
        let completedThrough: Int
        switch status {
        case "starting", "preparing":
            activeIndex = 0; completedThrough = -1
        case "prepared":
            activeIndex = 2; completedThrough = 1
        case "repo_updating":
            activeIndex = 2; completedThrough = 1
        case "repo_updated":
            activeIndex = 3; completedThrough = 2
        case "runtime_syncing":
            activeIndex = 3; completedThrough = 2
        case "runtime_synced":
            activeIndex = 4; completedThrough = 3
        case "dependency_activating":
            activeIndex = 4; completedThrough = 3
        case "dependencies_activated":
            activeIndex = 5; completedThrough = 4
        case "restarting":
            activeIndex = 5; completedThrough = 4
        case "health_verified", "marker_committed", "dependency_commit_started", "dependency_committed":
            activeIndex = nil; completedThrough = 6
        case "completed":
            activeIndex = nil; completedThrough = 6
        default:
            activeIndex = 0; completedThrough = -1
        }

        return titles.enumerated().map { index, item in
            let state: UpdateProgressStep.State
            if index == 4,
               ["restarting", "health_verified", "marker_committed", "dependency_commit_started", "dependency_committed", "completed"].contains(status),
               dependenciesUpdated != true,
               dependencyInstallAttempted != true {
                state = .skipped
            } else if index == 6, status == "completed", healthSkipped == true {
                state = .skipped
            } else if index <= completedThrough {
                state = .complete
            } else if activeIndex == index {
                state = .active
            } else {
                state = .pending
            }
            let detail: String
            switch state {
            case .active: detail = "In progress"
            case .complete: detail = "Done"
            case .skipped: detail = "Not required"
            case .failed: detail = "Failed"
            case .pending: detail = "Waiting"
            }
            return UpdateProgressStep(id: item.0, title: item.1, detail: detail, state: state)
        }
    }
}

enum UpdateStateStore {
    static func rootURL() -> URL {
        let env = ProcessInfo.processInfo.environment
        if let configured = env["MAC_MCP_UPDATE_DIR"], !configured.isEmpty {
            return URL(fileURLWithPath: NSString(string: configured).expandingTildeInPath)
        }
        let stateDirectory: URL
        if let configured = env["MAC_MCP_STATE_DIR"], !configured.isEmpty {
            stateDirectory = URL(fileURLWithPath: NSString(string: configured).expandingTildeInPath)
        } else {
            stateDirectory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".mac-mcp")
        }
        return stateDirectory.appendingPathComponent("update")
    }

    static func load() -> UpdateStateSnapshot? {
        guard let data = try? Data(contentsOf: rootURL().appendingPathComponent("state.json")) else { return nil }
        return try? JSONDecoder().decode(UpdateStateSnapshot.self, from: data)
    }
}

enum DashboardConnectionState: String, Equatable {
    case connecting
    case connected
    case degraded
    case disconnected
}

enum DashboardAPIError: Error {
    case httpStatus(Int)
}

struct ActionNotice: Identifiable, Equatable {
    enum Kind { case info, success, error, update }
    let id = UUID()
    let kind: Kind
    let message: String

    var symbolName: String {
        switch kind {
        case .info: return "info.circle.fill"
        case .success: return "checkmark.circle.fill"
        case .error: return "exclamationmark.triangle.fill"
        case .update: return "arrow.down.circle.fill"
        }
    }
}

struct SettingsDataState: Equatable {
    enum Phase: String, Equatable {
        case loading
        case fresh
        case stale
        case unavailable
        case error
    }

    let phase: Phase
    let lastUpdatedAt: Date?
    let message: String?

    static func loading(lastUpdatedAt: Date? = nil) -> SettingsDataState {
        SettingsDataState(phase: .loading, lastUpdatedAt: lastUpdatedAt, message: nil)
    }

    static func fresh(at date: Date = Date()) -> SettingsDataState {
        SettingsDataState(phase: .fresh, lastUpdatedAt: date, message: nil)
    }

    static func stale(lastUpdatedAt: Date?, message: String) -> SettingsDataState {
        SettingsDataState(phase: .stale, lastUpdatedAt: lastUpdatedAt, message: message)
    }

    static func unavailable(_ message: String) -> SettingsDataState {
        SettingsDataState(phase: .unavailable, lastUpdatedAt: nil, message: message)
    }

    static func error(_ message: String) -> SettingsDataState {
        SettingsDataState(phase: .error, lastUpdatedAt: nil, message: message)
    }
}


@MainActor
final class AppState: ObservableObject {
    @Published var serverRunning = false
    @Published var ngrokRunning = false
    @Published var cloudflareRunning = false
    @Published private(set) var cloudflareCredentialConfigured = false
    @Published private(set) var connectionState: DashboardConnectionState = .connecting
    @Published private(set) var connectionIssue: String?
    @Published private(set) var steeringSnapshotStale = false
    @Published private(set) var hasSteeringSnapshot = false
    @Published private(set) var nextPollDelaySeconds = 2.5
    @Published var version = "—"
    @Published var totalCalls = 0
    @Published var successRate = 100.0
    @Published var activeAgents = 0
    @Published private(set) var usageSummary: UsageSummaryEnvelope?
    @Published private(set) var usageLoading = false
    @Published private(set) var usageIssue: String?
    @Published private(set) var usagePrivacy: UsagePrivacyEnvelope?
    @Published private(set) var usageDataNotice: String?
    @Published private(set) var memoryOverview: MemoryOverview?
    @Published private(set) var memoryNotice: String?
    @Published private(set) var providerUsageSummary: ProviderUsageSummaryEnvelope?
    @Published private(set) var providerUsageLoading = false
    @Published private(set) var providerUsageIssue: String?
    @Published private(set) var decisionAcceleration: DecisionAccelerationEnvelope?
    @Published private(set) var decisionVerifying = false
    @Published private(set) var decisionIssue: String?
    @Published var recentEvents: [ToolEvent] = []
    @Published var activeEvents: [ToolEvent] = []
    @Published var browserActionStatus = ""
    @Published var securitySemantics: SecuritySemanticsEnvelope?
    @Published var permissionProfileChanging = false
    @Published var serverApprovalProfileChanging = false
    @Published var agents: [AgentInfo] = []
    @Published private(set) var agentTeams: [AgentTeamInfo] = []
    @Published private(set) var agentNotificationAuthorizationState: AgentNotificationAuthorizationState = .notDetermined
    @Published private(set) var agentNotificationPermissionChanging = false
    @Published var providerStatuses: [ProviderInfo] = []
    @Published var providerCatalogs: [String: ProviderCatalogInfo] = [:]
    @Published var mobileDevices: [MobileDeviceInfo] = []
    @Published var mobilePairingURL: String?
    @Published var mobilePairingCode: String?
    @Published var mobilePairingExpiresAt: Double?
    @Published var mobilePairingLoading = false
    @Published private(set) var providerSettingsState: SettingsDataState = .loading()
    @Published private(set) var mobileSettingsState: SettingsDataState = .loading()
    @Published private(set) var permissionsSettingsState: SettingsDataState = .loading()
    @Published private(set) var browserSettingsState: SettingsDataState = .loading()
    @Published private(set) var mobilePairingIssue: String?
    @Published private(set) var mobileDeviceActionIssue: String?
    @Published private(set) var permissionActionIssue: String?
    @Published var steeringSessions: [SteeringSession] = []
    @Published var steeringRecent: [SteeringRecent] = []
    @Published private(set) var steeringGenerationID: String?
    @Published var selectedSteeringSessionID: String?
    @Published var steeringPrompt = ""
    @Published var steeringStatus = "No agent sessions yet."
    @Published var steeringSending = false
    @Published var busyAction: String?
    @Published var actionNotice: ActionNotice?
    @Published private(set) var updateCheckInfo: UpdateCheckInfo?
    @Published private(set) var updateProgress: UpdateStateSnapshot?
    @Published private(set) var updateTransactionActive = false
    @Published private(set) var updateCheckLoading = false
    @Published var pulse = false
    @Published private(set) var safariExtensionEnabled = false
    @Published private(set) var safariExtensionRegistered = false
    @Published private(set) var safariExtensionStatus = "Checking…"

    let settings = SettingsStore()
    private var pollTask: Task<Void, Never>?
    private var toolActivityStreamTask: Task<Void, Never>?
    /// True while the /dashboard/events stream is open and feeding the activity list.
    private var toolActivityStreamConnected = false
    /// Set after one events poll completes on the current stream connection, so the
    /// list starts from a full snapshot before the stream alone keeps it current.
    private var streamActivitySeeded = false
    nonisolated static let recentActivityLimit = 20
    private var pulseTask: Task<Void, Never>?
    private var noticeTask: Task<Void, Never>?
    private var updateStatePollTask: Task<Void, Never>?
    private var consecutiveRefreshFailures = 0
    private var lastSteeringMessageID: String?
    private var pendingSteeringClientInstructionID: String?
    private var pendingSteeringSessionID: String?
    private var pendingSteeringText: String?
    private var pendingSteeringTextHash: String?
    private var pendingSteeringGenerationID: String?
    private var pendingSteeringRestoredFromDisk = false
    private let agentNotificationTracker = AgentNotificationTransitionTracker()
    private var lastPublicProcessCheckAt = 0.0
    private static let activePollIntervalSeconds = 2.5
    private static let idlePollIntervalSeconds = 12.0
    private static let ngrokProcessCheckIntervalSeconds = 30.0
    private static let publicProcessCheckIntervalSeconds = ngrokProcessCheckIntervalSeconds
    private static let maxRetryIntervalSeconds = 30.0

    init(startBackgroundTasks: Bool = true) {
        restorePendingSteeringSubmission()
        refreshSafariExtensionState()
        refreshCloudflareCredentialState()
        refreshPersistedUpdateState()
        if startBackgroundTasks {
            AgentNotificationController.shared.configure { [weak self] kind, targetID in
                Task { @MainActor in
                    self?.openDashboardNotificationTarget(kind: kind, targetID: targetID)
                }
            }
            startTasks()
            observeReduceMotion()
            Task { [weak self] in
                await self?.refreshAgentNotificationAuthorization(reconcilePreference: true)
            }
        }
    }
    deinit {
        pollTask?.cancel()
        toolActivityStreamTask?.cancel()
        pulseTask?.cancel()
        noticeTask?.cancel()
        updateStatePollTask?.cancel()
        if let reduceMotionObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(reduceMotionObserver)
        }
    }

    func refreshAgentNotificationAuthorization(reconcilePreference: Bool = false) async {
        let status = await AgentNotificationController.shared.authorizationState()
        setIfChanged(\.agentNotificationAuthorizationState, status)
        guard reconcilePreference,
              settings.agentCompletionNotificationsEnabled,
              status != .authorized else { return }
        settings.agentCompletionNotificationsEnabled = false
        try? settings.save()
        seedAgentNotificationBaseline()
    }

    func setAgentCompletionNotificationsEnabled(_ enabled: Bool) {
        if !enabled {
            settings.agentCompletionNotificationsEnabled = false
            do {
                try settings.save()
                AgentNotificationController.shared.removeDeliveredNotifications()
                seedAgentNotificationBaseline()
            } catch {
                settings.agentCompletionNotificationsEnabled = true
                showNotice(ActionNotice(kind: .error, message: "Couldn’t save notification settings."))
            }
            return
        }

        guard !agentNotificationPermissionChanging else { return }
        setIfChanged(\.agentNotificationPermissionChanging, true)
        Task { [weak self] in
            guard let self else { return }
            let granted = await AgentNotificationController.shared.requestAuthorization()
            let status = await AgentNotificationController.shared.authorizationState()
            self.setIfChanged(\.agentNotificationAuthorizationState, status)
            self.setIfChanged(\.agentNotificationPermissionChanging, false)

            guard granted, status == .authorized else {
                self.settings.agentCompletionNotificationsEnabled = false
                try? self.settings.save()
                self.seedAgentNotificationBaseline()
                self.showNotice(ActionNotice(
                    kind: .info,
                    message: "Agent completion notifications remain off because macOS notification permission was not granted."
                ))
                return
            }

            self.settings.agentCompletionNotificationsEnabled = true
            do {
                try self.settings.save()
                self.seedAgentNotificationBaseline()
                self.showNotice(ActionNotice(kind: .success, message: "Agent completion notifications enabled."))
            } catch {
                self.settings.agentCompletionNotificationsEnabled = false
                try? self.settings.save()
                self.showNotice(ActionNotice(kind: .error, message: "Couldn’t save notification settings."))
            }
        }
    }

    private func notificationAgentStates(_ values: [AgentInfo]) -> [AgentNotificationAgentState] {
        values.map {
            AgentNotificationAgentState(
                id: $0.agentID,
                teamID: $0.teamID,
                status: $0.status,
                title: $0.title
            )
        }
    }

    private func notificationTeamStates(_ values: [AgentTeamInfo]) -> [AgentNotificationTeamState] {
        values.map {
            AgentNotificationTeamState(
                id: $0.teamID,
                status: $0.status,
                title: $0.title,
                success: $0.success,
                partialFailure: $0.partialFailure,
                outcome: $0.outcome
            )
        }
    }

    private func seedAgentNotificationBaseline() {
        agentNotificationTracker.reset(
            agents: notificationAgentStates(agents),
            teams: notificationTeamStates(agentTeams)
        )
    }

    private func processAgentNotificationTransitions(
        agents newAgents: [AgentInfo],
        teams newTeams: [AgentTeamInfo]
    ) {
        let events = agentNotificationTracker.events(
            agents: notificationAgentStates(newAgents),
            teams: notificationTeamStates(newTeams),
            enabled: settings.agentCompletionNotificationsEnabled
        )
        for event in events {
            Task { await AgentNotificationController.shared.schedule(event) }
        }
    }

    @discardableResult
    private func setIfChanged<Value: Equatable>(_ keyPath: ReferenceWritableKeyPath<AppState, Value>, _ value: Value) -> Bool {
        guard self[keyPath: keyPath] != value else { return false }
        self[keyPath: keyPath] = value
        return true
    }

    var dashboardURL: URL? { URL(string: "http://127.0.0.1:\(settings.serverPort)/dashboard") }

    var safariExtensionBundleIdentifier: String {
        "\(Bundle.main.bundleIdentifier ?? "com.bulutarkan.mac-mcp.menu").safari"
    }

    func refreshSafariExtensionState() {
        let previous = browserSettingsState
        setIfChanged(\.browserSettingsState, .loading(lastUpdatedAt: previous.lastUpdatedAt))
        let identifier = safariExtensionBundleIdentifier
        SFSafariExtensionManager.getStateOfSafariExtension(withIdentifier: identifier) { [weak self] state, error in
            DispatchQueue.main.async {
                guard let self else { return }
                if let state, error == nil {
                    self.safariExtensionRegistered = true
                    self.safariExtensionEnabled = state.isEnabled
                    self.safariExtensionStatus = state.isEnabled ? "On" : "Needs enabling"
                    self.browserSettingsState = .fresh()
                } else if let error {
                    let message = "Could not check the Safari companion: \(error.localizedDescription)"
                    if let lastUpdatedAt = previous.lastUpdatedAt {
                        self.browserSettingsState = .stale(lastUpdatedAt: lastUpdatedAt, message: message)
                    } else {
                        self.browserSettingsState = .error(message)
                    }
                } else {
                    self.safariExtensionRegistered = false
                    self.safariExtensionEnabled = false
                    self.safariExtensionStatus = "Developer setup"
                    self.browserSettingsState = .unavailable("Safari Visual Companion is not registered in this build.")
                }
            }
        }
    }

    func openSafariExtensionPreferences() {
        let identifier = safariExtensionBundleIdentifier
        SFSafariExtensionManager.getStateOfSafariExtension(withIdentifier: identifier) { [weak self] state, error in
            guard let self else { return }
            if state != nil, error == nil {
                SFSafariApplication.showPreferencesForExtension(withIdentifier: identifier) { [weak self] preferencesError in
                    DispatchQueue.main.async {
                        guard let self else { return }
                        if let preferencesError {
                            self.actionNotice = ActionNotice(kind: .error, message: "Safari could not open the registered extension settings: \(preferencesError.localizedDescription)")
                        } else {
                            self.actionNotice = ActionNotice(kind: .info, message: "Enable Mac MCP Visual Companion in Safari, then allow website access.")
                        }
                        self.refreshSafariExtensionState()
                    }
                }
                return
            }
            DispatchQueue.main.async {
                self.openUnsignedSafariExtensionSetup()
            }
        }
    }

    private func openUnsignedSafariExtensionSetup() {
        let extensionSourceURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("mac-mcp/menu_app/BrowserVisualCompanion", isDirectory: true)
        let manifestURL = extensionSourceURL.appendingPathComponent("manifest.json")
        if FileManager.default.fileExists(atPath: manifestURL.path) {
            NSWorkspace.shared.activateFileViewerSelecting([manifestURL])
        }
        if let safariURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.Safari") {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            NSWorkspace.shared.openApplication(at: safariURL, configuration: configuration)
        }
        actionNotice = ActionNotice(
            kind: .info,
            message: "Local developer build: in Safari choose Develop → Allow Unsigned Extensions, then Add Temporary Extension… and select the revealed BrowserVisualCompanion folder."
        )
    }

    func openChromeExtensionSetup() {
        let extensionSourceURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("mac-mcp/menu_app/ChromeVisualCompanion", isDirectory: true)
        let manifestURL = extensionSourceURL.appendingPathComponent("manifest.json")
        if FileManager.default.fileExists(atPath: manifestURL.path) {
            NSWorkspace.shared.activateFileViewerSelecting([manifestURL])
        }
        if let chromeURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.google.Chrome") {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            configuration.arguments = ["chrome://extensions/"]
            NSWorkspace.shared.openApplication(at: chromeURL, configuration: configuration)
        }
        actionNotice = ActionNotice(
            kind: .info,
            message: "Chrome setup: enable Developer mode at chrome://extensions, choose Load unpacked, then select ChromeVisualCompanion. This companion provides focus-safe background tabs plus DOM/page execution and background-safe visual capture."
        )
    }

    static func pollDelaySeconds(forFailureCount failureCount: Int) -> Double {
        guard failureCount > 0 else { return activePollIntervalSeconds }
        return min(pow(2.0, Double(failureCount - 1)), maxRetryIntervalSeconds)
    }

    private var hasActiveWork: Bool {
        activeAgents > 0 || sessionActiveCount > 0
    }

    private var successfulPollIntervalSeconds: Double {
        hasActiveWork ? Self.activePollIntervalSeconds : Self.idlePollIntervalSeconds
    }

    /// Mirrors System Settings > Accessibility > Display > Reduce motion.
    @Published private(set) var reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    private var reduceMotionObserver: NSObjectProtocol?

    private var shouldPulse: Bool {
        // With Reduce Motion the menu bar keeps the static active symbol instead of blinking.
        activeAgents > 0 && !reduceMotion
    }

    private func observeReduceMotion() {
        guard reduceMotionObserver == nil else { return }
        reduceMotionObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.setIfChanged(\.reduceMotion, NSWorkspace.shared.accessibilityDisplayShouldReduceMotion)
                self.updatePulseTask()
            }
        }
    }

    private func refreshNgrokStateIfNeeded(now: Double = ProcessInfo.processInfo.systemUptime) {
        guard lastPublicProcessCheckAt == 0 || now - lastPublicProcessCheckAt >= Self.publicProcessCheckIntervalSeconds else { return }
        lastPublicProcessCheckAt = now
        let mode = settings.publicEndpointMode
        setIfChanged(\.ngrokRunning, mode == "ngrok" && Self.processExists(matching: "ngrok http"))
        setIfChanged(\.cloudflareRunning, mode == "cloudflare" && Self.processExists(matching: "cloudflared tunnel"))
        refreshCloudflareCredentialState()
    }

    func refreshCloudflareCredentialState() {
        setIfChanged(\.cloudflareCredentialConfigured, Self.cloudflareCredentialFileIsSecure())
    }

    private func updatePulseTask() {
        guard shouldPulse else {
            pulseTask?.cancel()
            pulseTask = nil
            setIfChanged(\.pulse, false)
            return
        }
        guard pulseTask == nil else { return }
        pulseTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                self.setIfChanged(\.pulse, !self.pulse)
                try? await Task.sleep(nanoseconds: 550_000_000)
            }
        }
    }

    var connectionStatusText: String {
        switch connectionState {
        case .connecting: return "Connecting…"
        case .connected: return "Server running · v\(version)"
        case .degraded: return "Server running · degraded"
        case .disconnected: return "Disconnected"
        }
    }

    var connectionBannerTitle: String {
        switch connectionState {
        case .connecting: return "Connecting to Mac MCP…"
        case .connected: return "Connected"
        case .degraded: return "Some dashboard data is unavailable"
        case .disconnected: return "Mac MCP server is unreachable"
        }
    }

    var connectionBannerDetail: String {
        var parts: [String] = []
        if let connectionIssue, !connectionIssue.isEmpty { parts.append(connectionIssue) }
        if connectionState == .degraded || connectionState == .disconnected {
            parts.append("Retrying in \(Self.formatPollDelay(nextPollDelaySeconds)).")
        }
        return parts.joined(separator: " · ")
    }

    private static func formatPollDelay(_ seconds: Double) -> String {
        seconds.rounded() == seconds ? "\(Int(seconds))s" : String(format: "%.1fs", seconds)
    }

    func startTasks() {
        restartToolActivityStream()
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                let delay = max(0.25, self.nextPollDelaySeconds)
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            }
        }
    }

    /// Poll /dashboard/api/events only when the stream cannot be trusted to keep
    /// the list current: not connected, or not yet seeded on this connection.
    nonisolated static func shouldPollActivityEvents(streamConnected: Bool, seeded: Bool) -> Bool {
        !(streamConnected && seeded)
    }

    /// Apply one stream event to the activity lists, keeping the same shape as a
    /// poll: newest first, at most `limit` recent events from the last hour.
    nonisolated static func applyActivityStreamEvent(
        kind: String,
        event: ToolEvent?,
        active: [ToolEvent],
        recent: [ToolEvent],
        limit: Int = recentActivityLimit,
        now: Double = Date().timeIntervalSince1970
    ) -> (active: [ToolEvent], recent: [ToolEvent]) {
        guard let event else { return (active, recent) }
        var nextActive = active.filter { $0.eventID != event.eventID }
        var nextRecent = recent
        if kind == "call_started" {
            nextActive.insert(event, at: 0)
        } else if kind == "call_finished" {
            nextRecent.removeAll { $0.eventID == event.eventID }
            nextRecent.insert(event, at: 0)
            nextRecent = Array(nextRecent.filter { $0.timestamp >= now - 3600 }.prefix(limit))
        } else {
            return (active, recent)
        }
        return (nextActive, nextRecent)
    }

    nonisolated static func toolEvent(from payload: [String: Any]) -> ToolEvent? {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload) else { return nil }
        return try? JSONDecoder().decode(ToolEvent.self, from: data)
    }

    func restartToolActivityStream() {
        toolActivityStreamTask?.cancel()
        toolActivityStreamTask = nil
        toolActivityStreamConnected = false

        guard settings.requireToolDescriptions, settings.showToolActivity else {
            ToolActivityBubbleController.shared.hideImmediately()
            return
        }

        toolActivityStreamTask = Task { [weak self] in
            await self?.consumeToolActivityStream()
        }
    }

    private func consumeToolActivityStream() async {
        while !Task.isCancelled {
            guard settings.requireToolDescriptions, settings.showToolActivity else {
                ToolActivityBubbleController.shared.hideImmediately()
                return
            }
            guard let url = URL(
                string: "http://127.0.0.1:\(settings.serverPort)/dashboard/events"
            ) else { return }

            var request = URLRequest(url: url)
            request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
            authorizeDashboardRequest(&request)
            request.timeoutInterval = 35

            do {
                let (bytes, response) = try await URLSession.shared.bytes(for: request)
                guard
                    let http = response as? HTTPURLResponse,
                    (200..<300).contains(http.statusCode)
                else {
                    throw URLError(.badServerResponse)
                }
                toolActivityStreamConnected = true
                // Reseed once per connection so nothing finished while it was down is lost.
                streamActivitySeeded = false

                for try await line in bytes.lines {
                    if Task.isCancelled { return }
                    guard line.hasPrefix("data:") else { continue }
                    let json = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                    guard
                        let data = json.data(using: .utf8),
                        let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                    else { continue }
                    handleToolActivityPayload(payload)
                }
            } catch {
                toolActivityStreamConnected = false
                if Task.isCancelled { return }
            }
            toolActivityStreamConnected = false

            try? await Task.sleep(nanoseconds: 900_000_000)
        }
        toolActivityStreamConnected = false
    }

    private func handleToolActivityPayload(_ payload: [String: Any]) {
        let kind = String(describing: payload["kind"] ?? "")

        if kind == "connected" {
            ToolActivityBubbleController.shared.hideImmediately()
            guard let active = payload["active"] as? [[String: Any]] else { return }
            setIfChanged(\.activeEvents, active.compactMap(Self.toolEvent(from:)))
            for event in active.reversed() {
                presentToolActivityEvent(event)
            }
            return
        }

        let lists = Self.applyActivityStreamEvent(
            kind: kind, event: Self.toolEvent(from: payload), active: activeEvents, recent: recentEvents
        )
        setIfChanged(\.activeEvents, lists.active)
        setIfChanged(\.recentEvents, lists.recent)

        guard let eventID = payload["event_id"] as? String, !eventID.isEmpty else { return }
        if kind == "call_started" {
            presentToolActivityEvent(payload)
        } else if kind == "call_finished" {
            ToolActivityBubbleController.shared.finish(eventID: eventID)
        }
    }

    private func presentToolActivityEvent(_ payload: [String: Any]) {
        guard settings.requireToolDescriptions, settings.showToolActivity else { return }
        guard
            let eventID = payload["event_id"] as? String,
            let arguments = payload["arguments"] as? [String: Any],
            let description = arguments["description"] as? String,
            !description.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return }

        ToolActivityBubbleController.shared.begin(
            eventID: eventID,
            tool: String(describing: payload["tool"] ?? "tool"),
            description: description,
            sessionID: payload["session_id"] as? String,
            agentID: payload["agent_id"] as? String,
            teamID: payload["team_id"] as? String
        )
    }

    func refresh() async {
        refreshNgrokStateIfNeeded()
        refreshPersistedUpdateState()
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            recordDisconnected(issue: "Invalid server URL.")
            return
        }
        do {
            let summary: DashboardSummary = try await fetch(base.appendingPathComponent("dashboard/api/summary"), query: ["hours": "1"])

            let pollEvents = Self.shouldPollActivityEvents(
                streamConnected: toolActivityStreamConnected, seeded: streamActivitySeeded
            )
            let streamWasConnected = toolActivityStreamConnected
            async let eventsFetch: EventsEnvelope? = fetchActivityEvents(base: base, poll: pollEvents)
            async let agentsFetch: AgentsEnvelope = fetch(base.appendingPathComponent("dashboard/api/agents"), query: ["limit": "20"])
            async let steeringFetch: SteeringEnvelope = fetch(base.appendingPathComponent("dashboard/api/steering"), query: [:])
            async let securityFetch: SecuritySemanticsEnvelope = fetch(base.appendingPathComponent("dashboard/api/security/semantics"), query: [:])

            setIfChanged(\.serverRunning, true)
            if let value = summary.version { setIfChanged(\.version, value) }
            if let value = summary.totalCalls { setIfChanged(\.totalCalls, value) }
            if let value = summary.successRate { setIfChanged(\.successRate, value) }

            var secondaryIssue: String?
            var resolvedActiveAgents = summary.activeAgents ?? activeAgents
            do {
                if let eventsEnvelope = try await eventsFetch {
                    setIfChanged(\.recentEvents, eventsEnvelope.events)
                    setIfChanged(\.activeEvents, eventsEnvelope.active)
                    if streamWasConnected && toolActivityStreamConnected { streamActivitySeeded = true }
                }
            } catch {
                setIfChanged(\.activeEvents, [])
                secondaryIssue = secondaryIssue ?? "Activity: \(Self.issueText(for: error))"
            }
            do {
                let agentsEnvelope = try await agentsFetch
                let teams = agentsEnvelope.teams ?? []
                processAgentNotificationTransitions(
                    agents: agentsEnvelope.agents,
                    teams: teams
                )
                setIfChanged(\.agents, agentsEnvelope.agents)
                setIfChanged(\.agentTeams, teams)
                resolvedActiveAgents = agentsEnvelope.agents.filter(\.isActive).count
            } catch {
                secondaryIssue = secondaryIssue ?? "Agents: \(Self.issueText(for: error))"
            }
            setIfChanged(\.activeAgents, resolvedActiveAgents)
            do {
                let steeringEnvelope = try await steeringFetch
                applySteeringSnapshot(steeringEnvelope)
                setIfChanged(\.hasSteeringSnapshot, true)
                setIfChanged(\.steeringSnapshotStale, false)
            } catch {
                setIfChanged(\.steeringSnapshotStale, true)
                secondaryIssue = secondaryIssue ?? "Sessions: \(Self.issueText(for: error))"
            }
            do {
                let semantics = try await securityFetch
                setIfChanged(\.securitySemantics, semantics)
                setIfChanged(\.permissionsSettingsState, .fresh())
            } catch {
                let message = "Could not refresh permission semantics: \(Self.issueText(for: error))"
                let state = settingsFailureState(
                    from: permissionsSettingsState,
                    hasData: securitySemantics != nil,
                    message: message
                )
                setIfChanged(\.permissionsSettingsState, state)
                secondaryIssue = secondaryIssue ?? "Security: \(Self.issueText(for: error))"
            }

            if let secondaryIssue {
                recordRefreshFailure(state: .degraded, issue: secondaryIssue)
            } else {
                recordConnected()
            }
        } catch {
            setIfChanged(\.activeEvents, [])
            setIfChanged(\.steeringSnapshotStale, true)
            let state = Self.failureState(for: error)
            setIfChanged(\.serverRunning, state == .degraded)
            if state == .disconnected { setIfChanged(\.activeAgents, 0) }
            let permissionMessage = "Could not refresh permission semantics: \(Self.issueText(for: error))"
            let permissionState = settingsFailureState(
                from: permissionsSettingsState,
                hasData: securitySemantics != nil,
                message: permissionMessage
            )
            setIfChanged(\.permissionsSettingsState, permissionState)
            recordRefreshFailure(state: state, issue: Self.issueText(for: error))
        }
    }

    private func settingsFailureState(
        from current: SettingsDataState,
        hasData: Bool,
        message: String,
        updatedAt: Date? = nil
    ) -> SettingsDataState {
        if hasData || current.lastUpdatedAt != nil {
            return .stale(lastUpdatedAt: updatedAt ?? current.lastUpdatedAt, message: message)
        }
        return .error(message)
    }

    private var lastUsageActorClass = "all"

    func refreshUsage(days: Int = 365, actorClass: String? = nil) async {
        // Refreshes after a settings change keep the Source filter the pane is showing.
        let actorClass = actorClass ?? lastUsageActorClass
        lastUsageActorClass = actorClass
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            setIfChanged(\.usageIssue, "Could not load Usage because the server URL is invalid.")
            return
        }
        setIfChanged(\.usageLoading, true)
        defer { setIfChanged(\.usageLoading, false) }
        do {
            let summary: UsageSummaryEnvelope = try await fetch(
                base.appendingPathComponent("dashboard/api/usage"),
                query: [
                    "days": String(max(1, min(days, 400))),
                    "actor": actorClass,
                ],
                timeout: 4.0
            )
            setIfChanged(\.usageSummary, summary)
            setIfChanged(\.usageIssue, nil)
        } catch {
            setIfChanged(\.usageIssue, "Could not refresh Usage: \(Self.issueText(for: error))")
        }
    }

    func refreshDecisionAcceleration(reloadKey: Bool = false) async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        do {
            let status: DecisionAccelerationEnvelope = try await fetch(
                base.appendingPathComponent("dashboard/api/decision-acceleration"),
                query: reloadKey ? ["reload": "1"] : [:],
                timeout: 8.0
            )
            setIfChanged(\.decisionAcceleration, status)
            setIfChanged(\.decisionIssue, nil)
        } catch {
            setIfChanged(\.decisionIssue, "Could not read Decision status: \(Self.issueText(for: error))")
        }
    }

    func updateUsagePrivacy(enabled: Bool? = nil, retentionDays: Int? = nil) async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        var body: [String: Any] = [:]
        if let enabled { body["metering_enabled"] = enabled }
        if let retentionDays { body["retention_days"] = retentionDays }
        guard !body.isEmpty else { return }
        do {
            let result: UsagePrivacyEnvelope = try await post(
                base.appendingPathComponent("dashboard/api/usage/settings"), body: body, timeout: 4.0
            )
            setIfChanged(\.usagePrivacy, result)
            setIfChanged(\.usageDataNotice, nil)
        } catch {
            setIfChanged(\.usageDataNotice, "Could not save usage settings: \(Self.issueText(for: error))")
        }
        await refreshUsage()
    }

    func clearUsage() async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        do {
            let result: UsageClearEnvelope = try await post(
                base.appendingPathComponent("dashboard/api/usage/clear"), body: ["confirm": true], timeout: 10.0
            )
            setIfChanged(
                \.usageDataNotice,
                "Deleted \(result.toolUsageRows + result.providerUsageRows) stored usage rows."
            )
        } catch {
            setIfChanged(\.usageDataNotice, "Could not clear usage data: \(Self.issueText(for: error))")
        }
        await refreshUsage()
        await refreshProviderUsage()
    }

    func refreshMemory() async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        do {
            let overview: MemoryOverview = try await fetch(base.appendingPathComponent("dashboard/api/memory"), query: [:], timeout: 6.0)
            setIfChanged(\.memoryOverview, overview)
        } catch {
            setIfChanged(\.memoryNotice, "Could not read memory: \(Self.issueText(for: error))")
        }
    }

    func updateMemoryRetention(days: Int? = nil, keepImportant: Bool? = nil) async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        var body: [String: Any] = [:]
        if let days { body["retention_days"] = days }
        if let keepImportant { body["keep_important"] = keepImportant }
        do {
            let overview: MemoryOverview = try await post(base.appendingPathComponent("dashboard/api/memory/settings"), body: body, timeout: 8.0)
            setIfChanged(\.memoryOverview, overview)
            setIfChanged(\.memoryNotice, nil)
        } catch {
            setIfChanged(\.memoryNotice, "Could not save memory settings: \(Self.issueText(for: error))")
        }
    }

    /// Without confirm this only counts what would be deleted.
    func clearMemory(confirm: Bool) async -> MemoryClearResult? {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return nil }
        do {
            let result: MemoryClearResult = try await post(base.appendingPathComponent("dashboard/api/memory/clear"), body: ["confirm": confirm], timeout: 20.0)
            if confirm {
                setIfChanged(\.memoryNotice, "Deleted \(result.deleted ?? 0) memories from the Markdown files and the search index.")
                await refreshMemory()
            }
            return result
        } catch {
            setIfChanged(\.memoryNotice, "Could not clear memory: \(Self.issueText(for: error))")
            return nil
        }
    }

    func exportMemory(to destination: URL) async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        var request = URLRequest(url: base.appendingPathComponent("dashboard/api/memory/export"))
        request.timeoutInterval = 30
        authorizeDashboardRequest(&request)
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            try data.write(to: destination, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
            setIfChanged(\.memoryNotice, "Exported memories to \(destination.lastPathComponent).")
        } catch {
            setIfChanged(\.memoryNotice, "Could not export memory: \(Self.issueText(for: error))")
        }
    }

    func verifyDecisionsKey() async {
        guard !decisionVerifying,
              let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        setIfChanged(\.decisionVerifying, true)
        defer { setIfChanged(\.decisionVerifying, false) }
        do {
            let result: DecisionAccelerationEnvelope = try await post(
                base.appendingPathComponent("dashboard/api/decision-acceleration/verify"),
                body: [:],
                timeout: 20.0
            )
            setIfChanged(\.decisionAcceleration, result)
            setIfChanged(\.decisionIssue, nil)
        } catch {
            setIfChanged(\.decisionIssue, "Could not verify the key: \(Self.issueText(for: error))")
        }
    }

    func refreshProviderUsage(days: Int = 365) async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            setIfChanged(
                \.providerUsageIssue,
                "Could not load Provider Tokens because the server URL is invalid."
            )
            return
        }
        setIfChanged(\.providerUsageLoading, true)
        defer { setIfChanged(\.providerUsageLoading, false) }
        do {
            let summary: ProviderUsageSummaryEnvelope = try await fetch(
                base.appendingPathComponent("dashboard/api/provider-usage"),
                query: ["days": String(max(1, min(days, 400)))],
                timeout: 4.0
            )
            setIfChanged(\.providerUsageSummary, summary)
            setIfChanged(\.providerUsageIssue, nil)
        } catch {
            setIfChanged(
                \.providerUsageIssue,
                "Could not refresh Provider Tokens: \(Self.issueText(for: error))"
            )
        }
    }

    func refreshProviders() async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            setIfChanged(\.providerSettingsState, .error("Could not load providers because the server URL is invalid."))
            return
        }
        let previous = providerSettingsState
        setIfChanged(\.providerSettingsState, .loading(lastUpdatedAt: previous.lastUpdatedAt))

        var failures: [String] = []
        var didUpdate = false
        do {
            let envelope: ProvidersEnvelope = try await fetch(
                base.appendingPathComponent("dashboard/api/providers"),
                query: [:],
                timeout: 8.0
            )
            setIfChanged(\.providerStatuses, envelope.providers)
            didUpdate = true
        } catch {
            failures.append("provider detection: \(Self.issueText(for: error))")
        }
        do {
            let catalog: AgentCatalogEnvelope = try await fetch(
                base.appendingPathComponent("dashboard/api/agent-catalog"),
                query: [:],
                timeout: 12.0
            )
            setIfChanged(\.providerCatalogs, catalog.providers)
            didUpdate = true
        } catch {
            failures.append("model catalog: \(Self.issueText(for: error))")
        }

        if failures.isEmpty {
            setIfChanged(\.providerSettingsState, .fresh())
        } else {
            let message = "Could not fully refresh providers (\(failures.joined(separator: "; ")))."
            let hasData = didUpdate || !providerStatuses.isEmpty || !providerCatalogs.isEmpty
            let updatedAt = didUpdate ? Date() : previous.lastUpdatedAt
            setIfChanged(
                \.providerSettingsState,
                settingsFailureState(from: previous, hasData: hasData, message: message, updatedAt: updatedAt)
            )
        }
    }

    func refreshMobileDevices() async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            setIfChanged(\.mobileSettingsState, .error("Could not load mobile devices because the server URL is invalid."))
            return
        }
        let previous = mobileSettingsState
        setIfChanged(\.mobileSettingsState, .loading(lastUpdatedAt: previous.lastUpdatedAt))
        do {
            let envelope: MobileDevicesEnvelope = try await fetch(
                base.appendingPathComponent("dashboard/api/mobile/devices"),
                query: [:]
            )
            setIfChanged(\.mobileDevices, envelope.devices)
            setIfChanged(\.mobileSettingsState, .fresh())
            setIfChanged(\.mobileDeviceActionIssue, nil)
        } catch {
            let message = "Could not load connected devices: \(Self.issueText(for: error))"
            let state = settingsFailureState(
                from: previous,
                hasData: !mobileDevices.isEmpty,
                message: message
            )
            setIfChanged(\.mobileSettingsState, state)
        }
    }

    func createMobilePairing() async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        setIfChanged(\.mobilePairingIssue, nil)
        setIfChanged(\.mobilePairingLoading, true)
        defer { setIfChanged(\.mobilePairingLoading, false) }
        do {
            let envelope: MobilePairingEnvelope = try await post(
                base.appendingPathComponent("dashboard/api/mobile/pairings"),
                body: [:]
            )
            setIfChanged(\.mobilePairingURL, envelope.pairURL)
            setIfChanged(\.mobilePairingCode, envelope.manualCode)
            setIfChanged(\.mobilePairingExpiresAt, envelope.expiresAt)
            setIfChanged(\.mobilePairingIssue, nil)
            await refreshMobileDevices()
        } catch {
            setIfChanged(\.mobilePairingURL, nil)
            setIfChanged(\.mobilePairingCode, nil)
            setIfChanged(\.mobilePairingExpiresAt, nil)
            let message = "Couldn’t create a mobile pairing code. Check the public HTTPS endpoint and Mac MCP server, then retry."
            setIfChanged(\.mobilePairingIssue, message)
            showNotice(ActionNotice(kind: .error, message: message))
        }
    }

    func revokeMobileDevice(_ deviceID: String) async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        setIfChanged(\.mobileDeviceActionIssue, nil)
        do {
            let envelope: MobileRevokeEnvelope = try await post(
                base.appendingPathComponent("dashboard/api/mobile/revoke"),
                body: ["device_id": deviceID]
            )
            if envelope.revoked {
                setIfChanged(\.mobileDevices, mobileDevices.filter { $0.deviceID != deviceID })
                setIfChanged(\.mobileDeviceActionIssue, nil)
                showNotice(ActionNotice(kind: .success, message: "Mobile device revoked."))
            }
        } catch {
            let message = "Couldn’t revoke the mobile device. Retry after refreshing the device list."
            setIfChanged(\.mobileDeviceActionIssue, message)
            showNotice(ActionNotice(kind: .error, message: message))
        }
    }

    func retryConnection() async {
        setIfChanged(\.connectionState, .connecting)
        setIfChanged(\.connectionIssue, nil)
        consecutiveRefreshFailures = 0
        setIfChanged(\.nextPollDelaySeconds, Self.activePollIntervalSeconds)
        await refresh()
    }

    private func recordConnected() {
        consecutiveRefreshFailures = 0
        setIfChanged(\.connectionState, .connected)
        setIfChanged(\.connectionIssue, nil)
        setIfChanged(\.nextPollDelaySeconds, successfulPollIntervalSeconds)
        updatePulseTask()
    }

    private func recordDisconnected(issue: String) {
        setIfChanged(\.serverRunning, false)
        recordRefreshFailure(state: .disconnected, issue: issue)
    }

    private func recordRefreshFailure(state: DashboardConnectionState, issue: String) {
        consecutiveRefreshFailures += 1
        setIfChanged(\.connectionState, state)
        setIfChanged(\.connectionIssue, issue)
        setIfChanged(\.nextPollDelaySeconds, Self.pollDelaySeconds(forFailureCount: consecutiveRefreshFailures))
        if state == .disconnected {
            pulseTask?.cancel()
            pulseTask = nil
            setIfChanged(\.pulse, false)
        } else {
            updatePulseTask()
        }
    }

    nonisolated private static func failureState(for error: Error) -> DashboardConnectionState {
        if error is DashboardAPIError || error is DecodingError { return .degraded }
        return .disconnected
    }

    nonisolated private static func issueText(for error: Error) -> String {
        if let apiError = error as? DashboardAPIError {
            switch apiError {
            case .httpStatus(let status): return "Server error (HTTP \(status))"
            }
        }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .timedOut: return "Request timed out"
            case .cannotConnectToHost: return "Connection refused"
            case .networkConnectionLost: return "Connection lost"
            case .notConnectedToInternet: return "Network unavailable"
            case .cannotFindHost: return "Server host unavailable"
            default: return "Connection error (\(urlError.code.rawValue))"
            }
        }
        if error is DecodingError { return "Invalid API response" }
        return "Connection error"
    }

    func setPermissionProfile(_ profile: String) {
        guard !permissionProfileChanging else { return }
        guard securitySemantics?.activeProfile != profile else { return }
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        permissionProfileChanging = true
        setIfChanged(\.permissionActionIssue, nil)
        Task {
            defer { permissionProfileChanging = false }
            do {
                let response: SecuritySemanticsEnvelope = try await post(
                    base.appendingPathComponent("dashboard/api/security/profile"),
                    body: ["profile": profile]
                )
                setIfChanged(\.securitySemantics, response)
                setIfChanged(\.permissionsSettingsState, .fresh())
                setIfChanged(\.permissionActionIssue, nil)
                await refresh()
            } catch {
                let message = "Couldn’t change the permission profile. Refresh and try again."
                setIfChanged(\.permissionActionIssue, message)
                await refresh()
            }
        }
    }

    func setServerApprovalProfile(_ profile: String) {
        guard !serverApprovalProfileChanging else { return }
        guard securitySemantics?.serverApproval?.activeProfile != profile else { return }
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        serverApprovalProfileChanging = true
        setIfChanged(\.permissionActionIssue, nil)
        Task {
            defer { serverApprovalProfileChanging = false }
            do {
                let response: SecuritySemanticsEnvelope = try await post(
                    base.appendingPathComponent("dashboard/api/security/server-approval"),
                    body: ["profile": profile]
                )
                setIfChanged(\.securitySemantics, response)
                setIfChanged(\.permissionsSettingsState, .fresh())
                setIfChanged(\.permissionActionIssue, nil)
                await refresh()
            } catch {
                let message = "Couldn’t change Server Approval. Refresh and try again."
                setIfChanged(\.permissionActionIssue, message)
                await refresh()
            }
        }
    }

    private func lifecycleArgs(_ command: String) -> [String] {
        var args = [command, "--public-mode", settings.publicEndpointMode]
        if settings.publicEndpointMode == "custom" || settings.publicEndpointMode == "cloudflare" {
            let url = settings.publicURL.trimmingCharacters(in: .whitespacesAndNewlines)
            if !url.isEmpty { args += ["--public-url", url] }
        }
        if settings.publicEndpointMode == "cloudflare" {
            let tunnel = settings.cloudflareTunnel.trimmingCharacters(in: .whitespacesAndNewlines)
            if !tunnel.isEmpty { args += ["--cloudflare-tunnel", tunnel] }
        }
        return args
    }

    func saveCloudflareCredential(_ rawValue: String) {
        guard busyAction == nil else { return }
        let token = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else {
            showNotice(ActionNotice(kind: .error, message: "Paste a Cloudflare Tunnel token first."))
            return
        }
        busyAction = "Saving credential"
        actionNotice = nil
        let cliPath = settings.cliPath
        let settingsPath = settings.path.path
        Task {
            let result = await Self.runCLI(
                args: ["credential", "cloudflare", "save"],
                configuredPath: cliPath,
                settingsPath: settingsPath,
                input: token + "\n"
            )
            busyAction = nil
            refreshCloudflareCredentialState()
            if result.code == 0 {
                showNotice(ActionNotice(kind: .success, message: "Cloudflare credential saved securely."))
            } else {
                showNotice(ActionNotice(kind: .error, message: "Couldn’t save the Cloudflare credential."))
            }
        }
    }

    /// Runs the Mac MCP CLI with this app's settings (used by Help & Diagnostics).
    func runCLICommand(_ args: [String]) async -> (code: Int32, output: String) {
        await Self.runCLI(args: args, configuredPath: settings.cliPath, settingsPath: settings.path.path)
    }

    /// Restarts like the menu's Restart action but lets the caller wait to recheck.
    func restartServerAndWait() async -> Bool {
        guard busyAction == nil else { return false }
        busyAction = "Restarting"
        actionNotice = nil
        let args = lifecycleArgs("restart")
        let result = await Self.runCLI(args: args, configuredPath: settings.cliPath, settingsPath: settings.path.path)
        busyAction = nil
        showNotice(Self.notice(for: args, result: result))
        await refresh()
        return result.code == 0
    }

    func startServer() { runAction(title: "Starting", args: lifecycleArgs("start")) }
    func stopServer() { runAction(title: "Stopping", args: ["stop"]) }
    func restartServer() { runAction(title: "Restarting", args: lifecycleArgs("restart")) }

    var canCheckForUpdates: Bool {
        busyAction == nil && !updateCheckLoading && !updateTransactionActive
    }

    var canInstallUpdate: Bool {
        guard busyAction == nil, !updateTransactionActive, updateCheckInfo?.dirty != true else { return false }
        let recoveryStatus = updateProgress?.normalizedStatus ?? ""
        let recoveryNeeded = updateProgress?.isInProgress == true
            || ["failed", "recovery_failed"].contains(recoveryStatus)
        if let updateCheckInfo {
            return updateCheckInfo.updateAvailable || recoveryNeeded
        }
        return true
    }

    var updateActionTitle: String {
        if updateTransactionActive || busyAction == "Updating" { return "Updating…" }
        if updateProgress?.isInProgress == true { return "Resume Recovery" }
        if updateProgress?.normalizedStatus == "recovery_failed" { return "Retry Recovery" }
        return "Update Now"
    }

    func refreshPersistedUpdateState() {
        let snapshot = UpdateStateStore.load()
        setIfChanged(\.updateProgress, snapshot)
        let transactionActive: Bool
        if let snapshot, snapshot.isInProgress, let pid = snapshot.activeProcessPID {
            transactionActive = Self.updaterProcessMatches(pid)
        } else {
            transactionActive = false
        }
        setIfChanged(\.updateTransactionActive, transactionActive)
    }

    private func startUpdateStatePolling() {
        updateStatePollTask?.cancel()
        updateStatePollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                self.refreshPersistedUpdateState()
                if self.busyAction != "Updating" && !self.updateTransactionActive { return }
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
        }
    }

    private func applyUpdateCheckOutput(_ output: String, code: Int32, shouldShowNotice: Bool) {
        guard code == 0 || code == 2,
              let data = output.data(using: .utf8),
              let info = try? JSONDecoder().decode(UpdateCheckInfo.self, from: data) else {
            if shouldShowNotice {
                showNotice(ActionNotice(kind: .error, message: "Couldn’t read update information."))
            }
            return
        }
        setIfChanged(\.updateCheckInfo, info)
        guard shouldShowNotice else { return }
        if info.dirty {
            showNotice(ActionNotice(kind: .error, message: "Update is blocked by local changes."))
        } else if info.updateAvailable {
            showNotice(ActionNotice(kind: .update, message: "Verified update available."))
        } else {
            showNotice(ActionNotice(kind: .success, message: "Mac MCP is up to date."))
        }
    }

    func checkForUpdates() {
        refreshPersistedUpdateState()
        guard canCheckForUpdates else { return }
        busyAction = "Checking update"
        updateCheckLoading = true
        actionNotice = nil
        noticeTask?.cancel()
        let cliPath = settings.cliPath
        let settingsPath = settings.path.path
        Task {
            let result = await Self.runCLI(
                args: ["update", "--check", "--json"],
                configuredPath: cliPath,
                settingsPath: settingsPath
            )
            busyAction = nil
            updateCheckLoading = false
            applyUpdateCheckOutput(result.output, code: result.code, shouldShowNotice: true)
            refreshPersistedUpdateState()
        }
    }

    func installUpdate() {
        refreshPersistedUpdateState()
        guard canInstallUpdate else {
            if updateTransactionActive {
                showNotice(ActionNotice(kind: .info, message: "An update transaction is already running."))
            }
            return
        }
        busyAction = "Updating"
        actionNotice = nil
        noticeTask?.cancel()
        let cliPath = settings.cliPath
        let settingsPath = settings.path.path
        startUpdateStatePolling()
        Task {
            let result = await Self.runCLI(
                args: ["update"],
                configuredPath: cliPath,
                settingsPath: settingsPath
            )
            busyAction = nil
            refreshPersistedUpdateState()
            updateStatePollTask?.cancel()
            updateStatePollTask = nil

            await refresh()

            let check = await Self.runCLI(
                args: ["update", "--check", "--json"],
                configuredPath: cliPath,
                settingsPath: settingsPath
            )
            applyUpdateCheckOutput(check.output, code: check.code, shouldShowNotice: false)
            refreshPersistedUpdateState()

            if let snapshot = updateProgress {
                switch snapshot.statusKind {
                case .success:
                    showNotice(ActionNotice(kind: .success, message: "Update completed and runtime health was verified."))
                case .recovery:
                    showNotice(ActionNotice(kind: .info, message: snapshot.statusTitle))
                case .warning:
                    showNotice(ActionNotice(kind: .info, message: snapshot.statusTitle))
                case .error:
                    showNotice(ActionNotice(kind: .error, message: snapshot.statusTitle))
                case .running:
                    let message = result.code == 0 ? "Update command finished; verifying final state." : "Update failed."
                    showNotice(ActionNotice(kind: result.code == 0 ? .info : .error, message: message))
                }
            } else {
                showNotice(ActionNotice(
                    kind: result.code == 0 ? .info : .error,
                    message: result.code == 0 ? "Update command completed." : "Update failed."
                ))
            }
        }
    }

    private func openDashboardNotificationTarget(
        kind: AgentTerminalNotification.TargetKind,
        targetID: String
    ) {
        switch kind {
        case .agent:
            openDashboard(focusAgentID: targetID)
        case .team:
            openDashboard(focusTeamID: targetID)
        }
    }

    func openDashboard(focusAgentID: String? = nil, focusTeamID: String? = nil) {
        guard let dashboardURL else { return }
        guard let token = dashboardToken(), !token.isEmpty else {
            showNotice(ActionNotice(kind: .error, message: "Dashboard credential is unavailable. Restart Mac MCP once."))
            return
        }
        var components = URLComponents(url: dashboardURL, resolvingAgainstBaseURL: false)
        var queryItems: [URLQueryItem] = []
        if let focusAgentID, !focusAgentID.isEmpty {
            queryItems.append(URLQueryItem(name: "focus_agent", value: focusAgentID))
        }
        if let focusTeamID, !focusTeamID.isEmpty {
            queryItems.append(URLQueryItem(name: "focus_team", value: focusTeamID))
        }
        if !queryItems.isEmpty {
            components?.queryItems = queryItems
        }
        components?.fragment = "token=\(token)"
        if let url = components?.url { NSWorkspace.shared.open(url) }
    }
    func quitApp() { NSApplication.shared.terminate(nil) }

    var activeBrowserEvents: [ToolEvent] {
        activeEvents.filter { $0.browserContext != nil }
    }

    func resolvedBrowserContext(for event: ToolEvent) -> BrowserContext? {
        guard let context = event.browserContext else { return nil }
        if context.site != nil || context.tabHandle == nil { return context }
        guard let handle = context.tabHandle else { return context }
        let prior = recentEvents.lazy.compactMap(\.browserContext).first {
            $0.tabHandle == handle && $0.site != nil
        }
        guard let prior else { return context }
        return BrowserContext(
            browser: context.browser ?? prior.browser,
            tabHandle: handle,
            site: prior.site,
            action: context.action
        )
    }

    func showBrowserTab(_ event: ToolEvent) {
        guard let context = resolvedBrowserContext(for: event),
              let browser = context.browser, !browser.isEmpty,
              let tabHandle = context.tabHandle, !tabHandle.isEmpty,
              let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            browserActionStatus = "This browser task no longer has a live tab reference."
            return
        }
        browserActionStatus = "Opening tab…"
        Task {
            do {
                let response: BrowserShowTabEnvelope = try await post(
                    base.appendingPathComponent("dashboard/api/browser/show-tab"),
                    body: ["browser": browser, "tab_handle": tabHandle]
                )
                browserActionStatus = response.ok ? "Opened the real browser tab." : "Could not open that browser tab."
            } catch {
                browserActionStatus = "That browser tab is closed or unavailable."
                await refresh()
            }
        }
    }


    func updateSteeringRetention(minutes: Int) {
        let normalized = max(1, minutes)
        settings.steeringSessionMinutes = normalized
        do {
            try settings.save()
        } catch {
            steeringStatus = "Could not save the session duration."
            return
        }
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        Task {
            do {
                let response: SteeringSettingsEnvelope = try await post(
                    base.appendingPathComponent("dashboard/api/steering/settings"),
                    body: ["session_ttl_minutes": normalized]
                )
                guard response.ok else { throw URLError(.badServerResponse) }
                settings.steeringSessionMinutes = max(1, response.sessionTTLMinutes)
                steeringStatus = "Sessions stay visible for \(response.sessionTTLMinutes) min after activity."
                await refresh()
            } catch {
                steeringStatus = "Duration saved; it will apply when the server next starts."
            }
        }
    }

    private func restorePendingSteeringSubmission() {
        guard let restored = PendingSteeringSubmissionStore.load() else { return }
        pendingSteeringClientInstructionID = restored.clientInstructionID
        pendingSteeringSessionID = restored.sessionID
        pendingSteeringTextHash = restored.textHash
        pendingSteeringGenerationID = restored.generationID
        pendingSteeringText = nil
        pendingSteeringRestoredFromDisk = true
        steeringStatus = "Checking the outcome of a steering send from the previous menu-app run…"
    }

    private func persistPendingSteeringSubmission(clientInstructionID: String, sessionID: String, textHash: String, generationID: String) {
        let state = PersistedPendingSteeringSubmission(
            schemaVersion: PendingSteeringSubmissionStore.schemaVersion,
            clientInstructionID: clientInstructionID,
            sessionID: sessionID,
            textHash: textHash,
            generationID: generationID,
            createdAt: Date().timeIntervalSince1970
        )
        try? PendingSteeringSubmissionStore.save(state)
    }

    private func steeringClientInstructionID(sessionID: String, text: String, generationID: String) -> String {
        let textHash = PendingSteeringSubmissionStore.textHash(text)
        if pendingSteeringSessionID == sessionID,
           pendingSteeringGenerationID == generationID,
           (pendingSteeringText == text || pendingSteeringTextHash == textHash),
           let existing = pendingSteeringClientInstructionID {
            pendingSteeringText = text
            pendingSteeringTextHash = textHash
            return existing
        }
        let created = UUID().uuidString.lowercased()
        pendingSteeringClientInstructionID = created
        pendingSteeringSessionID = sessionID
        pendingSteeringText = text
        pendingSteeringTextHash = textHash
        pendingSteeringGenerationID = generationID
        pendingSteeringRestoredFromDisk = false
        persistPendingSteeringSubmission(
            clientInstructionID: created,
            sessionID: sessionID,
            textHash: textHash,
            generationID: generationID
        )
        return created
    }

    private func clearPendingSteeringSubmission() {
        pendingSteeringClientInstructionID = nil
        pendingSteeringSessionID = nil
        pendingSteeringText = nil
        pendingSteeringTextHash = nil
        pendingSteeringGenerationID = nil
        pendingSteeringRestoredFromDisk = false
        PendingSteeringSubmissionStore.clear()
    }

    private func recoverSteeringMessageID(clientInstructionID: String, sessionID: String) -> String? {
        steeringRecent.first(where: {
            $0.clientInstructionID == clientInstructionID && $0.sessionID == sessionID
        })?.id
    }

    func sendSteering() {
        let text = steeringPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        guard let sessionID = selectedSteeringSessionID else {
            steeringStatus = steeringSessions.count > 1 ? "Choose an agent session first." : "No agent sessions yet."
            return
        }
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        guard let generationID = steeringGenerationID, !generationID.isEmpty else {
            steeringStatus = "Refreshing server generation before sending…"
            Task { await refresh() }
            return
        }
        let clientInstructionID = steeringClientInstructionID(sessionID: sessionID, text: text, generationID: generationID)
        steeringSending = true
        steeringStatus = "Sending…"
        Task {
            defer { steeringSending = false }
            var accepted: SteeringSendEnvelope.Message?
            var definitiveError: SteeringPostError?
            var lastAmbiguousError: Error?

            for attempt in 0..<2 {
                do {
                    let response = try await postSteering(
                        base.appendingPathComponent("dashboard/api/steering"),
                        body: [
                            "session_id": sessionID,
                            "text": text,
                            "client_instruction_id": clientInstructionID,
                            "generation_id": generationID,
                        ]
                    )
                    guard response.ok, let message = response.message else {
                        definitiveError = .server(status: 500, code: "invalid_response", reason: nil)
                        break
                    }
                    accepted = message
                    break
                } catch let error as SteeringPostError {
                    switch error {
                    case let .server(status, _, _):
                        if status >= 500 && attempt == 0 {
                            lastAmbiguousError = error
                            continue
                        }
                        if status >= 500 {
                            lastAmbiguousError = error
                        } else {
                            definitiveError = error
                        }
                    }
                    break
                } catch {
                    lastAmbiguousError = error
                    if attempt == 0 { continue }
                    break
                }
            }

            if let message = accepted {
                lastSteeringMessageID = message.id
                steeringPrompt = ""
                clearPendingSteeringSubmission()
                if message.sessionState == "idle" {
                    steeringStatus = message.idempotentReplay == true
                        ? "Recovered queued steering. The agent's next tool will be preempted."
                        : "Queued. The agent's next tool will be preempted."
                } else {
                    steeringStatus = message.idempotentReplay == true
                        ? "Recovered queued steering for the running agent task."
                        : "Queued for the running agent task."
                }
                await refresh()
                return
            }

            if let definitiveError {
                switch definitiveError {
                case let .server(_, code, reason):
                    switch code {
                    case "session_closed":
                        await refresh()
                        if let recoveredID = recoverSteeringMessageID(
                            clientInstructionID: clientInstructionID,
                            sessionID: sessionID
                        ) {
                            lastSteeringMessageID = recoveredID
                            steeringPrompt = ""
                            clearPendingSteeringSubmission()
                            steeringStatus = "The agent session ended after the steering instruction was accepted."
                        } else {
                            steeringStatus = "That agent session ended before the prompt could be queued."
                            clearPendingSteeringSubmission()
                        }
                        return
                    case "queue_full":
                        steeringStatus = "That agent already has too many queued steering messages."
                        clearPendingSteeringSubmission()
                    case "stale_generation":
                        steeringStatus = "Server restarted after the steering result became uncertain. Delivery outcome is unknown; review and send again to create a new instruction."
                        clearPendingSteeringSubmission()
                    case "idempotency_expired":
                        steeringStatus = "The safe retry window for that steering instruction expired. Delivery outcome is unknown; review and send again to create a new instruction."
                        clearPendingSteeringSubmission()
                    case "idempotency_conflict":
                        steeringStatus = reason == "client_instruction_id_belongs_to_another_session"
                            ? "The steering retry key no longer belongs to this agent session."
                            : "The steering retry conflicted with an earlier instruction. Edit the prompt and send again."
                        clearPendingSteeringSubmission()
                    default:
                        steeringStatus = "Could not queue steering message (\(code))."
                        clearPendingSteeringSubmission()
                    }
                }
                await refresh()
                return
            }

            if lastAmbiguousError != nil {
                await refresh()
                if pendingSteeringClientInstructionID == nil && steeringPrompt.isEmpty {
                    return
                }
                if let recoveredID = recoverSteeringMessageID(
                    clientInstructionID: clientInstructionID,
                    sessionID: sessionID
                ) {
                    lastSteeringMessageID = recoveredID
                    steeringPrompt = ""
                    clearPendingSteeringSubmission()
                    steeringStatus = "Recovered steering after a network timeout."
                } else {
                    steeringStatus = "Network result is uncertain. Send again to retry safely without duplicating the instruction."
                }
            }
        }
    }

    var steeringSessionGroups: SteeringSessionGroups {
        SteeringSessionGrouping.groups(
            sessions: steeringSessions,
            recentEvents: steeringRecent,
            retentionMinutes: settings.steeringSessionMinutes
        )
    }

    var sessionNeedsAttentionCount: Int {
        let groups = steeringSessionGroups
        return groups.needsAttention.count + groups.terminalAttention.count
    }

    var sessionActiveCount: Int { steeringSessionGroups.active.count }

    var hasReliableSessionSignal: Bool {
        !steeringSnapshotStale && connectionState != .disconnected
    }

    var steeringEmptyMessage: String {
        if let event = steeringRecent.first(where: { $0.kind == "session" }) {
            switch event.effectiveLifecycleState {
            case .expired: return "Last agent session expired."
            case .disconnected: return "Last transport session disconnected."
            default: break
            }
        }
        return "No agent sessions yet."
    }

    func applySteeringSnapshot(_ envelope: SteeringEnvelope) {
        let previousGenerationID = steeringGenerationID
        setIfChanged(\.steeringGenerationID, envelope.generationID)
        if let pendingGenerationID = pendingSteeringGenerationID,
           let currentGenerationID = envelope.generationID,
           pendingGenerationID != currentGenerationID {
            clearPendingSteeringSubmission()
            setIfChanged(\.steeringStatus, "Server restarted while a steering result was uncertain. Delivery outcome is unknown; review and send again to create a new instruction.")
        } else if previousGenerationID != nil,
                  let currentGenerationID = envelope.generationID,
                  previousGenerationID != currentGenerationID,
                  lastSteeringMessageID != nil {
            lastSteeringMessageID = nil
            setIfChanged(\.steeringStatus, "Server restarted; the previous steering lifecycle can no longer be confirmed.")
        }
        let sessionDiff = SteeringSessionGrouping.diff(current: steeringSessions, incoming: envelope.sessions)
        if sessionDiff.hasChanges { setIfChanged(\.steeringSessions, envelope.sessions) }
        setIfChanged(\.steeringRecent, envelope.recent)

        let sessionsForSelection = sessionDiff.hasChanges ? envelope.sessions : steeringSessions
        if sessionsForSelection.count == 1 {
            setIfChanged(\.selectedSteeringSessionID, sessionsForSelection[0].sessionID)
        } else if let selectedSteeringSessionID, !sessionsForSelection.contains(where: { $0.sessionID == selectedSteeringSessionID }) {
            setIfChanged(\.selectedSteeringSessionID, nil)
        }

        var recoveredPendingThisSnapshot = false
        if lastSteeringMessageID == nil,
           let clientInstructionID = pendingSteeringClientInstructionID,
           let pendingSessionID = pendingSteeringSessionID {
            if let recovered = envelope.recent.first(where: {
                $0.clientInstructionID == clientInstructionID && $0.sessionID == pendingSessionID
            }) {
                let restoredFromDisk = pendingSteeringRestoredFromDisk
                lastSteeringMessageID = recovered.id
                recoveredPendingThisSnapshot = true
                steeringPrompt = ""
                clearPendingSteeringSubmission()
                setIfChanged(
                    \.steeringStatus,
                    restoredFromDisk
                        ? "Recovered steering after the menu app relaunched."
                        : "Recovered steering after a delayed response."
                )
            } else if pendingSteeringRestoredFromDisk {
                let liveSessionStillExists = sessionsForSelection.contains(where: { $0.sessionID == pendingSessionID })
                setIfChanged(
                    \.steeringStatus,
                    liveSessionStillExists
                        ? "Previous steering outcome is uncertain after relaunch. Re-enter the same prompt to retry with its original idempotency key."
                        : "Previous steering outcome could not be confirmed because that agent session is no longer active."
                )
            }
        }

        if !recoveredPendingThisSnapshot,
           let messageID = lastSteeringMessageID,
           let recent = envelope.recent.first(where: { $0.id == messageID }) {
            switch recent.effectiveLifecycleState {
            case .queued:
                let text = sessionsForSelection.first(where: { $0.sessionID == recent.sessionID })?.isWorking == true
                    ? "Queued for the running agent task."
                    : "Queued. The agent's next tool will be preempted."
                setIfChanged(\.steeringStatus, text)
            case .delivered:
                setIfChanged(\.steeringStatus, recent.status == "preempted"
                    ? "Delivered before the next tool; that tool was not executed."
                    : "Delivered with the running tool result.")
                lastSteeringMessageID = nil
            case .acknowledged:
                setIfChanged(\.steeringStatus, "Acknowledged by the agent.")
                lastSteeringMessageID = nil
            case .failed:
                setIfChanged(\.steeringStatus, "Delivery failed; instruction will retry on the next tool call.")
            case .disconnected:
                setIfChanged(\.steeringStatus, "Agent session ended before acknowledgement.")
                lastSteeringMessageID = nil
            case .expired:
                setIfChanged(\.steeringStatus, "Session expired before acknowledgement.")
                lastSteeringMessageID = nil
            default:
                break
            }
        } else if sessionsForSelection.isEmpty && lastSteeringMessageID == nil {
            setIfChanged(\.steeringStatus, steeringEmptyMessage)
        } else if sessionsForSelection.count > 1 && selectedSteeringSessionID == nil {
            setIfChanged(\.steeringStatus, "Multiple agent sessions are available — choose the one you want to steer.")
        }
    }

    private func runAction(title: String, args: [String]) {
        guard busyAction == nil else { return }
        busyAction = title
        actionNotice = nil
        noticeTask?.cancel()
        let cliPath = settings.cliPath
        let settingsPath = settings.path.path
        Task {
            let result = await Self.runCLI(args: args, configuredPath: cliPath, settingsPath: settingsPath)
            busyAction = nil
            showNotice(Self.notice(for: args, result: result))
            try? await Task.sleep(nanoseconds: 800_000_000)
            await refresh()
        }
    }

    private func showNotice(_ notice: ActionNotice) {
        actionNotice = notice
        noticeTask?.cancel()
        let noticeID = notice.id
        let delay: UInt64 = notice.kind == .error ? 8_000_000_000 : 5_000_000_000
        noticeTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: delay)
            guard !Task.isCancelled, let self, self.actionNotice?.id == noticeID else { return }
            self.actionNotice = nil
        }
    }

    nonisolated private static func notice(for args: [String], result: (code: Int32, output: String)) -> ActionNotice {
        let output = result.output.lowercased()
        let updateCheck = args == ["update", "--check"]
        let failed = result.code != 0 && !(updateCheck && result.code == 2)
        if failed {
            let message: String
            switch args.first {
            case "start": message = "Couldn’t start the server."
            case "stop": message = "Couldn’t stop the server."
            case "restart": message = "Couldn’t restart the server."
            case "update": message = "Update failed."
            default: message = "Action failed."
            }
            return ActionNotice(kind: .error, message: message)
        }

        if updateCheck {
            if output.contains("update available") { return ActionNotice(kind: .update, message: "Update available.") }
            if output.contains("blocked") { return ActionNotice(kind: .error, message: "Update is blocked by local changes.") }
            return ActionNotice(kind: .success, message: "Mac MCP is up to date.")
        }
        switch args.first {
        case "start":
            return output.contains("already running")
                ? ActionNotice(kind: .info, message: "Server is already running.")
                : ActionNotice(kind: .success, message: "Server started.")
        case "stop":
            return output.contains("not running")
                ? ActionNotice(kind: .info, message: "Server is already stopped.")
                : ActionNotice(kind: .success, message: "Server stopped.")
        case "restart": return ActionNotice(kind: .success, message: "Server restarted.")
        case "update": return ActionNotice(kind: .success, message: "Mac MCP updated successfully.")
        default: return ActionNotice(kind: .success, message: "Done.")
        }
    }

    private static func dashboardTokenURL() -> URL {
        let env = ProcessInfo.processInfo.environment
        if let configured = env["MAC_MCP_DASHBOARD_TOKEN_FILE"], !configured.isEmpty {
            return URL(fileURLWithPath: NSString(string: configured).expandingTildeInPath)
        }
        let stateDirectory: URL
        if let configured = env["MAC_MCP_STATE_DIR"], !configured.isEmpty {
            stateDirectory = URL(fileURLWithPath: NSString(string: configured).expandingTildeInPath)
        } else {
            stateDirectory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".mac-mcp")
        }
        return stateDirectory.appendingPathComponent("dashboard-token")
    }

    private func dashboardToken() -> String? {
        guard let data = try? Data(contentsOf: Self.dashboardTokenURL()),
              let value = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { return nil }
        return value
    }

    func authorizeDashboardRequest(_ request: inout URLRequest) {
        if let token = dashboardToken() {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
    }

    private func fetchActivityEvents(base: URL, poll: Bool) async throws -> EventsEnvelope? {
        guard poll else { return nil }
        let envelope: EventsEnvelope = try await fetch(
            base.appendingPathComponent("dashboard/api/events"),
            query: ["hours": "1", "limit": String(Self.recentActivityLimit)]
        )
        return envelope
    }

    private func fetch<T: Decodable>(
        _ url: URL,
        query: [String: String],
        timeout: TimeInterval = 1.8
    ) async throws -> T {
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        var request = URLRequest(url: components.url!)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = timeout
        authorizeDashboardRequest(&request)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200..<300).contains(http.statusCode) else { throw DashboardAPIError.httpStatus(http.statusCode) }
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func postSteering(_ url: URL, body: [String: Any]) async throws -> SteeringSendEnvelope {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 2.0
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorizeDashboardRequest(&request)
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200..<300).contains(http.statusCode) else {
            let envelope = try? JSONDecoder().decode(SteeringErrorEnvelope.self, from: data)
            throw SteeringPostError.server(
                status: http.statusCode,
                code: envelope?.error ?? "http_\(http.statusCode)",
                reason: envelope?.reason
            )
        }
        return try JSONDecoder().decode(SteeringSendEnvelope.self, from: data)
    }

    private func post<T: Decodable>(_ url: URL, body: [String: Any], timeout: TimeInterval = 2.0) async throws -> T {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorizeDashboardRequest(&request)
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200..<300).contains(http.statusCode) else { throw DashboardAPIError.httpStatus(http.statusCode) }
        return try JSONDecoder().decode(T.self, from: data)
    }

    nonisolated private static func cloudflareCredentialFileIsSecure() -> Bool {
        let environment = ProcessInfo.processInfo.environment
        let path: String
        if let configured = environment["CLOUDFLARE_TUNNEL_TOKEN_FILE"], !configured.isEmpty {
            path = NSString(string: configured).expandingTildeInPath
        } else {
            let stateDirectory: String
            if let configured = environment["MAC_MCP_STATE_DIR"], !configured.isEmpty {
                stateDirectory = NSString(string: configured).expandingTildeInPath
            } else {
                stateDirectory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".mac-mcp").path
            }
            path = URL(fileURLWithPath: stateDirectory).appendingPathComponent("cloudflare-tunnel-token").path
        }
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: path),
              attributes[.type] as? FileAttributeType == .typeRegular,
              let permissions = attributes[.posixPermissions] as? NSNumber,
              permissions.intValue == 0o600,
              let owner = attributes[.ownerAccountID] as? NSNumber,
              owner.uint32Value == getuid() else { return false }
        return true
    }

    nonisolated private static func processExists(matching needle: String) -> Bool {
        let proc = Process(); proc.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep"); proc.arguments = ["-f", needle]
        proc.standardOutput = FileHandle.nullDevice; proc.standardError = FileHandle.nullDevice
        do { try proc.run(); proc.waitUntilExit(); return proc.terminationStatus == 0 } catch { return false }
    }

    nonisolated private static func updaterProcessMatches(_ pid: pid_t) -> Bool {
        guard pid > 0 else { return false }
        if Darwin.kill(pid, 0) != 0 && errno != EPERM { return false }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-p", String(pid), "-o", "command="]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return false }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            guard let command = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
                !command.isEmpty else { return false }
            let lower = command.lowercased()
            let cliUpdate = lower.contains("mac-mcp") && lower.contains(" update")
            let detachedHelper = lower.contains("update_helper.py")
                && lower.contains("--repo")
                && lower.contains("--runtime")
            return cliUpdate || detachedHelper
        } catch {
            return false
        }
    }

    nonisolated private static func runCLI(args: [String], configuredPath: String, settingsPath: String, input: String? = nil) async -> (code: Int32, output: String) {
        await Task.detached(priority: .userInitiated) {
            let process = Process()
            let home = FileManager.default.homeDirectoryForCurrentUser
            let candidates = [
                configuredPath,
                ProcessInfo.processInfo.environment["MAC_MCP_CLI_PATH"] ?? "",
                home.appendingPathComponent(".local/bin/mac-mcp").path,
                home.appendingPathComponent("scripts/mac-mcp").path,
                home.appendingPathComponent("mac-mcp/.venv/bin/mac-mcp").path,
                home.appendingPathComponent("Projects/mac-mcp/.venv/bin/mac-mcp").path,
                "/opt/homebrew/bin/mac-mcp",
            ].filter { !$0.isEmpty }
            if let executable = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) {
                process.executableURL = URL(fileURLWithPath: executable)
                process.arguments = args
            } else {
                process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                process.arguments = ["mac-mcp"] + args
            }
            var environment = ProcessInfo.processInfo.environment
            environment["MAC_MCP_SKIP_MENU_APP"] = "1"
            environment["MAC_MCP_SETTINGS_PATH"] = settingsPath
            process.environment = environment
            let pipe = Pipe(); process.standardOutput = pipe; process.standardError = pipe
            let inputPipe = input == nil ? nil : Pipe()
            process.standardInput = inputPipe ?? FileHandle.nullDevice
            do {
                try process.run()
                if let input, let inputPipe {
                    if let data = input.data(using: .utf8) { inputPipe.fileHandleForWriting.write(data) }
                    try? inputPipe.fileHandleForWriting.close()
                }
                process.waitUntilExit()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
            } catch { return (127, error.localizedDescription) }
        }.value
    }
}
