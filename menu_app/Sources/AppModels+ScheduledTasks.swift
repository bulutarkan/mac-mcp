import Foundation
import SwiftUI

/// Scheduled Tasks (#175) as the app reads them from /dashboard/api/scheduled-tasks: the Settings pane
/// shows them and AppState announces finished runs.

struct ScheduledTask: Identifiable, Equatable {
    let raw: [String: Any]

    static func == (lhs: ScheduledTask, rhs: ScheduledTask) -> Bool {
        NSDictionary(dictionary: lhs.raw).isEqual(NSDictionary(dictionary: rhs.raw))
    }

    var id: String { raw["id"] as? String ?? "" }
    var name: String { raw["name"] as? String ?? "Untitled" }
    var description: String { raw["description"] as? String ?? "" }
    var kind: String { raw["kind"] as? String ?? "ai" }
    var isAI: Bool { kind == "ai" }
    var state: String { raw["state"] as? String ?? "active" }
    var scheduleText: String { raw["schedule_text"] as? String ?? "" }
    var schedule: [String: Any] { raw["schedule"] as? [String: Any] ?? [:] }
    var nextRunAt: Date? { (raw["next_run_at"] as? Double).map { Date(timeIntervalSince1970: $0) } }
    var lastRun: TaskRun? { (raw["last_run"] as? [String: Any]).map(TaskRun.init) }
    var proposal: [String: Any]? { raw["proposal"] as? [String: Any] }
    var approved: Bool { raw["approved"] as? Bool ?? true }
    var paused: Bool { raw["paused"] as? Bool ?? false }
    var provider: String? { raw["provider"] as? String }
    var cwd: String? { raw["cwd"] as? String }
    var notify: Bool { raw["notify"] as? Bool ?? true }
    /// Runs when a file arrives in a folder (#193) instead of at set times.
    var watchesFolder: Bool { (schedule["type"] as? String) == "folder" }
    /// The optional usage limit of an AI task (#211): max_tokens and/or max_cost_usd.
    var usageLimit: [String: Any]? { (raw["usage_limit"] as? [String: Any]).flatMap { $0.isEmpty ? nil : $0 } }

    static func describeLimit(_ limit: [String: Any]) -> String {
        var parts: [String] = []
        if let tokens = limit["max_tokens"] as? Int { parts.append(tokens.formatted() + " tokens") }
        if let cost = limit["max_cost_usd"] as? Double { parts.append(String(format: "$%.2f", cost)) }
        return parts.joined(separator: " or ")
    }
}

struct TaskRun: Identifiable {
    let raw: [String: Any]
    init(_ raw: [String: Any]) { self.raw = raw }
    var id: String { raw["id"] as? String ?? "" }
    var status: String { raw["status"] as? String ?? "" }
    var reason: String? { raw["reason"] as? String }
    var summary: String? { (raw["summary"] as? String).flatMap { $0.isEmpty ? nil : $0 } }
    var output: String? { raw["output"] as? String }
    var trigger: String { raw["trigger"] as? String ?? "schedule" }
    /// The file whose arrival started the run (#193).
    var triggerFile: String? { (raw["trigger_file"] as? String).flatMap { $0.isEmpty ? nil : $0 } }
    var triggerFileName: String? { triggerFile.map { URL(fileURLWithPath: $0).lastPathComponent } }
    /// The usage limit and what the run used (#211); "stopped" names the limit it reached.
    var usageLimit: [String: Any]? { detail?["usage_limit"] as? [String: Any] }
    /// A run that did not finish (#226): its steps are partial and what it changed elsewhere is unknown.
    var evidence: [String: Any]? { detail?["evidence"] as? [String: Any] }

    var evidenceLine: String? {
        guard let evidence else { return nil }
        var line = "It stopped before it finished, so the steps below are only what was seen"
        if let tokens = evidence["observed_tokens"] as? Int {
            line += ", at least \(tokens.formatted()) tokens were used"
        } else if (evidence["usage"] as? String) == "unavailable" {
            line += ", its usage was not reported"
        }
        return line + ", and whether it changed anything outside this Mac is not known."
    }
    var skippedCount: Int { raw["skipped_count"] as? Int ?? 0 }
    var exitCode: Int? { raw["exit_code"] as? Int }
    var duration: Double? { raw["duration_s"] as? Double }
    var startedAt: Date { Date(timeIntervalSince1970: raw["started_at"] as? Double ?? 0) }
    var scheduledFor: Date? { (raw["scheduled_for"] as? Double).map { Date(timeIntervalSince1970: $0) } }
    var detail: [String: Any]? { raw["detail"] as? [String: Any] }
    /// When the run's contents were removed (Settings › Privacy, #199); how it ended is kept.
    var detailsExpiredAt: Date? { (raw["details_expired_at"] as? Double).map { Date(timeIntervalSince1970: $0) } }
    /// The owner's output checks (#198): "verified" or "failed"; nil when the task has none or the run did not finish.
    var verification: [String: Any]? { raw["verification"] as? [String: Any] }
    var verificationState: String? { verification?["state"] as? String }
    /// "unverified": a check could not look (a page unreachable, an MCP server not connected, #228).
    var checkFailed: Bool { status == "done" && (verificationState == "failed" || verificationState == "unverified") }
    var checks: [[String: Any]] { verification?["checks"] as? [[String: Any]] ?? [] }

    /// "report.csv is missing"; in Private mode only the outcome is kept.
    var checkProblem: String? {
        guard checkFailed else { return nil }
        return checks.first(where: { ($0["ok"] as? Bool) != true }).map(Self.checkLine) ?? "its check failed"
    }

