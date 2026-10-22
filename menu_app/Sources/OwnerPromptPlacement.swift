import CoreGraphics
import Foundation

/// Where a question shows and the small rules around it, kept free of AppKit state so tests can run it.
enum OwnerPromptPlacement {
    enum Style: Equatable { case island, card }

    static let maxAttachments = 6
    static let maxAttachmentBytes = 50 * 1024 * 1024
    static let islandWidth: CGFloat = 600
    static let cardWidth: CGFloat = 392
    /// Room around the shape for its shadow; the island's top stays flush with the screen edge.
    static let margin: CGFloat = 18

    /// The notch island when it is the live-activity style; every other choice, Off included, is the card.
    static func style(showLiveIsland: Bool) -> Style { showLiveIsland ? .island : .card }

    /// The window's frame (AppKit coordinates, y up) for content of the given height.
    static func frame(style: Style, contentHeight: CGFloat, screen: CGRect, visible: CGRect) -> CGRect {
        switch style {
        case .island:
            let width = islandWidth + margin * 2
            let height = contentHeight + margin
            return CGRect(x: screen.midX - width / 2, y: screen.maxY - height, width: width, height: height)
        case .card:
            let width = cardWidth + margin * 2
            let height = contentHeight + margin * 2
            return CGRect(x: visible.maxX - width + margin - 12, y: visible.maxY - height + margin - 8,
                          width: width, height: height)
        }
    }

    /// A free name in the upload folder: "shot.png", then "shot 2.png", "shot 3.png", …
    static func uniqueName(_ name: String, taken: Set<String>) -> String {
        let clean = name.replacingOccurrences(of: "/", with: "-").trimmingCharacters(in: .whitespaces)
        let base = clean.isEmpty ? "File" : clean
        guard taken.contains(base) else { return base }
        let ext = (base as NSString).pathExtension
        let stem = (base as NSString).deletingPathExtension
        var index = 2
        while true {
            let candidate = ext.isEmpty ? "\(stem) \(index)" : "\(stem) \(index).\(ext)"
            if !taken.contains(candidate) { return candidate }
            index += 1
        }
    }

    /// The agent's maker, for its logo: the running agent's provider, else a guess from the sender's name.
    static func providerID(sender: String, agentProvider: String?) -> String? {
        if let provider = agentProvider?.lowercased(), !provider.isEmpty {
            if provider == "codex" || provider.hasPrefix("chatgpt") { return "openai" }
            return provider
        }
        let name = sender.lowercased()
        if name.contains("claude") { return "claude" }
        if ["codex", "chatgpt", "openai", "gpt"].contains(where: name.contains) { return "openai" }
        return nil
    }

    static func remainingText(_ seconds: Int) -> String {
        seconds >= 60 ? "\(seconds / 60):" + String(format: "%02d", seconds % 60) : "\(max(0, seconds))"
    }
}
