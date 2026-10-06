import AppKit
import SwiftUI

private struct ToolActivityLane: Identifiable, Equatable {
    let id: String
    let label: String
    let tool: String
    let description: String
    let startedAt: Date
    let ordinal: Int
}

@MainActor
private final class ToolActivityBubbleModel: ObservableObject {
    @Published var lanes: [ToolActivityLane] = []
    @Published var sequence = 0
}

private struct ToolActivityBubbleShape: Shape {
    func path(in rect: CGRect) -> Path {
        Path(
            roundedRect: rect,
            cornerRadius: 14,
            style: .continuous
        )
    }
}

private struct ToolActivityBubbleView: View {
    @ObservedObject var model: ToolActivityBubbleModel

    private var primaryLane: ToolActivityLane {
        model.lanes.first ?? ToolActivityLane(
            id: "placeholder",
            label: "Agent 1",
            tool: "run_command",
            description: "Checking RAM & Disk Health",
            startedAt: Date(),
            ordinal: 1
        )
    }

    private var visibleLanes: [ToolActivityLane] {
        Array(model.lanes.prefix(3))
    }

    private var hiddenCount: Int {
        max(0, model.lanes.count - visibleLanes.count)
    }

    private var isMultiAgent: Bool {
        model.lanes.count > 1
    }

    var body: some View {
        Group {
            if isMultiAgent {
                multiAgentBody
            } else {
                singleAgentBody
            }
        }
        .animation(.spring(response: 0.30, dampingFraction: 0.88), value: model.lanes.count)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    private var singleAgentBody: some View {
        HStack(spacing: 9) {
            toolIcon(for: primaryLane.tool, size: 27, symbolSize: 12)

            VStack(alignment: .leading, spacing: 2) {
                Text("Mac MCP")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(Color(nsColor: .secondaryLabelColor))

                Text(primaryLane.description)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .contentTransition(.opacity)
                    .id(model.sequence)
            }

            Spacer(minLength: 4)

            ProgressView()
                .controlSize(.mini)
                .scaleEffect(0.82)
                .tint(.secondary)
        }
        .padding(.horizontal, 11)
        .padding(.top, 13)
        .padding(.bottom, 10)
        .frame(width: 286, height: 64)
        .background(
            Color(nsColor: .windowBackgroundColor).opacity(0.96),
            in: ToolActivityBubbleShape()
        )
        .overlay(
            ToolActivityBubbleShape()
                .stroke(Color.primary.opacity(0.09), lineWidth: 0.6)
        )
        .shadow(color: .black.opacity(0.16), radius: 10, y: 5)
        .padding(6)
    }

    private var multiAgentBody: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 8) {
                Text("Mac MCP")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color(nsColor: .secondaryLabelColor))

                Spacer()

                Text("\(model.lanes.count) agents active")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(.secondary)
            }

            ForEach(visibleLanes) { lane in
                HStack(alignment: .center, spacing: 9) {
                    toolIcon(for: lane.tool, size: 25, symbolSize: 11)

                    VStack(alignment: .leading, spacing: 1) {
                        Text(lane.description)
                            .font(.system(size: 11.5, weight: .semibold))
                            .foregroundStyle(.primary)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                            .contentTransition(.opacity)

                        Text(lane.label)
                            .font(.system(size: 8.5, weight: .medium))
                            .foregroundStyle(.tertiary)
                    }

                    Spacer(minLength: 3)

                    ProgressView()
                        .controlSize(.mini)
                        .scaleEffect(0.72)
                        .tint(.secondary)
                }
                .padding(.vertical, 3)
            }

            if hiddenCount > 0 {
                Text("+\(hiddenCount) more working")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .padding(.leading, 34)
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 15)
        .padding(.bottom, 13)
        .frame(width: 306, height: multiAgentBodyHeight, alignment: .topLeading)
        .background(
            Color(nsColor: .windowBackgroundColor).opacity(0.96),
            in: ToolActivityBubbleShape()
        )
        .overlay(
            ToolActivityBubbleShape()
                .stroke(Color.primary.opacity(0.09), lineWidth: 0.6)
        )
        .shadow(color: .black.opacity(0.16), radius: 10, y: 5)
        .padding(6)
    }

    private var multiAgentBodyHeight: CGFloat {
        switch model.lanes.count {
        case ...2:
            return 140
        case 3:
            return 182
        default:
            return 202
        }
    }

    private var accessibilityText: String {
        if model.lanes.count <= 1 {
            return "Mac MCP tool activity, \(primaryLane.description)"
        }
        return "Mac MCP, \(model.lanes.count) agents active, " +
            visibleLanes.map { "\($0.label): \($0.description)" }.joined(separator: ", ")
    }

    @ViewBuilder
    private func toolIcon(for tool: String, size: CGFloat, symbolSize: CGFloat) -> some View {
        ZStack {
            Circle()
                .fill(Color.accentColor.opacity(0.12))
            Image(systemName: symbol(for: tool))
                .font(.system(size: symbolSize, weight: .semibold))
                .foregroundStyle(Color.accentColor)
        }
        .frame(width: size, height: size)
    }

    private func symbol(for tool: String) -> String {
        let value = clean(tool)
        if value.contains("browser") { return "safari" }
        if value.contains("file") || value.contains("directory") { return "doc.fill" }
        if value.contains("snapshot") { return "viewfinder" }
        if value.contains("mac_") || value.contains("app") { return "macwindow" }
        if value.contains("agent") { return "cpu" }
        if value.contains("http") || value.contains("network") { return "network" }
        if value.contains("command") || value.contains("process") || value.contains("job") {
            return "terminal.fill"
        }
        return "hammer.fill"
    }

    private func clean(_ tool: String) -> String {
        tool
            .replacingOccurrences(of: "mcp__", with: "")
            .replacingOccurrences(of: "Macbook__", with: "")
            .lowercased()
    }
}

