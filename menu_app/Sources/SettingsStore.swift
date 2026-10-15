import Combine
import Foundation

/// The agent CLIs Mac MCP can hand work to, by the id the server and settings use.
enum AgentProviders {
    static let names: [String: String] = [
        "codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode", "kimi": "Kimi Code", "grok": "Grok Build",
        "antigravity": "Antigravity", "pi": "pi", "cline": "Cline", "kilo": "Kilo Code", "goose": "Goose",
    ]
    /// Agents kept in `SettingsStore.moreProviders*` rather than a field each.
    static let more = ["antigravity", "pi", "cline", "kilo", "goose"]

    static func name(_ id: String?) -> String? {
        guard let id else { return nil }
        return names[id.lowercased()]
    }

    /// A tool as an agent's CLI names it, without its MCP server: mcp__mac-mcp__read_file (Claude
    /// Code, Kimi, Antigravity), mcp__mac_mcp__read_file (pi), mac-mcp__read_file (Goose, Cline),
    /// mac-mcp_read_file (OpenCode, Kilo) are all read_file.
    static func toolName(_ raw: String) -> String {
        var value = raw
        if value.hasPrefix("mcp__"), let range = value.dropFirst(5).range(of: "__") {
            return String(value[range.upperBound...])
        }
        if let range = value.range(of: "__") { return String(value[range.upperBound...]) }
        for prefix in ["mac-mcp_", "mac_mcp_", "mcp_"] where value.hasPrefix(prefix) { value.removeFirst(prefix.count) }
        return value
    }
}

struct MenuSettings: Codable {
    struct ExperimentalTool: Codable { var enabled: Bool }
    struct Voice: Codable {
        var language: String
        var input_device: String
        var output_device: String
        var tts_rate: String
        var timeout_s: Int
        var voice: String
        /// "ask" before every recording, or "always" once the person chose Always Allow.
        var transcription_consent: String?
    }
    struct Server: Codable {
        var port: Int
        var cli_path: String
        var ngrok_on_start: Bool
        var public_endpoint_mode: String?
        var public_url: String?
        var cloudflare_tunnel: String?
        /// Start a server left running when the owner logs in (the server's default is on).
        var start_at_login: Bool?
    }
    struct Steering: Codable {
        var session_ttl_minutes: Int
    }
    struct ToolActivity: Codable {
        var show_bubble: Bool
        var require_descriptions: Bool
        /// The notch island style of live activity (#174); absent means off.
        var island: Bool?
    }
    struct Notifications: Codable {
        var agent_completion: Bool
    }
    /// Per-app settings for what agents may do in Mac apps (Settings > Apps).
    struct AppSend: Codable {
        /// Ask in a native panel before every send; the server treats anything but false as on.
        var confirm_send: Bool
    }
    /// Get Started (#59): hidden by the owner, or finished once (then it never shows again).
    struct Onboarding: Codable {
        var dismissed: Bool
        /// The first-run tour: "completed" or "skipped" once it was shown; absent before.
        var tour: String?
    }
    struct Apps: Codable {
        var mail: AppSend?
        var messages: AppSend?
    }
    struct DecisionAcceleration: Codable {
        var enabled: Bool
        var scope: String?
    }
    struct Provider: Codable {
        var enabled: Bool
        var binary_path: String?
    }
    struct DefaultAgent: Codable {
        var provider: String
        var model: String?
        var reasoning: String?
    }
    struct Subagents: Codable {
        var providers: [String: Provider]
        var defaultAgent: DefaultAgent?
        /// Most delegated agents running at once (the server's default is 8).
        var maxActive: Int?
        /// Days a finished agent's local files are kept; 0 keeps them (the server's default is 30).
        var retentionDays: Int?

        enum CodingKeys: String, CodingKey {
            case providers
            case defaultAgent = "default"
            case maxActive = "max_active"
            case retentionDays = "retention_days"
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
    var apps: Apps?
    var onboarding: Onboarding?

