import SwiftUI

/// A transient inspector anchored to its session row; it never starts network reads.
struct SessionDetailView: View {
    let session: SteeringSession
    let isStale: Bool
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let metrics = SteeringSessionPresentation(
                session: session, now: context.date.timeIntervalSince1970, isStale: isStale
            )
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "rectangle.stack.fill")
                        .font(.title3).foregroundStyle(Color.accentColor)
                        .padding(9)
                        .background(Color.accentColor.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Agent \(session.flowNumber)").font(.headline)
                        Label(metrics.status, systemImage: isStale ? "clock.arrow.circlepath" : (session.isWorking ? "bolt.fill" : "pause.fill"))
                            .font(.caption.weight(.medium))
                            .foregroundStyle(isStale || !session.isWorking ? Color.secondary : Color.accentColor)
                    }
                    Spacer(minLength: 0)
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.caption.weight(.semibold))
                            .frame(width: 24, height: 24).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
                    .accessibilityLabel("Close session details")
                    .keyboardShortcut(.cancelAction)
                }
                Text(session.label).font(.subheadline.weight(.medium))
                    .fixedSize(horizontal: false, vertical: true)

                Grid(horizontalSpacing: 8, verticalSpacing: 8) {
                    GridRow {
                        metric("Started", value: Date(timeIntervalSince1970: session.createdAt)
                            .formatted(date: .omitted, time: .shortened))
                            .help(Date(timeIntervalSince1970: session.createdAt).formatted(date: .complete, time: .standard))
                        metric("Session age", value: metrics.sessionDuration)
                    }
                    GridRow {
                        metric("Tool calls", value: metrics.callCount)
                        metric(isStale ? "Running at snapshot" : "Running now", value: String(max(0, session.activeCalls)))
                    }
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text(session.isWorking ? (isStale ? "Tool at snapshot" : "Current tool") : "Last tool")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(session.tool.isEmpty ? "—" : session.tool)
                        .font(.system(.callout, design: .monospaced))
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                    if let duration = metrics.activityDuration {
                        Text("\(isStale ? "Call age at snapshot" : "Call age") · \(duration)")
                            .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                    }
                    if let error = session.lastError, error.hasPrefix("security:") {
                        Label(String(error.dropFirst("security:".count)).replacingOccurrences(of: "_", with: " "),
                              systemImage: "exclamationmark.shield")
                            .font(.caption).foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Divider()
                VStack(alignment: .leading, spacing: 5) {
                    Label("Last activity · \(metrics.lastActivity)", systemImage: "clock")
                        .font(.caption).foregroundStyle(.secondary)
                    if isStale {
                        Text("Connection unavailable. Showing the last session snapshot.")
                            .font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(18)
        }
        .frame(width: 320)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Agent \(session.flowNumber) session details")
    }

    private func metric(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(.title3, design: .rounded).weight(.semibold))
                .monospacedDigit().lineLimit(1).minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(11)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .combine)
    }
}
