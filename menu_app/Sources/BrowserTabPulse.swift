import AppKit
import QuartzCore

/// The browser tab pulse (#79): when an agent uses a tab the owner is not looking at, a pale blue
/// glow sweeps across that tab in the tab bar once or twice and fades, one pulse per tool use.
///
/// The server finds the tab's button through Accessibility and says when the tab is the one on
/// show (then nothing is drawn). Like the desktop agent cursor, the glow sits in the window stack
/// directly above the browser window, so anything in front of the browser covers it; it ignores
/// the mouse, never activates and stays out of screen captures.
@MainActor
final class BrowserTabPulseController {
    static let shared = BrowserTabPulseController()

    /// E2E harnesses only: let screen capture record the glow.
    var capturableForTesting = false

    private var panel: NSPanel?
    private let glow = BrowserTabPulseLayer()
    private var fadeTask: Task<Void, Never>?
    /// Each pulse's number: a fade that ends after a newer pulse began leaves that one alone.
    private var generation = 0

    private init() {}

    // MARK: Pure helpers (compiled into tests)

    /// Two sweeps, then the fade: about 1.6 s in all.
    nonisolated static let sweepSeconds: Double = 0.65
    nonisolated static let sweeps: Float = 2
    nonisolated static let fadeSeconds: Double = 0.3

    /// The tab button's AX frame (origin top-left of the primary display, y down) in AppKit
    /// screen coordinates (origin bottom-left of the primary display, y up).
    nonisolated static func appKitFrame(ax: CGRect, primaryHeight: CGFloat) -> CGRect {
        CGRect(x: ax.minX, y: primaryHeight - ax.maxY, width: ax.width, height: ax.height)
    }

    // MARK: Drawing

    /// One pulse over the tab at `axFrame`, above the browser window `windowID`.
    func pulse(axFrame: CGRect, windowID: Int, reduceMotion: Bool) {
        guard axFrame.width > 8, axFrame.height > 8,
              let primary = NSScreen.screens.first?.frame else { return }
        let frame = Self.appKitFrame(ax: axFrame, primaryHeight: primary.height)
        let panel = self.panel ?? makePanel()
        self.panel = panel
        panel.sharingType = capturableForTesting ? .readOnly : .none
        panel.setFrame(frame, display: false)
        glow.frame = CGRect(origin: .zero, size: frame.size)
        panel.alphaValue = 1
        panel.order(.above, relativeTo: windowID)
        glow.play(width: frame.width, reduceMotion: reduceMotion)

        fadeTask?.cancel()
        generation += 1
        let mine = generation
        let visible = reduceMotion ? 0.9 : Self.sweepSeconds * Double(Self.sweeps)
        fadeTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(visible * 1_000_000_000))
            guard !Task.isCancelled, let self, let panel = self.panel, self.generation == mine else { return }
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
    }

    func hideImmediately() {
        fadeTask?.cancel()
        fadeTask = nil
        generation += 1
        panel?.orderOut(nil)
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.level = .normal
        panel.collectionBehavior = [.fullScreenAuxiliary, .ignoresCycle]
        panel.isReleasedWhenClosed = false
        let host = NSView()
        host.wantsLayer = true
        host.layer?.addSublayer(glow)
        panel.contentView = host
        return panel
    }
}

/// A soft pale blue tint with a brighter band that sweeps left to right, clipped to the tab's pill.
final class BrowserTabPulseLayer: CALayer {
    private let tint = CALayer()
    private let band = CAGradientLayer()

    override init() {
        super.init()
        cornerRadius = 9
        masksToBounds = true
        borderWidth = 1
        let blue = NSColor(calibratedRed: 0.55, green: 0.76, blue: 1.0, alpha: 1)
        borderColor = blue.withAlphaComponent(0.5).cgColor
        tint.backgroundColor = blue.withAlphaComponent(0.14).cgColor
        band.colors = [blue.withAlphaComponent(0).cgColor, blue.withAlphaComponent(0.42).cgColor,
                       blue.withAlphaComponent(0).cgColor]
        band.startPoint = CGPoint(x: 0, y: 0.5)
        band.endPoint = CGPoint(x: 1, y: 0.5)
        addSublayer(tint)
        addSublayer(band)
    }

    override init(layer: Any) { super.init(layer: layer) }
    required init?(coder: NSCoder) { nil }

    func play(width: CGFloat, reduceMotion: Bool) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        tint.frame = bounds
        band.frame = CGRect(x: 0, y: 0, width: max(40, width * 0.45), height: bounds.height)
        band.isHidden = reduceMotion
        CATransaction.commit()
        band.removeAllAnimations()
        tint.removeAllAnimations()
        let fadeIn = CABasicAnimation(keyPath: "opacity")
        fadeIn.fromValue = 0
        fadeIn.toValue = 1
        fadeIn.duration = 0.15
        tint.add(fadeIn, forKey: "in")
        guard !reduceMotion else { return }
        let sweep = CABasicAnimation(keyPath: "position.x")
        sweep.fromValue = -band.bounds.width / 2
        sweep.toValue = width + band.bounds.width / 2
        sweep.duration = BrowserTabPulseController.sweepSeconds
        sweep.repeatCount = BrowserTabPulseController.sweeps
        sweep.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        band.add(sweep, forKey: "sweep")
    }
}
