import SwiftUI

@main
struct MacMCPMenuApp: App {
    @NSApplicationDelegateAdaptor(MenuAppDelegate.self) private var appDelegate
    @StateObject private var state: AppState

    init() {
        let appState = AppState()
        _state = StateObject(wrappedValue: appState)
        // macmcp://recipe/run links reach the running server with the dashboard token.
        RecipeLauncher.shared.configure(
            baseURL: { [weak appState] in
                guard let appState else { return nil }
                return URL(string: "http://127.0.0.1:\(appState.settings.serverPort)")
            },
            authorize: { [weak appState] request in appState?.authorizeDashboardRequest(&request) }
        )
    }

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(state: state, settings: state.settings)
        } label: {
            Image(systemName: menuBarSymbol)
                .accessibilityLabel(menuBarAccessibilityLabel)
        }
        .menuBarExtraStyle(.window)
        .commands {
            CommandGroup(replacing: .appSettings) {
                Button("Settings…") {
                    SettingsWindowController.shared.show(state: state, settings: state.settings)
                }
                .keyboardShortcut(",", modifiers: .command)
            }
        }
    }

    private var menuBarSymbol: String {
        if !state.serverRunning { return "server.rack" }
        if state.hasReliableSessionSignal && state.sessionNeedsAttentionCount > 0 { return "exclamationmark.triangle.fill" }
        if state.activeAgents > 0 || (state.hasReliableSessionSignal && state.sessionActiveCount > 0) {
            return state.pulse ? "cpu.fill" : "cpu"
        }
        return "server.rack"
    }

    private var menuBarAccessibilityLabel: String {
        if !state.serverRunning { return "Mac MCP, server disconnected" }
        if state.hasReliableSessionSignal && state.sessionNeedsAttentionCount > 0 {
            return "Mac MCP, \(state.sessionNeedsAttentionCount) session\(state.sessionNeedsAttentionCount == 1 ? "" : "s") need attention"
        }
        if state.activeAgents > 0 || (state.hasReliableSessionSignal && state.sessionActiveCount > 0) {
            return "Mac MCP, active work"
        }
        return "Mac MCP"
    }
}
