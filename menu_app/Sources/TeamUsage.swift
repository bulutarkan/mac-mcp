import AppKit
import SwiftUI

// MARK: Usage › Teams, the data: the saved teams (Settings › Agents › Teams) that ran, by member, day and run,
// read from /dashboard/api/team-usage. The same provider tokens as Delegated Agents, grouped by team.

struct TeamUsageMember: Identifiable {
    let key: String
    let name: String
    let provider: String
    let model: String?
    let reasoning: String?
    let role: String?
    let tokens: Int
    let share: Double
    let runs: Int
    var id: String { key }
}

/// One member in one run, every agent it took (revisions too) together.
struct TeamUsageRunMember: Identifiable {
    let key: String
    let name: String
    let provider: String
    let model: String?
    let reasoning: String?
    let role: String?
    let tokens: Int
    let durationSeconds: Double?
    let status: String
    let gate: String?
    var id: String { key }
}

struct TeamUsageRun: Identifiable {
    let teamID: String
    let title: String
    let startedAt: Date
    let durationSeconds: Double?
    let tokens: Int
    let members: Int
    let completed: Int
    let running: Int
    let failed: Int
    let gates: Int
    let gatesPassed: Int
    let rows: [TeamUsageRunMember]
    var id: String { teamID }
}

struct TeamUsageTeam: Identifiable {
    let id: String
    let name: String
    let exists: Bool
    let tokens: Int
    let share: Double
    let runs: Int
    let lastRunAt: Date?
    let averageDurationSeconds: Double?
    let gates: Int
    let gatesPassed: Int
    let members: [TeamUsageMember]
    /// Each "yyyy-MM-dd" day's tokens by member key.
    let daily: [String: [String: Int]]
    let runList: [TeamUsageRun]
}

struct TeamUsageSummary {
    let totalTokens: Int
    let runs: Int
    let teams: [TeamUsageTeam]
    let otherTokens: Int
    let otherRuns: Int
    let unreportedTurns: Int
    let availableSince: String?

    init(_ object: [String: Any]) {
        func int(_ value: Any?) -> Int { (value as? NSNumber)?.intValue ?? 0 }
        func double(_ value: Any?) -> Double? { (value as? NSNumber)?.doubleValue }
        func text(_ value: Any?) -> String? {
            guard let value = value as? String, !value.isEmpty else { return nil }
            return value
        }
        func date(_ value: Any?) -> Date? { double(value).map { Date(timeIntervalSince1970: $0) } }
        totalTokens = int(object["total_tokens"])
        runs = int(object["runs"])
        let other = object["other_teams"] as? [String: Any] ?? [:]
        otherTokens = int(other["tokens"])
        otherRuns = int(other["runs"])
        unreportedTurns = int(object["unreported_turns"])
        availableSince = text(object["available_since"])
        teams = (object["teams"] as? [[String: Any]] ?? []).map { team in
            let members = (team["members"] as? [[String: Any]] ?? []).map {
                TeamUsageMember(key: text($0["key"]) ?? "?", name: text($0["name"]) ?? "?",
                                provider: text($0["provider"]) ?? "", model: text($0["model"]),
                                reasoning: text($0["reasoning"]), role: text($0["role"]),
                                tokens: int($0["tokens"]), share: double($0["share"]) ?? 0, runs: int($0["runs"]))
            }
            var daily: [String: [String: Int]] = [:]
            for day in team["daily"] as? [[String: Any]] ?? [] {
                guard let key = text(day["date"]) else { continue }
                daily[key] = (day["members"] as? [String: Any] ?? [:]).mapValues { int($0) }
            }
            let runs = (team["run_list"] as? [[String: Any]] ?? []).map { run -> TeamUsageRun in
                let outcome = run["outcome"] as? [String: Any] ?? [:]
                let rows = (run["members"] as? [[String: Any]] ?? []).map {
                    TeamUsageRunMember(key: text($0["key"]) ?? "?", name: text($0["name"]) ?? "?",
                                       provider: text($0["provider"]) ?? "", model: text($0["model"]),
                                       reasoning: text($0["reasoning"]), role: text($0["role"]),
                                       tokens: int($0["tokens"]), durationSeconds: double($0["duration_s"]),
                                       status: text($0["status"]) ?? "running", gate: text($0["gate"]))
                }
                return TeamUsageRun(teamID: text(run["team_id"]) ?? UUID().uuidString, title: text(run["title"]) ?? "",
                                    startedAt: date(run["started_at"]) ?? Date(timeIntervalSince1970: 0),
                                    durationSeconds: double(run["duration_s"]), tokens: int(run["tokens"]),
                                    members: int(outcome["members"]), completed: int(outcome["completed"]),
                                    running: int(outcome["running"]), failed: int(outcome["failed"]),
                                    gates: int(outcome["gates"]), gatesPassed: int(outcome["gates_passed"]), rows: rows)
            }
            return TeamUsageTeam(id: text(team["id"]) ?? "?", name: text(team["name"]) ?? "Team",
                                 exists: team["exists"] as? Bool ?? true, tokens: int(team["tokens"]),
                                 share: double(team["share"]) ?? 0, runs: int(team["runs"]),
                                 lastRunAt: date(team["last_run_at"]),
                                 averageDurationSeconds: double(team["average_duration_s"]),
                                 gates: int(team["gates"]), gatesPassed: int(team["gates_passed"]),
                                 members: members, daily: daily, runList: runs)
        }
    }

