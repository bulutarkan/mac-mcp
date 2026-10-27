import Foundation

/// What a team member is for. Absent means the member has no fixed role.
enum TeamPresetRole: String, CaseIterable, Identifiable {
    case coder
    case reviewer
    case orchestrator

    var id: String { rawValue }

    var title: String {
        switch self {
        case .coder: return "Coder"
        case .reviewer: return "Reviewer"
        case .orchestrator: return "Orchestrator"
        }
    }
}

/// One seat on a saved team: who it is and which provider, model and thinking it runs with.
struct TeamPresetMember: Identifiable, Equatable {
    var id: String
    var name: String
    var provider: String
    /// Empty means the provider's default model.
    var model: String = ""
    /// Empty means the provider's default thinking.
    var reasoning: String = ""
    var role: TeamPresetRole?
}

/// A named team the owner keeps in Settings › Agents › Teams, stored in settings.json under
/// `subagents.team_presets`. Member order is the team's default task order.
struct TeamPreset: Identifiable, Equatable {
    static let maxMembers = 10
    /// Name limits count Unicode scalars (`length`), not characters on screen: the server counts code
    /// points, so a family emoji (one character, seven scalars) counts as seven on both sides.
    static let maxNameLength = 60
    static let maxMemberNameLength = 40
    static let settingsKey = "team_presets"

    var id: String
    var name: String
    var members: [TeamPresetMember]
    var updatedAt: Date?

    /// Short names a new member gets until the owner renames it.
    static let suggestedNames = [
        "Atlas", "Nova", "Echo", "Orion", "Juno", "Pixel", "Rook", "Vega", "Comet", "Sage",
        "Flint", "Iris", "Kite", "Lumen", "Onyx", "Quill", "Remy", "Sol", "Tango", "Wren",
    ]

    static func newPresetID(existing: [TeamPreset]) -> String {
        newID(prefix: "tp_", taken: Set(existing.map(\.id)))
    }

    func newMemberID() -> String {
        Self.newID(prefix: "m_", taken: Set(members.map(\.id)))
    }

    /// `prefix` and 8 lowercase hex digits, not already taken.
    static func newID(prefix: String, taken: Set<String>) -> String {
        while true {
            let id = prefix + String(format: "%08x", UInt32.random(in: .min ... .max))
            if !taken.contains(id) { return id }
        }
    }

    /// The first suggested name no member uses yet.
    func suggestedMemberName() -> String {
        let used = Set(members.map { Self.folded($0.name) })
        if let name = Self.suggestedNames.first(where: { !used.contains(Self.folded($0)) }) { return name }
        var number = members.count + 1
        while used.contains(Self.folded("Agent \(number)")) { number += 1 }
        return "Agent \(number)"
    }

    /// A new member for `provider` with the provider's defaults, or nil when the team is full.
    func makeMember(provider: String) -> TeamPresetMember? {
        guard members.count < Self.maxMembers else { return nil }
        return TeamPresetMember(id: newMemberID(), name: suggestedMemberName(), provider: provider)
    }

    /// Adds a member for `provider` at slot `index` (or at the end); false when the team is full.
    @discardableResult
    mutating func addMember(provider: String, at index: Int? = nil) -> Bool {
        guard let member = makeMember(provider: provider) else { return false }
        members.insert(member, at: min(max(0, index ?? members.count), members.count))
        return true
    }

    /// Puts a member into slot `index`; a slot past the last member puts it last.
    @discardableResult
    mutating func moveMember(id: String, to index: Int) -> Bool {
        guard let from = members.firstIndex(where: { $0.id == id }) else { return false }
        let member = members.remove(at: from)
        members.insert(member, at: min(max(0, index), members.count))
        return from != members.firstIndex(where: { $0.id == id })
    }

    mutating func removeMember(id: String) {
        members.removeAll { $0.id == id }
    }

    /// A copy under a free name ("Night Shift Copy", "Night Shift Copy 2") with a new id.
    func duplicate(among presets: [TeamPreset]) -> TeamPreset {
        let used = Set(presets.map { Self.folded($0.name) })
        let base = Self.prefix(Self.trimmed(name), length: Self.maxNameLength - 10) + " Copy"
        var candidate = base
        var number = 2
        while used.contains(Self.folded(candidate)) {
            candidate = "\(base) \(number)"
            number += 1
        }
        return TeamPreset(id: Self.newPresetID(existing: presets), name: candidate, members: members, updatedAt: nil)
    }

    // MARK: Checks

    struct Issues: Equatable {
        var name: String?
        var members: String?
        /// By member id.
        var memberNames: [String: String] = [:]
        var memberProviders: [String: String] = [:]

