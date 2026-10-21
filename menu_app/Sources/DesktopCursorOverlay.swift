import AppKit
import QuartzCore

/// One `native_pointer` event: where a background (Accessibility) native action lands.
/// Points are desktop points in AX space: origin at the primary display's top-left, y down.
struct DesktopPointer: Equatable {
    let eventID: String?
    let sessionID: String?
    let pid: Int32?
    let point: CGPoint?
    let action: String
    let target: String
    let press: Bool
}

/// The desktop agent cursor: a synthetic arrow showing where an agent acts in another
/// app's window without moving the real pointer, like the browser companion's cursor.
///
/// It floats above every window on whichever desktop (Space) the owner is looking at, so it
/// shows every time: a window in front of the driven one, or the driven window being on
/// another desktop, no longer hides it. It is display only, ignores the mouse, never
/// activates, and stays out of screen captures, so the agent's own screenshots never see it.
@MainActor
final class DesktopCursorOverlayController {
    static let shared = DesktopCursorOverlayController()

    /// E2E harnesses only: let screen capture record the overlay.
    var capturableForTesting = false

    private var panel: DesktopCursorPanel?
    private var canvas = CALayer()
    private let cursor = DesktopCursorLayer()
    private var screenFrame: CGRect = .zero
    private var last: CGPoint?
    private var targetPID: Int32?
    private var lastEventID: String?
    private var hideTask: Task<Void, Never>?
    /// The press planned for when the arrow arrives; the next pointer event cancels it (#27).
    private var pressTask: Task<Void, Never>?

    private let idleHideSeconds: Double = 2.4
    private let finishedHideSeconds: Double = 0.9

    private init() {}

    // MARK: Pure helpers (compiled into tests)

    nonisolated static func label(agent: String?, action: String, target: String) -> String {
        let step = [action, target].filter { !$0.isEmpty }.joined(separator: " ")
        guard let agent, !agent.isEmpty else { return step }
        return step.isEmpty ? agent : "\(agent) · \(step)"
    }

    /// The screen holding an AX point and that point in the screen's own y-down space.
    nonisolated static func locate(_ ax: CGPoint, screens: [CGRect]) -> (screen: CGRect, local: CGPoint)? {
        guard let primary = screens.first else { return nil }
        let appKit = CGPoint(x: ax.x, y: primary.maxY - ax.y)
        let screen = screens.first { $0.contains(appKit) } ?? primary
        return (screen, CGPoint(x: appKit.x - screen.minX, y: screen.maxY - appKit.y))
    }

    // MARK: Presentation

    func show(_ pointer: DesktopPointer, agent: String?) {
        let text = Self.label(agent: agent, action: pointer.action, target: pointer.target)
        guard let point = pointer.point else {
            // A keyboard step: relabel in place if the cursor is already showing.
            if last != nil, pointer.pid == nil || pointer.pid == targetPID {
                cursor.setLabel(text)
                lastEventID = pointer.eventID ?? lastEventID
                scheduleHide(after: idleHideSeconds)
            }
            return
        }
        guard let located = Self.locate(point, screens: NSScreen.screens.map(\.frame)) else {
            hideImmediately()
            return
        }

        // Above everything, on the desktop being looked at: the driven window may be covered
        // or on another desktop, and the cursor still shows where the action lands.
        ensurePanel(screen: located.screen)
        targetPID = pointer.pid
        panel?.orderFrontRegardless()

        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let local = located.local
        let visible = cursor.opacity > 0.5 && last != nil
        let distance = last.map { hypot(local.x - $0.x, local.y - $0.y) } ?? 0
        let duration = (!visible || reduceMotion) ? 0 : max(0.22, min(0.62, 0.22 + Double(distance) * 0.00045))
        if duration > 0, let from = last { trail(from: from, to: local, duration: duration) }

        CATransaction.begin()
        CATransaction.setAnimationDuration(duration)
        CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(controlPoints: 0.2, 0.8, 0.2, 1))
        if duration == 0 { CATransaction.setDisableActions(true) }
        cursor.position = local
        CATransaction.commit()
        CATransaction.begin()
        CATransaction.setAnimationDuration(0.22)
        cursor.opacity = 1
        CATransaction.commit()
        cursor.setLabel(text)
        last = local
        lastEventID = pointer.eventID