    /// Saved teams as Settings › Agents lists them now; teams deleted since keep their history apart.
    var saved: [TeamUsageTeam] { teams.filter(\.exists) }
    var deleted: [TeamUsageTeam] { teams.filter { !$0.exists } }
}

enum TeamUsageFormat {
    /// "At" for Atlas, "NS" for Night Shift: what a member's tile shows.
    static func initials(_ name: String) -> String {
        let words = name.split(whereSeparator: { !$0.isLetter && !$0.isNumber })
        if words.count > 1 { return words.prefix(2).compactMap(\.first).map { String($0).uppercased() }.joined() }
        guard let word = words.first else { return "?" }
        return String(word.prefix(2)).capitalized
    }

    /// "26 s", "6 min", "1 h 5 min", or "—" while it is not known.
    static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds >= 0 else { return "—" }
        if seconds < 60 { return "\(max(1, Int(seconds.rounded()))) s" }
        let minutes = Int((seconds / 60).rounded())
        if minutes < 60 { return "\(minutes) min" }
        return minutes % 60 == 0 ? "\(minutes / 60) h" : "\(minutes / 60) h \(minutes % 60) min"
    }

    /// "today 15:45", "yesterday 02:10", "Oct 24": when a team last ran.
    static func when(_ date: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        let time = date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))
        if calendar.isDate(date, inSameDayAs: now) { return "today \(time)" }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: yesterday) {
            return "yesterday \(time)"
        }
        return calendar.isDate(date, equalTo: now, toGranularity: .year)
            ? date.formatted(.dateTime.month(.abbreviated).day())
            : date.formatted(.dateTime.month(.abbreviated).day().year())
    }

    /// A run's title without the team's own name in front ("Vurucu Team kısa test" is "kısa test").
    static func runTitle(_ title: String, team: String) -> String {
        let trimmed = title.trimmingCharacters(in: .whitespaces)
        guard !team.isEmpty, trimmed.lowercased().hasPrefix(team.lowercased()) else { return trimmed.isEmpty ? team : trimmed }
        let rest = trimmed.dropFirst(team.count).trimmingCharacters(in: CharacterSet(charactersIn: " ·:-—"))
        return rest.isEmpty ? trimmed : rest
    }

    /// "4/4 · review passed", "3/4 · 1 failed", "2/4 done, running": how a run ended, and whether it went well.
    static func outcome(_ run: TeamUsageRun) -> (text: String, good: Bool?) {
        if run.running > 0 { return ("\(run.completed)/\(run.members) done, running", nil) }
        var parts = ["\(run.completed)/\(run.members)"]
        if run.failed > 0 { parts.append("\(run.failed) failed") }
        if run.gates > 0 { parts.append(run.gatesPassed == run.gates ? "review passed" : "review failed") }
        return (parts.joined(separator: " · "), run.failed == 0 && run.gatesPassed == run.gates)
    }

    /// One member's line in a run: what it did, a reviewer's gate before its status.
    static func status(_ member: TeamUsageRunMember) -> String {
        if member.gate == "pass" { return "review passed" }
        if member.gate == "fail" { return "review failed" }
        switch member.status {
        case "completed": return "done"
        case "running": return "running"
        default: return member.status
        }
    }
}

enum TeamUsageColors {
    /// Each member in its provider's colour; a second member on the same provider in a lighter shade of it and
    /// a third in a darker one, so the bars and the list still tell them apart.
    static func members(_ members: [TeamUsageMember]) -> [String: Color] {
        var seen: [String: Int] = [:]
        var colors: [String: Color] = [:]
        for member in members {
            let index = seen[member.provider, default: 0]
            seen[member.provider] = index + 1
            colors[member.key] = shade(member.provider, index)
        }
        return colors
    }

    /// The provider's colour mixed toward white (odd turns) or black (even turns), more for each turn.
    static func shade(_ provider: String, _ index: Int) -> Color {
        guard index > 0 else { return UsageColors.agent(provider) }
        let base = NSColor(UsageColors.agent(provider))
        let fraction = CGFloat(min(0.6, 0.32 * Double((index + 1) / 2)))
        let toward: NSColor = index % 2 == 1 ? .white : .black
        return Color(nsColor: NSColor(name: nil) { appearance in
            var mixed = base
            appearance.performAsCurrentDrawingAppearance {
                mixed = base.usingColorSpace(.sRGB)?.blended(withFraction: fraction, of: toward) ?? base
            }
            return mixed
        })
    }
}