    static func defaults() -> MenuSettings {
        MenuSettings(
            experimental_tools: ["ask_user_voice": ExperimentalTool(enabled: false)],
            voice: Voice(language: "auto", input_device: "auto", output_device: "system", tts_rate: "-5%", timeout_s: 45, voice: "tr-TR-AhmetNeural", transcription_consent: "ask"),
            server: Server(port: 8000, cli_path: "", ngrok_on_start: false, public_endpoint_mode: "none", public_url: "", cloudflare_tunnel: "", start_at_login: true),
            steering: Steering(session_ttl_minutes: 10),
            tool_activity: ToolActivity(show_bubble: false, require_descriptions: false),
            notifications: Notifications(agent_completion: false),
            decision_acceleration: DecisionAcceleration(enabled: false, scope: "both"),
            subagents: Subagents(
                providers: [
                    "opencode": Provider(enabled: false, binary_path: nil),
                    "codex": Provider(enabled: false, binary_path: nil),
                    "claude": Provider(enabled: false, binary_path: nil),
                    "kimi": Provider(enabled: false, binary_path: nil),
                    "grok": Provider(enabled: false, binary_path: nil),
                ].merging(AgentProviders.more.map { ($0, Provider(enabled: false, binary_path: nil)) }) { current, _ in current },
                defaultAgent: nil
            ),
            apps: Apps(mail: AppSend(confirm_send: true), messages: AppSend(confirm_send: true))
        )
    }
}

@MainActor
final class SettingsStore: ObservableObject {
    @Published var voiceEnabled = false
    @Published var voiceAskEveryTime = true
    /// The value read from disk; the server may change it (Always Allow in the
    /// voice dialog), so only a change made here is written back.
    private var loadedVoiceAskEveryTime = true
    var voiceConsentEdited: Bool { voiceAskEveryTime != loadedVoiceAskEveryTime }
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
    @Published var startAtLogin = true
    @Published var onboardingDismissed = false
    @Published var onboardingTour: String?
    @Published var steeringSessionMinutes = 10
    @Published var showToolActivity = false
    @Published var showLiveIsland = false
    @Published var requireToolDescriptions = false
    @Published var agentCompletionNotificationsEnabled = false
    @Published var mailConfirmSend = true
    @Published var messagesConfirmSend = true
    @Published var opencodeEnabled = false
    @Published var codexEnabled = false
    @Published var claudeEnabled = false
    @Published var kimiEnabled = false
    @Published var grokEnabled = false
    @Published var opencodeBinaryPath = ""
    @Published var codexBinaryPath = ""
    @Published var claudeBinaryPath = ""
    @Published var kimiBinaryPath = ""
    @Published var grokBinaryPath = ""
    @Published var moreProvidersEnabled: [String: Bool] = [:]
    @Published var moreProvidersBinaryPath: [String: String] = [:]
    @Published var defaultAgentProvider = ""
    @Published var defaultAgentModel = ""
    @Published var defaultAgentReasoning = ""
    static let defaultMaxActiveAgents = 8
    static let defaultAgentRetentionDays = 30
    @Published var maxActiveAgents = SettingsStore.defaultMaxActiveAgents
    @Published var agentRetentionDays = SettingsStore.defaultAgentRetentionDays
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

