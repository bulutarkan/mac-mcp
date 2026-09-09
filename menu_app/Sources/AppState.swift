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
    let lastTool: String?
    let toolCallCount: Int?
    let durationMS: Int?
    var id: String { agentID }
    var isActive: Bool { status == "starting" || status == "running" }
    enum CodingKeys: String, CodingKey {
        case agentID = "agent_id"
        case status, phase, title, provider, model
        case lastTool = "last_tool"
        case toolCallCount = "tool_call_count"
        case durationMS = "duration_ms"
    }
}

struct AgentsEnvelope: Decodable { let agents: [AgentInfo] }

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
    @Published var busyAction: String?
    @Published var actionMessage = ""
    @Published var actionIsError = false
    @Published var pulse = false

    let settings = SettingsStore()
    private var pollTask: Task<Void, Never>?
    private var pulseTask: Task<Void, Never>?
    private var consecutiveRefreshFailures = 0

    init() { startTasks() }
    deinit { pollTask?.cancel(); pulseTask?.cancel() }

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
            let (eventsEnvelope, agentsEnvelope) = await (eventsResult, agentsResult)
            if let eventsEnvelope { recentEvents = eventsEnvelope.events }
            if let agentsEnvelope {
                agents = agentsEnvelope.agents
                activeAgents = agentsEnvelope.agents.filter(\.isActive).count
            }
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

    private func runAction(title: String, args: [String]) {
        guard busyAction == nil else { return }
        busyAction = title
        actionMessage = ""
        actionIsError = false
        let cliPath = settings.cliPath
        let settingsPath = settings.path.path
        Task {
            let result = await Self.runCLI(args: args, configuredPath: cliPath, settingsPath: settingsPath)
            busyAction = nil
            actionIsError = result.code != 0 && !(args == ["update", "--check"] && result.code == 2)
            let cleaned = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            actionMessage = cleaned.isEmpty ? (actionIsError ? "Command failed" : "Done") : String(cleaned.suffix(500))
            try? await Task.sleep(nanoseconds: 800_000_000)
            await refresh()
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
