import SwiftUI

/// Sentences for the menu's hover captions. The chip and the Conversations row share
/// `conversationCount`, so the two cannot show different numbers.
enum MenuHoverCopy {
    static func toolCalls(count: Int) -> String {
        switch count {
        case 0: return "No tool calls in the last hour"
        case 1: return "1 tool call in the last hour"
        default: return "\(count) tool calls in the last hour"
        }
    }

    /// Agrees with the percent the chip draws (`rounded()`), so 99.6% still reads as all succeeded.
    static func success(calls: Int, rate: Double) -> String {
        guard calls > 0 else { return "No tool calls in the last hour" }
        if Int(rate.rounded()) >= 100 {
            return calls == 1 ? "The call succeeded" : "All \(calls) succeeded"
        }
        let succeeded = min(calls, max(0, Int((Double(calls) * rate / 100).rounded())))
        if succeeded == calls { return calls == 1 ? "The call succeeded" : "All \(calls) succeeded" }
        if succeeded == 0 { return calls == 1 ? "The call failed" : "None of \(calls) succeeded" }
        return "\(succeeded) of \(calls) succeeded"
    }

    static func agents(running: Int) -> String {
        switch running {
        case 0: return "No agents working"
        case 1: return "1 agent working"
        default: return "\(running) agents working"
        }
    }

    /// Active, then idle, then ended. Idle wins over ended so a live conversation is not hidden
    /// behind an older one that already finished.
    static func conversationCount(active: Int, idle: Int, ended: Int) -> String {
        if active > 0 { return "\(active) active" }
        if idle > 0 { return "\(idle) idle" }
        return "\(ended) ended"
    }

    static func conversations(active: Int, idle: Int, ended: Int, reliable: Bool) -> String {
        guard reliable else { return "Conversations unavailable" }
        if active > 0 {
            return active == 1 ? "1 conversation active" : "\(active) conversations active"
        }
        if idle > 0 {
            return idle == 1 ? "1 conversation idle" : "\(idle) conversations idle"
        }
        if ended == 0 { return "No conversations" }
        return ended == 1 ? "1 conversation ended" : "\(ended) conversations ended"
    }
}

/// Where a hovered chip or row is, and what its caption says. Each target only reports itself;
/// `MenuHoverTipLayer`, laid over the whole panel, draws the one caption. So the caption is never
/// clipped by the scroll view, never drawn under a later row, and never squeezed to a chip's width.
struct MenuHoverTipTarget {
    let id: UUID
    let text: String
    let anchor: Anchor<CGRect>
}

struct MenuHoverTipKey: PreferenceKey {
    static var defaultValue: MenuHoverTipTarget? = nil

    static func reduce(value: inout MenuHoverTipTarget?, nextValue: () -> MenuHoverTipTarget?) {
        value = value ?? nextValue()
    }
}

/// Moving from one chip to the next while a caption shows switches at once, as macOS tooltips do;
/// the first caption waits a little so passing across the menu does not flash one.
@MainActor
enum MenuHoverTipClock {
    static var lastHidden = Date.distantPast

    static var delay: TimeInterval {
        Date().timeIntervalSince(lastHidden) < 0.45 ? 0 : MenuHoverTip.delay
    }
}

struct MenuHoverTip: ViewModifier {
    let text: String
    @MacMCPState private var id = UUID()
    @MacMCPState private var hovering = false
    @MacMCPState private var shown = false
    @MacMCPState private var hoverToken = 0

    func body(content: Content) -> some View {
        content
            .onHover { inside in
                hoverToken += 1
                let token = hoverToken
                hovering = inside
                if inside {
                    DispatchQueue.main.asyncAfter(deadline: .now() + MenuHoverTipClock.delay) {
                        if hovering, hoverToken == token { shown = true }
                    }
                } else {
                    if shown { MenuHoverTipClock.lastHidden = Date() }
                    shown = false
                }
            }
            .onDisappear { shown = false }
            .anchorPreference(key: MenuHoverTipKey.self, value: .bounds) { anchor in
                shown && !text.isEmpty ? MenuHoverTipTarget(id: id, text: text, anchor: anchor) : nil
            }
    }

    /// Long enough that moving across the menu does not flash a caption.
    static let delay: TimeInterval = 0.3
}

/// The caption for the hovered target: below it, or above it near the bottom of the panel;
/// centred on it and kept inside the panel. It takes no clicks and does not change the menu's
/// measured height (#200), because it is drawn over the panel, not in its stack.
struct MenuHoverTipLayer: View {
    let target: MenuHoverTipTarget?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let gap: CGFloat = 6
    static let margin: CGFloat = 10

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .topLeading) {
                Color.clear
                if let target {
                    let rect = proxy[target.anchor]
                    let size = proxy.size
                    let below = Self.placesBelow(rect, in: size.height)
                    MenuHoverCaption(text: target.text)
                        .alignmentGuide(.leading) { caption in
                            -Self.leading(for: rect, width: caption.width, in: size.width)
                        }
                        .alignmentGuide(.top) { caption in
                            -(below ? rect.maxY + Self.gap : rect.minY - Self.gap - caption.height)
                        }
                        .id(target.id)
                        .transition(transition(below: below))
                }
            }
            .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
            .animation(reduceMotion ? .easeOut(duration: 0.1) : .spring(response: 0.24, dampingFraction: 0.86),
                       value: target?.id)
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func transition(below: Bool) -> AnyTransition {
        guard !reduceMotion else { return .opacity }
        let grow = AnyTransition.scale(scale: 0.94, anchor: below ? .top : .bottom)
        let drift = AnyTransition.offset(y: below ? -4 : 4)
        return .asymmetric(insertion: AnyTransition.opacity.combined(with: grow).combined(with: drift),
                           removal: .opacity)
    }

    /// Below the target unless that leaves no room for a two-line caption.
    static func placesBelow(_ rect: CGRect, in height: CGFloat) -> Bool {
        rect.maxY + gap + 44 <= height - margin
    }

    /// Centred on the target, clamped inside the panel's margins.
    static func leading(for rect: CGRect, width: CGFloat, in panelWidth: CGFloat) -> CGFloat {
        let centred = rect.midX - width / 2
        return min(max(centred, margin), max(margin, panelWidth - width - margin))
    }
}

/// A short caption stays on one line; a longer one wraps at a fixed width.
struct MenuHoverCaption: View {
    let text: String

    static let wrapWidth: CGFloat = 280
    static let oneLineLimit = 42

    var body: some View {
        label
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.14), lineWidth: 0.5)
            )
            .shadow(color: .black.opacity(0.22), radius: 8, y: 3)
    }

    @ViewBuilder private var label: some View {
        let text = Text(self.text).font(.caption.weight(.medium)).foregroundStyle(.primary)
        if self.text.count <= Self.oneLineLimit {
            text.lineLimit(1).fixedSize()
        } else {
            text.lineLimit(4)
                .multilineTextAlignment(.leading)
                .frame(width: Self.wrapWidth, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

extension View {
    func menuHoverTip(_ text: String) -> some View {
        modifier(MenuHoverTip(text: text))
    }
}
