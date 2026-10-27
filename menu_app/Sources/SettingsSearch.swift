import Foundation

/// Where a search lands: a Settings pane by its id, the tab in it ("browser", "skills") when the
/// words name one, and how well it matched.
struct SettingsSearchHit: Equatable {
    let section: String
    let tab: String?
    let score: Int
}

/// Words each Settings section answers to, kept apart from the views so the
/// matching rules can be tested without building the UI.
enum SettingsSearchIndex {
    static let terms: [String: String] = [
        "start": "get started setup first run onboarding checklist begin connect ai app client first request permissions accessibility screen recording",
        "general": "general overview status server connected degraded disconnected update version issues remote access caught up",
        "activity": "activity notifications alerts completion attention agents macos permission tool bubble speech descriptions require show preview cursor pointer mouse desktop",
        "agents": "agents subagents provider providers codex opencode claude code model reasoning thinking default agent history retention delete keep team teams team presets saved teams squad roster members role coder reviewer orchestrator team history tool calls tokens messages between agents team messages skills skill instructions",
        "usage": "usage tokens payload calls errors latency heatmap activity input output top tools retention record history clear privacy data memory memories export delete",
        "permissions": "permissions safety approvals security profile read only trusted access saved logins passwords sign in keychain import",
        "scheduled": "scheduled tasks schedule cron recurring automation automate timer daily weekly monthly script run later approval review ai task recipe recipes saved steps shortcuts siri replay",
        "privacy": "privacy pause observation private mode incognito exclude never observe apps sites do not observe stores data kept retention where data goes recipients providers",
        "apps": "apps mail messages imessage email send confirm confirmation ask before sending read search messages full disk access xcode notes calendar shortcuts shortcut allowed",
        "mcpServers": "mcp servers server tools connect add exa github notion slack google drive service sign in token stdio remote url command json claude desktop cursor integration plugin extension",
        "macs": "macs other mac second mac computers fleet pair invite invitation code remote status recipes run on another mac devices iphone phone iphone mirroring telefon tap type",
        "connections": "connections browser safari chrome mobile iphone ipad pairing public endpoint ngrok cloudflare tunnel https remote ai clients connect claude code chatgpt codex opencode setup config",
        "voice": "voice audio microphone speaker groq language consent recording transcription",
        "advanced": "advanced developer runtime server port cli command decision acceleration openai decisions api key ambiguity",
        "help": "help diagnostics doctor troubleshoot problem support report bundle logs log bug issue security vulnerability version copy permissions accessibility screen recording automation microphone port conflict tunnel restart",
    ]

    /// What people type that no label on the pane says, by pane or "pane/tab" (#125). Visible
    /// labels come from the views themselves (SettingsSearchLabels.swift, generated).
    static let aliases: [String: [String]] = [
        "general": ["login", "start at login", "open at login", "launch at login", "login item", "startup"],
        "agents/agents-general": ["max active agents", "maximum agents", "concurrent agents", "parallel agents"],
        "connections/browser": ["browser", "browser activity", "browser extension", "safari extension", "chrome extension",
                                "visual companion"],
        "advanced": ["decision scope", "ambiguous targets", "openai decisions"],
        "voice": ["voice name", "speech rate", "rate", "text to speech", "answer time"],
    ]

    /// The tabs of tabbed panes, by the title each one shows.
    static let tabTitles: [String: [String]] = [
        "agents/agents-general": ["General"], "agents/providers": ["Providers"], "agents/skills": ["Skills"],
        "agents/teams": ["Teams"], "agents/team-history": ["Team History"],
        "usage/tool-usage": ["Tools"], "usage/agent-usage": ["Delegated Agents"], "usage/chat-usage": ["Chat"],
        "usage/usage-data": ["Data & Storage"],
        "privacy/protection": ["Protection"], "privacy/documents": ["Documents"], "privacy/your-data": ["Your Data"],
        "apps/sending": ["Mail & Messages"], "apps/shortcuts": ["Shortcuts"], "apps/app-support": ["App Support"],
        "connections/browser": ["Browser"], "connections/mobile": ["Mobile"], "connections/ai-clients": ["AI Clients"],
        "scheduled/scheduled-tasks": ["Scheduled Tasks"], "scheduled/recipes": ["Recipes"],
    ]

    /// Every word a section answers to: its terms, its labels, its tabs and its aliases, with words a
    /// label writes apart also written together ("log in" is found by "login").
    static func searchTerms(for section: String) -> String {
        allTerms[section] ?? terms[section] ?? ""
    }