private final class ToolActivityPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class ToolActivityBubbleController: NSObject, NSWindowDelegate {
    static let shared = ToolActivityBubbleController()

    private static let originXKey = "ToolActivityBubbleOriginX"
    private static let originYKey = "ToolActivityBubbleOriginY"

    private struct ActiveIntent {
        let eventID: String
        let laneKey: String
        let tool: String
        let description: String
        let startedAt: Date
    }

    private let model = ToolActivityBubbleModel()
    private var panel: ToolActivityPanel?
    private var active: [String: ActiveIntent] = [:]
    private var laneOrdinals: [String: Int] = [:]
    private var nextLaneOrdinal = 1
    private var hideTask: Task<Void, Never>?
    private var previewTask: Task<Void, Never>?
    private let minimumVisibleSeconds: TimeInterval = 2.0
    private let settledVisibleSeconds: TimeInterval = 0.45

    private override init() {
        super.init()
    }

    func begin(
        eventID: String,
        tool: String,
        description: String,
        sessionID: String? = nil,
        agentID: String? = nil,
        teamID: String? = nil
    ) {
        let cleanDescription = description.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !eventID.isEmpty, !cleanDescription.isEmpty else { return }

        let laneKey = laneKey(
            eventID: eventID,
            sessionID: sessionID,
            agentID: agentID,
            teamID: teamID
        )
        if laneOrdinals[laneKey] == nil {
            laneOrdinals[laneKey] = nextLaneOrdinal
            nextLaneOrdinal += 1
        }

        active[eventID] = ActiveIntent(
            eventID: eventID,
            laneKey: laneKey,
            tool: tool,
            description: cleanDescription,
            startedAt: Date()
        )
        presentActiveLanes()
    }

    func finish(eventID: String) {
        let finished = active.removeValue(forKey: eventID)
        if active.isEmpty {
            scheduleHide(startedAt: finished?.startedAt)
        } else {
            presentActiveLanes()
        }
    }