        var isEmpty: Bool { name == nil && members == nil && memberNames.isEmpty && memberProviders.isEmpty }
    }

    /// What must change before the team can be saved; `others` are the other saved teams.
    func issues(among others: [TeamPreset]) -> Issues {
        var issues = Issues()
        let teamName = Self.trimmed(name)
        if teamName.isEmpty {
            issues.name = "Give the team a name."
        } else if Self.length(teamName) > Self.maxNameLength {
            issues.name = Self.tooLong(Self.maxNameLength)
        } else if others.contains(where: { $0.id != id && Self.folded($0.name) == Self.folded(teamName) }) {
            issues.name = "Another team is already called “\(teamName)”."
        }
        if members.isEmpty {
            issues.members = "Add at least one member."
        } else if members.count > Self.maxMembers {
            issues.members = "A team holds up to \(Self.maxMembers) members."
        }
        var seen: [String: Int] = [:]
        for member in members { seen[Self.folded(member.name), default: 0] += 1 }
        for member in members {
            let memberName = Self.trimmed(member.name)
            if memberName.isEmpty {
                issues.memberNames[member.id] = "Name this member."
            } else if Self.length(memberName) > Self.maxMemberNameLength {
                issues.memberNames[member.id] = Self.tooLong(Self.maxMemberNameLength)
            } else if (seen[Self.folded(memberName)] ?? 0) > 1 {
                issues.memberNames[member.id] = "Name already used in this team."
            }
            if Self.trimmed(member.provider).isEmpty {
                issues.memberProviders[member.id] = "Choose a provider."
            }
        }
        return issues
    }

    static func trimmed(_ text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// A name's length as the server counts it: Unicode scalars (Python code points).
    static func length(_ text: String) -> Int {
        text.unicodeScalars.count
    }

    /// The longest start of `text` within `length` Unicode scalars, never cutting a character in two.
    static func prefix(_ text: String, length limit: Int) -> String {
        var kept = ""
        var used = 0
        for character in text {
            used += character.unicodeScalars.count
            guard used <= limit else { break }
            kept.append(character)
        }
        return kept
    }

    private static func tooLong(_ limit: Int) -> String {
        "Use \(limit) characters or fewer (an emoji can count as several)."
    }

    /// Names compare without case, accents or surrounding spaces.
    static func folded(_ text: String) -> String {
        trimmed(text).folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
    }

    // MARK: settings.json

    private static let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    /// The preset as the server reads it: names trimmed, defaults and no role left out.
    func jsonObject() -> [String: Any] {
        var object: [String: Any] = [
            "id": id,
            "name": Self.trimmed(name),
            "members": members.map { member -> [String: Any] in
                var item: [String: Any] = [
                    "id": member.id,
                    "name": Self.trimmed(member.name),
                    "provider": Self.trimmed(member.provider),
                ]
                if !Self.trimmed(member.model).isEmpty { item["model"] = Self.trimmed(member.model) }
                if !Self.trimmed(member.reasoning).isEmpty { item["reasoning"] = Self.trimmed(member.reasoning) }
                if let role = member.role { item["role"] = role.rawValue }
                return item
            },
        ]
        if let updatedAt { object["updated_at"] = Self.isoFormatter.string(from: updatedAt) }
        return object
    }

    /// A preset read from settings.json; nil when it lacks an id, a name or readable members.
    init?(jsonObject object: [String: Any]) {
        guard let id = object["id"] as? String, !id.isEmpty,
              let name = object["name"] as? String,
              let rawMembers = object["members"] as? [[String: Any]] else { return nil }
        var members: [TeamPresetMember] = []
        for raw in rawMembers {
            guard let memberID = raw["id"] as? String, !memberID.isEmpty,
                  let memberName = raw["name"] as? String,
                  let provider = raw["provider"] as? String else { return nil }
            members.append(TeamPresetMember(
                id: memberID,
                name: memberName,
                provider: provider,
                model: raw["model"] as? String ?? "",
                reasoning: raw["reasoning"] as? String ?? "",
                role: (raw["role"] as? String).flatMap { TeamPresetRole(rawValue: $0.lowercased()) }
            ))
        }
        self.init(
            id: id,
            name: name,
            members: members,
            updatedAt: (object["updated_at"] as? String).flatMap { Self.isoFormatter.date(from: $0) }
        )
    }

    init(id: String, name: String, members: [TeamPresetMember], updatedAt: Date? = nil) {
        self.id = id
        self.name = name
        self.members = members
        self.updatedAt = updatedAt
    }
}
