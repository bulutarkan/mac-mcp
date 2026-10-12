import AppKit
import Combine
import CryptoKit
import Darwin
import Foundation
import SafariServices

// Update check, progress and outcome models (split out of AppState.swift).

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

struct PackageInstallUpdateNotice: Decodable, Equatable {
    let installKind: String
    let message: String
    let downloadURL: String

    enum CodingKeys: String, CodingKey {
        case message
        case installKind = "install_kind"
        case downloadURL = "download_url"
    }
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
