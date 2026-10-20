import AppKit
import Combine
import CryptoKit
import Darwin
import Foundation
import SafariServices

enum DashboardConnectionState: String, Equatable {
    case connecting
    case connected
    case degraded
    case disconnected
}

enum DashboardAPIError: Error {
    case httpStatus(Int)
}

/// The few argument fields used by the live-activity surfaces.  Keeping this
/// typed lets the SSE path decode directly from its bytes instead of making a
/// dictionary and serializing it back to JSON for `ToolEvent`.
private struct ToolActivityStreamArguments: Decodable, Sendable {
    let description: String?
    let browser: String?
    let app: String?
}

private struct ToolActivityStreamPayload: Decodable, Sendable {
    let kind: String
    let eventID: String?
    let tool: String?
    let status: String?
    let startedAt: Double?
    let sessionID: String?
    let agentID: String?
    let client: String?
    let teamID: String?
    let arguments: ToolActivityStreamArguments?
    let active: [ToolActivityStreamPayload]?
    /// `native_pointer` only: where a background native action lands (desktop agent cursor).
    let pointer: DesktopPointer?
    /// Decoded from this payload's decoder rather than reconstructed from an
    /// untyped dictionary.  `connected` payloads deliberately have no event.
    let toolEvent: ToolEvent?

    enum CodingKeys: String, CodingKey {
        case kind, tool, status, client, arguments, active
        case eventID = "event_id"
        case startedAt = "started_at"
        case sessionID = "session_id"
        case agentID = "agent_id"
        case teamID = "team_id"
        case x, y, pid, action, target, press
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        kind = try values.decode(String.self, forKey: .kind)
        eventID = try values.decodeIfPresent(String.self, forKey: .eventID)
        tool = try values.decodeIfPresent(String.self, forKey: .tool)
        status = try values.decodeIfPresent(String.self, forKey: .status)
        startedAt = try values.decodeIfPresent(Double.self, forKey: .startedAt)
        sessionID = try values.decodeIfPresent(String.self, forKey: .sessionID)
        agentID = try values.decodeIfPresent(String.self, forKey: .agentID)
        client = try values.decodeIfPresent(String.self, forKey: .client)
        teamID = try values.decodeIfPresent(String.self, forKey: .teamID)
        arguments = try values.decodeIfPresent(ToolActivityStreamArguments.self, forKey: .arguments)
        active = try values.decodeIfPresent([ToolActivityStreamPayload].self, forKey: .active)
        if kind == "native_pointer" {
            let x = try values.decodeIfPresent(Double.self, forKey: .x)
            let y = try values.decodeIfPresent(Double.self, forKey: .y)
            pointer = DesktopPointer(
                eventID: eventID,
                sessionID: sessionID,
                pid: try values.decodeIfPresent(Int32.self, forKey: .pid),
                point: x.flatMap { x in y.map { CGPoint(x: x, y: $0) } },
                action: try values.decodeIfPresent(String.self, forKey: .action) ?? "",
                target: try values.decodeIfPresent(String.self, forKey: .target) ?? "",
                press: try values.decodeIfPresent(Bool.self, forKey: .press) ?? false
            )
        } else {
            pointer = nil
        }
        toolEvent = try? ToolEvent(from: decoder)
    }
}

/// What the server knows about the Chrome companion (#188).
struct ChromeCompanionStatus: Equatable {
    var checked = false
    var connected = false
    var version: String?
    var needsReload = false
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
    /// Pause and private mode from Settings › Privacy, as last reported by the server.
    @Published private(set) var privacyFlags: PrivacyFlags?
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
    @Published private(set) var teamHistory: TeamHistoryEnvelope?
    @Published private(set) var teamHistoryLoading = false
    @Published private(set) var teamHistoryIssue: String?
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
    /// Advances on each successful agent refresh so failures age out while the menu is open.
    @Published private(set) var agentFailureReferenceTime = Date().timeIntervalSince1970
    @Published private(set) var agentTeams: [AgentTeamInfo] = []
    @Published private(set) var agentNotificationAuthorizationState: AgentNotificationAuthorizationState = .notDetermined
    @Published private(set) var agentNotificationPermissionChanging = false
    @Published private(set) var startAtLoginChanging = false
    @Published private(set) var onboardingStatus: OnboardingStatus?

    var onboardingChecklist: OnboardingChecklist {
        OnboardingChecklist.make(serverRunning: serverRunning, status: onboardingStatus)
    }

    /// The menu reminder needs one answer from the server first, so a returning owner whose
    /// server is briefly down never sees "0 of 4".
    var showsOnboardingReminder: Bool {
        onboardingStatus != nil
            && OnboardingChecklist.shouldShowReminder(onboardingChecklist, dismissed: settings.onboardingDismissed)
    }
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
    @Published var steeringStatus = "No conversations yet."
    @Published var steeringSending = false
    @Published var busyAction: String?
    @Published var actionNotice: ActionNotice?
    /// Agents whose Stop button was pressed and whose cancellation is still being confirmed.
    @Published var cancellingAgentIDs: Set<String> = []
    @Published private(set) var updateCheckInfo: UpdateCheckInfo?
    @Published private(set) var updateProgress: UpdateStateSnapshot?
    @Published private(set) var updateTransactionActive = false
    @Published private(set) var updateCheckLoading = false
    /// A package install's own update answer (#232); nil for install.sh and npm installs.
    @Published private(set) var packageUpdate: PackageInstallUpdateNotice?
    @Published var pulse = false
    @Published private(set) var safariExtensionEnabled = false
    @Published private(set) var safariExtensionRegistered = false
    @Published private(set) var safariExtensionStatus = "Checking…"
    @Published private(set) var chromeCompanion = ChromeCompanionStatus()
    @Published private(set) var chromeBrowserInstalled = true
    private var chromeCompanionWatch: Task<Void, Never>?