        // A press belongs to the event that asked for it: a newer event (the arrow already heading elsewhere)
        // cancels it, so no click shows where nothing was clicked (#27).
        pressTask?.cancel()
        pressTask = nil
        if pointer.press {
            pressTask = Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
                guard !Task.isCancelled, let self else { return }
                self.cursor.press(reduceMotion: reduceMotion)
                self.ripple(at: local, reduceMotion: reduceMotion)
            }
        }
        scheduleHide(after: idleHideSeconds)
    }

    /// The tool call behind the cursor finished: let it go a little sooner.
    func finish(eventID: String) {
        guard last != nil, eventID == lastEventID else { return }
        scheduleHide(after: finishedHideSeconds)
    }

    func hideImmediately() {
        hideTask?.cancel()
        hideTask = nil
        pressTask?.cancel()
        pressTask = nil
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        cursor.opacity = 0
        CATransaction.commit()
        cursor.setLabel(nil)
        last = nil
        lastEventID = nil
        panel?.orderOut(nil)
    }

    private func fadeOut() {
        CATransaction.begin()
        CATransaction.setAnimationDuration(0.35)
        CATransaction.setCompletionBlock { [weak self] in
            MainActor.assumeIsolated {
                guard let self, self.last == nil else { return }
                self.panel?.orderOut(nil)
            }
        }
        cursor.opacity = 0
        CATransaction.commit()
        cursor.setLabel(nil)
        last = nil
        lastEventID = nil
    }

    private func scheduleHide(after seconds: Double) {
        hideTask?.cancel()
        hideTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.fadeOut()
        }
    }

    private func ensurePanel(screen: CGRect) {
        if let panel, screenFrame == screen {
            panel.sharingType = capturableForTesting ? .readOnly : .none
            return
        }
        panel?.orderOut(nil)
        let p = DesktopCursorPanel(
            contentRect: screen, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false
        )
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = false
        p.ignoresMouseEvents = true
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        p.isReleasedWhenClosed = false
        p.hidesOnDeactivate = false
        p.animationBehavior = .none
        p.sharingType = capturableForTesting ? .readOnly : .none
        p.setFrame(screen, display: false)

        let view = NSView(frame: CGRect(origin: .zero, size: screen.size))
        let hosting = CALayer()
        view.layer = hosting  // layer-hosting: the layer first, then wantsLayer
        view.wantsLayer = true
        // AppKit owns the hosting layer's geometry, so the y-down canvas is our own sublayer.
        canvas = CALayer()
        canvas.frame = view.bounds
        canvas.isGeometryFlipped = true
        hosting.addSublayer(canvas)
        cursor.removeFromSuperlayer()
        canvas.addSublayer(cursor)
        let scale = NSScreen.screens.first { $0.frame == screen }?.backingScaleFactor ?? 2
        cursor.setContentsScale(scale)
        p.contentView = view
        panel = p
        screenFrame = screen
        last = nil
    }

    // MARK: Effects

    /// A short comet tail: faint champagne ghosts lagging behind the glide.
    private func trail(from: CGPoint, to: CGPoint, duration: CFTimeInterval) {
        let path = DesktopCursorLayer.arrowPath()
        for i in 1...3 {
            let ghost = CAShapeLayer()
            ghost.path = path
            ghost.fillColor = DesktopCursorLayer.champagne.withAlphaComponent(0.6).cgColor
            ghost.shadowColor = DesktopCursorLayer.bronze.cgColor
            ghost.shadowOpacity = 0.6
            ghost.shadowRadius = 5
            ghost.shadowOffset = .zero
            ghost.contentsScale = cursor.contentsScale
            ghost.position = to
            ghost.opacity = 0
            canvas.insertSublayer(ghost, below: cursor)
            let lag = 0.07 * Double(i)
            let move = CABasicAnimation(keyPath: "position")
            move.fromValue = NSValue(point: from)
            move.toValue = NSValue(point: to)
            move.timingFunction = CAMediaTimingFunction(controlPoints: 0.2, 0.8, 0.2, 1)
            let fade = CAKeyframeAnimation(keyPath: "opacity")
            fade.values = [0, 0.32 / Float(i), 0]
            fade.keyTimes = [0, 0.25, 1]
            let group = CAAnimationGroup()
            group.animations = [move, fade]
            group.duration = duration + lag
            move.duration = group.duration
            fade.duration = group.duration
            ghost.add(group, forKey: "trail")
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: UInt64((group.duration + 0.05) * 1_000_000_000))
                ghost.removeFromSuperlayer()
            }
        }
    }

    private func ripple(at point: CGPoint, reduceMotion: Bool) {
        let ring = CAShapeLayer()
        ring.path = CGPath(ellipseIn: CGRect(x: -9, y: -9, width: 18, height: 18), transform: nil)
        ring.position = point
        ring.fillColor = NSColor.clear.cgColor
        ring.strokeColor = DesktopCursorLayer.bronze.withAlphaComponent(0.9).cgColor
        ring.lineWidth = 2
        ring.shadowColor = DesktopCursorLayer.champagneLight.cgColor
        ring.shadowOpacity = 0.6
        ring.shadowRadius = 1
        ring.shadowOffset = .zero
        ring.contentsScale = cursor.contentsScale
        canvas.insertSublayer(ring, below: cursor)
        let grow = CABasicAnimation(keyPath: "transform.scale")
        grow.fromValue = 0.4
        grow.toValue = 2.2
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 1
        fade.toValue = 0
        let group = CAAnimationGroup()
        group.animations = [grow, fade]
        group.duration = reduceMotion ? 0.2 : 0.55
        group.timingFunction = CAMediaTimingFunction(name: .easeOut)
        group.isRemovedOnCompletion = false
        group.fillMode = .forwards
        ring.add(group, forKey: "ripple")
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64((group.duration + 0.05) * 1_000_000_000))
            ring.removeFromSuperlayer()
        }
    }
}

