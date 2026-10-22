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

/// A caption above the pointer's target. It is an overlay, so it does not change the menu's
/// measured height (#200). It waits briefly so passing across a row does not flash.
struct MenuHoverTip: ViewModifier {
    let text: String
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
                    DispatchQueue.main.asyncAfter(deadline: .now() + Self.delay) {
                        if hovering, hoverToken == token { shown = true }
                    }
                } else {
                    shown = false
                }
            }
            .overlay(alignment: .topLeading) {
                if shown, !text.isEmpty {
                    caption
                        // The caption's bottom sits just above the row. The guide does not grow the row.
                        .alignmentGuide(.top) { dimensions in dimensions[.bottom] + 6 }
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
            .zIndex(shown ? 1 : 0)
    }

    private var caption: some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .foregroundStyle(.primary)
            .multilineTextAlignment(.leading)
            .lineLimit(2)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: 340, alignment: .leading)
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5)
            )
    }

    /// Long enough that moving across the chips does not flash a caption.
    static let delay: TimeInterval = 0.18
}

extension View {
    func menuHoverTip(_ text: String) -> some View {
        modifier(MenuHoverTip(text: text))
    }
}