    let settings = SettingsStore()
    private var pollTask: Task<Void, Never>?
    /// Scheduled runs that never started (#225), checked once a minute apart from the main poll.
    private var scheduleWatchdog = ScheduledTaskWatchdog()
    private var scheduleWatchdogTask: Task<Void, Never>?
    /// Requests from other Macs already announced, so each one notifies once.
    private var announcedFleetRequests: Set<String> = []
    private var wakeObserver: NSObjectProtocol?
    /// The poll loop's current wait; cancelling it runs the next poll now.
    private var pollSleepTask: Task<Void, Never>?
    private var securitySemanticsFetchedAt: Date?
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
    private var updateWatchTask: Task<Void, Never>?
    private var consecutiveRefreshFailures = 0
    private var lastSteeringMessageID: String?
    private var pendingSteeringClientInstructionID: String?
    private var pendingSteeringSessionID: String?
    private var pendingSteeringText: String?
    private var pendingSteeringTextHash: String?
    private var pendingSteeringGenerationID: String?
    private var pendingSteeringRestoredFromDisk = false
    private let agentNotificationTracker = AgentNotificationTransitionTracker()
    /// Scheduled Tasks runs that ended after this time have not been announced yet.
    private var scheduledTaskNotifySince = Date().timeIntervalSince1970
    /// The live-activity island (#174) listens here; set by the app at launch.
    var onLiveToolCallStarted: ((LiveToolCall) -> Void)?
    var onLiveToolCallFinished: ((String, Bool) -> Void)?
    var onScheduledRunFinished: ((TaskRun, String) -> Void)?
    private var lastPublicProcessCheckAt = 0.0
    private static let activePollIntervalSeconds = 2.5
    private static let idlePollIntervalSeconds = 12.0
    /// Idle with the activity stream open: the stream shows new work, so polls can be rarer.
    private static let quietIdlePollIntervalSeconds = 30.0
    /// Permission semantics change only with settings; background polls re-read them at most this often.
    private static let securitySemanticsMaxAgeSeconds: TimeInterval = 60
    private static let ngrokProcessCheckIntervalSeconds = 30.0
    private static let publicProcessCheckIntervalSeconds = ngrokProcessCheckIntervalSeconds
    private static let maxRetryIntervalSeconds = 30.0

