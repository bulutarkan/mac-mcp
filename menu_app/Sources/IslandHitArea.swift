import CoreGraphics

/// Where the live island takes clicks (#203). The island is drawn inside a larger, fixed
/// transparent panel; only its current shape (rest, peek or expanded) is interactive, and
/// everything else must let clicks fall through to the menu bar and apps below.
///
/// The panel's hit test and its mouse-through switch both use this, so they never disagree.
enum IslandHitArea {
    /// The island's shape: `size` centred horizontally and hanging from the top of `container`.
    static func rect(in container: CGRect, size: CGSize) -> CGRect {
        CGRect(x: container.midX - size.width / 2, y: container.maxY - size.height,
               width: size.width, height: size.height)
    }

    /// Whether a click at `point` (in the same coordinates as `container`) belongs to the island.
    static func takesClick(at point: CGPoint, in container: CGRect, size: CGSize) -> Bool {
        rect(in: container, size: size).contains(point)
    }
}
