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
    var skippedCount: Int { raw["skipped_count"] as? Int ?? 0 }
    var exitCode: Int? { raw["exit_code"] as? Int }
    var duration: Double? { raw["duration_s"] as? Double }
    var startedAt: Date { Date(timeIntervalSince1970: raw["started_at"] as? Double ?? 0) }
    var scheduledFor: Date? { (raw["scheduled_for"] as? Double).map { Date(timeIntervalSince1970: $0) } }
    var detail: [String: Any]? { raw["detail"] as? [String: Any] }

    var symbol: String {
        switch status {
        case "done": return "checkmark.circle.fill"
        case "skipped": return "forward.end.circle.fill"
        case "running": return "clock.arrow.circlepath"
        case "timed_out": return "hourglass.circle.fill"
        default: return "xmark.circle.fill"
        }
    }

    var color: Color {
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
        case "done": return "Done \(when)"
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
