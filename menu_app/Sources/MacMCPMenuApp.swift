import SwiftUI

@main
struct MacMCPMenuApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(state: state, settings: state.settings)
        } label: {
            Image(systemName: state.activeAgents > 0 ? (state.pulse ? "cpu.fill" : "cpu") : (state.serverRunning ? "server.rack" : "server.rack"))
                .accessibilityLabel(state.activeAgents > 0 ? "Mac MCP, agent active" : "Mac MCP")
        }
        .menuBarExtraStyle(.window)
    }
}
