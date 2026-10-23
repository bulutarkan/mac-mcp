import AppKit
import SwiftUI

/// One set of colours, spacing, radii and materials for the native app, matching the web
/// dashboard's glass palette (obsidian and ice). Colours adapt to light and dark on their own;
/// text keeps the system label colours, so it stays vibrant on materials.
///
/// State colours are for state only (working, done, needs you, a problem), always next to a
/// symbol and words. Champagne is the brand: the mark and what the owner is asked to do.
enum VisualTokens {
    /// A colour with its light and dark values, resolved by the view's appearance.
    static func adaptive(light: UInt32, dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? nsColor(dark) : nsColor(light)
        })
    }

    static func color(_ hex: UInt32) -> Color { Color(nsColor: nsColor(hex)) }

    private static func nsColor(_ hex: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }

    // MARK: Colour

    /// Interactive emphasis and work in progress.
    static let accent = adaptive(light: 0x0062CC, dark: 0x8FCEF8)
    /// Done and healthy, as text and symbols.
    static let success = adaptive(light: 0x1D7D36, dark: 0x30D158)
    /// Done and healthy, as a fill behind other content.
    static let successFill = adaptive(light: 0x34C759, dark: 0x30D158)
    /// Needs a look or a decision.
    static let warning = adaptive(light: 0xA65200, dark: 0xFFB340)
    /// Did not work.
    static let error = adaptive(light: 0xC4141D, dark: 0xFF6961)
    /// Champagne: the brand, and what the owner is asked to do.
    static let brand = adaptive(light: 0x9A6A1F, dark: 0xF3C66F)
    /// Text on a brand fill.
    static let onBrand = color(0x2A1A05)
    /// A quiet fill for tiles, chips and fields.
    static let quietFill = Color.primary.opacity(0.07)
    /// A hairline around quiet fills.
    static let hairline = Color.primary.opacity(0.12)

    /// The island is always black, so it uses the dark values whatever the system look.
    enum OnDark {
        static let accent = VisualTokens.color(0x8FCEF8)
        static let success = VisualTokens.color(0x30D158)
        static let warning = VisualTokens.color(0xFFB340)
        static let error = VisualTokens.color(0xFF6961)
        static let brand = VisualTokens.color(0xF3C66F)
    }

    // MARK: Layout

    enum Space {
        static let xxs: CGFloat = 4
        static let xs: CGFloat = 8
        static let s: CGFloat = 12
        static let m: CGFloat = 16
        static let l: CGFloat = 24
        static let xl: CGFloat = 32
    }

    /// Radius follows hierarchy: controls, rows and tiles, cards, windows.
    enum Radius {
        static let tile: CGFloat = 6
        static let control: CGFloat = 9
        static let card: CGFloat = 14
        static let panel: CGFloat = 18
        static let window: CGFloat = 24
    }
}
