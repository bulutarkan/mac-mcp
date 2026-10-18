import AppKit
import SwiftUI

/// Asks an open Settings window to switch panes; the window stays independent of the popover.
@MainActor
final class SettingsNavigation: ObservableObject {
    static let shared = SettingsNavigation()
    @Published var requestedSection: SettingsSection?
}

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    static let shared = SettingsWindowController()

    private var windowController: NSWindowController?

    func show(state: AppState, settings: SettingsStore, section: SettingsSection? = nil) {
        settings.load()
        let transientWindows = NSApplication.shared.windows.filter { candidate in
            // The Live Island is also a visible, untitled panel. Only dismiss
            // the MenuBarExtra popup when presenting Settings.
            candidate.isVisible && candidate.title.isEmpty && !(candidate is IslandPanel)
        }

        if let window = windowController?.window {
            if let section { SettingsNavigation.shared.requestedSection = section }
            present(window: window, transientWindows: transientWindows)
            return
        }

        let hostingController = NSHostingController(
            rootView: SettingsView(state: state, settings: settings, initialSection: section ?? .general)
        )
        let window = NSWindow(contentViewController: hostingController)
        window.title = "Mac MCP Settings"
        window.styleMask = [.titled, .closable, .resizable]
        window.setContentSize(NSSize(width: 920, height: 640))
        // Content sizes, like SettingsView's own frame: a frame size would count the title bar
        // and let the smallest window cut off the bottom of both panes.
        window.contentMinSize = NSSize(width: 820, height: 560)
        window.contentMaxSize = NSSize(width: 1240, height: 860)
        window.setFrameAutosaveName("MacMCPSettingsWindow")
        window.center()
        window.isReleasedWhenClosed = false
        window.delegate = self

        let controller = NSWindowController(window: window)
        windowController = controller
        present(window: window, transientWindows: transientWindows)
    }

    private func present(window: NSWindow, transientWindows: [NSWindow]) {
        window.center()
        NSApplication.shared.activate(ignoringOtherApps: true)
        windowController?.showWindow(nil)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()

        // MenuBarExtra(.window) owns a transient untitled popup at a higher
        // window level than normal app windows. When Settings is launched from
        // its gear button, explicitly dismiss that source popup instead of
        // making Settings permanently floating/always-on-top.
        for transientWindow in transientWindows where transientWindow !== window {
            transientWindow.orderOut(nil)
        }

        DispatchQueue.main.async {
            window.makeKeyAndOrderFront(nil)
            window.orderFrontRegardless()
        }
    }
}
