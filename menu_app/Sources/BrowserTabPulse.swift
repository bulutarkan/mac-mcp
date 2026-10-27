import AppKit
import QuartzCore

/// The browser tab pulse (#79): when an agent uses a tab the owner is not looking at, a pale blue
/// glow sweeps across that tab in the tab bar once or twice and fades, one pulse per tool use.
///
/// The server finds the tab's button through Accessibility and says when the tab is the one on
/// show (then nothing is drawn). While the pulse plays the glow follows the tab, so tabs opening
/// or closing beside it (which resize and move it) carry the glow along; when the tab is gone or
/// the owner switches to it, the glow stops. Like the desktop agent cursor, the glow sits in the
/// window stack directly above the browser window, so anything in front of the browser covers it;
/// it ignores the mouse, never activates and stays out of screen captures.
@MainActor
final class BrowserTabPulseController {
    static let shared = BrowserTabPulseController()

    /// E2E harnesses only: let screen capture record the glow.
    var capturableForTesting = false

    private var panel: NSPanel?
    private let glow = BrowserTabPulseLayer()
    private var playTask: Task<Void, Never>?
    /// Each pulse's number: a fade that ends after a newer pulse began leaves that one alone.
    private var generation = 0

    private init() {}

    // MARK: Pure helpers (compiled into tests)

    /// Two sweeps, then the fade: about 1.6 s in all.
    nonisolated static let sweepSeconds: Double = 0.65
    nonisolated static let sweeps: Float = 2
    nonisolated static let fadeSeconds: Double = 0.3
    /// How often the tab's place is read again while the glow shows. Each read is a server call and
    /// an Accessibility walk of the tab bar, so it is sparse (#124): twice in a 1.3 s pulse, not nine times.
    nonisolated static let followSeconds: Double = 0.6

    /// The tab button's AX frame (origin top-left of the primary display, y down) in AppKit
    /// screen coordinates (origin bottom-left of the primary display, y up).
    nonisolated static func appKitFrame(ax: CGRect, primaryHeight: CGFloat) -> CGRect {
        CGRect(x: ax.minX, y: primaryHeight - ax.maxY, width: ax.width, height: ax.height)
    }

    /// The sweep's gradient stops at the start and end of one pass, as fractions of the tab's width,
    /// so a tab that grows or shrinks mid-pulse still gets a full pass edge to edge.
    nonisolated static let sweepFrom: [Double] = [-0.45, -0.225, 0]
    nonisolated static let sweepTo: [Double] = [1, 1.225, 1.45]

    // MARK: Drawing

    /// One pulse over the tab at `axFrame`, above the browser window `windowID`. `follow` gives the
    /// tab's frame again (nil once it is gone or on show); it is asked about every `followSeconds`.
    func pulse(axFrame: CGRect, windowID: Int, reduceMotion: Bool, follow: @escaping () async -> CGRect?) {
        guard axFrame.width > 8, axFrame.height > 8, NSScreen.screens.first != nil else { return }
        let panel = self.panel ?? makePanel()
        self.panel = panel
        panel.sharingType = capturableForTesting ? .readOnly : .none
        place(axFrame, animated: false)
        panel.alphaValue = 1
        panel.order(.above, relativeTo: windowID)
        glow.play(reduceMotion: reduceMotion)

        playTask?.cancel()
        generation += 1
        let mine = generation
        let visible = reduceMotion ? 0.9 : Self.sweepSeconds * Double(Self.sweeps)
        playTask = Task { [weak self] in
            let ends = Date().addingTimeInterval(visible)
            while Date() < ends {
                try? await Task.sleep(nanoseconds: UInt64(Self.followSeconds * 1_000_000_000))
                guard !Task.isCancelled, let self, self.generation == mine else { return }
                guard let next = await follow() else { break }
                guard !Task.isCancelled, self.generation == mine else { return }
                self.place(next, animated: true)
                // A click in the browser brings its window to the top of the stack, over the glow
                // (switching tabs with the keyboard does not); keep the glow just above it again.
                self.panel?.order(.above, relativeTo: windowID)
            }
            guard !Task.isCancelled, let self, self.generation == mine else { return }
            self.fade(mine)
        }
    }

