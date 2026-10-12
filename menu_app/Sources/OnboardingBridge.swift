import Foundation

// The onboarding page (menu_app/Onboarding) asks the app to do things through
// window.webkit.messageHandlers.macmcp. Only this closed set of actions is understood;
// anything else is dropped. Foundation only, so it is tested on its own.

enum OnboardingAction: Equatable {
    case status
    case startServer
    case restartServer
    case requestPermission(String)
    case openSystemSettings(String)
    case connectConfig(String)
    case apps
    case connectApp(String)
    case copy(String)
    case openSettings(String)
    case finish(String)
}

struct OnboardingMessage: Equatable {
    let id: Int
    let action: OnboardingAction
}

enum OnboardingBridge {
    static let permissions: Set<String> = ["accessibility", "screen_recording"]
    static let clients: Set<String> = ["claude-code", "codex", "opencode", "claude-desktop", "chatgpt"]
    static let sections: Set<String> = ["start", "general", "connections", "permissions", "help"]
    static let outcomes: Set<String> = ["completed", "skipped"]
    static let copyLimit = 4096

    static func parse(_ body: Any) -> OnboardingMessage? {
        guard let message = body as? [String: Any],
              let id = message["id"] as? Int, id > 0,
              let name = message["action"] as? String else { return nil }
        let args = message["args"] as? [String: Any] ?? [:]
        func value(_ key: String, in allowed: Set<String>) -> String? {
            guard let text = args[key] as? String, allowed.contains(text) else { return nil }
            return text
        }
        let action: OnboardingAction?
        switch name {
        case "status": action = .status
        case "startServer": action = .startServer
        case "restartServer": action = .restartServer
        case "requestPermission": action = value("permission", in: permissions).map(OnboardingAction.requestPermission)
        case "openSystemSettings": action = value("permission", in: permissions).map(OnboardingAction.openSystemSettings)
        case "connectConfig": action = value("client", in: clients).map(OnboardingAction.connectConfig)
        case "apps": action = .apps
        case "connectApp": action = value("client", in: clients).map(OnboardingAction.connectApp)
        case "openSettings": action = value("section", in: sections).map(OnboardingAction.openSettings)
        case "finish": action = value("outcome", in: outcomes).map(OnboardingAction.finish)
        case "copy":
            if let text = args["text"] as? String, !text.isEmpty, text.utf8.count <= copyLimit {
                action = .copy(text)
            } else {
                action = nil
            }
        default: action = nil
        }
        return action.map { OnboardingMessage(id: id, action: $0) }
    }

    static func systemSettingsURL(_ permission: String) -> URL? {
        switch permission {
        case "accessibility":
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
        case "screen_recording":
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")
        default:
            return nil
        }
    }

    /// The JSON object a `mac-mcp ... --json` run printed; warnings may come before it.
    static func jsonResult(_ output: String) -> [String: Any]? {
        for line in output.split(separator: "\n").reversed() where line.hasPrefix("{") {
            if let object = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] {
                return object
            }
        }
        return nil
    }

    /// JavaScript that hands the answer to window.macmcp.receive. The payload is JSON with
    /// "<" escaped, so it can never close a script context or inject markup.
    static func replyScript(id: Int, ok: Bool, result: Any?, error: String?) -> String {
        var payload: [String: Any] = ["id": id, "ok": ok]
        if let result { payload["result"] = result }
        if let error { payload["error"] = error }
        let data = (try? JSONSerialization.data(withJSONObject: payload, options: [.fragmentsAllowed])) ?? Data("{}".utf8)
        let json = (String(data: data, encoding: .utf8) ?? "{}").replacingOccurrences(of: "<", with: "\\u003c")
        return "window.macmcp.receive(\(json));"
    }
}