    private static let allTerms: [String: String] = {
        var result: [String: String] = [:]
        for section in terms.keys {
            let texts = SettingsSearchIndex.phrases(for: section).map { $0.text }
            let words = [terms[section] ?? ""] + texts + texts.flatMap { SettingsSearchIndex.joined($0) }
            result[section] = words.joined(separator: " ")
        }
        return result
    }()

    /// A phrase a section shows or answers to, the tab it is on, and how much a match on it counts:
    /// an alias most, then the pane's own title (scored in `hit`), a label or a tab title, then any word.
    private static let aliasWeight = 5

    private struct Phrase {
        let tab: String?
        let text: String
        let weight: Int
    }

    private static func phrases(for section: String) -> [Phrase] {
        func from(_ table: [String: [String]], weight: Int) -> [Phrase] {
            table.keys.sorted().flatMap { key -> [Phrase] in
                let parts = key.split(separator: "/", maxSplits: 1).map(String.init)
                guard parts.first == section else { return [] }
                let tab = parts.count > 1 ? parts[1] : nil
                return table[key, default: []].map { Phrase(tab: tab, text: $0, weight: weight) }
            }
        }
        // On a tie the earlier phrase wins: a tab's own title before a label somewhere on the pane.
        return from(aliases, weight: aliasWeight) + from(tabTitles, weight: 3) + from(labels, weight: 3)
    }

    static func tokens(_ text: String) -> [String] {
        text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
            .lowercased()
            .split(whereSeparator: { !$0.isLetter && !$0.isNumber })
            .map(String.init)
    }

    /// "Start Mac MCP when you log in" also as "startmac", "login" and so on: each pair of neighbours as one word.
    private static func joined(_ text: String) -> [String] {
        let words = tokens(text)
        guard words.count > 1 else { return [] }
        return (1..<words.count).map { words[$0 - 1] + words[$0] }
    }

    /// Every query word must start one of the section's words, in any order, so
    /// "agent notifications" and "server port" find their section.
    static func matches(query: String, title: String, terms: String) -> Bool {
        let wanted = tokens(query)
        guard !wanted.isEmpty else { return true }
        let words = tokens(title + " " + terms)
        return wanted.allSatisfy { token in words.contains { $0.hasPrefix(token) } }
    }

    /// The query words in a row inside the phrase, each starting a word of it (or two of its words
    /// written together), so "max active" is in "max active agents" and "login" in "when you log in".
    /// An anchored phrase must match from its first word, so "agent" does not find "max active agents".
    static func phraseContains(_ wanted: [String], _ phrase: String, anchored: Bool = false) -> Bool {
        let words = tokens(phrase)
        guard !wanted.isEmpty, !words.isEmpty else { return false }
        func match(_ query: Int, _ word: Int) -> Bool {
            if query == wanted.count { return true }
            guard word < words.count else { return false }
            if words[word].hasPrefix(wanted[query]), match(query + 1, word + 1) { return true }
            if word + 1 < words.count, wanted[query].count > words[word].count,
               (words[word] + words[word + 1]).hasPrefix(wanted[query]), match(query + 1, word + 2) { return true }
            return false
        }
        return anchored ? match(0, 0) : words.indices.contains { match(0, $0) }
    }

    /// How well one section matches, and the tab the best phrase is on; nil when it does not match.
    static func hit(query: String, section: String, title: String) -> SettingsSearchHit? {
        let wanted = tokens(query)
        guard !wanted.isEmpty, matches(query: query, title: title, terms: searchTerms(for: section)) else { return nil }
        var best = SettingsSearchHit(section: section, tab: nil, score: phraseContains(wanted, title) ? 4 : 1)
        for phrase in phrases(for: section) where phrase.weight > best.score
            && phraseContains(wanted, phrase.text, anchored: phrase.weight == aliasWeight) {
            best = SettingsSearchHit(section: section, tab: phrase.tab, score: phrase.weight)
        }
        return best
    }

    /// Where a search should land: the best match, in sidebar order on a tie, but the open pane
    /// stays while it matches as well as any other. Nil for an empty query or no match.
    static func target(query: String, sections: [(id: String, title: String)], current: String?) -> SettingsSearchHit? {
        let hits = sections.compactMap { hit(query: query, section: $0.id, title: $0.title) }
        guard var best = hits.first else { return nil }
        for candidate in hits.dropFirst() where candidate.score > best.score { best = candidate }
        if let current, let open = hits.first(where: { $0.section == current }), open.score >= best.score {
            return open
        }
        return best
    }
}
