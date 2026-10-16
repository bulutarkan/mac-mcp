import Foundation

/// Keeps elapsed session age separate from tool execution time. Missing metrics
/// from older servers stay unknown, rather than appearing to be zero.
struct SteeringSessionPresentation {
    let session: SteeringSession
    let now: Double
    let isStale: Bool

    var status: String {
        let activity = session.isWorking ? "Working" : "Idle"
        return isStale ? "Last known: \(activity)" : activity
    }

    var callCount: String { session.toolCallCount.map { String(max(0, $0)) } ?? "—" }
    var sessionDuration: String { Self.duration(now - session.createdAt) }
    var summary: String {
        let count = session.toolCallCount == 1 ? "1 call" : "\(callCount) calls"
        let base = "\(Self.duration(now - session.createdAt, compact: true)) · \(count)"
        let lifecycle = session.effectiveLifecycleState
        return lifecycle.isSteeringNotice ? "\(base) · \(lifecycle.label)" : base
    }
    var activityDuration: String? {
        guard session.isWorking else { return nil }
        if !isStale, let startedAt = session.activityStartedAt {
            return Self.duration(now - startedAt)
        }
        return Self.duration(Double(session.activityMS) / 1000)
    }
    var lastActivity: String {
        if !isStale && session.isWorking { return "Now" }
        return "\(Self.duration(now - session.lastActivityAt, compact: true)) ago"
    }

    private static func duration(_ interval: Double, compact: Bool = false) -> String {
        let seconds = Int(max(0, min(interval.isFinite ? interval : 0, Double(Int.max / 2))))
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        if minutes < 60 { return compact ? "\(minutes)m" : "\(minutes)m \(seconds % 60)s" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours)h \(minutes % 60)m" }
        return "\(hours / 24)d \(hours % 24)h"
    }
}
