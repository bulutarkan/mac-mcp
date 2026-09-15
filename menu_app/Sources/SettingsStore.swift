import Combine
import Foundation

struct MenuSettings: Codable {
    struct ExperimentalTool: Codable { var enabled: Bool }
    struct Voice: Codable {
        var language: String
        var input_device: String
        var output_device: String
        var tts_rate: String
        var timeout_s: Int
        var voice: String
    }
    struct Server: Codable {
        var port: Int
        var cli_path: String
        var ngrok_on_start: Bool
    }
    struct Steering: Codable {
        var session_ttl_minutes: Int
    }
    struct Provider: Codable {
        var enabled: Bool
        var binary_path: String?
        var default_project: String?
    }
    struct Subagents: Codable {
        var providers: [String: Provider]
    }

    var experimental_tools: [String: ExperimentalTool]
    var voice: Voice
    var server: Server
    var steering: Steering?
    var subagents: Subagents?

    static func defaults() -> MenuSettings {
        MenuSettings(
            experimental_tools: ["ask_user_voice": ExperimentalTool(enabled: true)],
            voice: Voice(language: "auto", input_device: "auto", output_device: "system", tts_rate: "-5%", timeout_s: 45, voice: "tr-TR-AhmetNeural"),
            server: Server(port: 8000, cli_path: "", ngrok_on_start: false),
            steering: Steering(session_ttl_minutes: 10),
            subagents: Subagents(providers: [
                "opencode": Provider(enabled: true, binary_path: nil, default_project: nil),
                "codex": Provider(enabled: true, binary_path: nil, default_project: nil),
                "chatgpt": Provider(enabled: false, binary_path: nil, default_project: nil),
            ])
        )
    }
}

@MainActor
final class SettingsStore: ObservableObject {
    @Published var voiceEnabled = true
    @Published var language = "auto"
    @Published var inputDevice = "auto"
    @Published var outputDevice = "system"
    @Published var ttsRate = "-5%"
    @Published var timeoutSeconds = 45
    @Published var voiceName = "tr-TR-AhmetNeural"
    @Published var serverPort = 8000
    @Published var cliPath = ""
    @Published var ngrokOnStart = false
    @Published var steeringSessionMinutes = 10
    @Published var opencodeEnabled = true
    @Published var codexEnabled = true
    @Published var chatgptEnabled = false
    @Published var opencodeBinaryPath = ""
    @Published var codexBinaryPath = ""
    @Published var chatgptBinaryPath = ""
    @Published var chatgptDefaultProject = ""
    @Published var hasGroqKey = false

    let path: URL

    init() {
        let env = ProcessInfo.processInfo.environment
        if let configured = env["MAC_MCP_SETTINGS_PATH"], !configured.isEmpty {
            path = URL(fileURLWithPath: NSString(string: configured).expandingTildeInPath)
        } else {
            path = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".mac-mcp/settings.json")
        }
        load()
        hasGroqKey = KeychainStore.hasGroqKey()
    }

    func load() {
        let defaults = MenuSettings.defaults()
        var current = defaults
        if let data = try? Data(contentsOf: path),
           let decoded = try? JSONDecoder().decode(MenuSettings.self, from: data) {
            current = decoded
        }
        voiceEnabled = current.experimental_tools["ask_user_voice"]?.enabled ?? true
        language = current.voice.language
        inputDevice = current.voice.input_device
        outputDevice = current.voice.output_device
        ttsRate = current.voice.tts_rate
        timeoutSeconds = current.voice.timeout_s
        voiceName = current.voice.voice
        serverPort = current.server.port
        cliPath = current.server.cli_path
        ngrokOnStart = current.server.ngrok_on_start
        steeringSessionMinutes = max(1, current.steering?.session_ttl_minutes ?? 10)
        let providers = current.subagents?.providers ?? MenuSettings.defaults().subagents?.providers ?? [:]
        opencodeEnabled = providers["opencode"]?.enabled ?? true
        codexEnabled = providers["codex"]?.enabled ?? true
        chatgptEnabled = providers["chatgpt"]?.enabled ?? false
        opencodeBinaryPath = providers["opencode"]?.binary_path ?? ""
        codexBinaryPath = providers["codex"]?.binary_path ?? ""
        chatgptBinaryPath = providers["chatgpt"]?.binary_path ?? ""
        chatgptDefaultProject = providers["chatgpt"]?.default_project ?? ""
        if cliPath.isEmpty { cliPath = defaultCLIPath() }
    }

    func save() throws {
        let payload = MenuSettings(
            experimental_tools: ["ask_user_voice": .init(enabled: voiceEnabled)],
            voice: .init(language: language, input_device: inputDevice, output_device: outputDevice, tts_rate: ttsRate, timeout_s: timeoutSeconds, voice: voiceName),
            server: .init(port: serverPort, cli_path: cliPath, ngrok_on_start: ngrokOnStart),
            steering: .init(session_ttl_minutes: max(1, steeringSessionMinutes)),
            subagents: .init(providers: [
                "opencode": .init(enabled: opencodeEnabled, binary_path: opencodeBinaryPath.nilIfEmpty, default_project: nil),
                "codex": .init(enabled: codexEnabled, binary_path: codexBinaryPath.nilIfEmpty, default_project: nil),
                "chatgpt": .init(enabled: chatgptEnabled, binary_path: chatgptBinaryPath.nilIfEmpty, default_project: chatgptDefaultProject.nilIfEmpty),
            ])
        )
        let data = try JSONEncoder.pretty.encode(payload)
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: path, options: .atomic)
    }

    func providerEnabled(_ id: String) -> Bool {
        switch id {
        case "opencode": return opencodeEnabled
        case "codex": return codexEnabled
        case "chatgpt": return chatgptEnabled
        default: return false
        }
    }

    func setProviderEnabled(_ id: String, enabled: Bool) {
        switch id {
        case "opencode": opencodeEnabled = enabled
        case "codex": codexEnabled = enabled
        case "chatgpt": chatgptEnabled = enabled
        default: return
        }
    }

    func saveGroqKey(_ value: String) throws {
        try KeychainStore.saveGroqKey(value.trimmingCharacters(in: .whitespacesAndNewlines))
        hasGroqKey = KeychainStore.hasGroqKey()
    }

    func removeGroqKey() throws {
        try KeychainStore.removeGroqKey()
        hasGroqKey = false
    }

    private func defaultCLIPath() -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let candidates = [
            home.appendingPathComponent(".local/bin/mac-mcp").path,
            home.appendingPathComponent("scripts/mac-mcp").path,
            home.appendingPathComponent("mac-mcp/.venv/bin/mac-mcp").path,
            home.appendingPathComponent("Projects/mac-mcp/.venv/bin/mac-mcp").path,
            "/opt/homebrew/bin/mac-mcp",
        ]
        return candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) ?? ""
    }
}

private extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return encoder
    }
}

private extension String {
    var nilIfEmpty: String? {
        let value = trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }
}