    func hideImmediately() {
        playTask?.cancel()
        playTask = nil
        generation += 1
        panel?.orderOut(nil)
    }

    private func place(_ axFrame: CGRect, animated: Bool) {
        guard let panel, let primary = NSScreen.screens.first?.frame, axFrame.width > 8, axFrame.height > 8 else { return }
        let frame = Self.appKitFrame(ax: axFrame, primaryHeight: primary.height)
        guard frame != panel.frame else { return }
        if animated {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.12
                panel.animator().setFrame(frame, display: true)
            }
        } else {
            panel.setFrame(frame, display: false)
        }
        CATransaction.begin()
        CATransaction.setAnimationDuration(animated ? 0.12 : 0)
        CATransaction.setDisableActions(!animated)
        glow.resize(to: frame.size)
        CATransaction.commit()
    }

    private func fade(_ mine: Int) {
        guard let panel else { return }
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = Self.fadeSeconds
            panel.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            Task { @MainActor in
                guard let self, self.generation == mine else { return }
                self.panel?.orderOut(nil)
            }
        })
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.level = .normal
        // A window keeps to the desktop (Space) it first showed on: without this, a glow first shown
        // over Safari stays on Safari's desktop and never appears over a browser on another one.
        // The server gives a frame only for a browser window on a desktop on show.
        panel.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary, .ignoresCycle]
        panel.isReleasedWhenClosed = false
        let host = NSView()
        host.wantsLayer = true
        host.layer?.addSublayer(glow)
        panel.contentView = host
        return panel
    }
}

/// A soft pale blue tint with a brighter band that sweeps left to right, clipped to the tab's pill.
/// The band moves by gradient stops, which are fractions of the layer's width, so it fits any size.
final class BrowserTabPulseLayer: CALayer {
    private let tint = CALayer()
    private let band = CAGradientLayer()

    override init() {
        super.init()
        cornerRadius = 9
        masksToBounds = true
        borderWidth = 1
        let blue = NSColor(calibratedRed: 0.55, green: 0.76, blue: 1.0, alpha: 1)
        borderColor = blue.withAlphaComponent(0.25).cgColor
        tint.backgroundColor = blue.withAlphaComponent(0.08).cgColor
        band.colors = [blue.withAlphaComponent(0).cgColor, blue.withAlphaComponent(0.28).cgColor,
                       blue.withAlphaComponent(0).cgColor]
        band.startPoint = CGPoint(x: 0, y: 0.5)
        band.endPoint = CGPoint(x: 1, y: 0.5)
        band.locations = BrowserTabPulseController.sweepFrom.map { NSNumber(value: $0) }
        addSublayer(tint)
        addSublayer(band)
    }

    override init(layer: Any) { super.init(layer: layer) }
    required init?(coder: NSCoder) { nil }

    func resize(to size: CGSize) {
        frame = CGRect(origin: .zero, size: size)
        tint.frame = bounds
        band.frame = bounds
    }

    func play(reduceMotion: Bool) {
        band.isHidden = reduceMotion
        band.removeAllAnimations()
        tint.removeAllAnimations()
        let fadeIn = CABasicAnimation(keyPath: "opacity")
        fadeIn.fromValue = 0
        fadeIn.toValue = 1
        fadeIn.duration = 0.15
        tint.add(fadeIn, forKey: "in")
        guard !reduceMotion else { return }
        let sweep = CABasicAnimation(keyPath: "locations")
        sweep.fromValue = BrowserTabPulseController.sweepFrom.map { NSNumber(value: $0) }
        sweep.toValue = BrowserTabPulseController.sweepTo.map { NSNumber(value: $0) }
        sweep.duration = BrowserTabPulseController.sweepSeconds
        sweep.repeatCount = BrowserTabPulseController.sweeps
        sweep.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        band.add(sweep, forKey: "sweep")
    }
}
