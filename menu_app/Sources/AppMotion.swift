import AppKit
import SwiftUI

/// One motion system for the native app. Ease curves for status, text, counts, lists and panes;
/// springs only for shapes that move in space. With Reduce Motion nothing slides, scales or
/// bounces: changes fade briefly or happen at once, and nothing loops while idle.
enum AppMotion {
    static var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }

    /// Hover, focus, a press.
    static var micro: Animation { .easeOut(duration: reduceMotion ? 0.1 : 0.12) }
    /// A status or a small value that changes in place.
    static var control: Animation { .easeOut(duration: reduceMotion ? 0.1 : 0.14) }
    /// A section that opens, a row that arrives, search results.
    static var content: Animation { .easeInOut(duration: reduceMotion ? 0.1 : 0.18) }
    /// One Settings pane replacing another.
    static var pane: Animation { .easeInOut(duration: reduceMotion ? 0.1 : 0.16) }
    /// What a surface shows once it has opened.
    static var surface: Animation { .easeInOut(duration: reduceMotion ? 0.12 : 0.2) }
    /// A compact shape that grows or moves.
    static var spatial: Animation { reduceMotion ? .easeInOut(duration: 0.15) : .spring(response: 0.32, dampingFraction: 0.8) }
    /// The island opening: a little give at the end, like the iPhone island.
    static var islandOpen: Animation { reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.45, dampingFraction: 0.78) }
    /// The island closing: content leaves first, then the shape settles without bounce.
    static var islandClose: Animation { reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.38, dampingFraction: 0.92) }

    /// Content arriving from just above (a callout, a row): a short drift and a fade, or only the fade.
    static var arrive: AnyTransition {
        reduceMotion ? .opacity
            : .asymmetric(insertion: AnyTransition.opacity.combined(with: .offset(y: -4)), removal: .opacity)
    }

    /// A Settings pane coming in: a slight rise and a fade, or only the fade.
    static var paneChange: AnyTransition {
        reduceMotion ? .opacity
            : .asymmetric(insertion: AnyTransition.opacity.combined(with: .offset(y: 4)), removal: .opacity)
    }
}