    /// Re-read only the voice consent choice, which the voice dialog's Always
    /// Allow can change behind this window, without touching other edits.
    func refreshVoiceConsent() {
        guard let data = try? Data(contentsOf: path),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        let consent = (object["voice"] as? [String: Any])?["transcription_consent"] as? String ?? "ask"
        let askEveryTime = consent != "always"
        loadedVoiceAskEveryTime = askEveryTime
        if voiceAskEveryTime != askEveryTime { voiceAskEveryTime = askEveryTime }
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

        voiceEnabled = current.experimental_tools["ask_user_voice"]?.enabled ?? false
        voiceAskEveryTime = (current.voice.transcription_consent ?? "ask") != "always"
        loadedVoiceAskEveryTime = voiceAskEveryTime
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
        startAtLogin = current.server.start_at_login ?? true
        ngrokOnStart = publicEndpointMode == "ngrok"
        steeringSessionMinutes = max(1, current.steering?.session_ttl_minutes ?? 10)
        showToolActivity = current.tool_activity?.show_bubble ?? false
        requireToolDescriptions = current.tool_activity?.require_descriptions ?? false
        showLiveIsland = current.tool_activity?.island ?? false
        if !requireToolDescriptions {
            showToolActivity = false
        }
        agentCompletionNotificationsEnabled = current.notifications?.agent_completion ?? false
        mailConfirmSend = current.apps?.mail?.confirm_send ?? true
        messagesConfirmSend = current.apps?.messages?.confirm_send ?? true
        onboardingDismissed = current.onboarding?.dismissed ?? false
        onboardingTour = current.onboarding?.tour
        decisionAccelerationEnabled = current.decision_acceleration?.enabled ?? false
        let rawDecisionScope = current.decision_acceleration?.scope?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        decisionAccelerationScope = ["off", "browser", "native", "both"].contains(rawDecisionScope) ? rawDecisionScope : "both"
        let providers = current.subagents?.providers ?? [:]
        opencodeEnabled = providerConfigValid ? (providers["opencode"]?.enabled ?? false) : false
        codexEnabled = providerConfigValid ? (providers["codex"]?.enabled ?? false) : false
        claudeEnabled = providerConfigValid ? (providers["claude"]?.enabled ?? false) : false
        kimiEnabled = providerConfigValid ? (providers["kimi"]?.enabled ?? false) : false
        grokEnabled = providerConfigValid ? (providers["grok"]?.enabled ?? false) : false
        opencodeBinaryPath = providers["opencode"]?.binary_path ?? ""
        codexBinaryPath = providers["codex"]?.binary_path ?? ""
        claudeBinaryPath = providers["claude"]?.binary_path ?? ""
        kimiBinaryPath = providers["kimi"]?.binary_path ?? ""
        grokBinaryPath = providers["grok"]?.binary_path ?? ""
        moreProvidersEnabled = Dictionary(uniqueKeysWithValues: AgentProviders.more.map {
            ($0, providerConfigValid ? (providers[$0]?.enabled ?? false) : false)
        })
        moreProvidersBinaryPath = Dictionary(uniqueKeysWithValues: AgentProviders.more.map { ($0, providers[$0]?.binary_path ?? "") })
        defaultAgentProvider = current.subagents?.defaultAgent?.provider ?? ""
        defaultAgentModel = current.subagents?.defaultAgent?.model ?? ""
        defaultAgentReasoning = current.subagents?.defaultAgent?.reasoning ?? ""
        maxActiveAgents = min(16, max(1, current.subagents?.maxActive ?? Self.defaultMaxActiveAgents))
        agentRetentionDays = min(3650, max(0, current.subagents?.retentionDays ?? Self.defaultAgentRetentionDays))
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
            voice: .init(language: language, input_device: inputDevice, output_device: outputDevice, tts_rate: ttsRate, timeout_s: timeoutSeconds, voice: voiceName, transcription_consent: voiceAskEveryTime == loadedVoiceAskEveryTime ? nil : (voiceAskEveryTime ? "ask" : "always")),
            server: .init(
                port: serverPort,
                cli_path: cliPath,
                ngrok_on_start: publicEndpointMode == "ngrok",
                public_endpoint_mode: publicEndpointMode,
                public_url: publicURL,
                cloudflare_tunnel: cloudflareTunnel,
                start_at_login: startAtLogin
            ),
            steering: .init(session_ttl_minutes: max(1, steeringSessionMinutes)),
            tool_activity: .init(
                show_bubble: showToolActivity && requireToolDescriptions,
                require_descriptions: requireToolDescriptions,
                island: showLiveIsland ? true : nil
            ),
            notifications: .init(agent_completion: agentCompletionNotificationsEnabled),
            decision_acceleration: .init(enabled: decisionAccelerationEnabled, scope: decisionAccelerationScope),
            subagents: .init(
                providers: [
                    "opencode": .init(enabled: opencodeEnabled, binary_path: opencodeBinaryPath.nilIfEmpty),
                    "codex": .init(enabled: codexEnabled, binary_path: codexBinaryPath.nilIfEmpty),
                    "claude": .init(enabled: claudeEnabled, binary_path: claudeBinaryPath.nilIfEmpty),
                    "kimi": .init(enabled: kimiEnabled, binary_path: kimiBinaryPath.nilIfEmpty),
                    "grok": .init(enabled: grokEnabled, binary_path: grokBinaryPath.nilIfEmpty),
                ].merging(AgentProviders.more.map {
                    ($0, .init(enabled: moreProvidersEnabled[$0] ?? false, binary_path: moreProvidersBinaryPath[$0]?.nilIfEmpty))
                }) { current, _ in current },
                defaultAgent: defaultAgentProvider.nilIfEmpty.map {
                    .init(provider: $0, model: defaultAgentModel.nilIfEmpty, reasoning: defaultAgentReasoning.nilIfEmpty)
                },
                maxActive: min(16, max(1, maxActiveAgents)),
                retentionDays: min(3650, max(0, agentRetentionDays))
            ),
            apps: .init(mail: .init(confirm_send: mailConfirmSend), messages: .init(confirm_send: messagesConfirmSend)),
            onboarding: .init(dismissed: onboardingDismissed, tour: onboardingTour)
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
        loadedVoiceAskEveryTime = voiceAskEveryTime
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
        case "claude": return claudeEnabled
        case "kimi": return kimiEnabled
        case "grok": return grokEnabled
        default: return moreProvidersEnabled[id] ?? false
        }
    }

    func setProviderEnabled(_ id: String, enabled: Bool) {
        switch id {
        case "opencode": opencodeEnabled = enabled
        case "codex": codexEnabled = enabled
        case "claude": claudeEnabled = enabled
        case "kimi": kimiEnabled = enabled
        case "grok": grokEnabled = enabled
        default:
            guard AgentProviders.more.contains(id) else { return }
            moreProvidersEnabled[id] = enabled
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
