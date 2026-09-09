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

    var experimental_tools: [String: ExperimentalTool]
    var voice: Voice
    var server: Server

    static func defaults() -> MenuSettings {
        MenuSettings(
            experimental_tools: ["ask_user_voice": ExperimentalTool(enabled: true)],
            voice: Voice(language: "auto", input_device: "auto", output_device: "system", tts_rate: "-5%", timeout_s: 45, voice: "tr-TR-AhmetNeural"),
            server: Server(port: 8000, cli_path: "", ngrok_on_start: false)
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
        if cliPath.isEmpty { cliPath = defaultCLIPath() }
    }

    func save() throws {
        let payload = MenuSettings(
            experimental_tools: ["ask_user_voice": .init(enabled: voiceEnabled)],
            voice: .init(language: language, input_device: inputDevice, output_device: outputDevice, tts_rate: ttsRate, timeout_s: timeoutSeconds, voice: voiceName),
            server: .init(port: serverPort, cli_path: cliPath, ngrok_on_start: ngrokOnStart)
        )
        let data = try JSONEncoder.pretty.encode(payload)
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: path, options: .atomic)
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