    static func checkLine(_ check: [String: Any]) -> String {
        if let kind = check["kind"] as? String, kind == "url" || kind == "tool" { return outsideCheckLine(check) }
        let name = (check["path"] as? String).map { URL(fileURLWithPath: $0).lastPathComponent } ?? "The file"
        if (check["ok"] as? Bool) == true {
            let size = ByteCountFormatter.string(fromByteCount: Int64(check["size"] as? Int ?? 0), countStyle: .file)
            return "\(name) is there (\(size))"
        }
        switch check["problem"] as? String {
        case "missing": return "\(name) is missing"
        case "too_small": return "\(name) is smaller than expected"
        case "not_written_this_run": return "\(name) was not written by this run"
        case "link": return "\(name) is a link, not a file"
        case "not_a_file": return "\(name) is not a file"
        case "unreadable": return "\(name) could not be checked"
        default: return "\(name) did not pass its check"
        }
    }

    /// A page or MCP tool check (#228), by its host or tool name.
    static func outsideCheckLine(_ check: [String: Any]) -> String {
        let name = (check["url"] as? String).flatMap { URL(string: $0)?.host } ?? check["tool"] as? String ?? "The check"
        if (check["ok"] as? Bool) == true { return "\(name) shows what it should" }
        switch check["problem"] as? String {
        case "missing_text": return "\(name) doesn't show the expected text"
        case "status": return "\(name) answered with status \(check["status"] as? Int ?? 0)"
        case "unreachable": return "\(name) could not be reached"
        case "server_unavailable": return "\(name)'s MCP server is not connected"
        case "not_read_only": return "\(name) is not a read-only tool, so it was not used"
        case "timeout": return "\(name) did not answer in time"
        case "tool_error": return "\(name) returned an error"
        default: return "\(name) did not pass its check"
        }
    }

    static func displayPath(_ path: String) -> String {
        let home = NSHomeDirectory()
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }

    var symbol: String {
        if checkFailed { return "exclamationmark.triangle.fill" }
        if status == "done" && verificationState == "verified" { return "checkmark.seal.fill" }
        switch status {
        case "done": return "checkmark.circle.fill"
        case "skipped": return "forward.end.circle.fill"
        case "running": return "clock.arrow.circlepath"
        case "timed_out": return "hourglass.circle.fill"
        default: return "xmark.circle.fill"
        }
    }

    var color: Color {
        if checkFailed { return .orange }
        switch status {
        case "done": return .green
        case "skipped": return .orange
        case "running": return .accentColor
        default: return .red
        }
    }

    /// "Done this morning", "Skipped yesterday: Mac was asleep", "Failed: Exited with code 3".
    var shortLine: String {
        let when = TaskTime.relativeDay(scheduledFor ?? startedAt)
        switch status {
        case "done": return checkProblem.map { "Done \(when), but \($0)" } ?? "Done \(when)"
        case "running": return "Running now"
        case "skipped":
            let many = skippedCount > 1 ? "Skipped \(skippedCount) runs" : "Skipped \(when)"
            return reason.map { "\(many): \($0)" } ?? many
        case "timed_out": return "Stopped \(when): time limit"
        default: return "Failed \(when)" + (reason.map { ": \($0)" } ?? "")
        }
    }
}

enum TaskTime {
    static func relativeDay(_ date: Date) -> String {
        let calendar = Calendar.current
        if calendar.isDateInToday(date) {
            let hour = calendar.component(.hour, from: date)
            return hour < 12 ? "this morning" : hour < 18 ? "this afternoon" : "this evening"
        }
        if calendar.isDateInYesterday(date) { return "yesterday" }
        return "on " + date.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated))
    }

    /// "today at 18:00", "tomorrow at 09:00", "Mon 20 Oct at 09:00".
    static func upcoming(_ date: Date) -> String {
        let calendar = Calendar.current
        let clock = date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))
        if calendar.isDateInToday(date) { return "today at \(clock)" }
        if calendar.isDateInTomorrow(date) { return "tomorrow at \(clock)" }
        return date.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated)) + " at \(clock)"
    }

    static func stamp(_ date: Date) -> String {
        let calendar = Calendar.current
        let clock = date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))
        if calendar.isDateInToday(date) { return "Today, \(clock)" }
        if calendar.isDateInYesterday(date) { return "Yesterday, \(clock)" }
        return date.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated)) + ", \(clock)"
    }

    static func duration(_ seconds: Double) -> String {
        if seconds < 60 { return "\(Int(seconds.rounded())) s" }
        if seconds < 3600 { return "\(Int((seconds / 60).rounded())) min" }
        return String(format: "%.1f h", seconds / 3600)
    }
}

extension AppState {
    /// A dashboard call that hands back the server's error message instead of throwing on 4xx.
    func taskAPI(_ path: String, body: [String: Any]? = nil) async -> (ok: Bool, object: [String: Any]) {
        guard let base = URL(string: "http://127.0.0.1:\(settings.serverPort)") else {
            return (false, ["message": "Mac MCP is not running."])
        }
        var components = URLComponents(url: base.appendingPathComponent(path.components(separatedBy: "?")[0]),
                                       resolvingAgainstBaseURL: false)
        if let query = path.components(separatedBy: "?").dropFirst().first {
            components?.percentEncodedQuery = query
        }
        guard let url = components?.url else { return (false, ["message": "Bad request."]) }
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 20
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        authorizeDashboardRequest(&request)
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            return ((200..<300).contains(status), object)
        } catch {
            return (false, ["message": "Mac MCP is not answering. Is it running?"])
        }
    }
}
