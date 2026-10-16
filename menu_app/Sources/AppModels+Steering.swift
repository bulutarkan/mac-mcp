import AppKit
import Combine
import CryptoKit
import Darwin
import Foundation
import SafariServices

// Session steering models, grouping and pending-submission storage (split out of AppState.swift).

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

    var label: String {
        switch self {
        case .ready: return "Ready"
        case .queued: return "Steering queued"
        case .delivered: return "Steering delivered"
        case .acknowledged: return "Steering acknowledged"
        case .failed: return "Delivery not completed"
        case .disconnected: return "Ended"
        case .expired: return "Ended after idle"
        case .unknown: return "Status unavailable"
        }
    }

    /// Whether a prompt is in flight or failed, which the compact session row spells out.
    var isSteeringNotice: Bool {
        switch self {
        case .queued, .delivered, .acknowledged, .failed: return true
        default: return false
        }
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
    let toolCallCount: Int?
    let activityStartedAt: Double?
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
            && toolCallCount == other.toolCallCount
            && activityStartedAt == other.activityStartedAt
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
        case toolCallCount = "tool_call_count"
        case activityStartedAt = "activity_started_at"
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
    let active: [SteeringSession]
    let recent: [SteeringSession]
    /// Closed transport sessions are history, not an owner alert.
    let ended: [SteeringRecent]

    var totalItemCount: Int {
        active.count + recent.count + ended.count
    }

    var sectionCount: Int {
        var count = 0
        if !active.isEmpty { count += 1 }
        if !recent.isEmpty { count += 1 }
        if !ended.isEmpty { count += 1 }
        return count
    }
}

struct SteeringSessionGrouping {
    static func section(for session: SteeringSession) -> SteeringSessionSection {
        // Steering has no user-response state. Queued and delivered messages continue
        // automatically, so only an in-flight MCP call is active work. Everything else
        // is neutral session history, including delivery failures and terminal states.
        if session.effectiveActivityState == .working || session.activeCalls > 0 {
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

        let active = sessions
            .filter { section(for: $0) == .active }
            .sorted {
                if $0.isWorking != $1.isWorking { return $0.isWorking && !$1.isWorking }
                return $0.lastActivityAt > $1.lastActivityAt
            }
        let recent = sessions
            .filter { section(for: $0) == .recent && $0.lastActivityAt >= cutoff }
            .sorted { $0.lastActivityAt > $1.lastActivityAt }

        var seenEndedSessionIDs = Set<String>()
        let ended = recentEvents.filter { event in
            guard event.kind == "session", !liveSessionIDs.contains(event.sessionID) else { return false }
            guard let transitionedAt = event.transitionedAt, transitionedAt >= cutoff else { return false }
            return seenEndedSessionIDs.insert(event.sessionID).inserted
        }

        return SteeringSessionGroups(
            active: active,
            recent: recent,
            ended: ended
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
