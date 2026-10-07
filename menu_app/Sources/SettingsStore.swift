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
        var public_endpoint_mode: String?
        var public_url: String?
        var cloudflare_tunnel: String?
    }
    struct Steering: Codable {
        var session_ttl_minutes: Int
    }
    struct ToolActivity: Codable {
        var show_bubble: Bool
        var require_descriptions: Bool
    }
    struct Notifications: Codable {
        var agent_completion: Bool
    }
    struct DecisionAcceleration: Codable {
        var enabled: Bool
        var scope: String?
    }
    struct Provider: Codable {
        var enabled: Bool
        var binary_path: String?
        var default_project: String?
    }
    struct DefaultAgent: Codable {
        var provider: String
        var model: String?
        var reasoning: String?
    }
    struct Subagents: Codable {
        var providers: [String: Provider]
        var defaultAgent: DefaultAgent?

        enum CodingKeys: String, CodingKey {
            case providers
            case defaultAgent = "default"
        }
    }

    var experimental_tools: [String: ExperimentalTool]
    var voice: Voice
    var server: Server
    var steering: Steering?
    var tool_activity: ToolActivity?
    var notifications: Notifications?
    var decision_acceleration: DecisionAcceleration?
    var subagents: Subagents?

    static func defaults() -> MenuSettings {
        MenuSettings(
            experimental_tools: ["ask_user_voice": ExperimentalTool(enabled: true)],
            voice: Voice(language: "auto", input_device: "auto", output_device: "system", tts_rate: "-5%", timeout_s: 45, voice: "tr-TR-AhmetNeural"),
            server: Server(port: 8000, cli_path: "", ngrok_on_start: false, public_endpoint_mode: "none", public_url: "", cloudflare_tunnel: ""),
            steering: Steering(session_ttl_minutes: 10),
            tool_activity: ToolActivity(show_bubble: false, require_descriptions: false),
            notifications: Notifications(agent_completion: false),
            decision_acceleration: DecisionAcceleration(enabled: false, scope: "both"),
            subagents: Subagents(
                providers: [
                    "opencode": Provider(enabled: false, binary_path: nil, default_project: nil),
                    "codex": Provider(enabled: false, binary_path: nil, default_project: nil),
                    "chatgpt": Provider(enabled: false, binary_path: nil, default_project: nil),
                ],
                defaultAgent: nil
            )
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
    @Published var publicEndpointMode = "none"
    @Published var publicURL = ""
    @Published var cloudflareTunnel = ""
    @Published var steeringSessionMinutes = 10
    @Published var showToolActivity = false
    @Published var requireToolDescriptions = false
    @Published var agentCompletionNotificationsEnabled = false
    @Published var opencodeEnabled = false
    @Published var codexEnabled = false
    @Published var chatgptEnabled = false
    @Published var opencodeBinaryPath = ""
    @Published var codexBinaryPath = ""
    @Published var chatgptBinaryPath = ""
    @Published var chatgptDefaultProject = ""
    @Published var defaultAgentProvider = ""
    @Published var defaultAgentModel = ""
    @Published var defaultAgentReasoning = ""
    @Published var hasGroqKey = false
    @Published var decisionAccelerationEnabled = false
    @Published var decisionAccelerationScope = "both"
    @Published var hasDecisionsKey = false
    @Published private(set) var settingsLoadIssue = ""
    @Published private(set) var providerSettingsLocked = false

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
        hasDecisionsKey = KeychainStore.hasDecisionsKey()
    }

    func load() {
        let defaults = MenuSettings.defaults()
        var current = defaults
        var providerConfigValid = false
        settingsLoadIssue = ""
        providerSettingsLocked = false

        if FileManager.default.fileExists(atPath: path.path) {
            do {
                let data = try Data(contentsOf: path)
                guard let rawObject = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw CocoaError(.fileReadCorruptFile)
                }
                let defaultData = try JSONEncoder().encode(defaults)
                guard let defaultObject = try JSONSerialization.jsonObject(with: defaultData) as? [String: Any] else {
                    throw CocoaError(.fileReadCorruptFile)
                }
                let mergedObject = Self.deepMerge(defaultObject, rawObject)
                let mergedData = try JSONSerialization.data(withJSONObject: mergedObject)
                current = try JSONDecoder().decode(MenuSettings.self, from: mergedData)
                providerConfigValid = true
            } catch {
                settingsLoadIssue = "settings.json could not be read or decoded. Delegated providers are disabled until you repair the file."
                providerSettingsLocked = true
            }
        } else {
            settingsLoadIssue = "settings.json is missing. Delegated providers are disabled until settings are saved."
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
        let rawMode = current.server.public_endpoint_mode?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        if ["none", "ngrok", "cloudflare", "custom"].contains(rawMode) {
            publicEndpointMode = rawMode
        } else {
            publicEndpointMode = current.server.ngrok_on_start ? "ngrok" : "none"
        }
        publicURL = current.server.public_url ?? ""
        cloudflareTunnel = current.server.cloudflare_tunnel ?? ""
        ngrokOnStart = publicEndpointMode == "ngrok"
        steeringSessionMinutes = max(1, current.steering?.session_ttl_minutes ?? 10)
        showToolActivity = current.tool_activity?.show_bubble ?? false
        requireToolDescriptions = current.tool_activity?.require_descriptions ?? false
        if !requireToolDescriptions {
            showToolActivity = false
        }
        agentCompletionNotificationsEnabled = current.notifications?.agent_completion ?? false
        decisionAccelerationEnabled = current.decision_acceleration?.enabled ?? false
        let rawDecisionScope = current.decision_acceleration?.scope?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        decisionAccelerationScope = ["off", "browser", "native", "both"].contains(rawDecisionScope) ? rawDecisionScope : "both"
        let providers = current.subagents?.providers ?? [:]
        opencodeEnabled = providerConfigValid ? (providers["opencode"]?.enabled ?? false) : false
        codexEnabled = providerConfigValid ? (providers["codex"]?.enabled ?? false) : false
        chatgptEnabled = providerConfigValid ? (providers["chatgpt"]?.enabled ?? false) : false
        opencodeBinaryPath = providers["opencode"]?.binary_path ?? ""
        codexBinaryPath = providers["codex"]?.binary_path ?? ""
        chatgptBinaryPath = providers["chatgpt"]?.binary_path ?? ""
        chatgptDefaultProject = providers["chatgpt"]?.default_project ?? ""
        defaultAgentProvider = current.subagents?.defaultAgent?.provider ?? ""
        defaultAgentModel = current.subagents?.defaultAgent?.model ?? ""
        defaultAgentReasoning = current.subagents?.defaultAgent?.reasoning ?? ""
        if cliPath.isEmpty { cliPath = defaultCLIPath() }
    }

    func save() throws {
        if providerSettingsLocked && FileManager.default.fileExists(atPath: path.path) {
            throw NSError(
                domain: "MacMCPSettings",
                code: 75,
                userInfo: [NSLocalizedDescriptionKey: settingsLoadIssue]
            )
        }
        let payload = MenuSettings(
            experimental_tools: ["ask_user_voice": .init(enabled: voiceEnabled)],
            voice: .init(language: language, input_device: inputDevice, output_device: outputDevice, tts_rate: ttsRate, timeout_s: timeoutSeconds, voice: voiceName),
            server: .init(
                port: serverPort,
                cli_path: cliPath,
                ngrok_on_start: publicEndpointMode == "ngrok",
                public_endpoint_mode: publicEndpointMode,
                public_url: publicURL,
                cloudflare_tunnel: cloudflareTunnel
            ),
            steering: .init(session_ttl_minutes: max(1, steeringSessionMinutes)),
            tool_activity: .init(
                show_bubble: showToolActivity && requireToolDescriptions,
                require_descriptions: requireToolDescriptions
            ),
            notifications: .init(agent_completion: agentCompletionNotificationsEnabled),
            decision_acceleration: .init(enabled: decisionAccelerationEnabled, scope: decisionAccelerationScope),
            subagents: .init(
                providers: [
                    "opencode": .init(enabled: opencodeEnabled, binary_path: opencodeBinaryPath.nilIfEmpty, default_project: nil),
                    "codex": .init(enabled: codexEnabled, binary_path: codexBinaryPath.nilIfEmpty, default_project: nil),
                    "chatgpt": .init(enabled: chatgptEnabled, binary_path: chatgptBinaryPath.nilIfEmpty, default_project: chatgptDefaultProject.nilIfEmpty),
                ],
                defaultAgent: defaultAgentProvider.nilIfEmpty.map {
                    .init(provider: $0, model: defaultAgentModel.nilIfEmpty, reasoning: defaultAgentReasoning.nilIfEmpty)
                }
            )
        )
        let payloadData = try JSONEncoder.pretty.encode(payload)
        guard let payloadObject = try JSONSerialization.jsonObject(with: payloadData) as? [String: Any] else {
            throw CocoaError(.fileWriteUnknown)
        }
        var existing: [String: Any] = [:]
        if let currentData = try? Data(contentsOf: path),
           let currentObject = try? JSONSerialization.jsonObject(with: currentData) as? [String: Any] {
            existing = currentObject
        }
        var merged = Self.deepMerge(existing, payloadObject)
        if var subagents = merged["subagents"] as? [String: Any] {
            if let provider = defaultAgentProvider.nilIfEmpty {
                var preset: [String: Any] = ["provider": provider]
                if let model = defaultAgentModel.nilIfEmpty { preset["model"] = model }
                if let reasoning = defaultAgentReasoning.nilIfEmpty { preset["reasoning"] = reasoning }
                subagents["default"] = preset
            } else {
                subagents.removeValue(forKey: "default")
            }
            merged["subagents"] = subagents
        }
        let data = try JSONSerialization.data(withJSONObject: merged, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: path, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        settingsLoadIssue = ""
        providerSettingsLocked = false
    }

    private static func deepMerge(_ base: [String: Any], _ overlay: [String: Any]) -> [String: Any] {
        var result = base
        for (key, value) in overlay {
            if let overlayDictionary = value as? [String: Any],
               let baseDictionary = result[key] as? [String: Any] {
                result[key] = deepMerge(baseDictionary, overlayDictionary)
            } else {
                result[key] = value
            }
        }
        return result
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

    func saveDecisionsKey(_ value: String) throws {
        try KeychainStore.saveDecisionsKey(value.trimmingCharacters(in: .whitespacesAndNewlines))
        hasDecisionsKey = KeychainStore.hasDecisionsKey()
    }

    func removeDecisionsKey() throws {
        try KeychainStore.removeDecisionsKey()
        hasDecisionsKey = false
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
