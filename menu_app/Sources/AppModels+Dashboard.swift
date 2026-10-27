import AppKit
import Combine
import CryptoKit
import Darwin
import Foundation
import SafariServices

// Dashboard, usage, memory and permission API models (split out of AppState.swift).

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
    let memorySkills: MemorySkillUsage?
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
        case memorySkills = "memory_skills"
    }
}

/// What memory and skill calls came to (#110): outcomes per tool and the skills loaded most.
struct MemorySkillUsage: Decodable, Equatable {
    let tools: [String: [String: Int]]
    let skillsLoaded: [LoadedSkill]

    struct LoadedSkill: Decodable, Equatable {
        let name: String
        let count: Int
    }

    enum CodingKeys: String, CodingKey {
        case tools
        case skillsLoaded = "skills_loaded"
    }

    func count(_ tool: String, _ outcomes: String...) -> Int {
        let byOutcome = tools[tool] ?? [:]
        return outcomes.isEmpty ? byOutcome.values.reduce(0, +) : outcomes.reduce(0) { $0 + (byOutcome[$1] ?? 0) }
    }

    var isEmpty: Bool { tools.values.allSatisfy { $0.values.allSatisfy { $0 == 0 } } }
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

struct AgentCancelEnvelope: Decodable, Equatable {
    struct Cancellation: Decodable, Equatable {
        let state: String?
    }

    let ok: Bool
    let status: String?
    let cancellation: Cancellation?
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
    /// How many of these tokens saved teams used (Usage › Teams); nil from a server before teams were kept.
    let teamTokens: Int?

    enum CodingKeys: String, CodingKey {
        case provider, available, reason, turns, agents, source, sources, models
        case teamTokens = "team_tokens"
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
    let privacy: PrivacyFlags?
    enum CodingKeys: String, CodingKey {
        case version
        case totalCalls = "total_calls"
        case successRate = "success_rate"
        case activeAgents = "active_agents"
        case privacy
    }
}

/// Settings › Privacy switches the menu shows and flips (the server enforces them).
struct PrivacyFlags: Decodable, Equatable {
    let paused: Bool
    let privateMode: Bool
    enum CodingKeys: String, CodingKey {
        case paused
        case privateMode = "private_mode"
    }
}

struct BrowserContext: Decodable, Equatable, Sendable {
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

struct ToolEvent: Decodable, Identifiable, Equatable, Sendable {
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

/// One browser tab an agent or chat is using, or one the owner took over (#124).
struct BrowserTabActivity: Decodable, Identifiable, Equatable, Sendable {
    let tabHandle: String
    let browser: String?
    let site: String?
    let action: String?
    let agentID: String?
    let teamID: String?
    let sessionID: String?
    let client: String?
    /// The agent's team or "Codex agent"; nil for a chat, which the app names itself.
    let who: String?
    /// The agent's own short task title.
    let task: String?
    let running: Bool
    let takenOver: Bool
    let canStop: Bool
    var id: String { tabHandle }

    enum CodingKeys: String, CodingKey {
        case browser, site, action, client, who, task, running
        case tabHandle = "tab_handle"
        case agentID = "agent_id"
        case teamID = "team_id"
        case sessionID = "session_id"
        case takenOver = "taken_over"
        case canStop = "can_stop"
    }

    /// "Chrome" or "Safari" for the row's second line.
    var browserName: String {
        switch (browser ?? "").lowercased() {
        case "google chrome", "chrome", "chromium": return "Chrome"
        case "safari": return "Safari"
        default: return browser ?? "Browser"
        }
    }
}

struct BrowserTabsEnvelope: Decodable {
    let ok: Bool
    let tabs: [BrowserTabActivity]
}

struct BrowserTakeOverEnvelope: Decodable {
    let ok: Bool
    let shown: Bool?
}

/// Where an agent's tab sits in the tab bar, for the tab pulse (#79); no frame when the tab is on show.
struct BrowserTabFrameEnvelope: Decodable {
    struct Frame: Decodable {
        let x: Double
        let y: Double
        let width: Double
        let height: Double
    }

    /// Where the tab was found, sent back to follow it cheaply while the pulse plays.
    struct Hint: Codable {
        let windowIndex: Int
        let tabIndex: Int
        let title: String

        enum CodingKeys: String, CodingKey {
            case title
            case windowIndex = "window_index"
            case tabIndex = "tab_index"
        }

        var body: [String: Any] { ["window_index": windowIndex, "tab_index": tabIndex, "title": title] }
    }

    let ok: Bool
    let active: Bool?
    let frame: Frame?
    let windowID: Int?
    let hint: Hint?

    enum CodingKeys: String, CodingKey {
        case ok, active, frame, hint
        case windowID = "window_id"
    }

    /// The tab button's AX frame and the browser window to sit above, when there is something to draw.
    var target: (frame: CGRect, windowID: Int)? {
        guard ok, active != true, let frame, let windowID, windowID > 0 else { return nil }
        return (CGRect(x: frame.x, y: frame.y, width: frame.width, height: frame.height), windowID)
    }
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
