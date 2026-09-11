import AppKit
import Combine
import Darwin
import Foundation

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

struct ToolEvent: Decodable, Identifiable {
    let eventID: String
    let timestamp: Double
    let source: String
    let tool: String
    let status: String
    let durationMS: Int?
    var id: String { eventID }
    enum CodingKeys: String, CodingKey {
        case eventID = "event_id"
        case timestamp, source, tool, status
        case durationMS = "duration_ms"
    }
}

struct EventsEnvelope: Decodable { let events: [ToolEvent] }

struct AgentInfo: Decodable, Identifiable {
    let agentID: String
    let status: String?
    let phase: String?
    let title: String?
    let provider: String?
    let model: String?
    let reasoning: String?
    let lastTool: String?
    let toolCallCount: Int?
    let retryCount: Int?
    let durationMS: Int?
    var id: String { agentID }
    var isActive: Bool { status == "starting" || status == "running" }
    enum CodingKeys: String, CodingKey {
        case agentID = "agent_id"
        case status, phase, title, provider, model, reasoning
        case lastTool = "last_tool"
        case toolCallCount = "tool_call_count"
        case retryCount = "retry_count"
        case durationMS = "duration_ms"
    }
}

struct AgentsEnvelope: Decodable { let agents: [AgentInfo] }

struct SteeringTarget: Decodable, Identifiable {
    let eventID: String
    let flowNumber: Int
    let label: String
    let detail: String
    let tool: String
    let startedAt: Double
    let durationMS: Int
    let queued: Int
    var id: String { eventID }
    enum CodingKeys: String, CodingKey {
        case eventID = "event_id"
        case flowNumber = "flow_number"
        case label, detail, tool, queued
        case startedAt = "started_at"
        case durationMS = "duration_ms"
    }
}

struct SteeringRecent: Decodable {
    let id: String
    let eventID: String
    let status: String
    let deliveredAt: Double?
    enum CodingKeys: String, CodingKey {
        case id, status
        case eventID = "event_id"
        case deliveredAt = "delivered_at"
    }
}

struct SteeringEnvelope: Decodable {
    let targets: [SteeringTarget]
    let recent: [SteeringRecent]
}

struct SteeringSendEnvelope: Decodable {
    struct Message: Decodable { let id: String }
    let ok: Bool
    let status: String?
    let message: Message?
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

@MainActor
final class AppState: ObservableObject {
    @Published var serverRunning = false
    @Published var ngrokRunning = false
    @Published var version = "—"
    @Published var totalCalls = 0
    @Published var successRate = 100.0
    @Published var activeAgents = 0
    @Published var recentEvents: [ToolEvent] = []
    @Published var agents: [AgentInfo] = []
    @Published var steeringTargets: [SteeringTarget] = []
    @Published var selectedSteeringEventID: String?
    @Published var steeringPrompt = ""
    @Published var steeringStatus = "No active agent task."
    @Published var steeringSending = false
    @Published var busyAction: String?
    @Published var actionNotice: ActionNotice?
    @Published var pulse = false

    let settings = SettingsStore()
    private var pollTask: Task<Void, Never>?
    private var pulseTask: Task<Void, Never>?
    private var noticeTask: Task<Void, Never>?
    private var consecutiveRefreshFailures = 0
    private var lastSteeringMessageID: String?

    init() { startTasks() }
    deinit { pollTask?.cancel(); pulseTask?.cancel(); noticeTask?.cancel() }

    var dashboardURL: URL? { URL(string: "http://127.0.0.1:\(settings.serverPort)/dashboard") }

