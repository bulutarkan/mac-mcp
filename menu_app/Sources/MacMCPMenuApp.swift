import SwiftUI

@main
struct MacMCPMenuApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(state: state, settings: state.settings)
        } label: {
            Image(systemName: menuBarSymbol)
                .accessibilityLabel(menuBarAccessibilityLabel)
        }
        .menuBarExtraStyle(.window)
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
