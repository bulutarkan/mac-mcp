import Foundation

/// Words each Settings section answers to, kept apart from the views so the
/// matching rules can be tested without building the UI.
enum SettingsSearchIndex {
    static let terms: [String: String] = [
        "general": "general overview update version status notifications alerts completion attention agents macos permission",
        "agents": "agents subagents provider codex opencode chatgpt model reasoning thinking default agent",
        "usage": "usage tokens payload calls errors latency heatmap activity input output top tools retention record history clear privacy data",
        "permissions": "permissions safety approvals security profile read only trusted access",
        "connections": "connections browser safari chrome mobile iphone ipad pairing public endpoint ngrok cloudflare tunnel https remote",
        "voice": "voice audio microphone speaker groq language",
        "advanced": "advanced developer runtime server port cli command decision acceleration openai decisions api key ambiguity",
        "help": "help diagnostics doctor troubleshoot problem support report bundle logs log bug issue security vulnerability version copy permissions accessibility screen recording automation microphone port conflict tunnel restart",
    ]

    static func tokens(_ text: String) -> [String] {
        text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
            .lowercased()
            .split(whereSeparator: { !$0.isLetter && !$0.isNumber })
            .map(String.init)
    }

    /// Every query word must start one of the section's words, in any order, so
    /// "agent notifications" and "server port" find their section.
    static func matches(query: String, title: String, terms: String) -> Bool {
        let wanted = tokens(query)
        guard !wanted.isEmpty else { return true }
        let words = tokens(title + " " + terms)
        return wanted.allSatisfy { token in words.contains { $0.hasPrefix(token) } }
    }
}