    func startTasks() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 2_500_000_000)
            }
        }
        pulseTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                self.pulse = self.activeAgents > 0 ? !self.pulse : false
                try? await Task.sleep(nanoseconds: 550_000_000)
            }
        }
    }

    func refresh() async {
        ngrokRunning = Self.processExists(matching: "ngrok http")
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        do {
            let summary: DashboardSummary = try await fetch(base.appendingPathComponent("dashboard/api/summary"), query: ["hours": "1"])
            consecutiveRefreshFailures = 0
            serverRunning = true
            version = summary.version ?? "—"
            totalCalls = summary.totalCalls ?? 0
            successRate = summary.successRate ?? 100
            activeAgents = summary.activeAgents ?? 0
            async let eventsResult: EventsEnvelope? = try? fetch(base.appendingPathComponent("dashboard/api/events"), query: ["hours": "1", "limit": "20"])
            async let agentsResult: AgentsEnvelope? = try? fetch(base.appendingPathComponent("dashboard/api/agents"), query: ["limit": "20"])
            async let steeringResult: SteeringEnvelope? = try? fetch(base.appendingPathComponent("dashboard/api/steering"), query: [:])
            let (eventsEnvelope, agentsEnvelope, steeringEnvelope) = await (eventsResult, agentsResult, steeringResult)
            if let eventsEnvelope { recentEvents = eventsEnvelope.events }
            if let agentsEnvelope {
                agents = agentsEnvelope.agents
                activeAgents = agentsEnvelope.agents.filter(\.isActive).count
            }
            if let steeringEnvelope { applySteering(steeringEnvelope) }
        } catch {
            consecutiveRefreshFailures += 1
            if consecutiveRefreshFailures >= 3 {
                serverRunning = false
                version = "—"
                activeAgents = 0
            }
        }
    }

    func startServer() {
        var args = ["start"]
        if settings.ngrokOnStart { args.append("--ngrok") }
        runAction(title: "Starting", args: args)
    }
    func stopServer() { runAction(title: "Stopping", args: ["stop"]) }
    func restartServer() {
        var args = ["restart"]
        if settings.ngrokOnStart { args.append("--ngrok") }
        runAction(title: "Restarting", args: args)
    }
    func checkForUpdates() { runAction(title: "Checking update", args: ["update", "--check"]) }
    func installUpdate() { runAction(title: "Updating", args: ["update"]) }
    func openDashboard() { if let dashboardURL { NSWorkspace.shared.open(dashboardURL) } }
    func quitApp() { NSApplication.shared.terminate(nil) }

    func sendSteering() {
        let text = steeringPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        guard let eventID = selectedSteeringEventID else {
            steeringStatus = steeringTargets.count > 1 ? "Choose a flow first." : "No active agent task."
            return
        }
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else { return }
        steeringSending = true
        steeringStatus = "Sending…"
        Task {
            defer { steeringSending = false }
            do {
                let response: SteeringSendEnvelope = try await post(
                    base.appendingPathComponent("dashboard/api/steering"),
                    body: ["event_id": eventID, "text": text]
                )
                guard response.ok, let messageID = response.message?.id else {
                    steeringStatus = "Could not queue steering message."
                    return
                }
                lastSteeringMessageID = messageID
                steeringPrompt = ""
                steeringStatus = "Queued for the selected agent flow."
                await refresh()
            } catch {
                steeringStatus = "Target ended before the message could be queued."
                await refresh()
            }
        }
    }

    private func applySteering(_ envelope: SteeringEnvelope) {
        steeringTargets = envelope.targets
        if steeringTargets.count == 1 {
            selectedSteeringEventID = steeringTargets[0].eventID
        } else if let selectedSteeringEventID, !steeringTargets.contains(where: { $0.eventID == selectedSteeringEventID }) {
            self.selectedSteeringEventID = nil
        }

        if let messageID = lastSteeringMessageID,
           let recent = envelope.recent.first(where: { $0.id == messageID }) {
            if recent.status == "delivered" {
                steeringStatus = "Delivered to the agent with the tool result."
            } else if recent.status == "tool_failed" {
                steeringStatus = "Tool ended with an error before delivery."
            }
            lastSteeringMessageID = nil
        } else if steeringTargets.isEmpty && lastSteeringMessageID == nil {
            if !steeringStatus.hasPrefix("Delivered") && !steeringStatus.hasPrefix("Tool ended") {
                steeringStatus = "No active agent task."
            }
        } else if steeringTargets.count > 1 && selectedSteeringEventID == nil {
            steeringStatus = "Multiple agent flows are active — choose the one you want to steer."
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

    private func fetch<T: Decodable>(_ url: URL, query: [String: String]) async throws -> T {
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        var request = URLRequest(url: components.url!)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 1.8
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { throw URLError(.badServerResponse) }
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func post<T: Decodable>(_ url: URL, body: [String: String]) async throws -> T {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 2.0
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { throw URLError(.badServerResponse) }
        return try JSONDecoder().decode(T.self, from: data)
    }

    nonisolated private static func processExists(matching needle: String) -> Bool {
        let proc = Process(); proc.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep"); proc.arguments = ["-f", needle]
        proc.standardOutput = FileHandle.nullDevice; proc.standardError = FileHandle.nullDevice
        do { try proc.run(); proc.waitUntilExit(); return proc.terminationStatus == 0 } catch { return false }
    }

    nonisolated private static func runCLI(args: [String], configuredPath: String, settingsPath: String) async -> (code: Int32, output: String) {
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
            let pipe = Pipe(); process.standardOutput = pipe; process.standardError = pipe; process.standardInput = FileHandle.nullDevice
            do {
                try process.run(); process.waitUntilExit()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
            } catch { return (127, error.localizedDescription) }
        }.value
    }
}