final class DesktopCursorPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
    // Decorative: as an accessibility window it would shift the window numbers mac_act resolved for
    // Mac MCP's own windows (#221)
    override func isAccessibilityElement() -> Bool { false }
}

/// The arrow (the browser companion's shape, its tip on the target), a champagne aura
/// behind it, a soft bronze contact shadow under it and the step label beside it.
private final class DesktopCursorLayer: CALayer {
    // Brand: champagne #f1e7d2 -> #b9a98a, bronze #9c8a66 -> #6f5f42.
    static let champagneLight = NSColor(srgbRed: 0xf1 / 255, green: 0xe7 / 255, blue: 0xd2 / 255, alpha: 1)
    static let champagne = NSColor(srgbRed: 0xb9 / 255, green: 0xa9 / 255, blue: 0x8a / 255, alpha: 1)
    static let bronze = NSColor(srgbRed: 0x9c / 255, green: 0x8a / 255, blue: 0x66 / 255, alpha: 1)
    static let bronzeDeep = NSColor(srgbRed: 0x6f / 255, green: 0x5f / 255, blue: 0x42 / 255, alpha: 1)

    static func arrowPath(scale: CGFloat = 1.18) -> CGPath {
        let path = CGMutablePath()
        path.move(to: CGPoint(x: 3.4, y: 3.2))
        path.addLine(to: CGPoint(x: 23, y: 11.2))
        path.addLine(to: CGPoint(x: 14.6, y: 14.4))
        path.addLine(to: CGPoint(x: 11.3, y: 23.2))
        path.closeSubpath()
        var transform = CGAffineTransform(translationX: -3.4 * scale, y: -3.2 * scale).scaledBy(x: scale, y: scale)
        return path.copy(using: &transform) ?? path
    }

    private static func radial(_ size: CGSize, _ colors: [NSColor]) -> CAGradientLayer {
        let gradient = CAGradientLayer()
        gradient.type = .radial
        gradient.bounds = CGRect(origin: .zero, size: size)
        gradient.colors = colors.map(\.cgColor)
        gradient.locations = [0, 0.45, 1]
        gradient.startPoint = CGPoint(x: 0.5, y: 0.5)
        gradient.endPoint = CGPoint(x: 1, y: 1)
        return gradient
    }

