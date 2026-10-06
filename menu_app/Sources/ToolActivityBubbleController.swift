import AppKit
import SwiftUI

@MainActor
private final class ToolActivityBubbleModel: ObservableObject {
    @Published var tool = "run_command"
    @Published var description = "Checking RAM & Disk Health"
    @Published var sequence = 0
}

private struct ToolActivityBubbleShape: Shape {
    func path(in rect: CGRect) -> Path {
        let tailHeight: CGFloat = 7
        let tailWidth: CGFloat = 16
        let tailCenterX = rect.width - 38
        let body = CGRect(
            x: 0,
            y: tailHeight,
            width: rect.width,
            height: max(0, rect.height - tailHeight)
        )

        var path = Path(
            roundedRect: body,
            cornerRadius: 14,
            style: .continuous
        )
        path.move(to: CGPoint(x: tailCenterX - tailWidth / 2, y: tailHeight + 0.5))
        path.addLine(to: CGPoint(x: tailCenterX, y: 0))
        path.addLine(to: CGPoint(x: tailCenterX + tailWidth / 2, y: tailHeight + 0.5))
        path.closeSubpath()
        return path
    }
}

private struct ToolActivityBubbleView: View {
    @ObservedObject var model: ToolActivityBubbleModel

    var body: some View {
        HStack(spacing: 9) {
            ZStack {
                Circle()
                    .fill(Color.accentColor.opacity(0.12))
                Image(systemName: symbol(for: model.tool))
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.accentColor)
            }
            .frame(width: 27, height: 27)

            VStack(alignment: .leading, spacing: 2) {
                Text("Mac MCP")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(.secondary)

                Text(model.description)
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
        .padding(.top, 11)
        .padding(.bottom, 8)
        .frame(width: 286, height: 58)
        .background(.ultraThinMaterial, in: ToolActivityBubbleShape())
        .overlay(
            ToolActivityBubbleShape()
                .stroke(Color.primary.opacity(0.09), lineWidth: 0.6)
        )
        .shadow(color: .black.opacity(0.16), radius: 10, y: 5)
        .padding(6)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Mac MCP tool activity, \(model.description)")
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
        let tool: String
        let description: String
        let startedAt: Date
    }

    private let model = ToolActivityBubbleModel()
    private var panel: ToolActivityPanel?
    private var active: [String: ActiveIntent] = [:]
    private var hideTask: Task<Void, Never>?
    private var previewTask: Task<Void, Never>?
    private let minimumVisibleSeconds: TimeInterval = 1.5
    private let settledVisibleSeconds: TimeInterval = 0.45

    private override init() {
        super.init()
    }

    func begin(eventID: String, tool: String, description: String) {
        let cleanDescription = description.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !eventID.isEmpty, !cleanDescription.isEmpty else { return }

        active[eventID] = ActiveIntent(
            eventID: eventID,
            tool: tool,
            description: cleanDescription,
            startedAt: Date()
        )
        presentLatest()
    }

    func finish(eventID: String) {
        let finished = active.removeValue(forKey: eventID)
        if active.isEmpty {
            scheduleHide(startedAt: finished?.startedAt)
        } else {
            presentLatest()
        }
    }

    func preview(tool: String, description: String) {
        previewTask?.cancel()
        let eventID = "preview-\(UUID().uuidString)"
        begin(eventID: eventID, tool: tool, description: description)
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
        guard let panel else { return }
        panel.alphaValue = 0
        panel.orderOut(nil)
    }

    private func presentLatest() {
        guard let intent = active.values.max(by: { $0.startedAt < $1.startedAt }) else {
            scheduleHide()
            return
        }

        hideTask?.cancel()
        withAnimation(.spring(response: 0.28, dampingFraction: 0.86)) {
            model.tool = intent.tool
            model.description = intent.description
            model.sequence += 1
        }

        let panel = ensurePanel()
        position(panel)
        if !panel.isVisible {
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
            })
        }
    }

    private func ensurePanel() -> ToolActivityPanel {
        if let panel { return panel }

        let size = NSSize(width: 304, height: 78)
        let panel = ToolActivityPanel(
            contentRect: NSRect(origin: .zero, size: size),
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
                panel.setFrameOrigin(saved)
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