    func preview(tool: String, description: String) {
        previewTask?.cancel()
        let eventID = "preview-\(UUID().uuidString)"
        begin(
            eventID: eventID,
            tool: tool,
            description: description,
            sessionID: "preview-session"
        )
        previewTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_200_000_000)
            guard !Task.isCancelled else { return }
            self?.finish(eventID: eventID)
        }
    }

    func hideImmediately() {
        hideTask?.cancel()
        previewTask?.cancel()
        active.removeAll()
        resetLaneOrdinals()
        model.lanes = []
        guard let panel else { return }
        panel.alphaValue = 0
        panel.orderOut(nil)
    }

    private func laneKey(
        eventID: String,
        sessionID: String?,
        agentID: String?,
        teamID: String?
    ) -> String {
        let agent = (agentID ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !agent.isEmpty {
            return "agent:\(agent)"
        }

        let session = (sessionID ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !session.isEmpty {
            return "session:\(session)"
        }

        let team = (teamID ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !team.isEmpty {
            return "team:\(team):event:\(eventID)"
        }

        return "event:\(eventID)"
    }

    private func presentActiveLanes() {
        let lanes = currentLanes()
        guard !lanes.isEmpty else {
            scheduleHide()
            return
        }

        hideTask?.cancel()
        withAnimation(.spring(response: 0.28, dampingFraction: 0.86)) {
            model.lanes = lanes
            model.sequence += 1
        }

        let panel = ensurePanel()
        resize(panel, laneCount: lanes.count)

        if !panel.isVisible {
            position(panel)
            panel.alphaValue = 0
            panel.orderFrontRegardless()
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.18
                panel.animator().alphaValue = 1
            }
        } else {
            panel.alphaValue = 1
            panel.orderFrontRegardless()
        }
    }

    private func currentLanes() -> [ToolActivityLane] {
        var latestByLane: [String: ActiveIntent] = [:]

        for intent in active.values {
            if let existing = latestByLane[intent.laneKey],
               existing.startedAt >= intent.startedAt {
                continue
            }
            latestByLane[intent.laneKey] = intent
        }

        return latestByLane.map { laneKey, intent in
            let ordinal = laneOrdinals[laneKey] ?? 999
            return ToolActivityLane(
                id: laneKey,
                label: "Agent \(ordinal)",
                tool: intent.tool,
                description: intent.description,
                startedAt: intent.startedAt,
                ordinal: ordinal
            )
        }
        .sorted { lhs, rhs in
            if lhs.ordinal != rhs.ordinal {
                return lhs.ordinal < rhs.ordinal
            }
            return lhs.startedAt < rhs.startedAt
        }
    }

    private func scheduleHide(startedAt: Date? = nil) {
        hideTask?.cancel()
        let elapsed = startedAt.map { max(0, Date().timeIntervalSince($0)) } ?? minimumVisibleSeconds
        let delay = max(settledVisibleSeconds, minimumVisibleSeconds - elapsed)
        hideTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled, let self, self.active.isEmpty, let panel = self.panel else { return }
            NSAnimationContext.runAnimationGroup({ context in
                context.duration = 0.24
                panel.animator().alphaValue = 0
            }, completionHandler: {
                panel.orderOut(nil)
                Task { @MainActor [weak self] in
                    self?.model.lanes = []
                    self?.resetLaneOrdinals()
                }
            })
        }
    }

    private func resetLaneOrdinals() {
        laneOrdinals.removeAll(keepingCapacity: true)
        nextLaneOrdinal = 1
    }

    private func panelSize(for laneCount: Int) -> NSSize {
        switch laneCount {
        case ...1:
            return NSSize(width: 304, height: 82)
        case 2:
            return NSSize(width: 324, height: 152)
        case 3:
            return NSSize(width: 324, height: 194)
        default:
            return NSSize(width: 324, height: 214)
        }
    }

    private func ensurePanel() -> ToolActivityPanel {
        if let panel { return panel }

        let panel = ToolActivityPanel(
            contentRect: NSRect(origin: .zero, size: panelSize(for: 1)),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        panel.ignoresMouseEvents = false
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        panel.contentView = NSHostingView(rootView: ToolActivityBubbleView(model: model))
        self.panel = panel
        return panel
    }

    private func resize(_ panel: ToolActivityPanel, laneCount: Int) {
        let target = panelSize(for: laneCount)
        let current = panel.frame
        guard abs(current.width - target.width) > 0.5 || abs(current.height - target.height) > 0.5 else {
            return
        }

        let anchored = NSRect(
            x: current.maxX - target.width,
            y: current.maxY - target.height,
            width: target.width,
            height: target.height
        )
        panel.setFrame(clampedFrame(anchored), display: true, animate: panel.isVisible)
    }

    private func clampedFrame(_ proposed: NSRect) -> NSRect {
        guard let screen = NSScreen.screens.first(where: { $0.visibleFrame.intersects(proposed) })
            ?? NSScreen.main
            ?? NSScreen.screens.first
        else {
            return proposed
        }

        let visible = screen.visibleFrame
        var origin = proposed.origin
        origin.x = min(max(origin.x, visible.minX), max(visible.minX, visible.maxX - proposed.width))
        origin.y = min(max(origin.y, visible.minY), max(visible.minY, visible.maxY - proposed.height))
        return NSRect(origin: origin, size: proposed.size)
    }

    private func position(_ panel: NSPanel) {
        let defaults = UserDefaults.standard
        let size = panel.frame.size

        if defaults.object(forKey: Self.originXKey) != nil,
           defaults.object(forKey: Self.originYKey) != nil {
            let saved = NSPoint(
                x: defaults.double(forKey: Self.originXKey),
                y: defaults.double(forKey: Self.originYKey)
            )
            let savedFrame = NSRect(origin: saved, size: size)
            if NSScreen.screens.contains(where: { $0.visibleFrame.intersects(savedFrame) }) {
                panel.setFrameOrigin(clampedFrame(savedFrame).origin)
                return
            }
        }

        guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let frame = screen.visibleFrame
        panel.setFrameOrigin(
            NSPoint(
                x: frame.maxX - size.width - 12,
                y: frame.maxY - size.height - 6
            )
        )
    }

    func windowDidMove(_ notification: Notification) {
        guard let window = notification.object as? NSWindow, window === panel else { return }
        let origin = window.frame.origin
        UserDefaults.standard.set(Double(origin.x), forKey: Self.originXKey)
        UserDefaults.standard.set(Double(origin.y), forKey: Self.originYKey)
    }
}
