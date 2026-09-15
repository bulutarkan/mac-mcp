import AppKit
import SwiftUI

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    static let shared = SettingsWindowController()

    private var windowController: NSWindowController?

    func show(state: AppState, settings: SettingsStore) {
        settings.load()

        if let window = windowController?.window {
            window.center()
            NSApplication.shared.activate(ignoringOtherApps: true)
            windowController?.showWindow(nil)
            window.makeKeyAndOrderFront(nil)
            Task { await state.retryConnection() }
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

        NSApplication.shared.activate(ignoringOtherApps: true)
        controller.showWindow(nil)
        window.makeKeyAndOrderFront(nil)
    }
}