    private let shadow = DesktopCursorLayer.radial(CGSize(width: 38, height: 16), [
        DesktopCursorLayer.bronzeDeep.withAlphaComponent(0.55),
        DesktopCursorLayer.bronzeDeep.withAlphaComponent(0.2),
        DesktopCursorLayer.bronzeDeep.withAlphaComponent(0),
    ])
    private let glow = DesktopCursorLayer.radial(CGSize(width: 70, height: 70), [
        DesktopCursorLayer.champagne.withAlphaComponent(0.5),
        DesktopCursorLayer.bronze.withAlphaComponent(0.2),
        DesktopCursorLayer.bronze.withAlphaComponent(0),
    ])
    private let rim = CAShapeLayer()
    private let fill = CAShapeLayer()
    private let tip = CALayer()
    private let tipText = CATextLayer()

    override init() {
        super.init()
        bounds = CGRect(x: 0, y: 0, width: 1, height: 1)
        anchorPoint = .zero
        opacity = 0

        shadow.position = CGPoint(x: 15, y: 31)
        addSublayer(shadow)
        glow.position = CGPoint(x: 11, y: 12)
        addSublayer(glow)

        let path = Self.arrowPath()
        rim.path = path
        rim.fillColor = NSColor(white: 0.985, alpha: 0.97).cgColor
        rim.strokeColor = rim.fillColor
        rim.lineWidth = 3.8
        rim.lineJoin = .round
        rim.shadowColor = NSColor.black.cgColor
        rim.shadowOpacity = 0.4
        rim.shadowRadius = 3
        rim.shadowOffset = CGSize(width: 0, height: 1.5)
        addSublayer(rim)

        fill.path = path
        fill.fillColor = NSColor(srgbRed: 0.11, green: 0.11, blue: 0.12, alpha: 1).cgColor
        fill.strokeColor = fill.fillColor
        fill.lineWidth = 1.4
        fill.lineJoin = .round
        addSublayer(fill)

        tip.anchorPoint = .zero
        tip.cornerRadius = 8
        tip.backgroundColor = NSColor(srgbRed: 0.11, green: 0.11, blue: 0.12, alpha: 0.9).cgColor
        tip.borderColor = NSColor(white: 1, alpha: 0.2).cgColor
        tip.borderWidth = 1
        tip.shadowColor = NSColor.black.cgColor
        tip.shadowOpacity = 0.26
        tip.shadowRadius = 9
        tip.shadowOffset = CGSize(width: 0, height: 4)
        tip.opacity = 0
        addSublayer(tip)
        tipText.anchorPoint = .zero
        tipText.alignmentMode = .left
        tipText.truncationMode = .end
        tip.addSublayer(tipText)
    }

    override init(layer: Any) { super.init(layer: layer) }
    required init?(coder: NSCoder) { nil }

    func setContentsScale(_ scale: CGFloat) {
        contentsScale = scale
        for layer in [shadow, glow, rim, fill, tip, tipText] as [CALayer] { layer.contentsScale = scale }
    }

    func setLabel(_ text: String?) {
        guard let text, !text.isEmpty else {
            tip.opacity = 0
            return
        }
        let attributed = NSAttributedString(string: text, attributes: [
            .font: NSFont.systemFont(ofSize: 11.5, weight: .semibold),
            .foregroundColor: NSColor.white,
        ])
        let width = min(240, ceil(attributed.size().width))
        tipText.string = attributed
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        tipText.frame = CGRect(x: 9, y: 5, width: width, height: 15)
        tip.bounds = CGRect(x: 0, y: 0, width: width + 18, height: 24)
        tip.position = CGPoint(x: 28, y: 30)
        CATransaction.commit()
        tip.opacity = 1
    }

    func press(reduceMotion: Bool) {
        let squeeze = CAKeyframeAnimation(keyPath: "transform.scale")
        squeeze.values = reduceMotion ? [1, 0.9, 1] : [1, 0.82, 1.04, 1]
        squeeze.keyTimes = reduceMotion ? [0, 0.5, 1] : [0, 0.35, 0.75, 1]
        squeeze.duration = reduceMotion ? 0.12 : 0.32
        for layer in [rim, fill] { layer.add(squeeze, forKey: "press") }
    }
}
