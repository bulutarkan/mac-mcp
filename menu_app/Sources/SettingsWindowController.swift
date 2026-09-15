import AppKit
import SwiftUI

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    static let shared = SettingsWindowController()

    private var windowController: NSWindowController?

    func show(state: AppState, settings: SettingsStore) {
        settings.load()
        let transientWindows = NSApplication.shared.windows.filter { candidate in
            candidate.isVisible && candidate.title.isEmpty
        }

        if let window = windowController?.window {
            present(window: window, transientWindows: transientWindows)
            return
        }

        let hostingController = NSHostingController(
            rootView: SettingsView(state: state, settings: settings)
        )
        let window = NSWindow(contentViewController: hostingController)
        window.title = "Mac MCP Settings"
        window.styleMask = [.titled, .closable]
        window.setContentSize(NSSize(width: 720, height: 520))
        window.minSize = NSSize(width: 720, height: 520)
        window.maxSize = NSSize(width: 720, height: 520)
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