    init(startBackgroundTasks: Bool = true) {
        restorePendingSteeringSubmission()
        refreshSafariExtensionState()
        refreshCloudflareCredentialState()
        refreshPersistedUpdateState()
        if startBackgroundTasks {
            AgentNotificationController.shared.configure(
                openHandler: { [weak self] kind, targetID in
                    Task { @MainActor in
                        self?.openDashboardNotificationTarget(kind: kind, targetID: targetID)
                    }
                },
                updateHandler: { [weak self] in
                    Task { @MainActor in self?.showUpdate() }
                },
                fleetHandler: { [weak self] in
                    Task { @MainActor in self?.presentFleetRequests?() }
                }
            )
            startTasks()
            startUpdateWatch()
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
        updateWatchTask?.cancel()
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

    func refreshOnboarding() async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        guard let status: OnboardingStatus = try? await fetch(
            base.appendingPathComponent("dashboard/api/onboarding"), query: [:]
        ) else { return }
        setIfChanged(\.onboardingStatus, status)
        // Finished once: Get Started never comes back, even if something changes later.
        if serverRunning, onboardingChecklist.isComplete, !settings.onboardingDismissed {
            setOnboardingDismissed(true)
        }
    }

    // MARK: Privacy

    /// Flip pause or private mode from the menu; the server enforces it for every tool call.
    func setPrivacy(paused: Bool? = nil, privateMode: Bool? = nil) {
        var body: [String: Any] = [:]
        if let paused { body["paused"] = paused }
        if let privateMode { body["private_mode"] = privateMode }
        Task {
            do {
                _ = try await dashboardJSON("dashboard/api/privacy/settings", body: body)
                showNotice(ActionNotice(kind: .info, message: paused == true ? "Observation paused"
                    : paused == false ? "Observation resumed"
                    : privateMode == true ? "Private mode on: activity history is off" : "Private mode off"))
            } catch {
                showNotice(ActionNotice(kind: .error, message: serverRunning ? "Couldn’t change privacy. Try again."
                    : "Couldn’t change privacy: Mac MCP isn’t running."))
            }
            await refresh()
        }
    }

    // MARK: Start at login

    /// Keeps the menu bar app's login item in step with the setting; set by the app.
    var syncLoginItem: ((Bool) -> Void)?
    private let launchedAt = Date()
    /// Read once at launch: the server was meant to be running when the owner logged out.
    private lazy var serverIntendedRunningAtLaunch: Bool = Self.serverIntentIsRunning()

    /// Right after login the supervisor needs a minute to start the server: say so instead
    /// of reporting it unreachable.
    var startingAfterLogin: Bool {
        connectionState != .connected && connectionState != .degraded
            && Date().timeIntervalSince(launchedAt) < Self.loginStartGraceSeconds
            && settings.startAtLogin && serverIntendedRunningAtLaunch
    }

    private static let loginStartGraceSeconds: TimeInterval = 180

    private static func serverIntentIsRunning() -> Bool {
        let url = stateDirectoryURL().appendingPathComponent("server-intent.json")
        guard let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return object["desired"] as? String == "running"
    }

    // MARK: Onboarding tour (menu_app/Onboarding)

    private var tourOfferedThisLaunch = false
    /// Opens the tour window; set by the app (AppState stays free of window types).
    var presentTour: (() -> Void)?

    /// Open the tour once, on a launch where setup is unfinished and the tour was never shown.
    func maybeShowTourOnLaunch() {
        guard !tourOfferedThisLaunch, settings.onboardingTour == nil, !settings.onboardingDismissed else { return }
        if onboardingStatus != nil && onboardingChecklist.isComplete { return }
        tourOfferedThisLaunch = true
        presentTour?()
    }

    func showTour() {
        presentTour?()
    }

    func recordTour(_ outcome: String) {
        settings.onboardingTour = outcome
        try? settings.save()
    }

    struct OnboardingFailure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    func startServerForOnboarding() async -> Bool {
        let result = await Self.runCLI(args: lifecycleArgs("start"), configuredPath: settings.cliPath, settingsPath: settings.path.path)
        await refresh()
        return result.code == 0
    }

    func restartServerForOnboarding() async -> Bool {
        let result = await Self.runCLI(args: lifecycleArgs("restart") + ["--wait"], configuredPath: settings.cliPath, settingsPath: settings.path.path)
        await refresh()
        return result.code == 0
    }

    /// GET /dashboard/api/onboarding plus server_running, as the tour page reads it.
    func onboardingStatusPayload() async -> [String: Any] {
        guard var payload = try? await dashboardJSON("dashboard/api/onboarding", body: nil) as? [String: Any] else {
            return ["server_running": false]
        }
        setIfChanged(\.serverRunning, true)
        payload["server_running"] = true
        await refreshChromeCompanion()
        refreshSafariExtensionState(quiet: true)
        payload["browsers"] = browserCompanionStates
        return payload
    }

    func dashboardJSON(_ path: String, body: [String: Any]?) async throws -> Any {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            throw OnboardingFailure(message: "Mac MCP is not running.")
        }
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 15
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        authorizeDashboardRequest(&request)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw OnboardingFailure(message: "Mac MCP is not answering. Start it first.")
        }
        return try JSONSerialization.jsonObject(with: data)
    }

    func setOnboardingDismissed(_ dismissed: Bool) {
        settings.onboardingDismissed = dismissed
        do {
            try settings.save()
        } catch {
            settings.onboardingDismissed = !dismissed
            showNotice(ActionNotice(kind: .error, message: "Couldn’t save the Get Started setting."))
        }
    }

    /// The CLI saves the choice and moves the supervisor job in or out of ~/Library/LaunchAgents.
    func setStartAtLogin(_ enabled: Bool) {
        guard !startAtLoginChanging else { return }
        setIfChanged(\.startAtLoginChanging, true)
        let cliPath = settings.cliPath
        let settingsPath = settings.path.path
        Task {
            let result = await Self.runCLI(
                args: ["autostart", enabled ? "on" : "off", "--json"],
                configuredPath: cliPath,
                settingsPath: settingsPath
            )
            setIfChanged(\.startAtLoginChanging, false)
            if result.code == 0 {
                settings.startAtLogin = enabled
                syncLoginItem?(enabled)
            } else {
                showNotice(ActionNotice(kind: .error, message: "Couldn’t change start at login."))
            }
        }
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

    /// A notification for each finished Scheduled Tasks run whose task asks for one.
    private func announceScheduledTaskRuns() async {
        let result = await taskAPI("dashboard/api/scheduled-tasks/finished?since=\(scheduledTaskNotifySince)")
        guard result.ok else { return }
        if let now = result.object["now"] as? Double { scheduledTaskNotifySince = now }
        for raw in result.object["runs"] as? [[String: Any]] ?? [] {
            let run = TaskRun(raw)
            let name = raw["task_name"] as? String ?? "Scheduled task"
            onScheduledRunFinished?(run, name)
            guard (raw["notify"] as? Bool) ?? true else { continue }
            // Like agent notifications, never the prompt or the result itself: only how it ended.
            let title: String
            let body: String
            switch run.status {
            case "done" where run.checkFailed:
                title = run.verificationState == "unverified" ? "\(name) finished, but couldn’t be checked"
                    : "\(name) finished, but its check failed"
                let problem = run.checkProblem ?? "its check failed"
                body = problem.prefix(1).uppercased() + problem.dropFirst() + ". Details are in Scheduled Tasks."
            case "done":
                title = "\(name) finished"
                body = run.duration.map { "Done in \(TaskTime.duration($0)). The result is in Scheduled Tasks." } ?? "Done. The result is in Scheduled Tasks."
            case "skipped": title = "\(name) was skipped"; body = (run.reason ?? "It did not run") + "."
            case "timed_out": title = "\(name) stopped"; body = "It reached its time limit."
            default:
                title = "\(name) failed"
                body = run.exitCode != nil ? (run.reason ?? "It failed") + ". Details are in Scheduled Tasks." : "Details are in Scheduled Tasks."
            }
            _ = await AgentNotificationController.shared.scheduleMessage(title: title, body: String(body.prefix(240)))
        }
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

    /// Safari and Chrome companion states for the tour (#188): safari on|off|developer, chrome
    /// connected|reload|not_connected|not_installed.
    var browserCompanionStates: [String: String] {
        let safari = safariExtensionEnabled ? "on" : (safariExtensionRegistered ? "off" : "developer")
        let chrome: String
        if chromeCompanion.connected {
            chrome = chromeCompanion.needsReload ? "reload" : "connected"
        } else {
            chrome = chromeInstalled ? "not_connected" : "not_installed"
        }
        return ["safari": safari, "chrome": chrome]
    }

    /// Chrome or another Chromium build the companion loads in; looked up when the status refreshes.
    var chromeInstalled: Bool { chromeBrowserInstalled }

    private static let chromiumBrowsers = [
        "com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.dev", "com.google.Chrome.canary",
        "org.chromium.Chromium",
    ]

    private static func chromiumBrowserInstalled() -> Bool {
        chromiumBrowsers.contains { NSWorkspace.shared.urlForApplication(withBundleIdentifier: $0) != nil }
    }

    /// The server's view of the Chrome companion; the last known state stays if the server does not answer.
    func refreshChromeCompanion() async {
        let installed = Self.chromiumBrowserInstalled()
        if installed != chromeBrowserInstalled { chromeBrowserInstalled = installed }
        let result = await taskAPI("dashboard/api/diagnostics/runtime")
        guard result.ok else { return }
        let raw = result.object["chrome_companion"] as? [String: Any]
        let next = ChromeCompanionStatus(
            checked: true,
            connected: raw?["connected"] as? Bool ?? (result.object["chrome_companion_connected"] as? Bool ?? false),
            version: raw?["version"] as? String,
            needsReload: raw?["needs_reload"] as? Bool ?? false
        )
        if next != chromeCompanion { chromeCompanion = next }
    }

    func refreshSafariExtensionState(quiet: Bool = false) {
        let previous = browserSettingsState
        if !quiet { setIfChanged(\.browserSettingsState, .loading(lastUpdatedAt: previous.lastUpdatedAt)) }
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
        let reload = chromeCompanion.connected && chromeCompanion.needsReload
        if !reload {
            if FileManager.default.fileExists(atPath: manifestURL.path) {
                NSWorkspace.shared.activateFileViewerSelecting([manifestURL])
            }
            // Load unpacked opens a folder picker: ⌘⇧G and ⌘V go straight to it.
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(extensionSourceURL.path, forType: .string)
        }
        if let chromeURL = Self.chromiumBrowsers.lazy.compactMap({ NSWorkspace.shared.urlForApplication(withBundleIdentifier: $0) }).first {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            configuration.arguments = ["chrome://extensions/"]
            NSWorkspace.shared.openApplication(at: chromeURL, configuration: configuration)
        }
        actionNotice = ActionNotice(
            kind: .info,
            message: reload
                ? "Chrome: at chrome://extensions, press the reload arrow on Mac MCP Chrome Companion. Mac MCP sees it within a few seconds."
                : "Chrome: turn on Developer mode at chrome://extensions, choose Load unpacked, press ⌘⇧G and ⌘V (the folder is copied), then Select. Mac MCP sees the companion within a few seconds."
        )
        watchChromeCompanion()
    }

    /// After Chrome Setup, notice the companion connecting instead of asking the owner to check.
    private func watchChromeCompanion() {
        chromeCompanionWatch?.cancel()
        chromeCompanionWatch = Task { [weak self] in
            for _ in 0..<90 {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let self, !Task.isCancelled else { return }
                await self.refreshChromeCompanion()
                if self.chromeCompanion.connected && !self.chromeCompanion.needsReload {
                    let version = self.chromeCompanion.version.map { " (\($0))" } ?? ""
                    self.actionNotice = ActionNotice(kind: .success, message: "Chrome companion connected\(version).")
                    return
                }
            }
        }
    }

    static func pollDelaySeconds(forFailureCount failureCount: Int) -> Double {
        guard failureCount > 0 else { return activePollIntervalSeconds }
        return min(pow(2.0, Double(failureCount - 1)), maxRetryIntervalSeconds)
    }

    private var hasActiveWork: Bool {
        activeAgents > 0 || sessionActiveCount > 0
    }

    private var successfulPollIntervalSeconds: Double {
        if hasActiveWork { return Self.activePollIntervalSeconds }
        return toolActivityStreamConnected ? Self.quietIdlePollIntervalSeconds : Self.idlePollIntervalSeconds
    }

    /// Mirrors System Settings > Accessibility > Display > Reduce motion.
    @Published private(set) var reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    private var reduceMotionObserver: NSObjectProtocol?

    private var shouldPulse: Bool {
        // With Reduce Motion the menu bar keeps the static active symbol instead of blinking.
        hasActiveWork && !reduceMotion
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
        if startingAfterLogin { return "Starting…" }
        switch connectionState {
        case .connecting: return "Connecting…"
        case .connected: return "Server running · v\(version)"
        case .degraded: return "Server running · degraded"
        case .disconnected: return "Disconnected"
        }
    }

    var connectionBannerTitle: String {
        if startingAfterLogin { return "Starting Mac MCP…" }
        switch connectionState {
        case .connecting: return "Connecting to Mac MCP…"
        case .connected: return "Connected"
        case .degraded: return "Some dashboard data is unavailable"
        case .disconnected: return "Mac MCP server is unreachable"
        }
    }

    var connectionBannerDetail: String {
        if startingAfterLogin { return "It starts on its own after you log in. This can take a minute." }
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
        startScheduleWatchdog()
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            var first = true
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh(background: true)
                if first {
                    first = false
                    self.maybeShowTourOnLaunch()
                }
                let delay = max(0.25, self.nextPollDelaySeconds)
                let sleeper = Task { _ = try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
                self.pollSleepTask = sleeper
                await sleeper.value
                self.pollSleepTask = nil
            }
        }
    }

    private func startScheduleWatchdog() {
        guard scheduleWatchdogTask == nil else { return }
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.scheduleWatchdog.lastWakeAt = Date() }
        }
        scheduleWatchdogTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(ScheduledTaskWatchdog.checkEvery * 1_000_000_000))
                guard let self else { return }
                await self.checkScheduledRunsStarted()
            }
        }
    }

    /// Asks Mac MCP which runs are past their time without starting; when it does not answer, uses what
    /// it said was due. Says so once per run and never starts anything itself.
    private func checkScheduledRunsStarted() async {
        let now = Date()
        let result = await taskAPI("dashboard/api/scheduled-tasks/liveness")
        let alerts: [ScheduledTaskWatchdog.Alert]
        if result.ok {
            func items(_ key: String, _ field: String) -> [ScheduledTaskWatchdog.Due] {
                (result.object[key] as? [[String: Any]] ?? []).compactMap { row in
                    guard let id = row["id"] as? String, let at = row[field] as? Double else { return nil }
                    return .init(id: id, name: row["name"] as? String ?? "A scheduled task",
                                 at: Date(timeIntervalSince1970: at), notify: row["notify"] as? Bool ?? true)
                }
            }
            alerts = scheduleWatchdog.answered(now: now, overdue: items("overdue", "expected_at"),
                                               dueSoon: items("due_soon", "at"),
                                               tolerance: result.object["tolerance_s"] as? Double ?? 300)
        } else {
            alerts = scheduleWatchdog.unanswered(now: now, intendedRunning: Self.serverIntentIsRunning(), tolerance: 300)
        }
        for alert in alerts where alert.task.notify {
            let clock = alert.task.at.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))
            let (title, body): (String, String) = alert.kind == .notStarted
                ? ("\(alert.task.name) didn't start",
                   "It was due at \(clock), but Mac MCP's scheduler did not start it. Run it now from Scheduled Tasks.")
                : ("\(alert.task.name) may not have run",
                   "It was due at \(clock), but Mac MCP isn't answering.")
            _ = await AgentNotificationController.shared.scheduleMessage(title: title, body: body)
        }
    }

    /// A Mac asking to connect gets one native notification; clicking it opens Settings › Devices, where
    /// the request waits. Answered or expired requests take theirs back (#147).
    func checkFleetRequests() async {
        let result = await taskAPI("dashboard/api/fleet/requests")
        guard result.ok else { return }
        let rows = result.object["requests"] as? [[String: Any]] ?? []
        let pending = Set(rows.compactMap { $0["request_id"] as? String })
        AgentNotificationController.shared.removeFleetRequests(keeping: pending)
        announcedFleetRequests.formIntersection(pending)
        for row in rows {
            guard let id = row["request_id"] as? String, !announcedFleetRequests.contains(id) else { continue }
            announcedFleetRequests.insert(id)
            await AgentNotificationController.shared.scheduleFleetRequest(
                id: id, name: row["client_name"] as? String ?? "Another Mac",
                permission: row["permission"] as? String ?? "status", checkCode: row["check_code"] as? String ?? "")
        }
    }

    /// Run the next background poll now instead of at the end of an idle wait.
    private func wakePoll() {
        pollSleepTask?.cancel()
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

    nonisolated static func toolEvent(from data: Data) -> ToolEvent? {
        try? JSONDecoder().decode(ToolEvent.self, from: data)
    }

    nonisolated private static func toolActivityPayload(from data: Data) -> ToolActivityStreamPayload? {
        try? JSONDecoder().decode(ToolActivityStreamPayload.self, from: data)
    }

    func restartToolActivityStream() {
        toolActivityStreamTask?.cancel()
        toolActivityStreamTask = nil
        toolActivityStreamConnected = false

        // The stream also keeps the activity list current and lets idle polls be rarer, so it
        // runs with the bubble off; only the bubble itself follows the setting.
        if !(settings.requireToolDescriptions && settings.showToolActivity) {
            ToolActivityBubbleController.shared.hideImmediately()
        }

        toolActivityStreamTask = Task { [weak self] in
            await self?.consumeToolActivityStream()
        }
    }

    private func consumeToolActivityStream() async {
        while !Task.isCancelled {
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
                    guard let data = json.data(using: .utf8) else { continue }
                    // This loop is AppState-isolated, so keep JSON decoding off
                    // the main actor.  After the await, only state application
                    // runs on the main actor.
                    let payload = await Task.detached(priority: .utility) {
                        Self.toolActivityPayload(from: data)
                    }.value
                    guard let payload else { continue }
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

    private func handleToolActivityPayload(_ payload: ToolActivityStreamPayload) {
        let kind = payload.kind

        if kind == "native_pointer" {
            if let pointer = payload.pointer, settings.showDesktopCursor {
                DesktopCursorOverlayController.shared.show(pointer, agent: desktopCursorAgentLabel(pointer.sessionID))
            }
            return
        }

        if kind == "fleet_request" || kind == "fleet_request_closed" {
            Task { await checkFleetRequests() }
            return
        }

        if kind == "connected" {
            // Also catches a Mac that asked while the stream was down.
            Task { await checkFleetRequests() }
            ToolActivityBubbleController.shared.hideImmediately()
            guard let active = payload.active else { return }
            setIfChanged(\.activeEvents, active.compactMap(\.toolEvent))
            for event in active.reversed() {
                presentToolActivityEvent(event)
            }
            return
        }

        let lists = Self.applyActivityStreamEvent(
            kind: kind, event: payload.toolEvent, active: activeEvents, recent: recentEvents
        )
        setIfChanged(\.activeEvents, lists.active)
        setIfChanged(\.recentEvents, lists.recent)

        // A delegated agent or team starting while idle: poll now rather than after the idle wait.
        if kind == "call_finished", !hasActiveWork,
           let tool = payload.tool, tool.contains("agent") || tool.contains("team") {
            wakePoll()
        }

        guard let eventID = payload.eventID, !eventID.isEmpty else { return }
        if kind == "call_started" {
            presentToolActivityEvent(payload)
        } else if kind == "call_finished" {
            ToolActivityBubbleController.shared.finish(eventID: eventID)
            DesktopCursorOverlayController.shared.finish(eventID: eventID)
            let status = payload.status ?? "ok"
            onLiveToolCallFinished?(eventID, !["error", "failed", "denied", "blocked", "timeout"].contains(status))
        }
    }

    /// "Chat 2", as the Conversations list numbers it, once there is more than one.
    private func desktopCursorAgentLabel(_ sessionID: String?) -> String? {
        guard let sessionID, steeringSessions.count > 1,
              let session = steeringSessions.first(where: { $0.sessionID == sessionID })
        else { return nil }
        return "Chat \(session.flowNumber)"
    }

    private func presentToolActivityEvent(_ payload: ToolActivityStreamPayload) {
        guard
            let eventID = payload.eventID,
            let arguments = payload.arguments,
            let description = arguments.description,
            !description.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return }
        onLiveToolCallStarted?(LiveToolCall(
            eventID: eventID,
            tool: payload.tool ?? "tool",
            description: description,
            sessionID: payload.sessionID,
            agentID: payload.agentID,
            client: payload.client,
            target: arguments.browser ?? arguments.app,
            startedAt: payload.startedAt.map { Date(timeIntervalSince1970: $0) } ?? Date()
        ))
        // The bubble is one style of live activity; the island is the other.
        guard settings.requireToolDescriptions, settings.showToolActivity, !settings.showLiveIsland else { return }

        ToolActivityBubbleController.shared.begin(
            eventID: eventID,
            tool: payload.tool ?? "tool",
            description: description,
            sessionID: payload.sessionID,
            agentID: payload.agentID,
            teamID: payload.teamID,
            source: bubbleSource(agentID: payload.agentID, client: payload.client)
        )
    }

    /// Who a bubble lane belongs to (#26): an agent's provider and task title, or the chat app's name.
    private func bubbleSource(agentID: String?, client: String?) -> String? {
        if let agentID, !agentID.isEmpty {
            let info = agents.first { $0.agentID == agentID }
            let provider = AgentProviders.name(info?.provider)
            let title = Self.nonBlank(info?.title)
            switch (provider, title) {
            case let (provider?, title?): return "\(provider) · \(title)"
            case let (provider?, nil): return "\(provider) agent"
            case let (nil, title?): return title
            default: return nil
            }
        }
        return Self.nonBlank(client)
    }

    private static func nonBlank(_ text: String?) -> String? {
        guard let trimmed = text?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else { return nil }
        return trimmed
    }

    /// Background polls re-read permission semantics only when they are older than a minute;
    /// a refresh after a user action or on opening the menu reads everything.
    func refresh(background: Bool = false) async {
        refreshNgrokStateIfNeeded()
        refreshPersistedUpdateState()
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            recordDisconnected(issue: "Invalid server URL.")
            return
        }
        do {
            let pollEvents = Self.shouldPollActivityEvents(
                streamConnected: toolActivityStreamConnected, seeded: streamActivitySeeded
            )
            let streamWasConnected = toolActivityStreamConnected
            // Summary gates the state update below, but none of these requests
            // depends on it.  Start them together so a slow summary does not
            // delay the independent agent, session, activity, or semantics data.
            async let summaryFetch: DashboardSummary = fetch(
                base.appendingPathComponent("dashboard/api/summary"), query: ["hours": "1"]
            )
            async let eventsFetch: EventsEnvelope? = fetchActivityEvents(base: base, poll: pollEvents)
            async let agentsFetch: AgentsEnvelope = fetch(base.appendingPathComponent("dashboard/api/agents"), query: ["limit": "20"])
            async let steeringFetch: SteeringEnvelope = fetch(base.appendingPathComponent("dashboard/api/steering"), query: [:])
            let semanticsAge = securitySemanticsFetchedAt.map { Date().timeIntervalSince($0) } ?? .infinity
            let readSemantics = !background || securitySemantics == nil || semanticsAge >= Self.securitySemanticsMaxAgeSeconds
            async let securityFetch: SecuritySemanticsEnvelope? = readSemantics
                ? fetch(base.appendingPathComponent("dashboard/api/security/semantics"), query: [:])
                : nil

            let summary = try await summaryFetch

            setIfChanged(\.serverRunning, true)
            if !settings.onboardingDismissed { await refreshOnboarding() }
            if let value = summary.version { setIfChanged(\.version, value) }
            if let value = summary.totalCalls { setIfChanged(\.totalCalls, value) }
            if let value = summary.successRate { setIfChanged(\.successRate, value) }
            setIfChanged(\.privacyFlags, summary.privacy)

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
                agentFailureReferenceTime = Date().timeIntervalSince1970
                resolvedActiveAgents = agentsEnvelope.agents.filter(\.isActive).count
            } catch {
                secondaryIssue = secondaryIssue ?? "Agents: \(Self.issueText(for: error))"
            }
            setIfChanged(\.activeAgents, resolvedActiveAgents)
            await announceScheduledTaskRuns()
            do {
                let steeringEnvelope = try await steeringFetch
                applySteeringSnapshot(steeringEnvelope)
                setIfChanged(\.hasSteeringSnapshot, true)
                setIfChanged(\.steeringSnapshotStale, false)
            } catch {
                setIfChanged(\.steeringSnapshotStale, true)
                secondaryIssue = secondaryIssue ?? "Conversations: \(Self.issueText(for: error))"
            }
            do {
                if let semantics = try await securityFetch {
                    setIfChanged(\.securitySemantics, semantics)
                    setIfChanged(\.permissionsSettingsState, .fresh())
                    securitySemanticsFetchedAt = Date()
                }
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

    /// Stops a delegated agent on the user's behalf; the orchestrator sees it as cancelled by the user.
    func cancelAgent(_ agent: AgentInfo) async {
        guard !cancellingAgentIDs.contains(agent.agentID),
              let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        cancellingAgentIDs.insert(agent.agentID)
        defer { cancellingAgentIDs.remove(agent.agentID) }
        let name = agent.title ?? "the agent"
        do {
            let result: AgentCancelEnvelope = try await post(
                base.appendingPathComponent("dashboard/api/agents/cancel"), body: ["agent_id": agent.agentID], timeout: 15.0
            )
            if result.cancellation?.state == "unconfirmed" {
                showNotice(ActionNotice(kind: .error, message: "Stopped \(name), but it has not exited yet; Mac MCP keeps trying."))
            } else {
                showNotice(ActionNotice(kind: .success, message: "Stopped \(name)."))
            }
        } catch {
            showNotice(ActionNotice(kind: .error, message: "Couldn’t stop \(name): \(Self.issueText(for: error))"))
        }
        await refresh()
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

    /// Past agent teams for Settings › Agents › Team History (#263): the newest `limit` teams, counts only.
    func refreshTeamHistory(limit: Int = 10) async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            setIfChanged(\.teamHistoryIssue, "Could not load Team History because the server URL is invalid.")
            return
        }
        setIfChanged(\.teamHistoryLoading, true)
        defer { setIfChanged(\.teamHistoryLoading, false) }
        do {
            let history: TeamHistoryEnvelope = try await fetch(
                base.appendingPathComponent("dashboard/api/agents/history"),
                query: ["limit": String(max(1, min(limit, 50)))],
                timeout: 4.0
            )
            setIfChanged(\.teamHistory, history)
            setIfChanged(\.teamHistoryIssue, nil)
        } catch {
            setIfChanged(\.teamHistoryIssue, "Could not load Team History: \(Self.issueText(for: error))")
        }
    }

    /// Only which provider CLIs the server found on this Mac (no model catalogs), for the tour.
    func refreshProviderDetection() async {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        if let envelope: ProvidersEnvelope = try? await fetch(
            base.appendingPathComponent("dashboard/api/providers"), query: [:], timeout: 8.0
        ) {
            setIfChanged(\.providerStatuses, envelope.providers)
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
        setIfChanged(\.nextPollDelaySeconds, startingAfterLogin
            ? Self.activePollIntervalSeconds
            : Self.pollDelaySeconds(forFailureCount: consecutiveRefreshFailures))
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
    func runCLICommand(_ args: [String], input: String? = nil) async -> (code: Int32, output: String) {
        await Self.runCLI(args: args, configuredPath: settings.cliPath, settingsPath: settings.path.path, input: input)
    }

    /// Restarts like the menu's Restart action but lets the caller wait to recheck.
    func restartServerAndWait() async -> Bool {
        guard busyAction == nil else { return false }
        busyAction = "Restarting"
        actionNotice = nil
        let args = lifecycleArgs("restart") + ["--wait"]
        let result = await Self.runCLI(args: args, configuredPath: settings.cliPath, settingsPath: settings.path.path)
        busyAction = nil
        showNotice(Self.notice(for: args, result: result))
        await refresh()
        return result.code == 0
    }

    func startServer() { runAction(title: "Starting", args: lifecycleArgs("start")) }
    func stopServer() { runAction(title: "Stopping", args: ["stop"]) }
    // --wait: report the restart's real outcome, not just that launchd accepted the job.
    func restartServer() { runAction(title: "Restarting", args: lifecycleArgs("restart") + ["--wait"]) }

    var canCheckForUpdates: Bool {
        busyAction == nil && !updateCheckLoading && !updateTransactionActive
    }

    var canInstallUpdate: Bool {
        guard busyAction == nil, !updateTransactionActive, updateCheckInfo?.dirty != true else { return false }
        if let packageUpdate { return packageUpdate.updateAvailable == true }
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
        if let data = output.data(using: .utf8),
           let notice = try? JSONDecoder().decode(PackageInstallUpdateNotice.self, from: data),
           notice.installKind == "pkg" {
            setIfChanged(\.updateCheckInfo, nil)
            setIfChanged(\.packageUpdate, notice)
            guard shouldShowNotice else { return }
            if notice.checkFailed == true {
                showNotice(ActionNotice(kind: .error, message: "Couldn’t check for updates. Try again later."))
            } else if notice.updateAvailable == true {
                showNotice(ActionNotice(kind: .update, message: "A new update is ready."))
            } else {
                showNotice(ActionNotice(kind: .success, message: "Mac MCP is up to date."))
            }
            return
        }
        guard code == 0 || code == 2,
              let data = output.data(using: .utf8),
              let info = try? JSONDecoder().decode(UpdateCheckInfo.self, from: data) else {
            if shouldShowNotice {
                showNotice(ActionNotice(kind: .error, message: "Couldn’t read update information."))
            }
            return
        }
        setIfChanged(\.updateCheckInfo, info)
        setIfChanged(\.packageUpdate, nil)
        guard shouldShowNotice else { return }
        if info.dirty {
            showNotice(ActionNotice(kind: .error, message: "Update is blocked by local changes."))
        } else if info.updateAvailable {
            showNotice(ActionNotice(kind: .update, message: "A new update is ready."))
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

    /// One answer for every install (install.sh, npm or .pkg): a newer release is ready.
    var updateReady: Bool {
        if let packageUpdate { return packageUpdate.updateAvailable == true }
        guard let info = updateCheckInfo else { return false }
        return info.updateAvailable && !info.dirty
    }

    /// The Update button everywhere. Behind it, install.sh and npm installs run the signed updater
    /// and a package install opens the newer package's release page.
    func performUpdate() {
        if let packageUpdate {
            if packageUpdate.updateAvailable == true, let url = URL(string: packageUpdate.downloadURL) {
                NSWorkspace.shared.open(url)
            }
            return
        }
        installUpdate()
    }

    /// Where the update alert leads: the Updates card in Settings › General (set by the app).
    var presentUpdate: (() -> Void)?
    /// Opens Settings › Devices, where a Mac asking to connect waits (#147).
    var presentFleetRequests: (() -> Void)?

    func showUpdate() {
        presentUpdate?()
    }

    static let updateWatchFirstDelaySeconds: Double = 120
    static let updateWatchIntervalSeconds: Double = 6 * 60 * 60
    private static let announcedUpdateKey = "MacMCP.announcedUpdate"

    /// Looks for a new release in the background (#232): shortly after launch, then every few hours.
    /// It only looks; installing stays one click on Update.
    private func startUpdateWatch() {
        guard updateWatchTask == nil else { return }
        updateWatchTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.updateWatchFirstDelaySeconds * 1_000_000_000))
            while !Task.isCancelled {
                await self?.checkForUpdatesQuietly()
                try? await Task.sleep(nanoseconds: UInt64(Self.updateWatchIntervalSeconds * 1_000_000_000))
            }
        }
    }

    private func checkForUpdatesQuietly() async {
        guard settings.updateAlertsEnabled else { return }
        refreshPersistedUpdateState()
        guard canCheckForUpdates, busyAction == nil else { return }
        updateCheckLoading = true
        let result = await Self.runCLI(
            args: ["update", "--check", "--json"],
            configuredPath: settings.cliPath,
            settingsPath: settings.path.path
        )
        updateCheckLoading = false
        applyUpdateCheckOutput(result.output, code: result.code, shouldShowNotice: false)
        await announceUpdateIfNew()
    }

    /// Once per release: a notification when macOS allows it; the menu shows the update either way.
    private func announceUpdateIfNew() async {
        guard updateReady, settings.updateAlertsEnabled else {
            if !updateReady { AgentNotificationController.shared.removeUpdateReady() }
            return
        }
        let release = packageUpdate?.latestRelease ?? updateCheckInfo?.targetCommit ?? ""
        guard !release.isEmpty, UserDefaults.standard.string(forKey: Self.announcedUpdateKey) != release else { return }
        if await AgentNotificationController.shared.scheduleUpdateReady() {
            UserDefaults.standard.set(release, forKey: Self.announcedUpdateKey)
        }
    }

    func setUpdateAlertsEnabled(_ enabled: Bool) {
        settings.updateAlertsEnabled = enabled
        do {
            try settings.save()
        } catch {
            settings.updateAlertsEnabled = !enabled
            showNotice(ActionNotice(kind: .error, message: "Couldn’t save update settings."))
            return
        }
        if enabled {
            Task { await checkForUpdatesQuietly() }
        } else {
            AgentNotificationController.shared.removeUpdateReady()
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
            steeringStatus = "Could not save how long idle conversations are kept."
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
        steeringStatus = "Checking whether a message sent before the menu app restarted arrived…"
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
            steeringStatus = steeringSessions.count > 1 ? "Choose a conversation first." : "No conversations yet."
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
                        ? "Sent (recovered). This conversation will see your message before its next step."
                        : "Sent. This conversation will see your message before its next step."
                } else {
                    steeringStatus = message.idempotentReplay == true
                        ? "Sent (recovered). This conversation will see your message with the result of the step it is on."
                        : "Sent. This conversation will see your message with the result of the step it is on."
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
                            steeringStatus = "The conversation ended after your message was accepted."
                        } else {
                            steeringStatus = "That conversation ended before your message could be sent."
                            clearPendingSteeringSubmission()
                        }
                        return
                    case "queue_full":
                        steeringStatus = "That conversation already has too many messages waiting."
                        clearPendingSteeringSubmission()
                    case "stale_generation":
                        steeringStatus = "Mac MCP restarted before it confirmed your message, so whether it arrived is unknown. Check the conversation, then send it again if needed."
                        clearPendingSteeringSubmission()
                    case "idempotency_expired":
                        steeringStatus = "It is too late to retry that message safely, and whether it arrived is unknown. Check the conversation, then send it again if needed."
                        clearPendingSteeringSubmission()
                    case "idempotency_conflict":
                        steeringStatus = reason == "client_instruction_id_belongs_to_another_session"
                            ? "That message belongs to another conversation now. Send it again."
                            : "That retry clashed with an earlier message. Edit your message and send it again."
                        clearPendingSteeringSubmission()
                    default:
                        steeringStatus = "Could not send your message (\(code))."
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
                    steeringStatus = "Your message was sent; confirmed after a network timeout."
                } else {
                    steeringStatus = "No clear answer from Mac MCP. Send again to retry safely; it won't arrive twice."
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

    var sessionActiveCount: Int { steeringSessionGroups.active.count }
    var sessionIdleCount: Int { steeringSessionGroups.recent.count }
    var sessionEndedCount: Int { steeringSessionGroups.ended.count }

    var recentAgentFailureCount: Int {
        agents.filter { $0.isRecentFailure(at: agentFailureReferenceTime) }.count
    }

    var lastAgentRunAt: Double? {
        agents.compactMap(\.endedAt).max()
    }

    var hasReliableSessionSignal: Bool {
        !steeringSnapshotStale && connectionState != .disconnected
    }

    var steeringEmptyMessage: String {
        if let event = steeringRecent.first(where: { $0.kind == "session" }) {
            switch event.effectiveLifecycleState {
            case .expired, .disconnected, .failed: return "The last conversation ended."
            default: break
            }
        }
        return "No conversations yet."
    }

    func applySteeringSnapshot(_ envelope: SteeringEnvelope) {
        let previousGenerationID = steeringGenerationID
        setIfChanged(\.steeringGenerationID, envelope.generationID)
        if let pendingGenerationID = pendingSteeringGenerationID,
           let currentGenerationID = envelope.generationID,
           pendingGenerationID != currentGenerationID {
            clearPendingSteeringSubmission()
            setIfChanged(\.steeringStatus, "Mac MCP restarted before it confirmed your message, so whether it arrived is unknown. Check the conversation, then send it again if needed.")
        } else if previousGenerationID != nil,
                  let currentGenerationID = envelope.generationID,
                  previousGenerationID != currentGenerationID,
                  lastSteeringMessageID != nil {
            lastSteeringMessageID = nil
            setIfChanged(\.steeringStatus, "Mac MCP restarted, so what happened to your last message can no longer be confirmed.")
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
                        ? "Your message was sent; confirmed after the menu app restarted."
                        : "Your message was sent; confirmed after a slow reply."
                )
            } else if pendingSteeringRestoredFromDisk {
                let liveSessionStillExists = sessionsForSelection.contains(where: { $0.sessionID == pendingSessionID })
                setIfChanged(
                    \.steeringStatus,
                    liveSessionStillExists
                        ? "Whether your last message arrived is unknown after the restart. Type the same message again to retry it safely."
                        : "Whether your last message arrived is unknown: that conversation is no longer open."
                )
            }
        }

        if !recoveredPendingThisSnapshot,
           let messageID = lastSteeringMessageID,
           let recent = envelope.recent.first(where: { $0.id == messageID }) {
            switch recent.effectiveLifecycleState {
            case .queued:
                let text = sessionsForSelection.first(where: { $0.sessionID == recent.sessionID })?.isWorking == true
                    ? "Sent. This conversation will see your message with the result of the step it is on."
                    : "Sent. This conversation will see your message before its next step."
                setIfChanged(\.steeringStatus, text)
            case .delivered:
                setIfChanged(\.steeringStatus, recent.status == "preempted"
                    ? "Delivered: the conversation read it and did not take the step it was about to."
                    : "Delivered with the result of the step it was on.")
                lastSteeringMessageID = nil
            case .acknowledged:
                setIfChanged(\.steeringStatus, "The conversation acknowledged your message.")
                lastSteeringMessageID = nil
            case .failed:
                setIfChanged(\.steeringStatus, "Not delivered yet; it will be tried again at the conversation's next step.")
            case .disconnected:
                setIfChanged(\.steeringStatus, "The conversation ended before acknowledging your message.")
                lastSteeringMessageID = nil
            case .expired:
                setIfChanged(\.steeringStatus, "The conversation expired before acknowledging your message.")
                lastSteeringMessageID = nil
            default:
                break
            }
        } else if sessionsForSelection.isEmpty && lastSteeringMessageID == nil {
            setIfChanged(\.steeringStatus, steeringEmptyMessage)
        } else if sessionsForSelection.count > 1 && selectedSteeringSessionID == nil {
            setIfChanged(\.steeringStatus, "Several conversations are open: choose the one to send a message to.")
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
            case "restart":
                message = output.contains("public endpoint did not start")
                    ? "Server restarted locally, but the public endpoint didn’t start."
                    : "Couldn’t restart the server."
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

    private static func stateDirectoryURL() -> URL {
        if let configured = ProcessInfo.processInfo.environment["MAC_MCP_STATE_DIR"], !configured.isEmpty {
            return URL(fileURLWithPath: NSString(string: configured).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".mac-mcp")
    }

    private static func dashboardTokenURL() -> URL {
        let env = ProcessInfo.processInfo.environment
        if let configured = env["MAC_MCP_DASHBOARD_TOKEN_FILE"], !configured.isEmpty {
            return URL(fileURLWithPath: NSString(string: configured).expandingTildeInPath)
        }
        return stateDirectoryURL().appendingPathComponent("dashboard-token")
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
